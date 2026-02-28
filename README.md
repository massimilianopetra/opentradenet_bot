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
- **Multi-mercato** — perp standard, XYZ (GOLD, SILVER, NFLX, ORCL…), spot
- **Credenziali cifrate** — la private key API è salvata su disco con AES-128 (Fernet)

---

## Architettura

```
opentradenet_bot/
├── hyperliquid_bot.py      # Bot principale — comandi Telegram e polling
├── hl_wallet.py            # Gestione wallet, credenziali e client API Hyperliquid
├── opentradenet.env        # Configurazione (non committare su git!)
├── opentradenet_bot.service # Unit systemd
├── data/
│   ├── wallet/
│   │   └── {chat_id}/
│   │       ├── address.txt  # Account address pubblico
│   │       └── key.enc      # Private key cifrata con AES-128
│   └── prices/
│       └── {SIMBOLO}.csv    # Storico prezzi giornalieri
└── logs/
    └── hyperliquid_bot.log  # Log con rotazione giornaliera (7 giorni)
```

---

## Requisiti

```bash
pip install python-telegram-bot aiohttp python-dotenv cryptography \
            hyperliquid-python-sdk eth-account requests
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
LOG_DIR=logs
LOG_LEVEL=INFO

# Sicurezza wallet (generata automaticamente al primo avvio se mancante)
WALLET_ENCRYPTION_KEY=                   # chiave Fernet AES-128 (44 caratteri base64)

# Accesso ai comandi wallet/trading (lascia vuoto = tutti)
WALLET_ALLOWED_CHATS=                    # es: 123456789,987654321
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
| `/symbols QUERY` | Cerca simboli (es. `/symbols sil` → trova SILVER) |
| `/stats` | Statistiche del bot |

---

### Monitoraggio prezzi (Subscribe)

Monitora un simbolo e ricevi alert quando il prezzo varia oltre la soglia impostata.

| Comando | Descrizione |
|---|---|
| `/subscribe SIMBOLO` | Iscriviti agli alert di prezzo per un simbolo |
| `/unsubscribe SIMBOLO` | Cancella iscrizione |
| `/list` | Mostra subscribe attivi e posizioni in tracking |
| `/threshold` | Mostra soglia alert attuale |
| `/threshold 1.5` | Imposta soglia a ±1.5% |
| `/threshold reset` | Ripristina soglia globale dal `.env` |

**Come funziona:** il bot controlla ogni 10 secondi. Quando il prezzo varia di ±soglia% dal riferimento precedente, invia un alert. Il riferimento si aggiorna dopo ogni alert.

---

### PriceSpike

Alert su movimenti improvvisi da un poll al successivo (entità anomale, news, dump/pump).

| Comando | Descrizione |
|---|---|
| `/pricespike` | Attiva/disattiva pricespike |
| `/pricespike on` | Attiva |
| `/pricespike off` | Disattiva |
| `/pricespike 2.0` | Imposta soglia spike a ±2.0% per poll |
| `/pricespike status` | Mostra stato e soglia |

Monitora automaticamente tutti i simboli XYZ + i simboli extra configurati in `SPIKE_EXTRA_SYMBOLS`.

---

### Position Tracking

Le posizioni aperte su Hyperliquid vengono rilevate automaticamente e monitorate. Non serve fare `/subscribe` manualmente.

| Comando | Descrizione |
|---|---|
| `/trackpositions` | Mostra stato tracking |
| `/trackpositions on` | Attiva tracking (default se address configurato) |
| `/trackpositions off` | Disattiva tracking |

**Come funziona:**
- Ogni **5 minuti** — l'API viene interrogata per rilevare nuove posizioni o posizioni chiuse. Manda notifiche automatiche.
- Ogni **10 secondi** — il prezzo corrente viene confrontato con il riferimento. Se varia oltre la soglia, arriva un alert arricchito (entry, PnL, distanza liquidazione).

**Alert automatici:**

```
🎯 Nuova posizione rilevata
📈 LONG GOLD _XYZ_ 5x
Entry: $5,358.00  Size: 0.0037
Margin: $21.43  Liq: $4,450.00
Soglia alert: ±0.5%
```

```
🎯 Position Alert — 14:23:10 (soglia ±0.5%)
📈 GOLD LONG _XYZ_ 5x  size: 0.0037  ✅ a favore
  Rif: $5,340.00  →  $5,367.00  (+0.51%)
  Entry: $5,358.00  →  $5,367.00  (+0.17%)
  PnL: +$0.03 (+0.1%)
  Liq dist: 18.5%
```

```
🏁 Posizione chiusa: GOLD
LONG _XYZ_ 5x
Entry: $5,358.00
⏰ 19:56:22
```

---

### Wallet e credenziali

Permette di collegare il tuo wallet Hyperliquid al bot per usare i comandi di trading.

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
4. Usa `/setaddress` per il tuo account address principale (42 caratteri, visibile in alto a destra)
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
Invia /confirm per eseguire
⏰ Scade in 30 secondi

/confirm

✅ Ordine eseguito
📈 LONG GOLD _XYZ_
Size: 0.0933
Prezzo: $5,360.00
Importo: $500.00
⏰ 19:56:16
```

**Esempio chiusura parziale:**
```
/close GOLD 50%

⚠️ Conferma chiusura
🔴 CLOSE GOLD _XYZ_
Size da chiudere: 0.0466 (50% di 0.0933)

/confirm

✅ Ordine eseguito
🔴 CLOSE GOLD _XYZ_
Size: 0.0466
⏰ 20:01:33
```

La leva usata è quella già configurata sul simbolo con `/setleverage`. Il mercato (PERP o XYZ) viene rilevato automaticamente.

---

## Tipi di alert

| Alert | Trigger |
|---|---|
| 🔔 **Alert sottoscrizioni** | Prezzo varia ±soglia% dal riferimento (simboli in `/subscribe`) |
| ⚡ **PriceSpike** | Prezzo varia ±soglia% in un singolo poll (10s) |
| 🎯 **Position Alert** | Prezzo posizione varia ±soglia% dal riferimento |
| 🎯 **Nuova posizione** | Posizione aperta rilevata automaticamente ogni 5 min |
| 🏁 **Posizione chiusa** | Posizione non più presente nell'API |

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
| `opentradenet.env` | Configurazione (da non committare) |
| `opentradenet_bot.service` | Unit systemd |
| `generate_report.py` | Script standalone per report HTML delle performance da CSV Hyperliquid |

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
