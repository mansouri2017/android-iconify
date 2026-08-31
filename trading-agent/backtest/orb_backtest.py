"""
Python-Nachbau der Pine-Script-Strategie us100-orb-strategy.pine, zum lokalen
Backtesting/Ablation-Testing ohne TradingView.

WICHTIG: Dieses Skript laedt KEINE Marktdaten aus dem Internet (die Sandbox,
in der es entwickelt wurde, hat keinen allgemeinen Internetzugriff). Es
erwartet eine lokale CSV-Datei mit historischen OHLCV-Bars.

CSV-Format (Header erforderlich):
    time,open,high,low,close,volume
Wobei 'time' ein ISO-8601-Zeitstempel ist (z.B. 2026-03-02T09:30:00) in der
unten via --tz angegebenen Zeitzone, ODER bereits UTC/tz-aware.

So exportierst du die Daten aus TradingView:
    1. Chart auf 15m + US100 (bzw. dein Symbol) einstellen, gewuenschten
       Zeitraum sichtbar machen (bei Pro/Premium-Plan laedt Scrollen nach
       links weitere Historie nach).
    2. Rechtsklick auf den Chart -> "Exportieren" / Export-Icon in der
       unteren Toolbar (Kamera-Icon-Gruppe) -> "Chartdaten exportieren".
    3. CSV speichern und den Pfad unten als --csv angeben.

Nutzung:
    python3 orb_backtest.py --csv us100_15m.csv --ablation
    python3 orb_backtest.py --csv us100_15m.csv --self-test   (Synthetik-Smoketest, KEIN echtes Ergebnis)
"""

from __future__ import annotations

import argparse
import dataclasses
import itertools
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Parameter (Entsprechung zu den Pine-Script-Inputs)
# ---------------------------------------------------------------------------
@dataclass
class ORBParams:
    tz: str = "America/New_York"
    sess_start: tuple[int, int] = (9, 30)
    or_minutes: int = 15
    flat_time: tuple[int, int] = (15, 45)

    use_or_size_filter: bool = True
    daily_atr_len: int = 14
    min_or_atr_mult: float = 0.15
    max_or_atr_mult: float = 0.60

    use_vol_filter: bool = True
    vol_ma_len: int = 20
    vol_mult: float = 1.2

    use_trend_filter: bool = True
    trend_tf_minutes: int = 60
    trend_ema_len: int = 50

    use_mom_filter: bool = False

    trade_days: tuple[int, ...] = (0, 1, 2, 3, 4)  # Montag=0 ... Freitag=4

    buffer_points: float = 2.0
    require_close: bool = True
    rr_multiple: float = 1.5
    use_atr_stop: bool = False
    intraday_atr_len: int = 14
    atr_stop_mult: float = 1.0
    max_trades_per_day: int = 1

    commission_pct: float = 0.0
    slippage_points: float = 0.5  # je Seite


@dataclass
class Trade:
    entry_time: pd.Timestamp
    direction: str
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str
    pnl_points: float


# ---------------------------------------------------------------------------
# Datenaufbereitung
# ---------------------------------------------------------------------------
def load_csv(path: str, tz: str, broker_tz: str | None = None) -> pd.DataFrame:
    """
    Liest sowohl TradingView-Exporte (eine 'time'-Spalte mit vollem Datum)
    als auch MT5-History-Center-Exporte (getrennte <DATE>/<TIME>-Spalten,
    tab-getrennt, <TICKVOL>/<VOL> statt 'volume').

    broker_tz: Zeitzone, in der die Zeitstempel im CSV vorliegen (z.B. die
    Serverzeit deines MT5-Brokers, oft 'EET'/'Etc/GMT-2' o.ae. -- im MT5
    Terminal meist unten rechts oder unter Extras > Optionen > Server
    einsehbar). Wird auf 'tz' (Session-Zeitzone, Standard America/New_York)
    umgerechnet. Ohne Angabe wird angenommen, die Zeitstempel liegen schon
    in 'tz' vor (Standardfall bei TradingView-Exporten mit Zeitzonen-Option).
    """
    df = pd.read_csv(path, sep=None, engine="python")
    df.columns = [c.strip().strip("<>").lower() for c in df.columns]

    if "date" in df.columns and "time" in df.columns and df["date"].astype(str).str.len().median() <= 10:
        # MT5-Format: getrennte Datum/Zeit-Spalten -> zusammenfuehren
        df["time"] = pd.to_datetime(
            df["date"].astype(str).str.replace(".", "-", regex=False) + " " + df["time"].astype(str),
            utc=False,
        )
        df = df.drop(columns=["date"])
    elif "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], utc=False)
    else:
        raise ValueError("CSV hat weder eine 'time'-Spalte noch getrennte 'date'/'time'-Spalten (MT5-Format).")

    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV fehlen Spalten: {missing}")

    if "volume" not in df.columns or df["volume"].fillna(0).eq(0).all():
        if "tickvol" in df.columns:
            df["volume"] = df["tickvol"]  # Tick-Volumen als Proxy (bei Index-CFDs ueblich, echtes Volumen meist 0)
        elif "vol" in df.columns:
            df["volume"] = df["vol"]
        elif "volume" not in df.columns:
            df["volume"] = np.nan

    if df["time"].dt.tz is None:
        df["time"] = df["time"].dt.tz_localize(broker_tz or tz)
    if broker_tz:
        df["time"] = df["time"].dt.tz_convert(tz)

    df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    df = df.set_index("time")
    return df[["open", "high", "low", "close", "volume"]]


