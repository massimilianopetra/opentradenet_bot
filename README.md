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

Per `candle_chart.py` e `ml_features.py` bastano `matplotlib` e `numpy`:

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

> ⚠️ Invia `/setkey` solo in chat **privata** col bot. Il messaggio viene cancellato automaticamente dopo la ricezione.

**Come ottenere la API key su Hyperliquid:**
1. Vai su [app.hyperliquid.xyz/API](https://app.hyperliquid.xyz/API)
2. Genera un nuovo API wallet → copia la private key
3. Autorizza l'API wallet firmando la transazione
4. Usa `/setaddress` per il tuo account address principale (42 caratteri)
5. Usa `/setkey` per la private key dell'API wallet

La private key **non può prelevare fondi** — può solo aprire/chiudere posizioni.

### Posizioni e trading

| Comando | Descrizione |
|---|---|
| `/positions` | Posizioni aperte correnti |
| `/posalert SIMBOLO X` | Alert se posizione varia ±X% |
| `/buy SIMBOLO QTY [leverage]` | Apre long |
| `/sell SIMBOLO QTY [leverage]` | Apre short |
| `/close SIMBOLO` | Chiude posizione |
| `/stoploss SIMBOLO PREZZO` | Imposta stop loss |
| `/takeprofit SIMBOLO PREZZO` | Imposta take profit con trailing stop |
| `/cancelcond SIMBOLO` | Cancella ordini condizionali attivi |

### Grafici e analisi

| Comando | Descrizione |
|---|---|
| `/chart SIMBOLO [BARRE]` | Grafico candlestick 15m (es. `/chart GOLD 96`) |

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
| `fill_candles.py` | Riempie buchi nelle candele storiche |
| `ml_features.py` | Feature engineering ML su candele 15m |
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

---

## Report performance — `generate_report.py`

Script standalone che genera un report HTML interattivo dal CSV della trade history esportato da Hyperliquid.

```bash
python3 generate_report.py trade_history.csv
python3 generate_report.py trade_history.csv -o report_febbraio.html
python3 generate_report.py trade_history.csv --open   # apre nel browser
```

Il report include: P&L netto, win rate, profit factor, P&L per simbolo, split Long/Short e PERP/XYZ, timeline, tabella completa trade e posizioni ancora aperte.

---

## Fill candele — `fill_candles.py`

Recupera le candele storiche mancanti da Hyperliquid e le aggiunge ai CSV esistenti senza toccare il bot.

```bash
python3 fill_candles.py                  # usa opentradenet.env, ultimi 14 giorni
python3 fill_candles.py --days 30        # recupera fino a 30 giorni fa
python3 fill_candles.py --symbol BTC     # solo un simbolo
python3 fill_candles.py --dry-run        # mostra buchi senza scrivere
```

---

## ML Feature Engineering — `ml_features.py`

Script standalone che legge i CSV candele 15m e produce un dataset con **48 feature tecniche + label** pronto per il training di modelli ML (XGBoost, Random Forest ecc.).

Non richiede dipendenze nuove — usa solo `matplotlib` e `numpy` già presenti nel progetto. Tutti gli indicatori sono implementati in Python puro.

### A cosa serve

`ml_features.py` è il primo passo del pipeline ML. **Non produce segnali di trading** — analizza lo storico candele e risponde alla domanda:

> *"Quando il prezzo è poi salito/sceso di almeno X% nelle 4 ore successive, cosa stavano facendo gli indicatori in quel momento?"*

L'output (grafici + dataset CSV) serve a capire quali feature sono predittive su ogni simbolo, e a preparare i dati per allenare un modello che in futuro manderà alert Telegram LONG/SHORT.

### Uso rapido

```bash
# Tutti i simboli — soglia automatica — solo testo (CONSIGLIATO per 300+ simboli)
python3 ml_features.py --horizon 16 --auto-threshold --no-charts --quiet

# Come sopra + salva dataset CSV per il training ML
python3 ml_features.py --horizon 16 --auto-threshold --no-charts --quiet --save-csv

# Solo un simbolo con grafici
python3 ml_features.py --symbol BTC --horizon 16 --auto-threshold

# Soglia manuale fissa per tutti i simboli
python3 ml_features.py --horizon 16 --threshold 0.015
```

### Argomenti

| Argomento | Default | Descrizione |
|---|---|---|
| `--data-dir PATH` | `data/candles` | Directory base dei CSV candele |
| `--symbol SYM` | — | Processa solo questo simbolo |
| `--horizon N` | `4` | Candele future per il label (N × 15min = orizzonte previsione) |
| `--threshold F` | `0.003` | Soglia manuale % per LONG/SHORT (es. `0.015` = 1.5%) |
| `--auto-threshold` | off | Calcola soglia adattiva per simbolo (target ~62% NEUTRO) |
| `--save-csv` | off | Salva dataset in `ml_reports/SIMBOLO_dataset.csv` |
| `--no-charts` | off | Non generare PNG (molto più veloce su 300+ simboli) |
| `--quiet` | off | Output minimale: una riga per simbolo + tabella finale |

### Horizon — come si calcola

L'horizon è espresso in **numero di candele**, non in ore. Con candele a 15 minuti:

```
--horizon 4   →   1 ora avanti
--horizon 8   →   2 ore avanti
--horizon 16  →   4 ore avanti  (consigliato)
--horizon 32  →   8 ore avanti
--horizon 96  →  24 ore avanti

Formula: ore desiderate × 4 = horizon
```

### Label

Per ogni candela all'istante T, il label guarda il prezzo a T+horizon:

```
future_return = (close[T+horizon] - close[T]) / close[T]

label = +1  (LONG)   se future_return > +soglia
label = -1  (SHORT)  se future_return < -soglia
label =  0  (NEUTRO) altrimenti
```

La distribuzione ottimale è circa **62% NEUTRO, 19% LONG, 19% SHORT**. Con `--auto-threshold` la soglia viene calcolata automaticamente per ogni simbolo per avvicinarsi a questo target — necessario perché crypto e azionari hanno volatilità molto diverse.

### Feature calcolate (48 totali)

**Struttura candela corrente (6):** `body_ratio`, `upper_wick_ratio`, `lower_wick_ratio`, `close_position`, `is_bullish`, `range_pct`

**Momentum (3):** `ret_1`, `ret_3`, `ret_5` — return % su 1, 3, 5 candele passate

**Indicatori tecnici (10):** distanza % da EMA9/21/50, cross EMA9 vs EMA21, posizione nelle BB, larghezza BB, RSI 14, MACD histogram, variazione MACD histogram, ATR %

**Volume (2):** `vol_ratio` (vs SMA20), `vol_ratio_5` (vs SMA5)

**Sequenza (5):** direzione ultime 3 candele, candele consecutive rialziste/ribassiste

**Pattern candlestick (18):**
- Singola candela: `pat_doji`, `pat_hammer`, `pat_inverted_hammer`, `pat_marubozu_bull`, `pat_marubozu_bear`, `pat_spinning_top`
- Due candele: `pat_engulfing_bull`, `pat_engulfing_bear`, `pat_harami_bull`, `pat_harami_bear`, `pat_tweezer_bottom`, `pat_tweezer_top`, `pat_piercing_line`, `pat_dark_cloud_cover`
- Tre candele: `pat_morning_star`, `pat_evening_star`, `pat_three_white_soldiers`, `pat_three_black_crows`

**Contesto temporale (4):** ora UTC e giorno della settimana codificati ciclicamente (sin/cos)

### Output prodotto

Tutti i file vengono salvati in `ml_reports/`:

| File | Descrizione |
|---|---|
| `summary.txt` | Tabella riepilogativa di tutti i simboli: campioni, % LONG/SHORT/NEUTRO, soglia usata, top feature |
| `SIMBOLO_features.png` | Grafici analisi (6 pannelli, vedi sotto) |
| `SIMBOLO_dataset.csv` | Dataset pronto per training ML (con `--save-csv`) |

### Grafici — cosa leggere

I grafici PNG (generati senza `--no-charts`) mostrano 6 pannelli:

**Pannello 1 — Distribuzione label (torta):** mostra se il simbolo è bilanciato tra LONG/SHORT/NEUTRO. Un forte squilibrio (es. LONG 35% SHORT 8%) indica un bias storico rialzista — il modello ne terrà conto.

**Pannello 2 — Istogramma return futuro:** la forma della curva indica quanto è prevedibile il simbolo. Una campana larga con due gobbe è più favorevole al ML di una campana stretta centrata sullo zero.

**Pannello 3 — RSI medio per label:** se LONG ha RSI basso e SHORT ha RSI alto, la strategia oversold/overbought funziona su quel simbolo. Se i valori sono simili, RSI non è predittivo.

**Pannello 4 — Posizione BB per label:** se LONG ha `bb_position` bassa (vicino alla banda inferiore) il simbolo è mean-reverting. Se LONG ha `bb_position` alta, è momentum/breakout. Cambia tutto nell'approccio al trading.

**Pannello 5 — Volume ratio per label:** se i movimenti forti (LONG/SHORT) hanno volume ratio alto e NEUTRO ha volume basso, il volume è un filtro affidabile anche per il trading manuale.

**Pannello 6 — Pannello 6 — Correlazioni feature → label:** il pannello più importante. Mostra quali dei 48 indicatori sono più predittivi su quel simbolo specifico. Il segno indica la direzione: correlazione negativa su RSI significa "RSI alto → short, RSI basso → long". Usalo per capire la "personalità" del simbolo.

### Esempio output `--quiet`

```
  [  1/300]  AAPL         elapsed=0s   ETA=120s
  [ 10/300]  BTC          elapsed=12s  ETA=108s
  ...
  ────────────────────────────────────────────────────────────
  SIMBOLO      CAMPIONI   LONG%  SHORT%  NEUTRO%  SOGLIA
  AAPL             1640   19.1%   18.8%    62.1%   1.23%
  BTC              1650   19.4%   19.2%    61.4%   2.87%
  HYPE             1645   18.9%   19.3%    61.8%   1.91%
  INTC             1638   19.0%   19.1%    61.9%   0.87%
  ...
  ════════════════════════════════════════════════════════════
  ✅ Completato in 87.3s
  Simboli OK : 298
```

### Note tecniche

- Il file usa `matplotlib.use('Agg')` per funzionare correttamente come servizio systemd senza display
- Il path `data/candles` viene risolto relativamente alla posizione dello script (compatibile con systemd)
- Tutti gli indicatori (EMA, RSI, MACD, BB, ATR) sono implementati in Python puro — numpy usato solo dove richiesto da matplotlib
- I pattern candlestick sono tutti binari (0/1) e non richiedono librerie esterne come `ta-lib`
- Con `--no-charts --quiet` il tempo di esecuzione su 300 simboli è tipicamente 1-3 minuti

---

## Trailing stop automatico

Il take profit supporta un trailing stop opzionale: quando il prezzo supera il target e poi ritraccia della percentuale configurata, la posizione viene chiusa automaticamente senza intervento manuale.

```env
CONDITIONAL_TP_TRAILING_PCT=0.5   # % ritracciamento dal picco → chiusura automatica
```

---

## Licenza

MIT
