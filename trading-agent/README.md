# US100 TradingView → Telegram Signal Agent

Automatisierter Alert-Agent (kein Auto-Trader): TradingView erkennt ein Setup auf
**US100 (Nasdaq-100-Index)**, schickt einen Webhook an **n8n**, n8n validiert das
Signal, berechnet Take-Profit/Stop-Loss und schickt dir eine formatierte Nachricht
per **Telegram**. Du entscheidest weiterhin selbst, ob und wie du die Position
eroeffnest — es wird keine Order automatisch an einen Broker geschickt.

```
TradingView Alert --webhook--> n8n Workflow --Telegram API--> deine Telegram-App
```

## Dateien

| Datei | Zweck |
|---|---|
| `n8n-workflow.json` | Importierbarer n8n-Workflow (Webhook → Validierung/Berechnung → Telegram) |
| `tradingview-alert-template.json` | Alert-Nachricht fuer LONG-Signale |
| `tradingview-alert-template-sell.json` | Alert-Nachricht fuer SHORT-Signale |
| `.env.example` | Benoetigte Umgebungsvariablen fuer n8n |

## Voraussetzungen

- Ein laufendes n8n (self-hosted per Docker, oder n8n.cloud)
- Ein TradingView-Account mit Webhook-Alerts (Essential-Plan oder hoeher)
- Ein Telegram-Bot (kostenlos via [@BotFather](https://t.me/BotFather))

## Setup

### 1. Telegram-Bot anlegen
1. Mit [@BotFather](https://t.me/BotFather) chatten, `/newbot` senden, Token notieren.
2. Deinem neuen Bot in Telegram eine Nachricht schicken (z.B. "hi").
3. Chat-ID herausfinden:
   ```bash
   curl "https://api.telegram.org/bot<DEIN_TOKEN>/getUpdates"
   ```
   `message.chat.id` aus der Antwort in `.env` als `TELEGRAM_CHAT_ID` eintragen.

### 2. n8n-Workflow importieren
1. In n8n: **Workflows → Import from File** → `n8n-workflow.json` auswaehlen.
2. Im Node **"Telegram: Signal senden"** deine Telegram-API-Credentials hinterlegen
   (Bot-Token aus Schritt 1).
3. Umgebungsvariablen aus `.env.example` in deiner n8n-Instanz setzen:
   - `WEBHOOK_SECRET` — frei waehlbares, langes Geheimwort
   - `TELEGRAM_CHAT_ID` — aus Schritt 1
   - `TP_POINTS` / `SL_POINTS` — Abstand von Entry zu TP/SL in Indexpunkten
4. Workflow aktivieren. n8n zeigt dir jetzt die **Production Webhook URL**
   (z.B. `https://deine-n8n-instanz.tld/webhook/us100-alert`) — die brauchst du gleich.

### 3. TradingView-Alert einrichten
1. Chart mit US100 (oder deinem CFD-Broker-Symbol, z.B. `NAS100`, `USTEC`) oeffnen.
2. **Alarm erstellen** → Bedingung nach Wahl (Indikator, Preis-Trigger, Strategie).
3. Unter **Benachrichtigungen** → **Webhook-URL** aktivieren, die URL aus Schritt 2.4 eintragen.
4. Als **Nachricht** den Inhalt von `tradingview-alert-template.json` (LONG) bzw.
   `tradingview-alert-template-sell.json` (SHORT) einfuegen, `secret` auf dein
   `WEBHOOK_SECRET` setzen.

### 4. Testen
Bevor der erste echte Alert kommt, den Webhook manuell simulieren:

```bash
curl -X POST "https://deine-n8n-instanz.tld/webhook/us100-alert" \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "change-me-to-a-long-random-string",
    "symbol": "US100",
    "action": "buy",
    "price": 19850.5,
    "time": "2026-08-31T10:00:00Z"
  }'
```

Bei Erfolg erhaeltst du innerhalb weniger Sekunden eine Telegram-Nachricht mit
Entry, TP, SL und Chance-Risiko-Verhaeltnis (CRV).

## Sicherheit

- `WEBHOOK_SECRET` verhindert, dass jemand, der die Webhook-URL kennt, dir
  beliebige Fake-Signale unterschieben kann. Ohne korrektes Secret antwortet
  der Workflow mit HTTP 400 und sendet **nichts** an Telegram.
- Erlaubte Symbole sind im Code-Node (`Validate & Format Signal`) auf
  US100-Varianten begrenzt (`allowedSymbols`) — Alerts fuer andere Symbole
  werden verworfen.
- Committe niemals dein echtes `WEBHOOK_SECRET` oder deinen Telegram-Token in
  ein Git-Repo. `.env.example` enthaelt nur Platzhalter.

## Erweiterungsmoeglichkeiten

- **Mehrere Symbole**: `allowedSymbols`-Liste im Code-Node erweitern und pro
  Symbol eigene TP/SL-Punkte konfigurieren.
- **Positionsgroessen-Rechner**: Kontogroesse + Risiko-% als weitere
  Umgebungsvariablen ergaenzen, Lot-/Kontraktgroesse im Code-Node berechnen
  und in der Telegram-Nachricht mit ausgeben.
- **Automatische Ausfuehrung**: Falls du spaeter tatsaechlich automatisiert
  Orders platzieren willst, kannst du nach dem "Signal gueltig?"-Node einen
  zusaetzlichen HTTP-Request-Node einbauen, der die Order an die REST-API
  deines Brokers schickt. Das ist ein deutlich groesserer Schritt (echtes
  Geld, Broker-API-Keys, Fehlerbehandlung, Rate-Limits) und sollte erst nach
  ausgiebigem Test mit einem Demo-/Paper-Konto erfolgen.