def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def compute_daily_atr(df: pd.DataFrame, length: int) -> pd.Series:
    """Vortages-ATR (kein Lookahead), gemappt auf jede Intraday-Bar ihres Tages."""
    daily = df.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    daily_atr = wilder_atr(daily["high"], daily["low"], daily["close"], length).shift(1)
    daily_atr.index = daily_atr.index.date
    return pd.Series(df.index.date, index=df.index).map(daily_atr)


def compute_htf_ema(df: pd.DataFrame, tf_minutes: int, length: int) -> pd.Series:
    """HTF-EMA auf Basis der zuletzt ABGESCHLOSSENEN HTF-Bar (kein Lookahead)."""
    htf = df["close"].resample(f"{tf_minutes}min", label="right", closed="right").last().dropna()
    htf_ema = htf.ewm(span=length, adjust=False).mean().shift(1)
    return htf_ema.reindex(df.index, method="ffill")


# ---------------------------------------------------------------------------
# Backtest-Engine
# ---------------------------------------------------------------------------
def run_backtest(df: pd.DataFrame, p: ORBParams) -> list[Trade]:
    df = df.copy()
    df["vol_ma"] = df["volume"].rolling(p.vol_ma_len).mean()
    df["intraday_atr"] = wilder_atr(df["high"], df["low"], df["close"], p.intraday_atr_len)
    df["daily_atr"] = compute_daily_atr(df, p.daily_atr_len)
    df["trend_ema"] = compute_htf_ema(df, p.trend_tf_minutes, p.trend_ema_len)

    sess_start_min = p.sess_start[0] * 60 + p.sess_start[1]
    or_end_min = sess_start_min + p.or_minutes
    flat_min = p.flat_time[0] * 60 + p.flat_time[1]

    min_of_day = df.index.hour * 60 + df.index.minute
    df["min_of_day"] = min_of_day
    df["is_or_bar"] = (min_of_day >= sess_start_min) & (min_of_day < or_end_min)
    df["is_trading_window"] = (min_of_day >= or_end_min) & (min_of_day < flat_min)
    df["is_flat_bar"] = min_of_day >= flat_min
    df["dow"] = df.index.dayofweek

    trades: list[Trade] = []

    for _, day_df in df.groupby(df.index.date):
        if day_df["dow"].iloc[0] not in p.trade_days:
            continue

        or_bars = day_df[day_df["is_or_bar"]]
        if or_bars.empty:
            continue
        or_high = or_bars["high"].max()
        or_low = or_bars["low"].min()
        or_size = or_high - or_low
        session_open = or_bars["open"].iloc[0]

        window_bars = day_df[day_df["is_trading_window"]]
        flat_bars = day_df[day_df["is_flat_bar"]]

        trades_today = 0
        position = None  # dict: direction, entry_price, sl, tp

        rows = list(window_bars.itertuples())
        for i, bar in enumerate(rows):
            if position is not None:
                exited = False
                if position["direction"] == "long":
                    if bar.low <= position["sl"]:
                        trades.append(_close_trade(position, bar.Index, position["sl"], "SL"))
                        exited = True
                    elif bar.high >= position["tp"]:
                        trades.append(_close_trade(position, bar.Index, position["tp"], "TP"))
                        exited = True
                else:
                    if bar.high >= position["sl"]:
                        trades.append(_close_trade(position, bar.Index, position["sl"], "SL"))
                        exited = True
                    elif bar.low <= position["tp"]:
                        trades.append(_close_trade(position, bar.Index, position["tp"], "TP"))
                        exited = True
                if exited:
                    position = None
                continue

            if trades_today >= p.max_trades_per_day:
                continue
            if pd.isna(or_high) or pd.isna(or_low):
                continue

            or_size_ok = (not p.use_or_size_filter) or (
                not pd.isna(bar.daily_atr)
                and p.min_or_atr_mult * bar.daily_atr <= or_size <= p.max_or_atr_mult * bar.daily_atr
            )
            vol_ok = (not p.use_vol_filter) or (
                not pd.isna(bar.vol_ma) and bar.volume > bar.vol_ma * p.vol_mult
            )
            trend_ok_long = (not p.use_trend_filter) or (not pd.isna(bar.trend_ema) and bar.close > bar.trend_ema)
            trend_ok_short = (not p.use_trend_filter) or (not pd.isna(bar.trend_ema) and bar.close < bar.trend_ema)
            mom_ok_long = (not p.use_mom_filter) or (bar.close > session_open)
            mom_ok_short = (not p.use_mom_filter) or (bar.close < session_open)

            if p.require_close:
                long_break = bar.close > or_high + p.buffer_points
                short_break = bar.close < or_low - p.buffer_points
            else:
                long_break = bar.high > or_high + p.buffer_points
                short_break = bar.low < or_low - p.buffer_points

            long_cond = or_size_ok and vol_ok and trend_ok_long and mom_ok_long and long_break
            short_cond = or_size_ok and vol_ok and trend_ok_short and mom_ok_short and short_break

            if not (long_cond or short_cond):
                continue

            # Fill am Open der naechsten Bar (entspricht TradingView Standardverhalten)
            if i + 1 >= len(rows):
                continue
            fill_bar = rows[i + 1]

            if long_cond:
                entry_price = fill_bar.open + p.slippage_points
                sl = (entry_price - p.atr_stop_mult * bar.intraday_atr) if p.use_atr_stop else (or_low - p.buffer_points)
                sl_dist = entry_price - sl
                if sl_dist <= 0:
                    continue
                tp = entry_price + sl_dist * p.rr_multiple
                position = {"direction": "long", "entry_time": fill_bar.Index, "entry_price": entry_price, "sl": sl, "tp": tp}
                trades_today += 1
            elif short_cond:
                entry_price = fill_bar.open - p.slippage_points
                sl = (entry_price + p.atr_stop_mult * bar.intraday_atr) if p.use_atr_stop else (or_high + p.buffer_points)
                sl_dist = sl - entry_price
                if sl_dist <= 0:
                    continue
                tp = entry_price - sl_dist * p.rr_multiple
                position = {"direction": "short", "entry_time": fill_bar.Index, "entry_price": entry_price, "sl": sl, "tp": tp}
                trades_today += 1

        # Zwangsschliessung bei Erreichen der Flat-Zeit
        if position is not None and not flat_bars.empty:
            flat_bar = flat_bars.iloc[0]
            trades.append(_close_trade(position, flat_bar.name, flat_bar["open"], "FLAT"))
            position = None

    return trades


