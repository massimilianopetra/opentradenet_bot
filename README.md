# OpenTradeNet Bot

Bot Telegram per il monitoraggio dei prezzi e il trading su [Hyperliquid](https://hyperliquid.xyz), sviluppato in Python e pensato per girare come servizio su Ubuntu con systemd.

Supporta **perp standard**, **perp XYZ** (azioni, materie prime, forex, indici) e **spot**.

---

## Funzionalità principali

- **Monitoraggio prezzi** con alert su soglia personalizzata per ogni utente
- **PriceSpike** — alert su movimenti improvvisi poll-to-poll
- **Position Tracking automatico** — le tue posizioni aperte vengono monitorate in tempo reale senza configurazioni manuali
- **Trading via Telegram** — apri/chiudi posizioni long e short direttamente dalla chat, con conferma obbligatoria
- **Gestione leva** — imposta la leva su qualsiasi simbolo (perp standard e XYZ)
- **Storico prezzi** — snapshot giornaliero automatico su CSV
- **Candele 15m** — salvataggio continuo su CSV per analisi tecnica
- **Grafico candlestick** — visualizzazione locale con indicatori tecnici (`candle_chart.py`)
- **Multi-mercato** — perp standard, XYZ (GOLD, SILVER, NFLX, ORCL…), spot
- **Credenziali cifrate** — la private key API è salvata su disco con AES-128 (Fernet)

---

## Architettura

```
opentradenet_bot/
├── hyperliquid_bot.py      # Bot principale — comandi Telegram e polling
├── hl_wallet.py            # Gestione wallet, credenziali e client API Hyperliquid
├── candle_chart.py         # Grafico candlestick locale da CSV candele
├── generate_report.py      # Report HTML performance da CSV Hyperliquid
├── opentradenet.env        # Configurazione (non committare su git!)
├── opentradenet_bot.service # Unit systemd
├── data/
│   ├── wallet/
│   │   └── {chat_id}/
│   │       ├── address.txt  # Account address pubblico
│   │       └── key.enc      # Private key cifrata con AES-128
│   ├── prices/
│   │   └── {SIMBOLO}.csv    # Storico prezzi giornalieri
│   └── candles/
│       └── {SIMBOLO}/
│           └── {SIMBOLO}_15m.csv  # Candele 15 minuti
└── logs/
    └── hyperliquid_bot.log  # Log con rotazione giornaliera (7 giorni)
```

---

## Requisiti

```bash
pip install python-telegram-bot aiohttp python-dotenv cryptography \
            hyperliquid-python-sdk eth-account requests
```

Per `candle_chart.py` bastano `matplotlib` e `numpy` (nessuna dipendenza extra):

```bash
pip install matplotlib numpy
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
```

### Generare la chiave di cifratura

Se `WALLET_ENCRYPTION_KEY` è vuota, il bot la genera al primo avvio e la stampa nel log. Copiala nel `.env` e riavvia.

In alternativa:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

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
| `/symbols` | Lista tutti i simboli disponibili |
| `/symbols QUERY` | Cerca simboli (es. `/symbols sil`) |
| `/stats` | Statistiche bot (utenti, simboli, polling, log) |

---

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

---

### Wallet

| Comando | Descrizione |
|---|---|
| `/setaddress 0x...` | Salva il tuo account address pubblico |
| `/setkey 0x...` | Salva la private key API (cifrata su disco, messaggio auto-cancellato) |
| `/walletinfo` | Mostra stato credenziali |

> ⚠️ Invia `/setkey` solo in chat **privata** col bot. Il messaggio viene cancellato automaticamente dopo la ricezione.

**Come ottenere la API key su Hyperliquid:**
1. Vai su [app.hyperliquid.xyz/API](https://app.hyperliquid.xyz/API)
2. Genera un nuovo API wallet → copia la private key
3. Autorizza l'API wallet firmando la transazione
4. Usa `/setaddress` per il tuo account address principale (42 caratteri)
5. Usa `/setkey` per la private key dell'API wallet

La private key **non può prelevare fondi** — può solo aprire/chiudere posizioni.

---

### Leva

| Comando | Descrizione |
|---|---|
| `/setleverage SIMBOLO LEVA` | Imposta leva (auto-rileva mercato) |
| `/setleverage SIMBOLO LEVA xyz` | Forza mercato XYZ |
| `/setleverage SIMBOLO LEVA cross` | Usa cross margin invece di isolated |
| `/setleverage SIMBOLO LEVA xyz cross` | XYZ + cross margin |

**Esempi:**
```
/setleverage BTC 10          → PERP standard, isolated, 10x
/setleverage GOLD 5          → XYZ auto-rilevato, isolated, 5x
/setleverage NFLX 3 xyz      → XYZ esplicito, isolated, 3x
/setleverage MSTR 5 xyz cross → XYZ, cross margin, 5x
```

---

### Trading

Apri e chiudi posizioni direttamente dalla chat. **Gli importi sono sempre in dollari (notional)**, la size viene calcolata automaticamente sul prezzo corrente.

> ⚠️ Ogni ordine richiede conferma con `/confirm` entro 30 secondi.

| Comando | Descrizione |
|---|---|
| `/long SIMBOLO IMPORTO` | Apre posizione long per $IMPORTO |
| `/short SIMBOLO IMPORTO` | Apre posizione short per $IMPORTO |
| `/close SIMBOLO` | Chiude tutta la posizione aperta |
| `/close SIMBOLO 50%` | Chiude il 50% della posizione |
| `/close SIMBOLO 200` | Chiude $200 di posizione |
| `/confirm` | Conferma ed esegue l'ordine pendente |
| `/cancelorder` | Annulla l'ordine pendente |

**Esempio flusso long:**
```
/long GOLD 500

⚠️ Conferma ordine
📈 LONG GOLD _XYZ_
Importo: $500.00
Prezzo attuale: $5,358.05
Size stimata: 0.0933
Invia /confirm per eseguire — scade in 30 secondi

/confirm

✅ Ordine eseguito
📈 LONG GOLD _XYZ_  Size: 0.0933  Prezzo: $5,360.00
```

---

### Position Tracking

| Comando | Descrizione |
|---|---|
| `/positions` | Mostra posizioni aperte su Hyperliquid |
| `/trackpositions` | Mostra stato tracking |
| `/trackpositions on` | Attiva tracking automatico |
| `/trackpositions off` | Disattiva tracking |

Il tracking verifica automaticamente ogni `POSITION_TRACK_INTERVAL` secondi le posizioni aperte e notifica:
- nuove posizioni rilevate
- posizioni chiuse (rimosse dall'API)
- variazioni di prezzo oltre la soglia alert personale

---

### Ordini condizionali — Stop Loss e Take Profit

Imposta soglie di prezzo su qualsiasi simbolo. Quando la condizione si verifica, il bot notifica ogni 10 secondi con bottoni di azione.

| Comando | Descrizione |
|---|---|
| `/stoploss SIMBOLO PREZZO` | Alert quando prezzo ≤ PREZZO, chiudi tutto |
| `/stoploss SIMBOLO PREZZO 200` | Alert quando prezzo ≤ PREZZO, chiudi $200 |
| `/stoploss SIMBOLO PREZZO 50%` | Alert quando prezzo ≤ PREZZO, chiudi 50% |
| `/takeprofit SIMBOLO PREZZO` | Alert quando prezzo ≥ PREZZO, chiudi tutto |
| `/takeprofit SIMBOLO PREZZO 200` | Alert quando prezzo ≥ PREZZO, chiudi $200 |
| `/takeprofit SIMBOLO PREZZO 50%` | Alert quando prezzo ≥ PREZZO, chiudi 50% |
| `/orders` | Lista ordini condizionali attivi con ID |
| `/cancelcond ID` | Cancella un ordine condizionale |
| `/cancelcond all` | Cancella tutti gli ordini condizionali |

**Comportamento bottoni:**

| Bottone | Effetto |
|---|---|
| ✅ Esegui | Esegue il close immediatamente e rimuove l'ordine |
| ⏭ Salta (5 min) | Silenzia per 5 minuti — se il prezzo è ancora in zona riprende a notificare |
| 🗑 Cancella ordine | Rimuove definitivamente l'ordine condizionale |

**Trailing Stop automatico per Take Profit:**
Quando il TP è attivo e il prezzo supera il trigger, il bot traccia il picco e chiude automaticamente la posizione se il prezzo ritraccia di `CONDITIONAL_TP_TRAILING_PCT`% dal massimo (default 0.5%). In questo caso la chiusura avviene senza pressare bottoni.

```env
CONDITIONAL_TP_TRAILING_PCT=0.5   # % ritracciamento dal picco → chiusura automatica
```

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

---

## Sicurezza

- La **private key** non viene mai tenuta in memoria oltre il tempo della singola operazione
- Viene salvata cifrata con **AES-128** (Fernet) — leggibile solo con `WALLET_ENCRYPTION_KEY` presente nel `.env` del server
- Il messaggio Telegram con `/setkey` viene **cancellato automaticamente** dopo la ricezione
- L'API wallet Hyperliquid **non può prelevare fondi**, solo tradare
- Puoi limitare l'accesso ai comandi wallet a specifici `chat_id` con `WALLET_ALLOWED_CHATS`

---

## File prodotti

| File | Descrizione |
|---|---|
| `hyperliquid_bot.py` | Bot principale |
| `hl_wallet.py` | Modulo wallet e client API |
| `candle_chart.py` | Grafico candlestick locale da CSV candele (no pandas) |
| `generate_report.py` | Report HTML performance da CSV Hyperliquid |
| `opentradenet.env` | Configurazione (da non committare) |
| `opentradenet_bot.service` | Unit systemd |

---

## Grafico candlestick — `candle_chart.py`

Script standalone per visualizzare le candele 15m salvate dal bot. Non richiede pandas — usa solo `matplotlib` e `numpy`.

### Struttura directory attesa

Il file CSV viene cercato nei seguenti percorsi (in ordine di priorità):

```
data/candles/GOLD/GOLD_15m.csv    ← priorità
data/candles/GOLD_15m.csv
data/candles/GOLD/GOLD.csv
data/candles/GOLD.csv
```

Formato CSV (quello prodotto dal bot):
```
timestamp,open,high,low,close,volume
2026-03-01 14:00:00,5320.0,5325.5,5318.2,5322.8,45.12
```

### Uso

```bash
# Mostra finestra interattiva con le ultime 120 candele (~30h)
python3 candle_chart.py GOLD

# Specifica quante candele visualizzare
python3 candle_chart.py GOLD --bars 200

# Salva come PNG invece di aprire la finestra
python3 candle_chart.py GOLD --save gold_chart.png

# Directory CSV personalizzata
python3 candle_chart.py GOLD --data-dir /altro/percorso/candles

# Combinazioni
python3 candle_chart.py SILVER --bars 96 --save silver_4h.png
python3 candle_chart.py NFLX --bars 50 --data-dir /mnt/data/candles
```

### Argomenti

| Argomento | Default | Descrizione |
|---|---|---|
| `symbol` | (obbligatorio) | Simbolo da graficare (es. GOLD, SILVER, BTC) |
| `--bars N` | `120` | Numero di candele da visualizzare (≈ 30h a 15m) |
| `--data-dir PATH` | `data/candles` | Directory base dei file CSV |
| `--save FILE` | — | Salva PNG invece di aprire la finestra interattiva |

### Indicatori inclusi

- **Candele OHLC** colorate (verde rialzo / rosso ribasso)
- **Bollinger Bands** (periodo 20, deviazione ×2)
- **EMA 9** (giallo), **EMA 21** (rosso), **EMA 50** (azzurro)
- **Supporti e resistenze automatici** da pivot locali (ultimi 4 per tipo)
- **Volume** con media mobile a 20 periodi
- **RSI 14** con zone overbought (>70) e oversold (<30)
- **MACD** (12/26/9) con istogramma

### Esempio output

```
📂 data/candles/GOLD/GOLD_15m.csv
📊 Candele totali: 1538  |  Visualizzate: 120
🕯 Da 2026-03-02 16:00:00  a  2026-03-03 21:45:00
💰 Ultimo prezzo: 5084.4000
✅ Salvato: gold_chart.png
```

---

## Report performance

`generate_report.py` è uno script standalone (zero dipendenze extra) che genera un report HTML interattivo dal CSV della trade history esportato da Hyperliquid.

```bash
python3 generate_report.py trade_history.csv
python3 generate_report.py trade_history.csv -o report_febbraio.html
python3 generate_report.py trade_history.csv --open   # apre nel browser
```

Il report include: P&L netto, win rate, profit factor, P&L per simbolo, split Long/Short e PERP/XYZ, timeline, tabella completa trade e posizioni ancora aperte.

---

## Licenza

MIT
