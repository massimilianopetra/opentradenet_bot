# OpenTradeNet

Bot Telegram per il monitoraggio prezzi e la gestione di posizioni su [Hyperliquid](https://hyperliquid.xyz) — mercati perpetual, XYZ e spot.

---

## Struttura del progetto

```
opentradenet/
├── hyperliquid_bot.py           # Bot principale
├── hl_wallet.py                 # Modulo wallet e client API
├── candle_chart.py              # Grafico candlestick locale da CSV
├── generate_report.py           # Report HTML performance
├── fill_candles.py              # Riempie buchi nelle candele storiche
├── ml_features.py               # Feature engineering ML (candele → dataset)
├── ml_trainer.py                # Training modello XGBoost
├── ml_scanner.py                # Scanner opportunità (daemon 15m)
├── ml_analyst.py                # Analisi tecnica AI via Claude Vision API
├── opentradenet.env             # Configurazione (da non committare)
├── opentradenet_bot.service     # Unit systemd
├── data/
│   ├── wallet/
│   │   └── {chat_id}/
│   │       ├── address.txt      # Account address pubblico
│   │       └── key.enc          # Private key cifrata con AES-128
│   ├── prices/
│   │   └── {SIMBOLO}.csv        # Storico prezzi giornalieri
│   └── candles/
│       └── {SIMBOLO}/
│           └── {SIMBOLO}_15m.csv  # Candele 15 minuti
├── ml_reports/                  # Output di ml_features.py
│   ├── summary.txt              # Riepilogo tutti i simboli
│   ├── {SIMBOLO}_features.png   # Grafici analisi feature
│   └── {SIMBOLO}_dataset.csv    # Dataset pronto per training ML
└── logs/
    └── hyperliquid_bot.log      # Log con rotazione giornaliera (7 giorni)
```

---

## Requisiti

```bash
pip install python-telegram-bot aiohttp python-dotenv cryptography \
            hyperliquid-python-sdk eth-account requests
```

Per `candle_chart.py`, `ml_features.py` e `ml_analyst.py`:

```bash
pip install matplotlib numpy xgboost
```

Python 3.10+

---

## Configurazione

Copia e compila il file `.env`:

```env
# Telegram
TELEGRAM_TOKEN=il_tuo_token_bot

# Polling e soglie
POLL_INTERVAL=10                          # secondi tra un poll e l'altro
PRICE_CHANGE_THRESHOLD=0.5               # soglia alert subscribe (%)
SPIKE_THRESHOLD=1.0                      # soglia pricespike poll-to-poll (%)
SPIKE_EXTRA_SYMBOLS=BTC,SOL,ETH,HYPE     # simboli fissi per pricespike
SPIKE_EXCLUDE_SYMBOLS=                   # simboli da escludere dagli alert spike

# Mercati
SUPPORTED_DEXS=xyz                       # dex aggiuntivi (xyz, oppure xyz,altro)

# Position tracking
POSITION_TRACK_INTERVAL=300              # secondi tra un aggiornamento posizioni e l'altro

# Storage
DATA_DIR=data
PRICES_DIR=data/prices
PRICES_TIME=9                            # ora dopo cui salvare snapshot giornaliero
CANDLES_DIR=data/candles                 # directory candele 15m
CANDLES_INTERVAL_SECS=900               # intervallo aggiornamento candele (default 15 min)
LOG_DIR=logs
LOG_LEVEL=INFO

# Sicurezza wallet (generata automaticamente al primo avvio se mancante)
WALLET_ENCRYPTION_KEY=                   # chiave Fernet AES-128 (44 caratteri base64)

# Accesso ai comandi wallet/trading (lascia vuoto = tutti)
WALLET_ALLOWED_CHATS=                    # es: 123456789,987654321

# Ordini condizionali
CONDITIONAL_SNOOZE_SECS=300             # silenziamento dopo "Salta" (default 5 min)
CONDITIONAL_RENOTIFY_SECS=120           # intervallo re-notifica (default 2 min)
CONDITIONAL_TP_TRAILING_PCT=0.5         # % ritracciamento dal picco per trailing TP automatico

# Analisi tecnica AI — Claude Vision (ml_analyst.py)
ANTHROPIC_API_KEY=                       # sk-ant-... (da console.anthropic.com)
ANALYZE_ALLOWED_CHATS=                   # chat_id autorizzati a /analyze (es: 123456789)
```

### Generare la chiave di cifratura

Se `WALLET_ENCRYPTION_KEY` è vuota, il bot la genera al primo avvio e la stampa nel log. Copiala nel `.env` e riavvia.

In alternativa:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### Ottenere l'API key Anthropic

1. Registrati su [console.anthropic.com](https://console.anthropic.com)
2. Sezione **API Keys** → Create Key
3. Copia subito la chiave (non sarà più visibile)
4. Sezione **Billing** → aggiungi crediti (sistema prepagato, ~$0.004 per chiamata `/analyze`)

Il tuo `chat_id` Telegram lo trovi scrivendo `/start` a `@userinfobot`.

---

## Installazione come servizio systemd

```ini
# /etc/systemd/system/opentradenet_bot.service
[Unit]
Description=OpenTradeNet Hyperliquid Bot
After=network.target

[Service]
Type=simple
User=solana
WorkingDirectory=/home/solana/opentradenet_bot
ExecStart=/usr/bin/python3 hyperliquid_bot.py opentradenet.env
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable opentradenet_bot
sudo systemctl start opentradenet_bot
sudo systemctl status opentradenet_bot
```

---

## Comandi Telegram

### Generali

| Comando | Descrizione |
|---|---|
| `/start` | Messaggio di benvenuto e lista comandi |
| `/help` | Lista comandi |
| `/price SIMBOLO` | Prezzo corrente di un simbolo (es. `/price BTC`) |
| `/spread SIMBOLO` | Spread bid/ask corrente (es. `/spread GOLD`) |
| `/symbols` | Lista tutti i simboli disponibili |
| `/symbols QUERY` | Cerca simboli (es. `/symbols sil`) |
| `/stats` | Statistiche bot (utenti, simboli, polling, log) |

### Monitoraggio prezzi

| Comando | Descrizione |
|---|---|
| `/subscribe SIMBOLO` | Inizia a monitorare un simbolo |
| `/unsubscribe SIMBOLO` | Smetti di monitorare |
| `/list` | Lista delle tue sottoscrizioni attive |
| `/threshold X` | Imposta soglia alert personale in % (es. `/threshold 1.5`) |
| `/threshold reset` | Ripristina soglia globale |
| `/pricespike N` | Attiva alert spike con soglia N% poll-to-poll |
| `/pricespike status` | Mostra stato e soglia spike attuale |

### Wallet

| Comando | Descrizione |
|---|---|
| `/setaddress 0x...` | Salva il tuo account address pubblico |
| `/setkey 0x...` | Salva la private key API (cifrata su disco, messaggio auto-cancellato) |
| `/walletinfo` | Mostra stato credenziali |

> ⚠️ Invia `/setkey` solo in chat **privata** col bot.

### Trading

| Comando | Descrizione |
|---|---|
| `/positions` | Posizioni aperte correnti |
| `/trackpositions` | Attiva/disattiva tracking automatico posizioni |
| `/setleverage SYM N` | Imposta leva per un simbolo |
| `/long SYM IMPORTO` | Apre long (es. `/long SOL 500`) |
| `/short SYM IMPORTO` | Apre short |
| `/close SYM` | Chiude posizione |
| `/confirm` | Conferma ordine pendente |
| `/cancelorder` | Annulla ordine pendente |

### Ordini condizionali

| Comando | Descrizione |
|---|---|
| `/stoploss SYM PREZZO` | Imposta stop loss nativo (alert + chiusura automatica) |
| `/cancelsl [SYM]` | Cancella stop loss nativo (tutti o per simbolo) |
| `/takeprofit SYM PREZZO` | Imposta take profit con trailing stop |
| `/orders` | Lista ordini condizionali attivi |
| `/cancelcond ID` | Cancella ordine condizionale |

### Grafici e analisi

| Comando | Descrizione |
|---|---|
| `/chart SYM [N]` | Grafico candlestick 15m (es. `/chart GOLD 96`) |
| `/analyze SYM` | Analisi tecnica AI: chart daily + 15m + setup suggerito |

### Scanner VSA

| Comando | Descrizione |
|---|---|
| `/scan vsa [N]` | Top N simboli per score VSA (def. 10) |
| `/scan volume [N]` | Top N simboli per spike di volume 15m con valore $ (def. 10) |
| `/scan SYM [SCORE]` | Scan VSA manuale su un simbolo con soglia score opzionale |
| `/scan [SCORE]` | Scan VSA manuale su tutti i simboli |
| `/scan daemon vsa [SCORE]` | Avvia daemon: alert VSA automatico dopo ogni candela 15m |
| `/scan daemon volume` | Avvia daemon: alert volume spike automatico ogni 15m |
| `/scanstop` | Ferma il daemon attivo |
| `/scanstatus` | Modalità, soglia e ultimi segnali del daemon |

### Admin

| Comando | Descrizione |
|---|---|
| `/message all TESTO` | Broadcast a tutti gli utenti noti |
| `/message CHAT_ID TESTO` | Messaggio diretto a una chat specifica |

---

## Tipi di alert

| Alert | Trigger |
|---|---|
| 🔔 **Alert sottoscrizioni** | Prezzo varia ±soglia% dal riferimento (simboli in `/subscribe`) |
| ⚡ **PriceSpike** | Prezzo varia ±soglia% in un singolo poll (10s) |
| 🎯 **Position Alert** | Prezzo posizione varia ±soglia% dal riferimento |
| 🎯 **Nuova posizione** | Posizione aperta rilevata automaticamente ogni 5 min |
| 🏁 **Posizione chiusa** | Posizione non più presente nell'API |
| 🛑 **Stop Loss** | Prezzo ≤ soglia impostata con `/stoploss` |
| 🎯 **Take Profit** | Prezzo ≥ soglia impostata con `/takeprofit` |
| 🤖 **ML Scanner** | Opportunità LONG/SHORT rilevata con score ≥ soglia |

---

## Sicurezza

- La **private key** non viene mai tenuta in memoria oltre il tempo della singola operazione
- Viene salvata cifrata con **AES-128** (Fernet) — leggibile solo con `WALLET_ENCRYPTION_KEY` presente nel `.env` del server
- Il messaggio Telegram con `/setkey` viene **cancellato automaticamente** dopo la ricezione
- L'API wallet Hyperliquid **non può prelevare fondi**, solo tradare
- Puoi limitare l'accesso ai comandi wallet a specifici `chat_id` con `WALLET_ALLOWED_CHATS`
- Il comando `/analyze` è accessibile solo ai `chat_id` in `ANALYZE_ALLOWED_CHATS` (costa API)

---

## File prodotti

| File | Descrizione |
|---|---|
| `hyperliquid_bot.py` | Bot principale |
| `hl_wallet.py` | Modulo wallet e client API |
| `candle_chart.py` | Grafico candlestick locale da CSV candele (no pandas) |
| `generate_report.py` | Report HTML performance da CSV Hyperliquid |
| `fill_candles.py` | Riempie buchi nelle candele storiche |
| `ml_features.py` | Feature engineering ML su candele 15m (48 feature per candela) |
| `ml_trainer.py` | Training modello XGBoost su dataset ML |
| `ml_scanner.py` | Scanner opportunità con alert Telegram (daemon 15m) |
| `ml_analyst.py` | Analisi tecnica AI via Claude Vision (usato da `/analyze`) |
| `opentradenet.env` | Configurazione (da non committare) |
| `opentradenet_bot.service` | Unit systemd |

---

## Grafico candlestick — `candle_chart.py`

Script standalone per visualizzare le candele 15m salvate dal bot. Non richiede pandas — usa solo `matplotlib` e `numpy`.

### Struttura directory attesa

```
data/candles/GOLD/GOLD_15m.csv    ← priorità
data/candles/GOLD_15m.csv
data/candles/GOLD/GOLD.csv
data/candles/GOLD.csv
```

Formato CSV:
```
timestamp,open,high,low,close,volume
2026-03-01 14:00:00,5320.0,5325.5,5318.2,5322.8,45.12
```

### Uso

```bash
python3 candle_chart.py GOLD                          # ultime 120 candele (~30h)
python3 candle_chart.py GOLD --bars 200               # candele personalizzate
python3 candle_chart.py GOLD --save gold_chart.png    # salva PNG
python3 candle_chart.py GOLD --data-dir /altro/path   # directory custom
```

---

## Pipeline ML

La pipeline ML è composta da script modulari indipendenti, eseguibili in sequenza.

### 1. Feature engineering — `ml_features.py`

Produce 48 feature per candela da CSV 15m: EMA/RSI/MACD/Bollinger/ATR, struttura candela, momentum, volumi, 18 pattern candlestick binari, contesto temporale ciclico.

```bash
python3 ml_features.py --horizon 16 --auto-threshold --no-charts --quiet --save-csv
```

| Argomento | Descrizione |
|---|---|
| `--horizon N` | Orizzonte predizione in candele (ore × 4, es. 4h = 16) |
| `--auto-threshold` | Soglia LONG/SHORT adattiva per simbolo (target ~62% NEUTRO) |
| `--no-charts` | Disabilita grafici (obbligatorio per 300+ simboli) |
| `--quiet` | Output minimale |
| `--save-csv` | Salva dataset in `ml_reports/` |

### 2. Training — `ml_trainer.py`

Training XGBoost sul dataset prodotto da `ml_features.py`.

```bash
python3 ml_trainer.py
```

### 3. Scanner — `ml_scanner.py`

Valuta tutti i simboli con VSA (Volume Spread Analysis) + Bollinger Band + EMA + MACD + funding rate e produce uno score 0–100.

Modalità disponibili via bot:

| Modalità | Comando | Descrizione |
|---|---|---|
| Scan manuale VSA | `/scan [SYM] [SCORE]` | Analisi istantanea con alert completo |
| Top VSA | `/scan vsa [N]` | Top N simboli per score, output compatto |
| Top volume | `/scan volume [N]` | Top N spike di volume con valore in $ |
| Daemon VSA | `/scan daemon vsa [SCORE]` | Alert automatico ogni chiusura candela 15m |
| Daemon volume | `/scan daemon volume` | Summary volume spike ogni 15m |

### 4. Analisi AI — `ml_analyst.py`

Modulo richiamato dal comando `/analyze`. Genera chart daily (ricostruito da 15m) e chart 15m, li invia alla Claude Vision API e riceve un'analisi tecnica strutturata con entry/stop/target suggeriti.

**Flusso:**
1. Legge CSV candele 15m del simbolo
2. Ricampiona in daily (ultimi 56 giorni)
3. Genera i due PNG con `candle_chart.py`
4. Chiama Claude API (modello Haiku, vision)
5. Restituisce testo analisi + path chart al bot
6. Il bot invia chart + analisi su Telegram

**Costo stimato:** ~$0.004 per chiamata (modello Haiku).

**Output esempio:**
```
📊 SOL — Analisi Tecnica

🔵 DAILY: Trend ribassista, EMA in ordine discendente, RSI 43 in recupero da oversold.
  Supporto TL-S attivo, resistenza area 87-90.
🟢 15m: Struttura minimi crescenti, TL-R rispettata. MACD positivo.
  Volume spike rialzista confermato.

🤖 Bias: LONG

📐 Setup suggerito:
• Entry: 83.5 - 84.0
• Stop: 81.5 (-2.9%)
• Target 1: 87.5 (+4.1%) → R:R 1:1.4
• Target 2: 90.0 (+7.1%) → R:R 1:2.4

⚠️ Contro-trend daily — size ridotta consigliata
```