def _close_trade(position: dict, exit_time, exit_price: float, reason: str) -> Trade:
    if position["direction"] == "long":
        pnl = exit_price - position["entry_price"]
    else:
        pnl = position["entry_price"] - exit_price
    return Trade(
        entry_time=position["entry_time"],
        direction=position["direction"],
        entry_price=position["entry_price"],
        exit_time=exit_time,
        exit_price=exit_price,
        exit_reason=reason,
        pnl_points=pnl,
    )


# ---------------------------------------------------------------------------
# Statistik
# ---------------------------------------------------------------------------
def compute_stats(trades: list[Trade], commission_pct: float = 0.0, point_value: float = 1.0) -> dict:
    n = len(trades)
    if n == 0:
        return {"n_trades": 0, "win_rate": np.nan, "profit_factor": np.nan, "net_pnl": 0.0, "max_dd_points": np.nan}

    pnls = np.array([t.pnl_points for t in trades])
    if commission_pct > 0:
        pnls = pnls - np.array([abs(t.entry_price) * commission_pct / 100 for t in trades])

    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    win_rate = len(wins) / n * 100
    gross_win = wins.sum() if len(wins) else 0.0
    gross_loss = -losses.sum() if len(losses) else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else np.nan

    equity = np.cumsum(pnls)
    running_max = np.maximum.accumulate(equity)
    drawdown = running_max - equity
    max_dd = drawdown.max() if len(drawdown) else 0.0

    return {
        "n_trades": n,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2) if not np.isnan(profit_factor) else np.nan,
        "net_pnl": round(pnls.sum() * point_value, 1),
        "max_dd_points": round(max_dd, 1),
    }


