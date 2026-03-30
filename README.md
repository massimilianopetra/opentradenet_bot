# OpenTradeNet

Bot Telegram per il monitoraggio prezzi e la gestione di posizioni su [Hyperliquid](https://hyperliquid.xyz) — mercati perpetual, XYZ e spot.

---

## Struttura del progetto

```
opentradenet/
├── hyperliquid_bot.py           # Bot principale Telegram
├── hl_wallet.py                 # Modulo wallet e client API Hyperliquid
├── candle_chart.py              # Grafico candlestick da CSV (no pandas)
├── bot_monitor.py               # Monitor prezzi e position tracking
├── bot_tasks.py                 # Task asincroni (polling, candele, scanner daemon)
├── ml_scanner.py                # Scanner VSA + FVG + volume + pattern
├── ml_analyst.py                # Analisi tecnica AI via Claude Vision API
├── ml_journal.py                # Journal trade (log aperture/chiusure)
├── ml_features.py               # Feature engineering ML
├── ml_trainer.py                # Training modello XGBoost
├── generate_report.py           # Report HTML performance
├── fill_candles.py              # Riempie buchi nelle candele storiche
├── opentradenet.env             # Configurazione (da non committare)
├── opentradenet_bot.service     # Unit systemd
├── data/
│   ├── wallet/
│   │   └── {chat_id}/
│   │       ├── address.txt      # Account address pubblico
│   │       └── key.enc          # Private key cifrata con AES-128
│   ├── prices/
│   │   └── {SIMBOLO}.csv        # Storico prezzi giornalieri
│   ├── candles/
│   │   └── {SIMBOLO}/
│   │       └── {SIMBOLO}_15m.csv  # Candele 15 minuti
│   └── chart_prefs/
│       └── trendlines.json      # Preferenze trendline per-utente
├── ml_reports/
│   ├── summary.txt
│   ├── {SIMBOLO}_features.png
│   └── {SIMBOLO}_dataset.csv
└── logs/
    └── hyperliquid_bot.log      # Log con rotazione giornaliera (7 giorni)
```

---

## Requisiti

```bash
pip install python-telegram-bot aiohttp python-dotenv cryptography \
            hyperliquid-python-sdk eth-account requests \
            matplotlib numpy xgboost anthropic
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
SPIKE_EXTRA_SYMBOLS=BTC,SOL,ETH,HYPE     # simboli fissi sempre monitorati per spike
SPIKE_EXCLUDE_SYMBOLS=                   # simboli da escludere dagli alert spike

# Mercati
SUPPORTED_DEXS=xyz                       # dex aggiuntivi (es: xyz, oppure xyz,altro)

# Position tracking
POSITION_TRACK_INTERVAL=300              # secondi tra un aggiornamento posizioni e l'altro

# Storage
DATA_DIR=data
PRICES_DIR=data/prices
PRICES_TIME=9                            # ora UTC dopo cui salvare snapshot giornaliero
CANDLES_DIR=data/candles                 # directory candele 15m
CANDLES_INTERVAL_SECS=900               # intervallo aggiornamento candele (default 15 min)
LOG_DIR=logs
LOG_LEVEL=INFO

# Sicurezza wallet
WALLET_ENCRYPTION_KEY=                   # chiave Fernet AES-128 (generata automaticamente)

# Accesso ai comandi wallet/trading (lascia vuoto = tutti)
WALLET_ALLOWED_CHATS=                    # es: 123456789,987654321

# Ordini condizionali
CONDITIONAL_SNOOZE_SECS=300             # silenziamento dopo "Salta" (default 5 min)
CONDITIONAL_RENOTIFY_SECS=120           # intervallo re-notifica (default 2 min)
CONDITIONAL_TP_TRAILING_PCT=0.5         # % ritracciamento dal picco per trailing TP automatico

# Scanner daemon
SCANNER_CHAT_IDS=                        # chat_id che ricevono alert scanner daemon
SCANNER_24H_SYMBOLS=BTC,ETH,SOL,...     # simboli crypto (mercato 24/7, no filtro weekend)

# Analisi tecnica AI — Claude Vision
ANTHROPIC_API_KEY=                       # sk-ant-... (da console.anthropic.com)
ANALYZE_ALLOWED_CHATS=                   # chat_id autorizzati a /analyze
```

### Generare la chiave di cifratura

Se `WALLET_ENCRYPTION_KEY` è vuota il bot la genera al primo avvio e la stampa nel log. Copiala nel `.env` e riavvia.

In alternativa:
```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### Ottenere l'API key Anthropic (per `/analyze`)

1. Registrati su [console.anthropic.com](https://console.anthropic.com)
2. Sezione **API Keys** → Create Key
3. Copia la chiave (non sarà più visibile)
4. Sezione **Billing** → aggiungi crediti (sistema prepagato, ~$0.004 per chiamata)

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
| `/price SIMBOLO` | Prezzo corrente live (es. `/price BTC`) |
| `/spread SIMBOLO` | Spread bid/ask corrente |
| `/symbols` | Lista tutti i simboli disponibili con prezzi |
| `/symbols QUERY` | Cerca simboli per nome (es. `/symbols sil`) |
| `/info SIMBOLO` | Scheda dettagliata dal database: mercato, leva max, orari sessione, alias |
| `/history [N]` | Trade history degli ultimi N giorni (default 7, max 30). Mostra P&L netto, fees, win rate e ultimi close |
| `/stats` | Statistiche bot — solo admin |

### Monitoraggio prezzi

| Comando | Descrizione |
|---|---|
| `/subscribe SIMBOLO` | Inizia a monitorare un simbolo. Alert quando il prezzo varia ±soglia% dal riferimento |
| `/unsubscribe SIMBOLO` | Rimuovi sottoscrizione |
| `/list` | Lista delle tue sottoscrizioni attive con prezzi correnti |
| `/threshold X` | Imposta soglia alert personale in % (es. `/threshold 1.5`) |
| `/threshold reset` | Ripristina soglia globale predefinita |
| `/pricespike` | Attiva/disattiva alert spike improvvisi poll-to-poll |
| `/pricespike N` | Imposta soglia spike in % (es. `/pricespike 2.0`) |
| `/pricespike status` | Mostra stato attuale e soglia spike |

> Gli alert spike monitorano tutti i simboli XYZ più quelli in `SPIKE_EXTRA_SYMBOLS`, controllando ogni `POLL_INTERVAL` secondi.

### Wallet e account

| Comando | Descrizione |
|---|---|
| `/setaddress 0x...` | Salva il tuo account address pubblico Hyperliquid |
| `/setkey 0x...` | Salva la private key API (cifrata AES-128, messaggio auto-cancellato) |
| `/walletinfo` | Mostra stato credenziali, equity, margine usato, P&L non realizzato |

> ⚠️ Invia `/setkey` **solo in chat privata** col bot. Il messaggio viene cancellato immediatamente dopo la ricezione.

### Trading

| Comando | Descrizione |
|---|---|
| `/positions` | Posizioni aperte correnti con P&L, size, entry, leva |
| `/trackpositions on\|off` | Attiva/disattiva tracking automatico: notifiche su apertura, modifica e chiusura posizioni |
| `/setleverage SYM LEVA [xyz] [cross]` | Imposta leva per un simbolo. Il mercato viene auto-rilevato; aggiungi `xyz` se fallisce |
| `/long SYM IMPORTO` | Apre long per IMPORTO in USD (es. `/long SOL 500`) |
| `/short SYM IMPORTO` | Apre short |
| `/close SYM` | Chiude posizione al mercato |
| `/confirm` | Conferma ordine pendente |
| `/cancelorder` | Annulla ordine pendente |

**Esempi setleverage:**
```
/setleverage NFLX 5            — auto-rileva mercato
/setleverage SILVER 5 xyz      — forza mercato xyz
/setleverage BTC 10            — PERP standard
/setleverage MSTR 3 xyz cross  — xyz con cross margin
```

### Ordini condizionali

Gli ordini condizionali vengono monitorati dal bot e restano attivi anche se la posizione viene modificata. A differenza dello stop loss nativo, rimangono in carico al bot (non a Hyperliquid direttamente).

#### `/stoploss SYM VALORE`

Imposta uno stop loss nativo su Hyperliquid (triggerato dall'exchange, attivo anche se il bot è offline).

Il VALORE può essere:

| Formato | Significato |
|---|---|
| `4600` | Prezzo assoluto |
| `2%` | Distanza percentuale dal **prezzo attuale** |
| `50$` | Perdita massima in dollari dal **prezzo attuale** |

> Il calcolo è sempre sul **prezzo di mercato corrente**, non sul prezzo di carico. Questo permette di richiamare il comando più volte per aggiornare lo stop loss dinamicamente.

Se esiste già uno stop loss per quel simbolo, viene sostituito automaticamente.

```
/stoploss GOLD 4600       — stop a prezzo fisso 4600
/stoploss GOLD 2%         — stop 2% sotto il prezzo attuale (LONG) o sopra (SHORT)
/stoploss GOLD 50$        — stop quando la perdita raggiunge $50
```

#### `/cancelsl [SYM]`

Cancella lo stop loss nativo. Senza simbolo cancella tutti.

```
/cancelsl GOLD    — cancella solo GOLD
/cancelsl         — cancella tutti
```

#### `/takeprofit SYM VALORE [SIZE%]`

Imposta un take profit condizionale con trailing stop automatico. Il trailing si attiva quando il prezzo supera il trigger e poi ritraccia del `CONDITIONAL_TP_TRAILING_PCT`% dal picco.

Il VALORE può essere:

| Formato | Significato |
|---|---|
| `5600` | Prezzo assoluto |
| `1.3%` | Distanza percentuale dal **prezzo attuale** |
| `12$` | Delta in dollari dal **prezzo attuale** |

Il parametro opzionale SIZE% indica la percentuale della posizione da chiudere (default 100%).

> Anche qui il calcolo è sempre sul **prezzo corrente**, non sul prezzo di carico.

```
/takeprofit GOLD 5100          — TP a prezzo fisso
/takeprofit GOLD 1.5%          — TP 1.5% sopra il prezzo attuale (LONG)
/takeprofit GOLD 15$           — TP quando il guadagno raggiunge $15
/takeprofit GOLD 1.5% 50%      — TP parziale: chiudi solo il 50% della posizione
```

#### `/orders`

Lista tutti gli ordini condizionali attivi con ID, simbolo, tipo, trigger e stato trailing.

#### `/cancelcond ID`

Cancella un ordine condizionale tramite il suo ID (visibile in `/orders`).

### Grafici

#### `/chart SYM [N] [TF]`

Grafico candlestick con Bollinger Bands, EMA 9/21/50, RSI, MACD, volume, supporti/resistenze e trendline dinamiche.

```
/chart GOLD           — ultime ~120 candele 15m
/chart GOLD 200       — 200 candele 15m
/chart GOLD 60 1H     — 60 candele orarie (ricampionate da 15m)
/chart GOLD 56 1D     — 56 candele daily
```

#### `/chartlines on|off`

Attiva o disattiva le trendline di supporto/resistenza dinamiche nel grafico. Preferenza salvata per-utente. Default: off.

```
/chartlines on    — mostra trendline
/chartlines off   — nasconde trendline
```

### Analisi AI — `/analyze`

Genera il chart daily (ricampionato da 15m, ultimi 56 giorni) e il chart 15m, li invia alla Claude Vision API e riceve un'analisi tecnica strutturata.

```
/analyze GOLD           — analisi completa
/analyze GOLD --dryrun  — test senza chiamare l'API (usa cache)
```

**Output:**
- Bias direzionale (LONG / SHORT / NEUTRAL)
- Lettura daily e 15m separata
- Setup suggerito con entry, stop e 2 target con R:R
- Avvisi su contro-trend, squeeze, funding rate

**Costo stimato:** ~$0.004 per chiamata (modello Haiku).

**Accesso:** limitato ai `chat_id` in `ANALYZE_ALLOWED_CHATS`.

### Scanner — `/scan`

Lo scanner analizza i CSV delle candele 15m su tutti i simboli disponibili in `data/candles/`.

---

#### `/scan vsa [N]`

Top N simboli per score VSA (Volume Spread Analysis). Lo score 0–100 combina:
- Pattern VSA (climax, effort/result, stopping volume, no supply/demand)
- Contesto Bollinger Bands (posizione, squeeze)
- Allineamento EMA 9/21/50
- MACD momentum
- Funding rate (penalità se contrario)

```
/scan vsa       — top 10
/scan vsa 5     — top 5
```

#### `/scan SYM [SCORE]` — scan VSA su singolo simbolo

Analisi VSA completa su un simbolo specifico. Se lo score è sotto soglia mostra comunque una mini-analisi con trend, BB, EMA, prezzo live e funding rate.

```
/scan BTC        — analisi BTC con soglia default
/scan BTC 50     — mostra sempre anche sotto score 50
/scan 70         — scan tutti i simboli con soglia 70
```

#### `/scan volume [N]`

Top N simboli per spike di volume sull'ultima candela 15m rispetto alla media delle 20 precedenti.

```
/scan volume      — top 10
/scan volume 5    — top 5
```

Output per simbolo: ratio volume (es. `x3.4`), volume assoluto e valore in USD.

#### `/scan pattern [N]` — pattern candlestick

Top N simboli con pattern candlestick attivi sull'ultima candela 15m.

```
/scan pattern         — top 10 simboli con pattern
/scan pattern 5       — top 5
/scan pattern GOLD    — pattern su GOLD con grafico annotato
```

Pattern riconosciuti: Doji, Hammer, Shooting Star, Engulfing, Morning/Evening Star, Harami, Marubozu, Spinning Top, Piercing Line, Dark Cloud Cover, e altri.

---

#### `/scan fvg [SOGLIA%]` — Fair Value Gap su tutti i simboli

Scansiona tutti i simboli cercando il FVG daily più recente ancora attivo.

**Cos'è un FVG:** zona di squilibrio formata da tre candele consecutive dove la candela centrale crea un gap tra l'ombra della prima e l'ombra della terza:
- **BULLISH:** `candela[i].low > candela[i-2].high` — gap tra il high di due giorni fa e il low di oggi
- **BEARISH:** `candela[i].high < candela[i-2].low` — gap tra il low di due giorni fa e il high di oggi

Un FVG è **attivo** finché il prezzo non ha attraversato completamente il gap. Il mercato tende a tornare in queste zone a cercare liquidità.

Il parametro `SOGLIA%` (default `1.0%`) filtra i gap troppo piccoli. Solo i gap con ampiezza ≥ soglia vengono restituiti.

```
/scan fvg           — tutti i simboli, gap min 1%
/scan fvg 2%        — tutti i simboli, gap min 2%
/scan fvg 0.5       — tutti i simboli, gap min 0.5%
```

Output ordinato per distanza crescente dal prezzo attuale (i gap più vicini prima). Max 20 risultati.

#### `/scan fvg SIMBOLO [SOGLIA%]` — FVG dettagliato su un simbolo

Analisi FVG daily su un singolo simbolo con grafico e messaggio testuale.

```
/scan fvg NFLX         — FVG su NFLX, gap min 1%
/scan fvg NFLX 2%      — FVG su NFLX, gap min 2%
```

**Il grafico mostra:**
- Candele daily (ultime 60 chiuse + candela odierna live in colore attenuato)
- Banda blu semitrasparente = zona FVG con bordi evidenziati
- Testo nella banda: `gap_low – gap_high (ampiezza%)`
- Linea tratteggiata bianca = prezzo live attuale (coincide con il close della candela live)

**Il messaggio testuale mostra:**
- Tipo (BULLISH / BEARISH)
- Zona gap con bordi precisi
- Data di formazione
- Prezzo attuale
- Distanza% dal bordo più vicino del gap

> Le candele daily vengono ricampionate dai CSV 15m escludendo il giorno corrente. La candela live viene costruita dai 15m di oggi e il suo close viene aggiornato al prezzo API in tempo reale.

---

#### `/scan daemon vsa [SCORE]`

Avvia il daemon scanner: dopo ogni chiusura di candela 15m, calcola lo score VSA su tutti i simboli e invia alert per quelli sopra soglia (default 60).

```
/scan daemon vsa         — daemon VSA con soglia 60
/scan daemon vsa 75      — daemon VSA più selettivo
```

#### `/scan daemon volume`

Daemon per spike di volume: alert automatico ogni 15m sui simboli con volume spike significativo.

#### `/scan daemon pattern`

Daemon per pattern candlestick: alert automatico ogni 15m sui simboli con pattern attivi sull'ultima candela.

#### `/scanstop`

Ferma il daemon attivo per la tua chat.

#### `/scanstatus`

Mostra modalità attiva, soglia corrente, numero utenti in ascolto e ultimi segnali.

---

### Admin

| Comando | Descrizione |
|---|---|
| `/message all TESTO` | Broadcast a tutti gli utenti noti |
| `/message CHAT_ID TESTO` | Messaggio diretto a una chat specifica |
| `/reloadinfo` | Ricarica `symbols_info.json` senza riavviare il bot |
| `/stats` | Statistiche bot: utenti, simboli, sottoscrizioni, log recenti |

---

## Tipi di alert automatici

| Alert | Trigger |
|---|---|
| 🔔 **Sottoscrizione** | Prezzo varia ±soglia% dal riferimento (simboli in `/subscribe`) |
| ⚡ **PriceSpike** | Prezzo varia ±soglia% in un singolo poll (10s) |
| 🎯 **Position alert** | P&L posizione varia ±soglia% dal riferimento |
| 🆕 **Nuova posizione** | Posizione aperta rilevata automaticamente ogni 5 min |
| 🏁 **Posizione chiusa** | Posizione non più presente nell'API |
| 🛑 **Stop Loss** | Prezzo raggiunge la soglia impostata con `/stoploss` |
| 🎯 **Take Profit** | Prezzo raggiunge la soglia e poi ritraccia (trailing) |
| 🤖 **Scanner VSA** | Score VSA ≥ soglia (daemon attivo) |
| 📊 **Volume Spike** | Spike volume significativo (daemon attivo) |
| 🕯 **Pattern** | Pattern candlestick attivo sull'ultima candela (daemon attivo) |

---

## Sicurezza

- La **private key** non viene mai tenuta in memoria oltre il tempo della singola operazione
- Viene salvata cifrata con **AES-128** (Fernet) — leggibile solo con `WALLET_ENCRYPTION_KEY` presente nel `.env` del server
- Il messaggio Telegram con `/setkey` viene **cancellato automaticamente** dopo la ricezione
- L'API wallet Hyperliquid **non può prelevare fondi**, solo eseguire ordini
- Puoi limitare l'accesso ai comandi wallet a specifici `chat_id` con `WALLET_ALLOWED_CHATS`
- Il comando `/analyze` è accessibile solo ai `chat_id` in `ANALYZE_ALLOWED_CHATS`

---

## Grafico candlestick — `candle_chart.py`

Script standalone per visualizzare le candele. Non richiede pandas.

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
python3 candle_chart.py GOLD                        # ultime ~120 candele 15m
python3 candle_chart.py GOLD --bars 200             # N candele personalizzate
python3 candle_chart.py GOLD --save gold_chart.png  # salva PNG
python3 candle_chart.py GOLD --data-dir /altro/path # directory custom
```

---

## Pipeline ML

### 1. Feature engineering — `ml_features.py`

Produce 48 feature per candela da CSV 15m: EMA/RSI/MACD/Bollinger/ATR, struttura candela, momentum, volumi, 18 pattern candlestick binari, contesto temporale ciclico.

```bash
python3 ml_features.py --horizon 16 --auto-threshold --no-charts --quiet --save-csv
```

| Argomento | Descrizione |
|---|---|
| `--horizon N` | Orizzonte predizione in candele (ore × 4, es. 4h = 16) |
| `--auto-threshold` | Soglia LONG/SHORT adattiva per simbolo |
| `--no-charts` | Disabilita grafici (obbligatorio su molti simboli) |
| `--quiet` | Output minimale |
| `--save-csv` | Salva dataset in `ml_reports/` |

### 2. Training — `ml_trainer.py`

```bash
python3 ml_trainer.py
```

### 3. Analisi AI — `ml_analyst.py`

Richiamato dal comando `/analyze`. Ricampiona i 15m in daily, genera due PNG (daily + 15m) e li invia alla Claude Vision API.

**Costo stimato:** ~$0.004 per chiamata (modello Haiku).