# ---------------------------------------------------------------------------
# Ablation-Runner: testet Filter-Kombinationen systematisch
# ---------------------------------------------------------------------------
def run_ablation(df: pd.DataFrame, base: ORBParams) -> pd.DataFrame:
    rows = []
    toggles = {
        "use_or_size_filter": [True, False],
        "use_vol_filter": [True, False],
        "use_trend_filter": [True, False],
        "use_mom_filter": [False, True],
    }
    max_trades_options = [1, 2, 3]

    keys = list(toggles.keys())
    for combo in itertools.product(*toggles.values()):
        for mtd in max_trades_options:
            params = dataclasses.replace(base, max_trades_per_day=mtd, **dict(zip(keys, combo)))
            trades = run_backtest(df, params)
            stats = compute_stats(trades, commission_pct=params.commission_pct)
            row = {**dict(zip(keys, combo)), "max_trades_per_day": mtd, **stats}
            rows.append(row)

    result = pd.DataFrame(rows)
    return result.sort_values(["n_trades", "profit_factor"], ascending=[False, False])


# ---------------------------------------------------------------------------
# Synthetischer Smoketest (KEIN echtes Backtest-Ergebnis!)
# ---------------------------------------------------------------------------
def make_synthetic_data(days: int = 40, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    bars_per_day = 96  # 24h * 4 (15-Minuten-Bars)
    idx = pd.date_range("2026-01-05 00:00", periods=days * bars_per_day, freq="15min", tz="America/New_York")
    price = 19000 + np.cumsum(rng.normal(0, 3.0, size=len(idx)))
    high = price + rng.uniform(0, 4, size=len(idx))
    low = price - rng.uniform(0, 4, size=len(idx))
    open_ = price + rng.normal(0, 1, size=len(idx))
    close = price + rng.normal(0, 1, size=len(idx))
    volume = rng.uniform(500, 5000, size=len(idx))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=str, help="Pfad zur OHLCV-CSV (TradingView- oder MT5-Export)")
    ap.add_argument("--tz", type=str, default="America/New_York", help="Session-Zeitzone fuer die Opening-Range-Logik")
    ap.add_argument("--broker-tz", type=str, default=None, help="Zeitzone der CSV-Zeitstempel, falls abweichend von --tz (z.B. bei MT5-Serverzeit)")
    ap.add_argument("--ablation", action="store_true", help="Filter-Ablation-Grid ausfuehren")
    ap.add_argument("--self-test", action="store_true", help="Nur Code-Smoketest mit synthetischen Zufallsdaten")
    ap.add_argument("--out", type=str, default="ablation_results.csv")
    args = ap.parse_args()

    if args.self_test:
        print("=== SELF-TEST (synthetische Zufallsdaten, KEIN echtes Backtest-Ergebnis) ===")
        df = make_synthetic_data()
        trades = run_backtest(df, ORBParams())
        print(f"Bars: {len(df)}, Trades erzeugt: {len(trades)}")
        print(compute_stats(trades))
        return

    if not args.csv:
        raise SystemExit("Bitte --csv <pfad> angeben (oder --self-test fuer den Code-Smoketest).")

    df = load_csv(args.csv, args.tz, broker_tz=args.broker_tz)
    print(f"Geladen: {len(df)} Bars, {df.index[0]} bis {df.index[-1]}")

    if args.ablation:
        result = run_ablation(df, ORBParams(tz=args.tz))
        result.to_csv(args.out, index=False)
        print(f"Ablation-Ergebnisse gespeichert: {args.out}")
        print(result.head(20).to_string(index=False))
    else:
        trades = run_backtest(df, ORBParams(tz=args.tz))
        stats = compute_stats(trades)
        print(stats)


if __name__ == "__main__":
    main()
