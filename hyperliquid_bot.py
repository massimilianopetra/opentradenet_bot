import asyncio
import csv
import logging
import logging.handlers
import os
import sys
from typing import Dict, Set, Optional, Tuple
from datetime import datetime, date
from pathlib import Path
from dotenv import load_dotenv
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from hl_wallet import WalletStore, HyperliquidClient, generate_encryption_key
import tempfile
import matplotlib
matplotlib.use('Agg')   # ← fondamentale su server senza display
import matplotlib.pyplot as plt
import candle_chart as cc

# ---------------------------------------------------------------------------
# Configurazione ambiente
# ---------------------------------------------------------------------------

if len(sys.argv) > 1:
    env_file = sys.argv[1]
else:
    env_file = 'opentradenet.env'

env_path = Path(env_file) if Path(env_file).is_absolute() else Path('.') / env_file

if not env_path.exists():
    print(f"❌ Errore: File {env_file} non trovato!")
    sys.exit(1)

load_dotenv(dotenv_path=env_path)
print(f"✅ Caricato file di configurazione: {env_file}")

# ---------------------------------------------------------------------------
# Logging: console + file con rotazione giornaliera (7 giorni)
# ---------------------------------------------------------------------------

LOG_DIR  = Path(os.getenv('LOG_DIR', 'logs'))
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / 'hyperliquid_bot.log'

fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

console_handler = logging.StreamHandler()
console_handler.setFormatter(fmt)

file_handler = logging.handlers.TimedRotatingFileHandler(
    LOG_FILE,
    when='midnight',
    interval=1,
    backupCount=7,
    encoding='utf-8'
)
file_handler.setFormatter(fmt)
file_handler.suffix = '%Y-%m-%d'

root_logger = logging.getLogger()
_log_level = getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO)
root_logger.setLevel(_log_level)
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)

# Silenzia i log di httpx e httpcore (usati internamente da python-telegram-bot)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN         = os.getenv('TELEGRAM_TOKEN')
HYPERLIQUID_API        = os.getenv('HYPERLIQUID_API', 'https://api.hyperliquid.xyz/info')
POLL_INTERVAL          = int(os.getenv('POLL_INTERVAL', '10'))
PRICE_CHANGE_THRESHOLD = float(os.getenv('PRICE_CHANGE_THRESHOLD', '0.5'))
MAX_SYMBOLS_DISPLAY    = int(os.getenv('MAX_SYMBOLS_DISPLAY', '20'))

# DEX aggiuntivi: lista separata da virgola nel .env
# Es: SUPPORTED_DEXS=xyz        oppure SUPPORTED_DEXS=xyz,flx,vntl
# "" = perp standard (sempre incluso), "xyz" = perp XYZ ecc.
SUPPORTED_DEXS = [d.strip() for d in os.getenv('SUPPORTED_DEXS', 'xyz').split(',') if d.strip()]

# Simboli fissi per pricespike: lista separata da virgola nel .env
# Es: SPIKE_EXTRA_SYMBOLS=BTC,SOL,ETH,XRP,SUI,HYPER
# I simboli xyz vengono aggiunti automaticamente a runtime dal batch
SPIKE_EXTRA_SYMBOLS    = [s.strip().upper() for s in os.getenv('SPIKE_EXTRA_SYMBOLS', 'BTC,SOL,ETH,XRP,SUI,HYPE').split(',') if s.strip()]
SPIKE_THRESHOLD        = float(os.getenv('SPIKE_THRESHOLD', '1.0'))   # soglia % poll-to-poll per pricespike
# Simboli xyz da escludere dagli alert spike (bassi volumi) — solo gli avvisi, lo storico viene comunque salvato
# Es: SPIKE_EXCLUDE_SYMBOLS=SYMB1,SYMB2
SPIKE_EXCLUDE_SYMBOLS  = {s.strip().upper() for s in os.getenv('SPIKE_EXCLUDE_SYMBOLS', '').split(',') if s.strip()}

# Directory storico prezzi giornalieri (un CSV per simbolo)
# Record_TIME: ora dopo cui scrivere la quotazione del giorno (default 09:00)
PRICES_DIR    = Path(os.getenv('PRICES_DIR', 'data/prices'))
COND_DIR      = Path(os.getenv('COND_DIR',   'data/conditional_orders'))
CANDLES_DIR   = Path(os.getenv('CANDLES_DIR', 'data/candles'))
CANDLES_INTERVAL_SECS = int(os.getenv('CANDLES_INTERVAL_SECS', '900'))  # 900s = 15 minuti
PRICES_TIME   = int(os.getenv('PRICES_TIME', '9'))  # ora intera (0-23)

# Intervallo aggiornamento position tracking (default 5 minuti)
POSITION_TRACK_INTERVAL = int(os.getenv('POSITION_TRACK_INTERVAL', '300'))

# Silenziamento ordini condizionali dopo "Salta" (default 5 minuti)
CONDITIONAL_SNOOZE_SECS   = int(os.getenv('CONDITIONAL_SNOOZE_SECS',      '300'))
# Intervallo re-notifica ordini condizionali (nuovo messaggio con bip, default 2 minuti)
CONDITIONAL_RENOTIFY_SECS  = int(os.getenv('CONDITIONAL_RENOTIFY_SECS',  '120'))
# Trailing stop per takeprofit: % ritracciamento dal picco → chiusura automatica
CONDITIONAL_TP_TRAILING_PCT = float(os.getenv('CONDITIONAL_TP_TRAILING_PCT', '0.5'))

# Whitelist chat_id autorizzati a usare comandi wallet/trading (separati da virgola)
# Es: WALLET_ALLOWED_CHATS=123456789,987654321
# Se vuoto: tutti gli utenti possono registrare le loro credenziali
WALLET_ALLOWED_CHATS = {int(x.strip()) for x in os.getenv('WALLET_ALLOWED_CHATS', '').split(',') if x.strip()}

# Chiave di cifratura per le private key su disco (Fernet AES-128)
# Se non presente nel .env viene generata automaticamente al primo avvio e stampata in log
WALLET_ENCRYPTION_KEY = os.getenv('WALLET_ENCRYPTION_KEY', '')


if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_TOKEN non trovato nel file .env")
    sys.exit(1)

logger.info(f"Config: POLL={POLL_INTERVAL}s THRESHOLD={PRICE_CHANGE_THRESHOLD}% DEX={SUPPORTED_DEXS}")
logger.info(f"Log file: {LOG_FILE.resolve()}")

# Inizializza WalletStore — genera chiave di cifratura se non presente nel .env
if not WALLET_ENCRYPTION_KEY:
    _new_key = generate_encryption_key()
    logger.warning("=" * 60)
    logger.warning("WALLET_ENCRYPTION_KEY non trovata nel .env!")
    logger.warning(f"Aggiungi questa riga al tuo {env_file}:")
    logger.warning(f"WALLET_ENCRYPTION_KEY={_new_key}")
    logger.warning("=" * 60)
    _enc_key = _new_key
else:
    _enc_key = WALLET_ENCRYPTION_KEY

DATA_DIR    = Path(os.getenv('DATA_DIR', 'data'))
wallet_store = WalletStore(DATA_DIR, _enc_key)


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class HyperliquidPriceMonitor:
    def __init__(self):
        self.subscribers:       Dict[int, Set[str]] = {}
        self.last_prices:       Dict[str, float]    = {}  # aggiornato ogni poll
        self.alert_base_prices:     Dict[str, float]    = {}  # riferimento alert: impostato al subscribe, aggiornato dopo ogni alert
        self.subscribe_base_prices: Dict[str, float]    = {}  # prezzo al momento del primo subscribe, mai modificato
        self.user_thresholds:   Dict[int, float]    = {}  # soglia subscribe per-utente, fallback a PRICE_CHANGE_THRESHOLD
        self.spike_thresholds:  Dict[int, float]    = {}  # soglia spike per-utente, fallback a SPIKE_THRESHOLD
        self.spike_subscribers: Set[int]             = set()  # chat_id iscritti a pricespike
        self.spike_prev_prices: Dict[str, float]     = {}     # prezzi poll precedente per pricespike
        self.last_snapshot_date: Optional[date]       = None   # data dell'ultimo snapshot giornaliero scritto
        self._first_track_done: set                   = set()  # chat_id già passati per il primo ciclo tracking
        # Position tracking: {chat_id: {coin: {entry_px, is_long, size, margin, leverage, dex, alert_base, added_at}}}
        self.position_tracks:   Dict[int, Dict[str, dict]] = {}
        # Ordini in attesa di conferma: {chat_id: {action, coin, dex, is_buy, usd, expires_at}}
        self.pending_orders:    Dict[int, dict]             = {}
        # Ordini condizionali: {chat_id: {order_id: {coin, dex, type, trigger_px, action, usd, pct, created_at, snoozed_until}}}
        self.conditional_orders: Dict[int, Dict[str, dict]] = {}
        self._cond_id_counter: int = 0
        # chat_id con tracking attivo (default: tutti quelli con address configurato)
        self.tracking_enabled:  Set[int]                   = set()
        self.session: Optional[aiohttp.ClientSession] = None

    async def init_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
            logger.debug("Sessione HTTP inizializzata")

    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None
            logger.debug("Sessione HTTP chiusa")

    async def _allMids(self, dex: str) -> dict:
        """
        POST /info {"type": "allMids", "dex": dex}
          dex=""    -> dict piatto {"BTC": "price", "PURR/USDC": "price", ...}
          dex="xyz" -> dict piatto {"xyz:MSTR": "price", ...}
        """
        await self.init_session()
        try:
            async with self.session.post(
                HYPERLIQUID_API,
                json={"type": "allMids", "dex": dex}
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error(f"allMids dex='{dex}' HTTP {resp.status}")
                return {}
        except asyncio.TimeoutError:
            logger.error(f"allMids dex='{dex}' timeout")
            return {}
        except Exception as e:
            logger.error(f"allMids dex='{dex}' errore: {e}")
            return {}

    async def fetch_all_prices(self) -> Dict[str, Tuple[float, str, Optional[str]]]:
        """
        Recupera TUTTI i prezzi in batch (2 chiamate HTTP totali).
        Ritorna: { symbol -> (prezzo, tipo, dex_label) }
        """
        prices: Dict[str, Tuple[float, str, Optional[str]]] = {}

        # Perp standard + spot
        data = await self._allMids("")
        for key, val in data.items():
            try:
                price = float(val)
            except (ValueError, TypeError):
                continue
            if '/' in key:
                sym = key.split('/')[0]
                prices[sym] = (price, 'SPOT', None)
            elif ':' not in key:
                prices[key] = (price, 'PERP', None)

        # Perp DEX
        for dex in SUPPORTED_DEXS:
            dex_data = await self._allMids(dex)
            for key, val in dex_data.items():
                if ':' not in key:
                    continue
                try:
                    price = float(val)
                except (ValueError, TypeError):
                    continue
                sym = key.split(':', 1)[1]
                prices[sym] = (price, 'PERP', dex.upper())

        return prices

    async def get_price(self, symbol: str) -> Optional[Tuple[float, str, Optional[str]]]:
        """Prezzo singolo (usato da /price e /subscribe)."""
        sym = symbol.upper()

        data = await self._allMids("")
        if sym in data:
            return (float(data[sym]), 'PERP', None)
        if f"{sym}/USDC" in data:
            return (float(data[f"{sym}/USDC"]), 'SPOT', None)

        for dex in SUPPORTED_DEXS:
            dex_data = await self._allMids(dex)
            if f"{dex}:{sym}" in dex_data:
                return (float(dex_data[f"{dex}:{sym}"]), 'PERP', dex.upper())

        logger.debug(f"Simbolo {sym} non trovato")
        return None

    async def get_all_symbols(self) -> dict:
        result: dict = {'perps': [], 'spot': [], 'dex_perps': {}}
        data = await self._allMids("")
        result['perps'] = sorted(k for k in data if '/' not in k and ':' not in k and not k.startswith('@'))
        result['spot']  = sorted(k for k in data if '/' in k)
        for dex in SUPPORTED_DEXS:
            dex_data = await self._allMids(dex)
            if dex_data:
                result['dex_perps'][dex.upper()] = sorted(
                    k.split(':', 1)[1] for k in dex_data if ':' in k
                )
        return result

    # ---------------------------------------------------------------------------
    # Persistenza ordini condizionali
    # ---------------------------------------------------------------------------

    def _cond_path(self, chat_id: int) -> Path:
        p = COND_DIR / str(chat_id)
        p.mkdir(parents=True, exist_ok=True)
        return p / 'conditional_orders.json'

    def save_conditional_orders(self, chat_id: int) -> None:
        """Salva gli ordini condizionali di un chat_id su file JSON."""
        import json
        conds = self.conditional_orders.get(chat_id, {})
        data  = {}
        for oid, o in conds.items():
            row = dict(o)
            # datetime non è serializzabile — converti in ISO string
            if isinstance(row.get('created_at'), datetime):
                row['created_at'] = row['created_at'].isoformat()
            data[oid] = row
        try:
            self._cond_path(chat_id).write_text(
                json.dumps(data, indent=2), encoding='utf-8'
            )
        except Exception as e:
            logger.error(f"Errore salvataggio ordini condizionali {chat_id}: {e}")

    def load_conditional_orders(self, chat_id: int) -> None:
        """Carica gli ordini condizionali da file per un chat_id."""
        import json
        path = self._cond_path(chat_id)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            orders = {}
            for oid, o in data.items():
                # Ripristina datetime
                if isinstance(o.get('created_at'), str):
                    try:
                        o['created_at'] = datetime.fromisoformat(o['created_at'])
                    except Exception:
                        o['created_at'] = datetime.now()
                orders[oid] = o
                # Aggiorna contatore ID
                try:
                    num = int(oid[1:])
                    if num > self._cond_id_counter:
                        self._cond_id_counter = num
                except Exception:
                    pass
            self.conditional_orders[chat_id] = orders
            logger.info(f"Caricati {len(orders)} ordini condizionali per chat {chat_id}")
        except Exception as e:
            logger.error(f"Errore caricamento ordini condizionali {chat_id}: {e}")

    def load_all_conditional_orders(self) -> None:
        """Carica gli ordini condizionali di tutti i wallet al boot."""
        COND_DIR.mkdir(parents=True, exist_ok=True)
        for chat_dir in COND_DIR.iterdir():
            if chat_dir.is_dir():
                try:
                    self.load_conditional_orders(int(chat_dir.name))
                except Exception:
                    pass
        # Arricchisce gli ordini senza is_long recuperando le posizioni dall'API
        self._fix_missing_is_long()

    def _fix_missing_is_long(self) -> None:
        """
        Per ogni ordine caricato senza campo is_long, recupera la direzione
        dalla posizione aperta su Hyperliquid. Chiamato una volta al boot.
        Usa user_state per perp standard e clearinghouseState+dex per xyz.
        """
        import requests as _req
        API = 'https://api.hyperliquid.xyz/info'

        for chat_id, conds in self.conditional_orders.items():
            needs_fix = [o for o in conds.values() if 'is_long' not in o]
            if not needs_fix:
                continue

            # Legge address
            addr = None
            try:
                addr_file = DATA_DIR / 'wallet' / str(chat_id) / 'address.txt'
                if addr_file.exists():
                    addr = addr_file.read_text(encoding='utf-8').strip()
            except Exception:
                pass
            if not addr:
                for o in needs_fix:
                    o['is_long'] = True
                continue

            pos_by_coin = {}

            # 1) Perp standard — usa user_state
            try:
                resp  = _req.post(API, json={'type': 'clearinghouseState',
                                             'user': addr}, timeout=8)
                state = resp.json()
                for ap in state.get('assetPositions', []):
                    pos = ap.get('position', {})
                    szi = float(pos.get('szi', 0))
                    if szi == 0:
                        continue
                    coin = pos.get('coin', '').split(':')[-1]
                    pos_by_coin[coin] = szi > 0
                    logger.info(f"_fix_is_long PERP {coin}: szi={szi} is_long={szi>0}")
            except Exception as e:
                logger.warning(f"_fix_missing_is_long perp: {e}")

            # 2) DEX xyz — usa clearinghouseState con dex
            for dex in SUPPORTED_DEXS:
                try:
                    resp  = _req.post(API, json={'type': 'clearinghouseState',
                                                 'user': addr, 'dex': dex}, timeout=8)
                    state = resp.json()
                    for ap in state.get('assetPositions', []):
                        pos = ap.get('position', {})
                        szi = float(pos.get('szi', 0))
                        if szi == 0:
                            continue
                        coin = pos.get('coin', '').split(':')[-1]
                        pos_by_coin[coin] = szi > 0
                        logger.info(f"_fix_is_long {dex} {coin}: szi={szi} is_long={szi>0}")
                except Exception as e:
                    logger.warning(f"_fix_missing_is_long dex={dex}: {e}")

            # Applica is_long agli ordini e salva
            changed = False
            for o in needs_fix:
                coin = o.get('coin', '')
                if coin in pos_by_coin:
                    o['is_long'] = pos_by_coin[coin]
                    logger.info(f"Ordine {coin} ({o['type']}) is_long={o['is_long']}")
                else:
                    o['is_long'] = True
                    logger.warning(f"Posizione {coin} non trovata nell'API, default is_long=True")
                changed = True
            if changed:
                self.save_conditional_orders(chat_id)

    def get_threshold(self, chat_id: int) -> float:
        """Soglia subscribe per-utente, fallback a PRICE_CHANGE_THRESHOLD globale."""
        return self.user_thresholds.get(chat_id, PRICE_CHANGE_THRESHOLD)

    def set_threshold(self, chat_id: int, threshold: float):
        self.user_thresholds[chat_id] = threshold
        logger.info(f"Chat {chat_id} soglia subscribe impostata a {threshold}%")

    def get_spike_threshold(self, chat_id: int) -> float:
        """Soglia spike per-utente, fallback a SPIKE_THRESHOLD globale."""
        return self.spike_thresholds.get(chat_id, SPIKE_THRESHOLD)

    def set_spike_threshold(self, chat_id: int, threshold: float):
        self.spike_thresholds[chat_id] = threshold
        logger.info(f"Chat {chat_id} soglia spike impostata a {threshold}%")

    def add_subscriber(self, chat_id: int, symbol: str):
        self.subscribers.setdefault(chat_id, set()).add(symbol.upper())
        logger.info(f"Chat {chat_id} sottoscritta a {symbol.upper()}")

    def remove_subscriber(self, chat_id: int, symbol: str):
        if chat_id in self.subscribers:
            self.subscribers[chat_id].discard(symbol.upper())
            if not self.subscribers[chat_id]:
                del self.subscribers[chat_id]

    def get_subscriptions(self, chat_id: int) -> Set[str]:
        return self.subscribers.get(chat_id, set())

    def get_all_monitored_symbols(self) -> Set[str]:
        result: Set[str] = set()
        for syms in self.subscribers.values():
            result.update(syms)
        # Aggiunge i coin degli ordini condizionali (stoploss/takeprofit)
        for conds in self.conditional_orders.values():
            for o in conds.values():
                result.add(o['coin'])
        return result


monitor = HyperliquidPriceMonitor()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def market_label(market_type: str, dex_name: Optional[str]) -> Tuple[str, str]:
    if market_type == 'PERP':
        return ("🔥", f"PERP ({dex_name})") if dex_name else ("⚡", "PERP")
    return "💎", "SPOT"


# ---------------------------------------------------------------------------
# Comandi Telegram
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dex_list = ', '.join(d.upper() for d in SUPPORTED_DEXS)
    msg = (
        "🤖 *Bot Hyperliquid Price Monitor*\n\n"
        "Monitora:\n"
        "⚡ Perpetuals standard\n"
        f"🔥 Perpetuals DEX ({dex_list})\n"
        "💎 Spot tokens\n\n"
        "Comandi:\n"
        "/price SYMBOL — prezzo corrente\n"
        "/subscribe SYMBOL — inizia a monitorare\n"
        "/unsubscribe SYMBOL — smetti di monitorare\n"
        "/list — le tue sottoscrizioni\n"
        "/threshold [X|reset] — soglia alert personale\n"
        "/pricespike [N|status] — spike improvvisi poll-to-poll\n"
        "/symbols — simboli disponibili\n"
        "/setaddress 0x... — imposta wallet address\n"
        "/setkey 0x...     — imposta chiave API (cifrata)\n"
        "/walletinfo       — stato credenziali\n"
        "/positions        — posizioni aperte su Hyperliquid\n"
        "/trackpositions   — attiva/disattiva tracking automatico posizioni\n"
        "/setleverage SYM LEV [cross] — imposta leva su un simbolo\n"
        "/long SYM IMPORTO          — apre long per $IMPORTO\n"
        "/short SYM IMPORTO         — apre short per $IMPORTO\n"
        "/close SYM [%|importo]     — chiude posizione (tutto/parziale)\n"
        "/confirm                   — conferma ordine pendente\n"
        "/cancelorder               — annulla ordine pendente\n"
        "/stoploss SYM PX [sz|%]    — imposta stop loss\n"
        "/takeprofit SYM PX [sz|%]  — imposta take profit\n"
        "/orders                    — ordini condizionali attivi\n"
        "/cancelcond ID|all         — cancella ordine condizionale\n"
        "/chart SYM [N]            — grafico candele 15m\n"
        "/stats — statistiche bot\n"
        "/help — questo messaggio\n"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /price SYMBOL  (es: /price BTC, /price MSTR, /price PURR)")
        return

    sym = context.args[0].upper()
    await update.message.reply_text(f"🔍 Recupero prezzo di {sym}...")

    result = await monitor.get_price(sym)
    if not result:
        await update.message.reply_text(
            f"❌ *{sym}* non trovato. Usa /symbols per i simboli disponibili.",
            parse_mode='Markdown'
        )
        return

    price_val, mtype, dex = result
    emoji, label = market_label(mtype, dex)

    # Delta rispetto al prezzo del primo subscribe (se sottoscritto)
    sub_msg = ""
    base = monitor.subscribe_base_prices.get(sym)
    if base:
        pct_s = (price_val - base) / base * 100
        arrow_s = "📈" if pct_s > 0 else "📉" if pct_s < 0 else "➡️"
        sub_msg = f"\n{arrow_s} Da subscribe ({base:,.6f}): {pct_s:+.2f}%"

    # Delta rispetto al prezzo del giorno prima (dal CSV storico)
    yesterday_msg = ""
    yesterday = get_yesterday_price(sym)
    if yesterday:
        pct_d = (price_val - yesterday) / yesterday * 100
        arrow_d = "📈" if pct_d > 0 else "📉" if pct_d < 0 else "➡️"
        yesterday_msg = f"\n{arrow_d} Ieri ({yesterday:,.6f}): {pct_d:+.2f}%"

    await update.message.reply_text(
        f"{emoji} *{sym}* ({label})\n"
        f"Prezzo: ${price_val:,.6f}"
        f"{sub_msg}"
        f"{yesterday_msg}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}",
        parse_mode='Markdown'
    )

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /subscribe SYMBOL")
        return

    sym = context.args[0].upper()
    chat_id = update.effective_chat.id

    await update.message.reply_text(f"🔍 Verifico {sym}...")
    result = await monitor.get_price(sym)

    if not result:
        await update.message.reply_text(
            f"❌ *{sym}* non trovato. Usa /symbols per i simboli disponibili.",
            parse_mode='Markdown'
        )
        return

    price_val, mtype, dex = result
    emoji, label = market_label(mtype, dex)

    monitor.add_subscriber(chat_id, sym)
    # Salva il prezzo base al momento del subscribe come riferimento per gli alert
    monitor.last_prices[sym]       = price_val
    monitor.alert_base_prices[sym] = price_val
    # Salva il prezzo originale del subscribe (non viene mai aggiornato)
    monitor.subscribe_base_prices.setdefault(sym, price_val)

    await update.message.reply_text(
        f"✅ Monitoro *{sym}* per te!\n"
        f"{emoji} Tipo: {label}\n"
        f"Prezzo attuale: ${price_val:,.6f}\n"
        f"Soglia notifiche: ±{monitor.get_threshold(chat_id)}%",
        parse_mode='Markdown'
    )

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /unsubscribe SYMBOL")
        return

    sym = context.args[0].upper()
    chat_id = update.effective_chat.id

    if sym not in monitor.get_subscriptions(chat_id):
        await update.message.reply_text(
            f"❌ Non stai monitorando *{sym}*. Usa /list.",
            parse_mode='Markdown'
        )
        return

    monitor.remove_subscriber(chat_id, sym)
    await update.message.reply_text(f"✅ Non monitoro più *{sym}*", parse_mode='Markdown')

async def pricespike(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /pricespike        — attiva/disattiva il monitoraggio spike
    /pricespike status — mostra stato e soglia
    /pricespike N      — imposta soglia personalizzata (es: /pricespike 2.0)
    """
    chat_id = update.effective_chat.id
    arg = context.args[0].lower() if context.args else ''

    if arg == 'status':
        active = chat_id in monitor.spike_subscribers
        stato  = "✅ ATTIVO" if active else "⏸ NON ATTIVO"
        thresh = monitor.get_spike_threshold(chat_id)
        await update.message.reply_text(
            f"⚡ *PriceSpike* — {stato}\n\n"
            f"Soglia: ±{thresh}% per poll ({POLL_INTERVAL}s)\n"
            f"Simboli monitorati: tutti XYZ + {', '.join(SPIKE_EXTRA_SYMBOLS)}\n\n"
            f"Usa /pricespike per attivare/disattivare.",
            parse_mode='Markdown'
        )
        return

    if arg and arg not in ('on', 'off'):
        # Prova a interpretarlo come soglia numerica
        try:
            value = float(arg.replace(',', '.'))
            if value <= 0 or value > 100:
                raise ValueError
            monitor.set_spike_threshold(chat_id, value)
            await update.message.reply_text(
                f"✅ Soglia spike aggiornata a ±{value}% per poll\n"
                f"Usa /pricespike per attivare/disattivare."
            )
            return
        except ValueError:
            await update.message.reply_text(
                "❌ Argomento non valido.\n"
                "/pricespike — attiva/disattiva\n"
                "/pricespike 2.0 — imposta soglia\n"
                "/pricespike status — mostra stato"
            )
            return

    # On / off / toggle
    if arg == 'off':
        monitor.spike_subscribers.discard(chat_id)
        logger.info(f"Chat {chat_id} disattivato pricespike")
        await update.message.reply_text("⏸ *PriceSpike disattivato.*", parse_mode='Markdown')
        return

    if arg == 'on' or chat_id not in monitor.spike_subscribers:
        monitor.spike_subscribers.add(chat_id)
        logger.info(f"Chat {chat_id} attivato pricespike")
        thresh = monitor.get_spike_threshold(chat_id)
        await update.message.reply_text(
            f"⚡ *PriceSpike attivato!*\n\n"
            f"Ti avviso se un prezzo varia di ±{thresh}% "
            f"da un poll all'altro ({POLL_INTERVAL}s).\n"
            f"Monitorati: tutti gli XYZ + {', '.join(SPIKE_EXTRA_SYMBOLS)}\n\n"
            f"Usa /pricespike off per disattivare, /pricespike status per dettagli.",
            parse_mode='Markdown'
        )
    else:
        # già attivo e nessun argomento: informa l'utente
        thresh = monitor.get_spike_threshold(chat_id)
        await update.message.reply_text(
            f"⚡ *PriceSpike già attivo* (soglia ±{thresh}%)\n"
            f"Usa /pricespike off per disattivare.",
            parse_mode='Markdown'
        )

async def threshold_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not context.args:
        current = monitor.get_threshold(chat_id)
        is_custom = chat_id in monitor.user_thresholds
        src = "personalizzata" if is_custom else "globale (default)"
        await update.message.reply_text(
            f"📊 Soglia attuale: ±{current}% ({src})\n\n"
            f"Per cambiarla: /threshold 1.5\n"
            f"Per ripristinare il default: /threshold reset"
        )
        return

    arg = context.args[0].lower()

    if arg == 'reset':
        monitor.user_thresholds.pop(chat_id, None)
        await update.message.reply_text(
            f"✅ Soglia ripristinata al default globale: ±{PRICE_CHANGE_THRESHOLD}%"
        )
        return

    try:
        value = float(arg.replace(',', '.'))
        if value <= 0 or value > 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Valore non valido. Usa un numero positivo, es: /threshold 1.5\n"
            "Per ripristinare il default: /threshold reset"
        )
        return

    monitor.set_threshold(chat_id, value)
    await update.message.reply_text(
        f"✅ Soglia aggiornata a ±{value}%\n"
        f"Gli alert scatteranno quando il prezzo varia di più del {value}% dal riferimento."
    )

async def list_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs = monitor.get_subscriptions(chat_id)

    tracks = monitor.position_tracks.get(chat_id, {})

    if not subs and not tracks:
        await update.message.reply_text(
            "📋 Nessuna sottoscrizione attiva e nessuna posizione in tracking.\n"
            "Usa /subscribe SYMBOL per monitorare un simbolo.\n"
            "Usa /setaddress per attivare il position tracking automatico."
        )
        return

    # Fetch prezzi correnti in batch
    all_prices = await monitor.fetch_all_prices()
    threshold  = monitor.get_threshold(chat_id)
    msg        = ""

    # --- Sezione position tracking ---
    if tracks:
        msg += f"📊 *Position Tracking* (soglia: ±{threshold}%):\n\n"
        for coin, track in sorted(tracks.items()):
            price_info    = all_prices.get(coin)
            current_price = price_info[0] if price_info else None
            entry         = track['entry_px']
            base          = track['alert_base']
            is_long       = track['is_long']
            liq           = track['liq_px']
            # PnL real-time e % su margine iniziale calcolato da entry
            cp          = current_price or entry
            pnl_rt      = (cp - entry) * track['size']
            pnl_s       = "+" if pnl_rt >= 0 else ""
            try:    _lev = float(str(track.get('leverage',1)).replace('x',''))
            except: _lev = 1
            _margin_i   = abs(entry * track['size'] / _lev) if _lev else track['margin'] or 1
            pnl_pct     = pnl_rt / _margin_i * 100 if _margin_i else 0
            fmt           = lambda x: f"{x:,.5g}"

            arrow_dir = "📈" if is_long else "📉"
            dir_s     = "LONG" if is_long else "SHORT"

            # Formatter prezzi: decimali adattivi (2 per grandi, 4 per piccoli)
            def fmt(x):
                if x == 0: return "0"
                if x >= 1000: return f"{x:,.2f}"
                if x >= 1:    return f"{x:,.3f}"
                return f"{x:,.5f}"

            if current_price and entry:
                pct_from_entry = (current_price - entry) / entry * 100
                arrow_p = "📈" if pct_from_entry > 0 else "📉" if pct_from_entry < 0 else "➡️"
                pct_str = f"  {arrow_p} Da entry: {pct_from_entry:+.2f}%  (rif alert: {fmt(base)})\n"
            else:
                pct_str = ""

            liq_dist = abs((liq - (current_price or entry)) / (current_price or entry) * 100) if liq else 0

            msg += (
                f"{arrow_dir} *{coin}* {dir_s} _{track['dex']}_ {track['leverage']}x\n"
                f"  Entry: {fmt(entry)}  Ora: {fmt(current_price) if current_price else '—'}  Size: {abs(track['size'])}\n"
                f"  PnL: {pnl_s}${pnl_rt:.2f} ({pnl_s}{pnl_pct:.1f}%)\n"
                f"{pct_str}"
                f"  Liq: {fmt(liq)} (dist: {liq_dist:.1f}%)\n\n"
            )

    # --- Sezione subscribe manuali ---
    if subs:
        msg += f"📋 *Subscribe manuali* (soglia: ±{threshold}%):\n\n"
        for sym in sorted(subs):
            base         = monitor.alert_base_prices.get(sym)
            current_info = all_prices.get(sym)
            current      = current_info[0] if current_info else monitor.last_prices.get(sym)

            if base and current:
                pct   = (current - base) / base * 100
                arrow = "📈" if pct > 0 else "📉" if pct < 0 else "➡️"
                msg  += (
                    f"• *{sym}*\n"
                    f"  Riferimento: ${base:,.6f}\n"
                    f"  Attuale:     ${current:,.6f}  {arrow} {pct:+.2f}%\n\n"
                )
            elif current:
                msg += f"• *{sym}* — ${current:,.6f}\n\n"
            else:
                msg += f"• *{sym}* — prezzo non ancora disponibile\n\n"

    await update.message.reply_text(msg.strip(), parse_mode='Markdown')

async def symbols_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /symbols          — lista tutti i simboli
    /symbols QUERY    — cerca simboli che contengono QUERY (es. /symbols sil)
    """
    query    = context.args[0].upper() if context.args else ''
    await update.message.reply_text("🔍 Recupero simboli...")
    all_syms  = await monitor.get_all_symbols()

    perps     = all_syms.get('perps', [])
    spots     = all_syms.get('spot', [])
    dex_perps = all_syms.get('dex_perps', {})

    # Se c'è una query, filtra e mostra solo i match
    if query:
        matches = []
        for s in perps:
            if query in s.upper():
                matches.append(f"⚡ {s} (PERP)")
        for dex_code, dex_syms in dex_perps.items():
            for s in dex_syms:
                if query in s.upper():
                    matches.append(f"🔥 {s} ({dex_code.upper()})")
        for s in spots:
            clean = s.replace('/USDC', '')
            if query in clean.upper():
                matches.append(f"💎 {clean} (SPOT)")
        if matches:
            msg = f"🔍 *Risultati per '{query}'*:\n\n" + "\n".join(matches)
        else:
            msg = f"❌ Nessun simbolo trovato per '{query}'"
        await update.message.reply_text(msg, parse_mode='Markdown')
        return

    # Lista completa con troncamento
    msg = "📊 *Simboli disponibili*\n\n"

    if perps:
        msg += f"⚡ *PERPETUALS* ({len(perps)} totali):\n"
        msg += ", ".join(perps[:MAX_SYMBOLS_DISPLAY])
        if len(perps) > MAX_SYMBOLS_DISPLAY:
            msg += f"\n... e altri {len(perps) - MAX_SYMBOLS_DISPLAY}  (usa /symbols QUERY per cercare)"
        msg += "\n\n"

    for dex_code, dex_syms in dex_perps.items():
        msg += f"🔥 *{dex_code.upper()}* ({len(dex_syms)} totali):\n"
        msg += ", ".join(dex_syms[:MAX_SYMBOLS_DISPLAY])
        if len(dex_syms) > MAX_SYMBOLS_DISPLAY:
            msg += f"\n... e altri {len(dex_syms) - MAX_SYMBOLS_DISPLAY}  (usa /symbols QUERY per cercare)"
        msg += "\n\n"

    if spots:
        clean = [s.replace('/USDC', '') for s in spots]
        msg += f"💎 *SPOT* ({len(spots)} totali):\n"
        msg += ", ".join(clean[:MAX_SYMBOLS_DISPLAY])
        if len(spots) > MAX_SYMBOLS_DISPLAY:
            msg += f"\n... e altri {len(spots) - MAX_SYMBOLS_DISPLAY}"
        msg += "\n\n"

    total = len(perps) + len(spots) + sum(len(v) for v in dex_perps.values())
    msg += f"*Totale: {total} simboli*\nUsa /symbols QUERY per cercare, es: /symbols sil"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    monitored = monitor.get_all_monitored_symbols()
    # Usa parse_mode=None per evitare problemi con path che contengono caratteri speciali Markdown
    msg = (
        "📊 Statistiche Bot\n\n"
        f"👥 Utenti attivi: {len(monitor.subscribers)}\n"
        f"📈 Simboli monitorati: {len(monitored)} — {', '.join(sorted(monitored)) if monitored else 'nessuno'}\n"
        f"💾 Prezzi in cache: {len(monitor.last_prices)}\n"
        f"⏱️ Intervallo polling: {POLL_INTERVAL}s\n"
        f"📊 Soglia notifiche: ±{monitor.get_threshold(update.effective_chat.id)}% (globale: ±{PRICE_CHANGE_THRESHOLD}%)\n"
        f"🔗 DEX supportati: {', '.join(d.upper() for d in SUPPORTED_DEXS)}\n"
        f"⚡ PriceSpike: {'attivo' if update.effective_chat.id in monitor.spike_subscribers else 'non attivo'} (soglia ±{monitor.get_spike_threshold(update.effective_chat.id)}% per poll)\n"
        f"📁 Log: {LOG_FILE}\n"
        f"⚙️ Config: {env_file}\n"
    )
    await update.message.reply_text(msg)

# =============================================================================
# PATCH — aggiungi /chart a hyperliquid_bot.py
#
# STEP 1 — Aggiungi questi import in cima al file, dopo gli import esistenti:
#
#   import tempfile
#   import matplotlib
#   matplotlib.use('Agg')   # backend non-interattivo, obbligatorio su server
#   import matplotlib.pyplot as plt
#   import candle_chart as cc
#
# STEP 2 — Incolla la funzione chart_command qui sotto nel bot,
#           vicino agli altri comandi (es. dopo stats())
#
# STEP 3 — Registra il handler in main(), dopo la riga cancelcond:
#
#   app.add_handler(CommandHandler("chart", chart_command))
#
# STEP 4 — Aggiungi il comando alla lista in start() / help_command():
#
#   "/chart SYM [N]            — grafico candele 15m (es: /chart GOLD 96)\n"
#
# =============================================================================


async def chart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /chart SIMBOLO [BARRE]
    Genera e invia un grafico candlestick 15m del simbolo richiesto.

    Esempi:
        /chart GOLD          — ultime 120 candele (~30h)
        /chart SILVER 96     — ultime 96 candele (~24h)
        /chart BTC 200
    """
    chat_id = update.effective_chat.id

    # ── Validazione argomenti ────────────────────────────────────────────
    if not context.args:
        await update.message.reply_text(
            "📊 *Chart candlestick 15m*\n\n"
            "Uso: `/chart SIMBOLO [BARRE]`\n\n"
            "Esempi:\n"
            "  `/chart GOLD`       — ultime 120 candele (~30h)\n"
            "  `/chart SILVER 96`  — ultime 96 candele (~24h)\n"
            "  `/chart BTC 200`",
            parse_mode='Markdown'
        )
        return

    symbol = context.args[0].upper()

    bars = 120
    if len(context.args) > 1:
        try:
            bars = int(context.args[1])
            if bars < 10 or bars > 500:
                await update.message.reply_text("❌ Numero barre deve essere tra 10 e 500.")
                return
        except ValueError:
            await update.message.reply_text("❌ Numero barre non valido.")
            return

    # ── Controlla che il CSV esista ──────────────────────────────────────
    try:
        csv_path = cc.find_csv(symbol, Path(CANDLES_DIR))
    except FileNotFoundError:
        await update.message.reply_text(
            f"❌ Nessuna candela disponibile per *{symbol}*.\n"
            f"Il simbolo deve essere monitorato dal bot (presente in `data/candles/`).",
            parse_mode='Markdown'
        )
        return

    # ── Messaggio di attesa ──────────────────────────────────────────────
    wait_msg = await update.message.reply_text(f"⏳ Generazione grafico *{symbol}*...",
                                                parse_mode='Markdown')

    tmp_path = None
    try:
        # ── Carica dati e genera grafico ─────────────────────────────────
        data = cc.load_csv(csv_path, bars)
        n    = len(data['closes'])

        if n < 20:
            await wait_msg.edit_text(
                f"❌ Dati insufficienti per *{symbol}* ({n} candele disponibili, minimo 20).",
                parse_mode='Markdown'
            )
            return

        fig = cc.plot_chart(data, symbol)

        # ── Salva in file temporaneo e invia ─────────────────────────────
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False, prefix=f'chart_{symbol}_') as tmp:
            tmp_path = tmp.name

        fig.savefig(tmp_path, dpi=120, bbox_inches='tight', facecolor='#0d1117')
        plt.close(fig)

        # Caption con info essenziali
        last_close = data['closes'][-1]
        change     = data['closes'][-1] - data['closes'][-2] if n > 1 else 0
        change_pct = change / data['closes'][-2] * 100 if n > 1 else 0
        arrow      = '▲' if change >= 0 else '▼'
        caption = (
            f"📊 *{symbol}* — 15m  |  {n} candele\n"
            f"C: `{last_close:.2f}`  {arrow} `{abs(change):.2f}` (`{abs(change_pct):.2f}%`)\n"
            f"Da: {data['dates'][0][:16]}  →  {data['dates'][-1][:16]}"
        )

        await wait_msg.delete()
        await update.message.reply_photo(
            photo=open(tmp_path, 'rb'),
            caption=caption,
            parse_mode='Markdown'
        )
        logger.info(f"Chart inviato: {symbol} {n} candele → chat {chat_id}")

    except Exception as e:
        logger.error(f"Errore chart {symbol} chat {chat_id}: {e}", exc_info=True)
        try:
            await wait_msg.edit_text(
                f"❌ Errore nella generazione del grafico per *{symbol}*:\n`{e}`",
                parse_mode='Markdown'
            )
        except Exception:
            pass

    finally:
        # ── Pulizia file temporaneo ───────────────────────────────────────
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# Snapshot giornaliero prezzi
# ---------------------------------------------------------------------------

def get_yesterday_price(sym: str) -> Optional[float]:
    """Legge il prezzo del giorno precedente dal CSV dello storico."""
    csv_path = PRICES_DIR / f"{sym}.csv"
    if not csv_path.exists():
        return None
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            rows = [r for r in csv.reader(f) if r and r[0] != 'date']
        # Cerca il giorno precedente: l'ultima riga con data < oggi
        today = date.today().isoformat()
        past = [r for r in rows if r[0] < today]
        if past:
            return float(past[-1][1])
    except Exception:
        pass
    return None

def save_daily_snapshot(prices: Dict[str, Tuple[float, str, Optional[str]]]) -> int:
    """
    Scrive la quotazione odierna di ogni simbolo nel proprio CSV.
    Formato: data/prices/BTC.csv  con colonne date,price,type,dex
    Salta il simbolo se la data di oggi è già presente nel file.
    Ritorna il numero di simboli scritti.
    """
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    today     = date.today().isoformat()   # es: 2026-01-15
    written   = 0

    for sym, (price, mtype, dex) in prices.items():
        csv_path = PRICES_DIR / f"{sym}.csv"

        # Controlla se oggi è già stato scritto
        if csv_path.exists():
            try:
                with open(csv_path, 'r', encoding='utf-8') as f:
                    last_line = f.readlines()[-1].strip()
                if last_line.startswith(today):
                    continue   # già registrato oggi
            except Exception:
                pass  # file vuoto o corrotto: riscrivi comunque

        # Scrivi (o crea) il CSV
        is_new = not csv_path.exists()
        try:
            with open(csv_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                if is_new:
                    writer.writerow(['date', 'price', 'type', 'dex'])
                writer.writerow([today, price, mtype, dex or ''])
            written += 1
        except Exception as e:
            logger.error(f"Errore scrittura snapshot {sym}: {e}")

    return written


# ---------------------------------------------------------------------------
# Candele 15 minuti
# ---------------------------------------------------------------------------

async def fetch_candles_15m(symbol: str, dex: str = '') -> list:
    """
    Fetcha le ultime N candele a 15 minuti per un simbolo da Hyperliquid.
    Ritorna lista di dict: {timestamp, open, high, low, close, volume}
    """
    import aiohttp
    # Hyperliquid vuole il nome interno: 'xyz:GOLD' per i dex, 'GOLD' per perp standard
    internal_name = f"{dex}:{symbol}" if dex else symbol

    # Calcola window: ultime 2 candele (coprono l'intervallo corrente + precedente)
    import time
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - 90 * 60 * 1000  # 90 minuti fa = 6 candele da 15m

    payload = {
        "type":       "candleSnapshot",
        "req": {
            "coin":       internal_name,
            "interval":   "15m",
            "startTime":  start_ms,
            "endTime":    now_ms,
        }
    }
    if dex:
        payload["dex"] = dex

    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            'https://api.hyperliquid.xyz/info',
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            data = await r.json()

    if not isinstance(data, list) or not data:
        return []

    result = []
    for c in data:
        try:
            result.append({
                'timestamp': datetime.utcfromtimestamp(c['t'] / 1000).strftime('%Y-%m-%d %H:%M:%S'),
                'open':      float(c['o']),
                'high':      float(c['h']),
                'low':       float(c['l']),
                'close':     float(c['c']),
                'volume':    float(c['v']),
            })
        except Exception:
            continue
    return result


def save_candles(symbol: str, candles: list) -> int:
    """
    Salva le candele in data/candles/SIMBOLO/SIMBOLO_15m.csv
    Deduplicazione + merge ordinato cronologicamente (no append cieco).
    Ritorna il numero di righe nuove scritte.
    """
    if not candles:
        return 0

    sym_dir  = CANDLES_DIR / symbol
    sym_dir.mkdir(parents=True, exist_ok=True)
    csv_path = sym_dir / f"{symbol}_15m.csv"

    # Legge righe esistenti
    existing_rows = []
    existing_ts   = set()
    if csv_path.exists():
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                next(reader, None)  # salta header
                for row in reader:
                    if row:
                        existing_rows.append(row)
                        existing_ts.add(row[0])
        except Exception:
            pass

    # Aggiunge solo le candele con timestamp nuovo
    written   = 0
    new_rows  = []
    for c in candles:
        if c['timestamp'] not in existing_ts:
            new_rows.append([
                c['timestamp'], c['open'], c['high'],
                c['low'], c['close'], c['volume']
            ])
            existing_ts.add(c['timestamp'])
            written += 1

    if not written:
        return 0

    # Merge + ordinamento cronologico + riscrittura completa
    all_rows = existing_rows + new_rows
    all_rows.sort(key=lambda r: r[0])
    try:
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            writer.writerows(all_rows)
    except Exception as e:
        logger.error(f"Errore scrittura candele {symbol}: {e}")
        return 0

    return written


async def candle_task(application: Application):
    """
    Task che ogni 15 minuti fetcha e salva le candele per tutti i simboli
    monitorati (stessi del daily snapshot: xyz + SPIKE_EXTRA_SYMBOLS).
    """
    logger.info(f"🕯 Task candele avviato (intervallo: {CANDLES_INTERVAL_SECS}s)")
    CANDLES_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            # Fetch prezzi per ricavare l'universo simboli + info dex
            all_prices = await monitor.fetch_all_prices()

            xyz_syms      = {sym: v for sym, (_, _, dex) in all_prices.items()
                             if dex and dex.upper() == 'XYZ'
                             for sym, v in [(sym, all_prices[sym])]}
            # Ricostruisce correttamente
            xyz_set       = {sym for sym, (_, _, dex) in all_prices.items() if dex and dex.upper() == 'XYZ'}
            extra_set     = set(SPIKE_EXTRA_SYMBOLS)
            all_symbols   = xyz_set | extra_set

            total_written = 0
            errors        = 0

            for sym in sorted(all_symbols):
                info = all_prices.get(sym)
                dex  = info[2].lower() if info and info[2] else ''
                try:
                    candles = await fetch_candles_15m(sym, dex)
                    n       = save_candles(sym, candles)
                    total_written += n
                    if n:
                        logger.debug(f"Candele {sym}: {n} nuove righe salvate")
                except Exception as e:
                    errors += 1
                    logger.warning(f"Errore fetch candele {sym}: {e}")
                # Piccola pausa tra simboli per non martellare l'API
                await asyncio.sleep(0.3)

            logger.info(
                f"🕯 Candele 15m: {total_written} righe scritte su {len(all_symbols)} simboli"
                + (f" ({errors} errori)" if errors else "")
            )

        except Exception as e:
            logger.error(f"Errore nel candle task: {e}", exc_info=True)

        await asyncio.sleep(CANDLES_INTERVAL_SECS)


# ---------------------------------------------------------------------------
# Position tracking
# ---------------------------------------------------------------------------

async def trackpositions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /trackpositions        — mostra stato
    /trackpositions on     — attiva (default)
    /trackpositions off    — disattiva
    """
    chat_id = update.effective_chat.id
    arg     = context.args[0].lower() if context.args else ''

    if not wallet_store.get_address(chat_id):
        await update.message.reply_text(
            "❌ Imposta prima il tuo address con /setaddress 0x..."
        )
        return

    if arg == 'off':
        monitor.tracking_enabled.discard(chat_id)
        monitor.position_tracks.pop(chat_id, None)
        logger.info(f"Chat {chat_id} position tracking disattivato")
        await update.message.reply_text("⏸ *Position tracking disattivato.*", parse_mode='Markdown')
        return

    if arg == 'on' or not arg:
        monitor.tracking_enabled.add(chat_id)
        logger.info(f"Chat {chat_id} position tracking attivato")
        n = len(monitor.position_tracks.get(chat_id, {}))
        await update.message.reply_text(
            f"✅ *Position tracking attivo*\n\n"
            f"Le tue posizioni aperte vengono monitorate automaticamente.\n"
            f"Aggiornamento ogni {POSITION_TRACK_INTERVAL//60} minuti.\n"
            f"Posizioni in tracking: {n}\n\n"
            f"Usa /trackpositions off per disattivare.",
            parse_mode='Markdown'
        )
        return

    # status
    active = chat_id in monitor.tracking_enabled
    tracks = monitor.position_tracks.get(chat_id, {})
    await update.message.reply_text(
        f"📊 *Position Tracking* — {'✅ attivo' if active else '⏸ non attivo'}\n"
        f"Posizioni tracciate: {len(tracks)}\n"
        f"Aggiornamento: ogni {POSITION_TRACK_INTERVAL//60} min\n\n"
        f"/trackpositions on/off",
        parse_mode='Markdown'
    )


# ---------------------------------------------------------------------------
# Ordini condizionali — stoploss / takeprofit
# ---------------------------------------------------------------------------

def _next_cond_id() -> str:
    monitor._cond_id_counter += 1
    return f"C{monitor._cond_id_counter:04d}"


def _cond_label(order: dict) -> str:
    is_sl   = order['type'] == 'stoploss'
    is_long = order.get('is_long', True)  # default long per ordini vecchi senza campo
    t       = "🛑 SL" if is_sl else "🎯 TP"
    # Freccia corretta per direzione + tipo:
    # LONG:  SL ≤ trigger, TP ≥ trigger
    # SHORT: SL ≥ trigger, TP ≤ trigger
    if is_sl:
        arrow = "≤" if is_long else "≥"
    else:
        arrow = "≥" if is_long else "≤"
    size_s = ""
    if order.get('pct'):
        size_s = f" {order['pct']*100:.0f}%"
    elif order.get('usd'):
        size_s = f" ${order['usd']:,.0f}"
    else:
        size_s = " tutto"
    coin  = order['coin']
    dex_s = f" _{order['dex'].upper()}_" if order['dex'] else " PERP"
    dir_s = "L" if is_long else "S"
    return f"{t}({dir_s}) *{coin}*{dex_s} {arrow} ${_fmt(order['trigger_px'])}{size_s}"


async def stoploss_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /stoploss SIMBOLO PREZZO [importo|%]
    Es: /stoploss GOLD 5100
        /stoploss GOLD 5100 200     — chiudi $200
        /stoploss GOLD 5100 50%     — chiudi 50%
    Triggera quando prezzo <= PREZZO.
    """
    await _set_conditional(update, context, 'stoploss')


async def takeprofit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /takeprofit SIMBOLO PREZZO [importo|%]
    Es: /takeprofit GOLD 5600
        /takeprofit NFLX 155 50%
    Triggera quando prezzo >= PREZZO.
    """
    await _set_conditional(update, context, 'takeprofit')


async def _set_conditional(update: Update, context: ContextTypes.DEFAULT_TYPE, ctype: str):
    chat_id = update.effective_chat.id
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    label = "Stop Loss" if ctype == 'stoploss' else "Take Profit"
    arrow = "≤ (scende sotto)" if ctype == 'stoploss' else "≥ (sale sopra)"

    if len(context.args) < 2:
        await update.message.reply_text(
            f"📌 *{label}*\n\n"
            f"Uso: /{ctype.replace('profit','profit')} SIMBOLO PREZZO [importo|%]\n\n"
            f"Triggera quando prezzo {arrow}\n\n"
            f"Esempi:\n"
            f"  /{ctype} GOLD 5100         — chiudi tutto\n"
            f"  /{ctype} GOLD 5100 200     — chiudi $200\n"
            f"  /{ctype} GOLD 5100 50%     — chiudi 50%",
            parse_mode='Markdown'
        )
        return

    symbol = context.args[0].upper()
    try:
        trigger_px = float(context.args[1].replace(',', '.'))
    except ValueError:
        await update.message.reply_text("❌ Prezzo non valido.")
        return

    usd_amount = None
    pct        = None
    if len(context.args) > 2:
        arg = context.args[2]
        if arg.endswith('%'):
            try:    pct = float(arg[:-1]) / 100
            except: await update.message.reply_text("❌ Percentuale non valida."); return
        else:
            try:    usd_amount = float(arg.replace(',', '.'))
            except: await update.message.reply_text("❌ Importo non valido."); return

    # Verifica posizione aperta
    track = monitor.position_tracks.get(chat_id, {}).get(symbol)
    if not track:
        await update.message.reply_text(
            f"❌ Nessuna posizione aperta su *{symbol}*.\n"
            f"Apri prima una posizione con /long o /short.",
            parse_mode='Markdown'
        )
        return

    is_long    = track['is_long']
    current_px = monitor.last_prices.get(symbol, track['entry_px'])

    # Validazione direzione trigger
    if ctype == 'stoploss':
        if is_long and trigger_px >= current_px:
            await update.message.reply_text(
                f"❌ Stop Loss LONG deve essere *sotto* il prezzo attuale\n"
                f"Attuale: ${_fmt(current_px)} — inserisci un valore inferiore",
                parse_mode='Markdown'); return
        if not is_long and trigger_px <= current_px:
            await update.message.reply_text(
                f"❌ Stop Loss SHORT deve essere *sopra* il prezzo attuale\n"
                f"Attuale: ${_fmt(current_px)} — inserisci un valore superiore",
                parse_mode='Markdown'); return
    else:  # takeprofit
        if is_long and trigger_px <= current_px:
            await update.message.reply_text(
                f"❌ Take Profit LONG deve essere *sopra* il prezzo attuale\n"
                f"Attuale: ${_fmt(current_px)} — inserisci un valore superiore",
                parse_mode='Markdown'); return
        if not is_long and trigger_px >= current_px:
            await update.message.reply_text(
                f"❌ Take Profit SHORT deve essere *sotto* il prezzo attuale\n"
                f"Attuale: ${_fmt(current_px)} — inserisci un valore inferiore",
                parse_mode='Markdown'); return

    dex = await _detect_dex(symbol)

    # Un solo ordine per tipo per simbolo — sostituisce il precedente
    conds   = monitor.conditional_orders.setdefault(chat_id, {})
    old_oid = next((o for o, v in conds.items()
                    if v['coin'] == symbol and v['type'] == ctype), None)
    if old_oid:
        conds.pop(old_oid)

    oid   = _next_cond_id()
    order = {
        'coin':             symbol,
        'dex':              dex,
        'type':             ctype,
        'trigger_px':       trigger_px,
        'usd':              usd_amount,
        'pct':              pct,
        'is_long':          is_long,   # direzione posizione al momento della creazione
        'created_at':       datetime.now(),
        'snoozed_until':    None,
        'alert_message_id': None,
        'last_notify_ts':   None,
        'tp_peak_price':    None,
    }
    conds[oid] = order
    monitor.save_conditional_orders(chat_id)

    size_s    = ""
    if pct:          size_s = f" ({pct*100:.0f}%)"
    elif usd_amount: size_s = f" (${usd_amount:,.0f})"
    else:            size_s = " (tutto)"
    market_s = f"_{dex.upper()}_" if dex else "PERP"
    dir_s    = "LONG" if is_long else "SHORT"
    upd_note = f" _(sostituisce {old_oid})_" if old_oid else ""

    # Freccia e operatore corretti per direzione + tipo ordine
    if ctype == 'stoploss':
        sym_arrow = "🔽 ≤" if is_long else "🔼 ≥"
    else:  # takeprofit
        sym_arrow = "🔼 ≥" if is_long else "🔽 ≤"

    await update.message.reply_text(
        f"✅ *{label} impostato* — `{oid}`{upd_note}\n\n"
        f"{'📈' if is_long else '📉'} {dir_s} *{symbol}* {market_s}\n"
        f"{sym_arrow} ${_fmt(trigger_px)}{size_s}\n"
        f"Prezzo attuale: ${_fmt(current_px)}\n\n"
        f"Usa /orders per vedere gli ordini\n"
        f"/cancelcond {oid} per cancellare",
        parse_mode='Markdown'
    )


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista tutti gli ordini condizionali attivi."""
    chat_id = update.effective_chat.id
    conds   = monitor.conditional_orders.get(chat_id, {})
    if not conds:
        await update.message.reply_text("📋 Nessun ordine condizionale attivo.")
        return

    msg = "📋 *Ordini condizionali attivi*\n\n"
    for oid, o in conds.items():
        snooze_s = ""
        if o.get('snoozed_until') and datetime.now().timestamp() < o['snoozed_until']:
            rem = int(o['snoozed_until'] - datetime.now().timestamp())
            snooze_s = f"  💤 silenziato {rem}s"
        msg += f"`{oid}` {_cond_label(o)}{snooze_s}\n"

    msg += f"\nUsa /cancelcond ID per cancellare"
    await update.message.reply_text(msg, parse_mode='Markdown')


async def cancelcond_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /cancelcond ID   — cancella ordine condizionale
    /cancelcond all  — cancella tutti
    """
    chat_id = update.effective_chat.id
    conds   = monitor.conditional_orders.get(chat_id, {})

    if not context.args:
        await update.message.reply_text("Uso: /cancelcond ID  oppure  /cancelcond all")
        return

    arg = context.args[0].upper()
    if arg == 'ALL':
        n = len(conds)
        monitor.conditional_orders[chat_id] = {}
        monitor.save_conditional_orders(chat_id)
        await update.message.reply_text(f"🗑 Cancellati {n} ordini condizionali.")
        return

    if arg not in conds:
        await update.message.reply_text(f"❌ Ordine `{arg}` non trovato.", parse_mode='Markdown')
        return

    o = conds.pop(arg)
    monitor.save_conditional_orders(chat_id)
    await update.message.reply_text(
        f"🗑 Cancellato: {_cond_label(o)}", parse_mode='Markdown'
    )


# ---------------------------------------------------------------------------
# Helper ordini
# ---------------------------------------------------------------------------

async def _detect_dex(symbol: str) -> str:
    """Ritorna 'xyz' se il simbolo è sul dex xyz, '' altrimenti."""
    import aiohttp as _aiohttp
    try:
        async with _aiohttp.ClientSession() as sess:
            async with sess.post(
                'https://api.hyperliquid.xyz/info',
                json={'type': 'meta', 'dex': 'xyz'},
                timeout=_aiohttp.ClientTimeout(total=8)
            ) as r:
                meta = await r.json()
        names = {a.get('name','').upper().replace('XYZ:','').replace('XYZ:','')
                 for a in meta.get('universe',[])}
        return 'xyz' if symbol.upper() in names else ''
    except Exception:
        return ''

def _fmt(x):
    if x == 0: return "0"
    if x >= 1000: return f"{x:,.2f}"
    if x >= 1:    return f"{x:,.3f}"
    return f"{x:,.5f}"


# ---------------------------------------------------------------------------
# Comandi long / short / close / confirm / cancelorder
# ---------------------------------------------------------------------------

async def _order_precheck(update, chat_id):
    """Controlli comuni: autorizzazione, address, key."""
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return False
    if not wallet_store.get_address(chat_id):
        await update.message.reply_text("❌ Imposta prima /setaddress")
        return False
    if not wallet_store.get_key(chat_id):
        await update.message.reply_text("❌ Imposta prima /setkey")
        return False
    return True


async def long_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /long SIMBOLO IMPORTO_USD
    Es: /long GOLD 500   → apre long GOLD per $500
    """
    chat_id = update.effective_chat.id
    if not await _order_precheck(update, chat_id): return

    if len(context.args) < 2:
        await update.message.reply_text(
            "📈 *Long*\n\nUso: /long SIMBOLO IMPORTO\n"
            "Es: /long GOLD 500\n"
            "Es: /long BTC 1000",
            parse_mode='Markdown')
        return

    symbol = context.args[0].upper()
    try:
        usd = float(context.args[1].replace(',', '.'))
        if usd <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Importo non valido.")
        return

    await _prepare_order(update, chat_id, symbol, usd, is_buy=True)


async def short_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /short SIMBOLO IMPORTO_USD
    Es: /short NFLX 300  → apre short NFLX per $300
    """
    chat_id = update.effective_chat.id
    if not await _order_precheck(update, chat_id): return

    if len(context.args) < 2:
        await update.message.reply_text(
            "📉 *Short*\n\nUso: /short SIMBOLO IMPORTO\n"
            "Es: /short NFLX 300\n"
            "Es: /short GOLD 500",
            parse_mode='Markdown')
        return

    symbol = context.args[0].upper()
    try:
        usd = float(context.args[1].replace(',', '.'))
        if usd <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Importo non valido.")
        return

    await _prepare_order(update, chat_id, symbol, usd, is_buy=False)


async def _prepare_order(update, chat_id, symbol, usd, is_buy):
    """Mostra riepilogo e chiede conferma."""
    await update.message.reply_text("⏳ Recupero prezzo...")

    addr = wallet_store.get_address(chat_id)
    key  = wallet_store.get_key(chat_id)
    dex  = await _detect_dex(symbol)

    try:
        client = HyperliquidClient(addr, private_key=key)
        price  = await asyncio.get_event_loop().run_in_executor(
            None, lambda: client.get_current_price(symbol, dex)
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Errore prezzo: {e}")
        return

    size         = usd / price
    market_s     = f"_{dex.upper()}_" if dex else "PERP"
    direction    = "📈 LONG" if is_buy else "📉 SHORT"
    expires_at   = datetime.now().timestamp() + 30

    # Salva ordine pendente
    monitor.pending_orders[chat_id] = {
        'symbol':   symbol,
        'dex':      dex,
        'is_buy':   is_buy,
        'usd':      usd,
        'price':    price,
        'size':     size,
        'expires_at': expires_at,
    }

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Conferma", callback_data="order_confirm"),
        InlineKeyboardButton("🚫 Annulla",  callback_data="order_cancel"),
    ]])
    await update.message.reply_text(
        f"⚠️ *Conferma ordine*\n\n"
        f"{direction} *{symbol}* {market_s}\n"
        f"Importo: ${usd:,.2f}\n"
        f"Prezzo attuale: ${_fmt(price)}\n"
        f"Size stimata: {size:.4f}\n"
        f"⏰ Scade in 30 secondi",
        parse_mode='Markdown',
        reply_markup=keyboard
    )


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /close SIMBOLO          → chiude tutta la posizione
    /close SIMBOLO 50%      → chiude il 50%
    /close SIMBOLO 200      → chiude $200 di posizione
    """
    chat_id = update.effective_chat.id
    if not await _order_precheck(update, chat_id): return

    if not context.args:
        await update.message.reply_text(
            "🔴 *Close*\n\nUso:\n"
            "/close SIMBOLO          — chiude tutto\n"
            "/close SIMBOLO 50%      — chiude il 50%\n"
            "/close SIMBOLO 200      — chiude $200",
            parse_mode='Markdown')
        return

    symbol     = context.args[0].upper()
    usd_amount = None
    pct        = None

    if len(context.args) > 1:
        arg = context.args[1]
        if arg.endswith('%'):
            try:
                pct = float(arg[:-1]) / 100
            except ValueError:
                await update.message.reply_text("❌ Percentuale non valida.")
                return
        else:
            try:
                usd_amount = float(arg.replace(',', '.'))
            except ValueError:
                await update.message.reply_text("❌ Importo non valido.")
                return

    addr = wallet_store.get_address(chat_id)
    key  = wallet_store.get_key(chat_id)
    dex  = await _detect_dex(symbol)

    # Trova posizione aperta
    tracks = monitor.position_tracks.get(chat_id, {})
    pos    = tracks.get(symbol)
    if not pos:
        # Prova a leggere live
        try:
            client   = HyperliquidClient(addr, private_key=key)
            pos_list = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.get_positions(extra_dexs=SUPPORTED_DEXS)
            )
            pos = next((p for p in pos_list if p['coin'].upper() == symbol), None)
        except Exception:
            pass

    size_info = ""
    if pos:
        total_size = abs(pos['size'] if isinstance(pos, dict) and 'size' in pos else pos.get('size', 0))
        if pct is not None:
            close_size = round(total_size * pct, 4)
            size_info  = f"Size da chiudere: {close_size} ({pct*100:.0f}% di {total_size})"
        elif usd_amount is not None:
            price = pos.get('entry_px', 1)
            close_size = round(usd_amount / price, 4)
            size_info  = f"Size da chiudere: ~{close_size} (${usd_amount:.2f})"
        else:
            size_info  = f"Size da chiudere: {total_size} (tutto)"

    market_s   = f"_{dex.upper()}_" if dex else "PERP"
    expires_at = datetime.now().timestamp() + 30

    monitor.pending_orders[chat_id] = {
        'symbol':     symbol,
        'dex':        dex,
        'is_buy':     None,  # None = close
        'usd':        usd_amount,
        'pct':        pct,
        'expires_at': expires_at,
    }

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Conferma", callback_data="order_confirm"),
        InlineKeyboardButton("🚫 Annulla",  callback_data="order_cancel"),
    ]])
    await update.message.reply_text(
        f"⚠️ *Conferma chiusura*\n\n"
        f"🔴 CLOSE *{symbol}* {market_s}\n"
        f"{size_info}\n"
        f"⏰ Scade in 30 secondi",
        parse_mode='Markdown',
        reply_markup=keyboard
    )


async def confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Conferma ed esegue l'ordine pendente."""
    chat_id = update.effective_chat.id
    order   = monitor.pending_orders.get(chat_id)

    if not order:
        await update.message.reply_text("❌ Nessun ordine in attesa. Usa /long, /short o /close.")
        return

    if datetime.now().timestamp() > order['expires_at']:
        monitor.pending_orders.pop(chat_id, None)
        await update.message.reply_text("⏰ Ordine scaduto. Ripeti il comando.")
        return

    monitor.pending_orders.pop(chat_id, None)
    await update.message.reply_text("⏳ Esecuzione ordine...")

    addr   = wallet_store.get_address(chat_id)
    key    = wallet_store.get_key(chat_id)
    symbol = order['symbol']
    dex    = order['dex']
    client = HyperliquidClient(addr, private_key=key)

    try:
        if order['is_buy'] is None:
            # CLOSE
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.market_close(
                    symbol, dex,
                    usd_amount=order.get('usd'),
                    pct=order.get('pct')
                )
            )
            direction = "🔴 CLOSE"
        else:
            # LONG / SHORT
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.market_open(
                    symbol, order['is_buy'], order['usd'], dex
                )
            )
            direction = "📈 LONG" if order['is_buy'] else "📉 SHORT"

        logger.info(f"ORDER {direction} {symbol} chat {chat_id}: {result}")

        # Controlla errore
        if isinstance(result, dict) and result.get('status') == 'err':
            await update.message.reply_text(f"❌ Errore exchange: {result.get('response', result)}")
            return

        market_s = f"_{dex.upper()}_" if dex else "PERP"
        size     = result.get('_size', '?')
        price    = result.get('_price')
        usd      = result.get('_usd') or order.get('usd')

        # Riga prezzo — solo se disponibile (non per close)
        price_line = f"Prezzo: ${_fmt(price)}\n" if isinstance(price, float) else ""
        # Riga importo — solo se disponibile
        usd_line   = f"Importo: ${usd:,.2f}\n" if isinstance(usd, (int, float)) else ""

        await update.message.reply_text(
            f"✅ *Ordine eseguito*\n\n"
            f"{direction} *{symbol}* {market_s}\n"
            f"Size: {size}\n"
            f"{price_line}"
            f"{usd_line}"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Errore esecuzione ordine {symbol} chat {chat_id}: {e}")
        await update.message.reply_text(f"❌ Errore: {e}")


async def cancelorder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Annulla l'ordine pendente."""
    chat_id = update.effective_chat.id
    if monitor.pending_orders.pop(chat_id, None):
        await update.message.reply_text("🚫 Ordine annullato.")
    else:
        await update.message.reply_text("ℹ️ Nessun ordine in attesa.")


async def order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce i bottoni inline Conferma / Annulla degli ordini."""
    query   = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()  # rimuove il "loading" sul bottone

    if query.data == "order_confirm":
        # Rimuove i bottoni dal messaggio originale
        await query.edit_message_reply_markup(reply_markup=None)
        # Esegue come se l'utente avesse scritto /confirm
        order = monitor.pending_orders.get(chat_id)
        if not order:
            await query.message.reply_text("⏰ Ordine scaduto o già eseguito.")
            return
        if datetime.now().timestamp() > order['expires_at']:
            monitor.pending_orders.pop(chat_id, None)
            await query.message.reply_text("⏰ Ordine scaduto. Ripeti il comando.")
            return
        monitor.pending_orders.pop(chat_id, None)
        await query.message.reply_text("⏳ Esecuzione ordine...")

        addr   = wallet_store.get_address(chat_id)
        key    = wallet_store.get_key(chat_id)
        symbol = order['symbol']
        dex    = order['dex']
        client = HyperliquidClient(addr, private_key=key)

        try:
            if order['is_buy'] is None:
                result    = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: client.market_close(symbol, dex,
                                                      usd_amount=order.get('usd'),
                                                      pct=order.get('pct'))
                )
                direction = "🔴 CLOSE"
            else:
                result    = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: client.market_open(symbol, order['is_buy'], order['usd'], dex)
                )
                direction = "📈 LONG" if order['is_buy'] else "📉 SHORT"

            logger.info(f"ORDER (btn) {direction} {symbol} chat {chat_id}: {result}")

            if isinstance(result, dict) and result.get('status') == 'err':
                await query.message.reply_text(f"❌ Errore exchange: {result.get('response', result)}")
                return

            market_s   = f"_{dex.upper()}_" if dex else "PERP"
            size       = result.get('_size', '?')
            price      = result.get('_price')
            usd        = result.get('_usd') or order.get('usd')
            price_line = f"Prezzo: ${_fmt(price)}\n" if isinstance(price, float) else ""
            usd_line   = f"Importo: ${usd:,.2f}\n" if isinstance(usd, (int, float)) else ""

            await query.message.reply_text(
                f"✅ *Ordine eseguito*\n\n"
                f"{direction} *{symbol}* {market_s}\n"
                f"Size: {size}\n"
                f"{price_line}{usd_line}"
                f"⏰ {datetime.now().strftime('%H:%M:%S')}",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Errore esecuzione ordine (btn) {symbol} chat {chat_id}: {e}")
            await query.message.reply_text(f"❌ Errore: {e}")

    elif query.data == "order_cancel":
        monitor.pending_orders.pop(chat_id, None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("🚫 Ordine annullato.")


async def cond_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce i bottoni Esegui / Salta / Cancella degli ordini condizionali."""
    query = update.callback_query
    await query.answer()
    data  = query.data  # cond_exec_CHATID_OID | cond_snooze_... | cond_cancel_...

    parts  = data.split('_')
    action = parts[1]           # exec | snooze | cancel
    cid    = int(parts[2])
    oid    = parts[3]

    conds = monitor.conditional_orders.get(cid, {})
    order = conds.get(oid)

    await query.edit_message_reply_markup(reply_markup=None)

    if not order:
        await query.message.reply_text(f"ℹ️ Ordine `{oid}` non più attivo.", parse_mode='Markdown')
        return

    if action == 'cancel':
        conds.pop(oid, None)
        monitor.save_conditional_orders(cid)
        await query.edit_message_text(
            text=f"🗑 Ordine cancellato: {_cond_label(order)}",
            parse_mode='Markdown'
        )



    elif action == 'snooze':
        order['snoozed_until'] = datetime.now().timestamp() + CONDITIONAL_SNOOZE_SECS
        order['snoozed_until']    = datetime.now().timestamp() + CONDITIONAL_SNOOZE_SECS
        order['alert_message_id'] = None  # alla ripresa manda nuovo bip
        monitor.save_conditional_orders(cid)
        mins = CONDITIONAL_SNOOZE_SECS // 60
        await query.edit_message_text(
            text=(
                f"{'🛑 Stop Loss' if order['type'] == 'stoploss' else '🎯 Take Profit'}"
                f" — `{oid}`\n💤 Silenziato per {mins} minuti"
            ),
            parse_mode='Markdown'
        )

    elif action == 'exec':
        await query.message.reply_text("⏳ Esecuzione ordine condizionale...")
        addr   = wallet_store.get_address(cid)
        key    = wallet_store.get_key(cid)
        symbol = order['coin']
        dex    = order['dex']
        client = HyperliquidClient(addr, private_key=key)

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.market_close(
                    symbol, dex,
                    usd_amount=order.get('usd'),
                    pct=order.get('pct')
                )
            )
            logger.info(f"COND ORDER exec {oid} {symbol} chat {cid}: {result}")

            if isinstance(result, dict) and result.get('status') == 'err':
                await query.message.reply_text(f"❌ Errore exchange: {result.get('response', result)}")
                return

            # Rimuove l'ordine dopo esecuzione
            conds.pop(oid, None)
            monitor.save_conditional_orders(cid)

            size = result.get('_size', '?')
            market_s = f"_{dex.upper()}_" if dex else "PERP"
            t_label  = "🛑 Stop Loss" if order['type'] == 'stoploss' else "🎯 Take Profit"

            await query.message.reply_text(
                f"✅ *{t_label} eseguito*\n\n"
                f"🔴 CLOSE *{symbol}* {market_s}\n"
                f"Size: {size}\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')}",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Errore exec cond {oid} {symbol} chat {cid}: {e}")
            await query.message.reply_text(f"❌ Errore: {e}")


# ---------------------------------------------------------------------------
# Comando setleverage
# ---------------------------------------------------------------------------

async def setleverage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setleverage SYMBOL LEVERAGE [cross]
    Es: /setleverage NFLX 5
        /setleverage BTC 10
        /setleverage MSTR 3 cross

    Funziona su perp standard e xyz. Default: isolated.
    Richiede /setaddress e /setkey.
    """
    chat_id = update.effective_chat.id
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "📐 *Set Leverage*\n\n"
            "Uso: /setleverage SIMBOLO LEVA [xyz] [cross]\n\n"
            "Esempi:\n"
            "  /setleverage NFLX 5           — auto-rileva mercato\n"
            "  /setleverage SILVER 5 xyz     — forza xyz\n"
            "  /setleverage BTC 10           — PERP standard\n"
            "  /setleverage MSTR 3 xyz cross — xyz cross margin\n\n"
            "Il mercato viene auto-rilevato. Aggiungi 'xyz' se fallisce.",
            parse_mode='Markdown'
        )
        return

    addr = wallet_store.get_address(chat_id)
    key  = wallet_store.get_key(chat_id)
    if not addr:
        await update.message.reply_text("❌ Imposta prima /setaddress")
        return
    if not key:
        await update.message.reply_text("❌ Imposta prima /setkey per usare comandi di trading.")
        return

    symbol   = context.args[0].upper().replace('(XYZ)', '').strip()
    try:
        leverage = int(context.args[1])
        if leverage < 1 or leverage > 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Leva non valida (1-100).")
        return

    # Parsing argomenti extra: [xyz] [cross] in qualsiasi ordine
    extra_args = [a.lower() for a in context.args[2:]]
    is_cross   = 'cross' in extra_args
    force_xyz  = 'xyz' in extra_args
    margin_s   = "cross" if is_cross else "isolated"

    # Determina se è un simbolo xyz:
    # 1. Se l'utente ha passato 'xyz' esplicitamente → forza xyz
    # 2. Altrimenti interroga il meta API del dex xyz
    import aiohttp as _aiohttp
    dex      = 'xyz' if force_xyz else ''
    market_s = 'XYZ' if force_xyz else 'PERP'

    if not force_xyz:
        try:
            async with _aiohttp.ClientSession() as _sess:
                async with _sess.post(
                    'https://api.hyperliquid.xyz/info',
                    json={'type': 'meta', 'dex': 'xyz'},
                    timeout=_aiohttp.ClientTimeout(total=10)
                ) as _r:
                    _meta = await _r.json()
            _xyz_names = {a.get('name', '').upper().replace('XYZ:', '') for a in _meta.get('universe', [])}
            logger.debug(f"setleverage xyz universe: {_xyz_names}")
            if symbol.upper() in _xyz_names:
                dex      = 'xyz'
                market_s = 'XYZ'
        except Exception as _e:
            logger.warning(f"setleverage: impossibile determinare mercato per {symbol}: {_e}")

    await update.message.reply_text(f"⏳ Imposto leva {leverage}x {margin_s} su {symbol} ({market_s})...")

    try:
        client = HyperliquidClient(addr, private_key=key)
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: client.set_leverage(symbol, leverage, is_cross, dex)
        )
        logger.info(f"setleverage {symbol} {leverage}x {margin_s} ({market_s}) -> {result}")

        # Controlla se l'SDK ha restituito un errore
        if isinstance(result, dict) and result.get('status') == 'err':
            await update.message.reply_text(f"❌ Errore: {result.get('response', result)}")
        else:
            await update.message.reply_text(
                f"✅ *Leva aggiornata*\n\n"
                f"Simbolo: *{symbol}* ({market_s})\n"
                f"Leva: *{leverage}x*\n"
                f"Margine: *{margin_s}*",
                parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"Errore setleverage {symbol} chat {chat_id}: {e}")
        await update.message.reply_text(f"❌ Errore: {e}")


# ---------------------------------------------------------------------------
# Comandi wallet / API Hyperliquid
# ---------------------------------------------------------------------------

def _is_wallet_allowed(chat_id: int) -> bool:
    """True se il chat_id è autorizzato a usare i comandi wallet."""
    return not WALLET_ALLOWED_CHATS or chat_id in WALLET_ALLOWED_CHATS


async def setaddress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setaddress 0x...  — salva il tuo account address Hyperliquid (pubblico).
    Necessario per /positions e futuri comandi di trading.
    """
    chat_id = update.effective_chat.id
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    if not context.args:
        addr = wallet_store.get_address(chat_id)
        if addr:
            short = f"{addr[:6]}...{addr[-4:]}"
            await update.message.reply_text(
                f"📍 Address attuale: `{short}`\n\n"
                f"Per cambiarlo: /setaddress 0x...",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "📍 Nessun address impostato.\n"
                "Usa: /setaddress 0x7D5..."
            )
        return

    address = context.args[0].strip()
    if not address.startswith('0x') or len(address) < 20:
        await update.message.reply_text("❌ Address non valido. Deve iniziare con 0x.")
        return

    wallet_store.save_address(chat_id, address)
    short = f"{address[:6]}...{address[-4:]}"
    # Tracking on per default
    monitor.tracking_enabled.add(chat_id)
    # Cancella il messaggio con l'address per sicurezza
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.effective_chat.send_message(
        f"✅ Address salvato: `{short}`\n"
        f"🎯 Position tracking *attivato automaticamente*.\n"
        f"Riceverai notifiche sulle tue posizioni aperte.\n"
        f"Usa /trackpositions off per disattivarlo.",
        parse_mode='Markdown'
    )


async def setkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setkey 0x...  — salva la private key API (cifrata su disco).
    Necessaria per i futuri comandi di trading (close, stop loss, ecc.).
    IMPORTANTE: invia il comando in chat privata col bot, non in gruppi.
    """
    chat_id = update.effective_chat.id
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    if not context.args:
        has = wallet_store.has_key(chat_id)
        await update.message.reply_text(
            f"🔑 Chiave API: {'✅ impostata' if has else '❌ non impostata'}\n\n"
            f"Per impostarla: /setkey 0x...\n"
            f"⚠️ Invia solo in chat privata col bot."
        )
        return

    key = context.args[0].strip()
    if not key.startswith('0x') or len(key) < 20:
        await update.message.reply_text("❌ Chiave non valida.")
        return

    # Cancella subito il messaggio con la chiave
    try:
        await update.message.delete()
    except Exception:
        pass

    wallet_store.save_key(chat_id, key)
    await update.effective_chat.send_message(
        "✅ *Chiave API salvata* (cifrata su disco).\n"
        "Il messaggio con la chiave è stato cancellato.",
        parse_mode='Markdown'
    )


async def walletinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /walletinfo  — mostra stato credenziali + saldo wallet.
    """
    chat_id = update.effective_chat.id
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    addr    = wallet_store.get_address(chat_id)
    has_key = wallet_store.has_key(chat_id)
    addr_str = f"`{addr[:6]}...{addr[-4:]}`" if addr else "❌ non impostato"

    # Sezione credenziali
    msg = (
        f"🔐 *Wallet Info*\n\n"
        f"📍 Address: {addr_str}\n"
        f"🔑 Chiave API: {'✅ impostata' if has_key else '❌ non impostata'}\n"
    )

    # Saldo — richiede solo address
    if addr:
        await update.message.reply_text("⏳ Recupero saldo...")
        try:
            client  = HyperliquidClient(addr)
            summary = await asyncio.get_event_loop().run_in_executor(
                None, client.get_account_summary
            )
            equity    = summary['account_value']
            margin    = summary['total_margin']
            available = max(equity - margin, 0)
            ntl_perp  = summary['total_ntl_pos']  # solo perp standard dall'API

            # Esposizione XYZ dalle posizioni tracciate in memoria
            ntl_xyz = 0.0
            tracks  = monitor.position_tracks.get(chat_id, {})
            all_px  = await monitor.fetch_all_prices() if tracks else {}
            for coin, track in tracks.items():
                if not track.get('dex'):
                    continue  # skip perp standard, già in ntl_perp
                price_info = all_px.get(coin)
                if price_info:
                    ntl_xyz += abs(track['size'] * price_info[0])

            ntl_tot = ntl_perp + ntl_xyz

            # Ordini condizionali attivi
            n_conds = len(monitor.conditional_orders.get(chat_id, {}))

            ntl_line = f"  Esposizione tot:  ${ntl_tot:,.2f}"
            if ntl_xyz > 0:
                ntl_line += f"  (PERP ${ntl_perp:,.2f} + XYZ ${ntl_xyz:,.2f})"

            msg += (
                f"\n💰 *Saldo*\n"
                f"  Equity:           ${equity:,.2f}\n"
                f"  Margine usato:    ${margin:,.2f}\n"
                f"  Disponibile:      ${available:,.2f}\n"
                f"{ntl_line}\n"
            )
            if n_conds:
                msg += f"\n📌 Ordini condizionali attivi: {n_conds}  (/orders per dettagli)\n"
        except Exception as e:
            msg += f"\n⚠️ Impossibile recuperare saldo: {e}\n"

    msg += (
        f"\nComandi:\n"
        f"/setaddress 0x... — imposta address\n"
        f"/setkey 0x...     — imposta chiave API\n"
        f"/positions        — posizioni aperte\n"
        f"/orders           — ordini condizionali"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')


async def positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /positions  — mostra le posizioni aperte su Hyperliquid.
    Richiede solo l'address pubblico (/setaddress).
    """
    chat_id = update.effective_chat.id
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    addr = wallet_store.get_address(chat_id)
    if not addr:
        await update.message.reply_text(
            "❌ Address non impostato.\n"
            "Usa prima /setaddress 0x..."
        )
        return

    await update.message.reply_text("⏳ Recupero posizioni...")

    try:
        client   = HyperliquidClient(addr)
        pos_list = await asyncio.get_event_loop().run_in_executor(
            None, lambda: client.get_positions(extra_dexs=SUPPORTED_DEXS)
        )
        summary = await asyncio.get_event_loop().run_in_executor(
            None, client.get_account_summary
        )
    except Exception as e:
        logger.error(f"Errore get_positions chat {chat_id}: {e}")
        await update.message.reply_text(f"❌ Errore API: {e}")
        return

    if not pos_list:
        await update.message.reply_text("📭 Nessuna posizione aperta.")
        return

    now    = datetime.now().strftime('%H:%M:%S')
    msg    = f"📊 *Posizioni aperte* — {now}\n"
    msg   += f"Equity: ${summary['account_value']:,.2f}  Margin: ${summary['total_margin']:,.2f}\n"
    msg   += "─" * 30 + "\n"
    def fmt_px(x):
        if x == 0: return "0"
        if x >= 1000: return f"{x:,.2f}"
        if x >= 1:   return f"{x:,.3f}"
        return f"{x:,.5f}"

    for p in pos_list:
        arrow    = "📈 LONG" if p['is_long'] else "📉 SHORT"
        dex_tag  = f" _{p['dex']}_" if p.get('dex', 'PERP') != 'PERP' else ""
        pnl_col  = p['unrealized']
        pnl_sign = "+" if pnl_col >= 0 else ""
        try:    _lev = float(str(p.get('leverage',1)).replace('x',''))
        except: _lev = 1
        _margin_i = abs(p['entry_px'] * p['size'] / _lev) if _lev else p['margin'] or 1
        pnl_pct  = pnl_col / _margin_i * 100 if _margin_i else 0

        msg += (
            f"\n{arrow} *{p['coin']}*{dex_tag} {p['leverage']}x  size: {abs(p['size'])}\n"
            f"  Entry: {fmt_px(p['entry_px'])}  Liq: {fmt_px(p['liq_px'])}\n"
            f"  PnL: {pnl_sign}${pnl_col:.2f} ({pnl_sign}{pnl_pct:.1f}%)\n"
        )

    await update.message.reply_text(msg, parse_mode='Markdown')


# ---------------------------------------------------------------------------
# Position tracking task — aggiorna ogni POSITION_TRACK_INTERVAL secondi
# ---------------------------------------------------------------------------

async def position_tracking_task(application: Application):
    logger.info(f"🎯 Task position tracking avviato (ogni {POSITION_TRACK_INTERVAL}s)")

    while True:
        try:
            for chat_id in list(monitor.tracking_enabled):
                addr = wallet_store.get_address(chat_id)
                if not addr:
                    continue

                try:
                    client   = HyperliquidClient(addr)
                    pos_list = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: client.get_positions(extra_dexs=SUPPORTED_DEXS)
                    )
                except Exception as e:
                    logger.error(f"Position tracking fetch error chat {chat_id}: {e}")
                    continue

                current_tracks = monitor.position_tracks.get(chat_id, {})
                new_tracks     = {}
                threshold      = monitor.get_threshold(chat_id)
                is_first_cycle = chat_id not in monitor._first_track_done

                # Simboli attualmente aperti dall'API
                for p in pos_list:
                    coin = p['coin']
                    dex_s = f"_{p.get('dex','PERP').upper()}_" if p.get('dex') else "PERP"
                    dir_s = "📈 LONG" if p['is_long'] else "📉 SHORT"

                    if coin in current_tracks:
                        # Posizione già tracciata — confronta per rilevare modifiche
                        old_track = current_tracks[coin]
                        old_size  = abs(old_track['size'])
                        new_size  = abs(p['size'])
                        old_entry = old_track['entry_px']
                        new_entry = p['entry_px']

                        # Notifica se size o entry_px sono cambiati significativamente
                        size_changed  = abs(new_size - old_size) / old_size > 0.001 if old_size else False
                        entry_changed = abs(new_entry - old_entry) / old_entry > 0.001 if old_entry else False

                        if (size_changed or entry_changed) and not is_first_cycle:
                            change_parts = []
                            if size_changed:
                                change_parts.append(
                                    f"Size: {old_size} → {new_size} "                                    f"({'▲' if new_size > old_size else '▼'})"                                )
                            if entry_changed:
                                change_parts.append(
                                    f"Entry: ${_fmt(old_entry)} → ${_fmt(new_entry)}"                                )
                            try:
                                await application.bot.send_message(
                                    chat_id=chat_id,
                                    text=(
                                        f"✏️ *Posizione aggiornata*: *{coin}*\n\n"
                                        f"{dir_s} {dex_s} {p['leverage']}x\n"
                                        + "\n".join(change_parts) +
                                        f"\nMargin: ${p['margin']:.2f}  Liq: ${_fmt(p['liq_px'])}"
                                    ),
                                    parse_mode='Markdown'
                                )
                            except Exception as e:
                                logger.error(f"Errore notifica aggiornamento posizione {chat_id}: {e}")

                        # Aggiorna tutti i dati
                        track = old_track.copy()
                        track['entry_px']   = p['entry_px']
                        track['size']       = p['size']
                        track['margin']     = p['margin']
                        track['leverage']   = p['leverage']
                        track['unrealized'] = p['unrealized']
                        track['liq_px']     = p['liq_px']
                        track['is_long']    = p['is_long']
                        new_tracks[coin]    = track

                    else:
                        # Nuova posizione — aggiungi al tracking
                        new_tracks[coin] = {
                            'entry_px':   p['entry_px'],
                            'is_long':    p['is_long'],
                            'size':       p['size'],
                            'margin':     p['margin'],
                            'leverage':   p['leverage'],
                            'dex':        p.get('dex', 'PERP'),
                            'unrealized': p['unrealized'],
                            'liq_px':     p['liq_px'],
                            'alert_base': monitor.last_prices.get(coin, p['entry_px']),
                            'added_at':   datetime.now(),
                        }
                        logger.info(f"TRACK NEW {coin} ({'L' if p['is_long'] else 'S'}) -> chat {chat_id}")

                        # Notifica sempre, anche al primo ciclo — vogliamo sapere le posizioni attive
                        try:
                            await application.bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    f"🎯 *{'Posizione attiva al boot' if is_first_cycle else 'Nuova posizione rilevata'}*\n\n"
                                    f"{dir_s} *{coin}* {dex_s} {p['leverage']}x\n"
                                    f"Entry: ${_fmt(p['entry_px'])}  Size: {abs(p['size'])}\n"
                                    f"Margin: ${p['margin']:.2f}  Liq: ${_fmt(p['liq_px'])}\n"
                                    f"Soglia alert: ±{threshold}%"
                                ),
                                parse_mode='Markdown'
                            )
                        except Exception as e:
                            logger.error(f"Errore notifica posizione {chat_id}: {e}")

                # Posizioni chiuse: erano in track ma non più nell'API
                closed = set(current_tracks.keys()) - set(new_tracks.keys())
                for coin in closed:
                    track = current_tracks[coin]
                    logger.info(f"TRACK CLOSED {coin} -> chat {chat_id}")

                    # Cancella automaticamente tutti gli ordini condizionali su questo simbolo
                    conds        = monitor.conditional_orders.get(chat_id, {})
                    removed_oids = [oid for oid, o in conds.items() if o['coin'] == coin]
                    for oid in removed_oids:
                        conds.pop(oid, None)
                    if removed_oids:
                        monitor.save_conditional_orders(chat_id)
                        logger.info(f"Rimossi {len(removed_oids)} ordini condizionali per {coin} chiuso")

                    try:
                        cond_note = f"\n🗑 Rimossi {len(removed_oids)} ordini condizionali" if removed_oids else ""
                        dir_s     = "LONG" if track['is_long'] else "SHORT"
                        dex_s     = f"_{track['dex'].upper()}_" if track.get('dex') else "PERP"
                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"🏁 *Posizione chiusa*: *{coin}*\n"
                                f"{dir_s} {dex_s} {track['leverage']}x\n"
                                f"Entry: ${_fmt(track['entry_px'])}\n"
                                f"⏰ {datetime.now().strftime('%H:%M:%S')}{cond_note}"
                            ),
                            parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.error(f"Errore notifica chiusura {chat_id}: {e}")

                # Marca questo chat come già passato per il primo ciclo
                monitor._first_track_done.add(chat_id)
                monitor.position_tracks[chat_id] = new_tracks

        except Exception as e:
            logger.error(f"Errore nel position tracking task: {e}", exc_info=True)

        await asyncio.sleep(POSITION_TRACK_INTERVAL)


# ---------------------------------------------------------------------------
# Polling task — BATCH: 2 chiamate HTTP per tutti i simboli monitorati
# ---------------------------------------------------------------------------

async def price_polling_task(application: Application):
    logger.info("🚀 Task di polling avviato (modalità batch)")

    while True:
        try:
            monitored = monitor.get_all_monitored_symbols()

            all_prices: Dict[str, Tuple[float, str, Optional[str]]] = {}
            if monitored:
                # Unica fetch batch per tutti i prezzi
                all_prices = await monitor.fetch_all_prices()
                logger.debug(f"Batch fetch: {len(all_prices)} prezzi ricevuti, monitoro {len(monitored)} simboli")

                # Accumula alert subscribe per chat_id: {chat_id: [righe]}
                subscribe_alerts: Dict[int, list] = {}
                # Traccia quali simboli hanno scatenato alert (per aggiornare base price)
                triggered_syms: Set[str] = set()

                for sym in monitored:
                    if sym not in all_prices:
                        logger.debug(f"Simbolo {sym} non trovato nel batch")
                        continue

                    price_val, mtype, dex = all_prices[sym]
                    emoji, label = market_label(mtype, dex)
                    base = monitor.alert_base_prices.get(sym)

                    if base is not None:
                        pct = (price_val - base) / base * 100
                        for chat_id, syms in monitor.subscribers.items():
                            if sym not in syms:
                                continue
                            threshold = monitor.get_threshold(chat_id)
                            if abs(pct) >= threshold:
                                logger.info(f"ALERT {sym} ({label}): {base:.6f} -> {price_val:.6f} ({pct:+.2f}%)")
                                arrow = "📈" if price_val > base else "📉"
                                subscribe_alerts.setdefault(chat_id, []).append(
                                    f"{arrow} *{sym}* {emoji} {label}\n"
                                    f"  Rif: ${base:,.6f}  →  ${price_val:,.6f}  ({pct:+.2f}%)"
                                )
                                triggered_syms.add(sym)

                    monitor.last_prices[sym] = price_val

                # Invia un unico messaggio per chat con tutti gli alert subscribe
                now = datetime.now().strftime('%H:%M:%S')
                for chat_id, lines in subscribe_alerts.items():
                    threshold = monitor.get_threshold(chat_id)
                    msg = f"🔔 *Alert sottoscrizioni* — {now} (soglia ±{threshold}%)\n\n" + "\n".join(lines)
                    try:
                        await application.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
                    except Exception as e:
                        logger.error(f"Errore invio subscribe alert a {chat_id}: {e}")

                # Aggiorna base price solo per i simboli che hanno triggerato
                for sym in triggered_syms:
                    if sym in all_prices:
                        monitor.alert_base_prices[sym] = all_prices[sym][0]

                # --- POSITION TRACKING ALERTS: check soglia ogni 10s su last_prices ---
                track_alerts: Dict[int, list] = {}
                track_triggered: list = []  # [(chat_id, coin, new_base), ...]

                for chat_id, tracks in monitor.position_tracks.items():
                    threshold = monitor.get_threshold(chat_id)
                    for coin, track in tracks.items():
                        price_info = all_prices.get(coin)
                        if not price_info:
                            continue
                        current_price = price_info[0]
                        base = track.get('alert_base', track['entry_px'])
                        if not base or base <= 0:
                            continue
                        pct = (current_price - base) / base * 100
                        if abs(pct) >= threshold:
                            is_long  = track['is_long']
                            favor    = (pct > 0 and is_long) or (pct < 0 and not is_long)
                            favor_s  = "✅ a favore" if favor else "⚠️ contro"
                            arrow    = "📈" if pct > 0 else "📉"
                            pnl      = track['unrealized']
                            pnl_s    = "+" if pnl >= 0 else ""
                            liq      = track['liq_px']
                            liq_dist = abs((liq - current_price) / current_price * 100) if liq else 0
                            logger.info(f"TRACK ALERT {coin}: {pct:+.2f}% -> chat {chat_id}")
                            entry_px  = track['entry_px']
                            size_val  = track['size']  # positivo=long, negativo=short
                            pct_entry = (current_price - entry_px) / entry_px * 100 if entry_px else 0
                            # PnL real-time: per long (current-entry)*size, per short (entry-current)*size
                            pnl_rt    = (current_price - entry_px) * size_val
                            pnl_rt_s  = "+" if pnl_rt >= 0 else ""
                            # Margine calcolato da entry (non dall'API che si aggiorna ogni 5min)
                            lev_val   = track.get('leverage', 1)
                            try:    lev_num = float(str(lev_val).replace('x',''))
                            except: lev_num = 1
                            margin_calc = abs(entry_px * size_val / lev_num) if lev_num else track['margin']
                            pnl_pct   = pnl_rt / margin_calc * 100 if margin_calc else 0
                            track_alerts.setdefault(chat_id, []).append(
                                f"{arrow} *{coin}* {'LONG' if is_long else 'SHORT'} "
                                f"_{track['dex']}_ {track['leverage']}x  size: {abs(size_val)}  {favor_s}\n"
                                f"  Rif: ${_fmt(base)}  →  ${_fmt(current_price)}  ({pct:+.2f}%)\n"
                                f"  Entry: ${_fmt(entry_px)}  →  ${_fmt(current_price)}  ({pct_entry:+.2f}%)\n"
                                f"  PnL: {pnl_rt_s}${pnl_rt:.2f} ({pnl_rt_s}{pnl_pct:.1f}%)\n"
                                f"  Liq dist: {liq_dist:.1f}%"
                            )
                            track_triggered.append((chat_id, coin, current_price))

                # Invia alert position tracking (un messaggio per chat)
                now = datetime.now().strftime('%H:%M:%S')
                for chat_id, lines in track_alerts.items():
                    threshold = monitor.get_threshold(chat_id)
                    msg = (f"🎯 *Position Alert* — {now} (soglia ±{threshold}%)\n\n"
                           + "\n\n".join(lines))
                    try:
                        await application.bot.send_message(
                            chat_id=chat_id, text=msg, parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.error(f"Errore invio track alert {chat_id}: {e}")

                # Aggiorna alert_base per i coin che hanno triggerato
                for chat_id, coin, new_base in track_triggered:
                    if coin in monitor.position_tracks.get(chat_id, {}):
                        monitor.position_tracks[chat_id][coin]['alert_base'] = new_base

            # (fine blocco if monitored)

            # --- ORDINI CONDIZIONALI: stoploss / takeprofit ---
            # Eseguito sempre, indipendentemente dai subscribe attivi
            if monitor.conditional_orders:
                if not all_prices:
                    all_prices = await monitor.fetch_all_prices()
                now_ts = datetime.now().timestamp()
                for cid, conds in list(monitor.conditional_orders.items()):
                    for oid, o in list(conds.items()):
                        coin       = o['coin']
                        price_info = all_prices.get(coin)
                        if not price_info:
                            continue
                        current_px = price_info[0]

                        is_tp = o['type'] == 'takeprofit'
                        is_sl = o['type'] == 'stoploss'

                        # Direzione della posizione (long o short)
                        # Priorità: 1) posizione tracciata in memoria (la più aggiornata)
                        #            2) campo is_long salvato nell'ordine
                        #            3) mai un default cieco — segnala il problema
                        _track  = monitor.position_tracks.get(cid, {}).get(coin)
                        if _track:
                            is_long = _track['is_long']
                            # Aggiorna anche l'ordine se non ce l'aveva
                            if 'is_long' not in o:
                                o['is_long'] = is_long
                                monitor.save_conditional_orders(cid)
                        elif 'is_long' in o:
                            is_long = o['is_long']
                        else:
                            # Nessuna info disponibile — salta questo ciclo, riprova dopo
                            logger.warning(f"is_long sconosciuto per {coin} ordine {oid} — skip")
                            continue

                        # Verifica trigger — dipende dalla direzione della posizione:
                        # LONG:  TP scatta quando prezzo >= trigger, SL quando prezzo <= trigger
                        # SHORT: TP scatta quando prezzo <= trigger, SL quando prezzo >= trigger
                        if is_long:
                            triggered = (
                                (is_sl and current_px <= o['trigger_px']) or
                                (is_tp and current_px >= o['trigger_px'])
                            )
                        else:
                            triggered = (
                                (is_sl and current_px >= o['trigger_px']) or
                                (is_tp and current_px <= o['trigger_px'])
                            )

                        # ── TRAILING STOP automatico per TAKEPROFIT ──────────────────────
                        if is_tp and triggered:
                            track    = monitor.position_tracks.get(cid, {}).get(coin)
                            is_long  = track['is_long'] if track else True
                            peak     = o.get('tp_peak_price')

                            # Aggiorna il picco e calcola ritracciamento
                            retrace_pct = 0.0
                            if is_long:
                                # Long: il picco è il massimo — aspettiamo che scenda
                                if peak is None or current_px > peak:
                                    o['tp_peak_price'] = current_px
                                    peak = current_px
                                    logger.info(f"TP TRAILING {coin} {oid} — nuovo picco LONG: ${peak:.4f}")
                                retrace_pct = (peak - current_px) / peak * 100 if peak else 0
                            else:
                                # Short: il picco è il minimo — aspettiamo che risalga
                                if peak is None or current_px < peak:
                                    o['tp_peak_price'] = current_px
                                    peak = current_px
                                    logger.info(f"TP TRAILING {coin} {oid} — nuovo picco SHORT: ${peak:.4f}")
                                retrace_pct = (current_px - peak) / peak * 100 if peak else 0

                            logger.info(
                                f"TP TRAILING {coin} {oid} — "
                                f"px=${current_px:.4f}  picco=${peak:.4f}  "
                                f"retrace={retrace_pct:.3f}%  soglia={CONDITIONAL_TP_TRAILING_PCT}%"
                            )

                            trailing_triggered = retrace_pct >= CONDITIONAL_TP_TRAILING_PCT

                            if trailing_triggered:
                                logger.info(f"TP TRAILING AUTO-CLOSE {coin} chat {cid} retrace={retrace_pct:.2f}%")
                                try:
                                    _key    = wallet_store.get_key(cid)
                                    _addr   = wallet_store.get_address(cid)
                                    _client = HyperliquidClient(_addr, private_key=_key)
                                    await asyncio.get_event_loop().run_in_executor(
                                        None, lambda: _client.market_close(
                                            coin, o['dex'],
                                            usd_amount=o.get('usd'), pct=o.get('pct'))
                                    )
                                    conds.pop(oid, None)
                                    monitor.save_conditional_orders(cid)
                                    _retrace = retrace_pct
                                    _close_txt = (
                                        f"🎯✅ *Take Profit — chiusura trailing automatica*\n\n"
                                        f"*{coin}* {'_'+o['dex'].upper()+'_' if o['dex'] else 'PERP'}\n"
                                        f"Picco: ${_fmt(peak)}  →  Attuale: ${_fmt(current_px)}\n"
                                        f"Ritracciamento: {_retrace:.2f}% ≥ soglia {CONDITIONAL_TP_TRAILING_PCT}%\n"
                                        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                    )
                                    _mid = o.get('alert_message_id')
                                    if _mid:
                                        try:
                                            await application.bot.edit_message_text(
                                                chat_id=cid, message_id=_mid,
                                                text=_close_txt, parse_mode='Markdown')
                                        except Exception:
                                            await application.bot.send_message(
                                                chat_id=cid, text=_close_txt, parse_mode='Markdown')
                                    else:
                                        await application.bot.send_message(
                                            chat_id=cid, text=_close_txt, parse_mode='Markdown')
                                except Exception as e:
                                    logger.error(f"Errore trailing auto-close {coin}: {e}")
                                    await application.bot.send_message(
                                        chat_id=cid,
                                        text=f"⚠️ Trailing TP *{coin}*: errore chiusura\n`{e}`\nChiudi manualmente!",
                                        parse_mode='Markdown')
                                continue  # ordine rimosso, passa al prossimo
                        # ─────────────────────────────────────────────────────────────────

                        if not triggered:
                            # Uscito dalla zona: resetta picco e pulisci messaggio
                            if o.get('tp_peak_price') is not None:
                                logger.info(
                                    f"TP TRAILING {coin} {oid} — "
                                    f"uscito dalla zona (px=${current_px:.4f} < trigger=${o['trigger_px']:.4f}), "
                                    f"picco resettato da ${o['tp_peak_price']:.4f}"
                                )
                                o['tp_peak_price'] = None
                                try:
                                    await application.bot.edit_message_text(
                                        chat_id=cid,
                                        message_id=o['alert_message_id'],
                                        text=(
                                            f"{'🛑 Stop Loss' if is_sl else '🎯 Take Profit'}"
                                            f" — `{oid}` ↩️ uscito dalla zona\n"
                                            f"Trigger: ${_fmt(o['trigger_px'])}  Attuale: ${_fmt(current_px)}"
                                        ),
                                        parse_mode='Markdown'
                                    )
                                except Exception:
                                    pass
                                o['alert_message_id'] = None
                            continue

                        # Silenziato dopo "Salta"?
                        if o.get('snoozed_until') and now_ts < o['snoozed_until']:
                            continue

                        # Costruisce testo alert — freccia dipende da tipo + direzione
                        t_label  = "🛑 Stop Loss" if is_sl else "🎯 Take Profit"
                        # LONG:  SL=🔽 (scende sotto), TP=🔼 (sale sopra)
                        # SHORT: SL=🔼 (sale sopra),   TP=🔽 (scende sotto)
                        if is_sl:
                            arrow = "🔽" if is_long else "🔼"
                        else:
                            arrow = "🔼" if is_long else "🔽"
                        dir_label = "LONG" if is_long else "SHORT"
                        dex_s    = f"_{o['dex'].upper()}_" if o['dex'] else "PERP"
                        size_s   = ""
                        if o.get('pct'):   size_s = f"  ({o['pct']*100:.0f}%)"
                        elif o.get('usd'): size_s = f"  (${o['usd']:,.0f})"
                        else:              size_s = "  (tutto)"

                        # PnL dalla posizione tracciata
                        pnl_line     = ""
                        trailing_line = ""
                        track = monitor.position_tracks.get(cid, {}).get(coin)
                        if track:
                            entry_px = track['entry_px']
                            size_val = track['size']
                            pnl_rt   = (current_px - entry_px) * size_val
                            try:    lev_n = float(str(track.get('leverage',1)).replace('x',''))
                            except: lev_n = 1
                            margin_i = abs(entry_px * size_val / lev_n) if lev_n else 1
                            pnl_pct  = pnl_rt / margin_i * 100 if margin_i else 0
                            pnl_s    = "+" if pnl_rt >= 0 else ""
                            pnl_line = f"\nPnL: {pnl_s}${pnl_rt:.2f} ({pnl_s}{pnl_pct:.1f}%)"

                        # Info trailing (solo per TP attivo)
                        if is_tp and o.get('tp_peak_price'):
                            trailing_line = (
                                f"\n📍 Picco: ${_fmt(o['tp_peak_price'])}"
                                f"  trailing: -{CONDITIONAL_TP_TRAILING_PCT}%"
                            )

                        now_str = datetime.now().strftime('%H:%M:%S')
                        txt = (
                            f"{t_label} — `{oid}`  _{now_str}_\n\n"
                            f"{arrow} {dir_label} *{coin}* {dex_s}\n"
                            f"Trigger: ${_fmt(o['trigger_px'])}  Attuale: ${_fmt(current_px)}"
                            f"{pnl_line}{trailing_line}\n"
                            f"Azione: chiudi posizione{size_s}"
                        )
                        keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ Esegui",  callback_data=f"cond_exec_{cid}_{oid}"),
                            InlineKeyboardButton("⏭ Salta",   callback_data=f"cond_snooze_{cid}_{oid}"),
                            InlineKeyboardButton("🗑 Cancella",callback_data=f"cond_cancel_{cid}_{oid}"),
                        ]])

                        try:
                            mid           = o.get('alert_message_id')
                            last_notify   = o.get('last_notify_ts') or 0
                            need_renotify = mid and (now_ts - last_notify >= CONDITIONAL_RENOTIFY_SECS)

                            if need_renotify:
                                try:
                                    await application.bot.delete_message(chat_id=cid, message_id=mid)
                                except Exception:
                                    pass
                                o['alert_message_id'] = None
                                mid = None

                            if mid:
                                # Edit silenzioso — aggiorna prezzi/PnL senza bip
                                await application.bot.edit_message_text(
                                    chat_id=cid, message_id=mid,
                                    text=txt, parse_mode='Markdown',
                                    reply_markup=keyboard
                                )
                            else:
                                # Nuovo messaggio con bip
                                sent = await application.bot.send_message(
                                    chat_id=cid, text=txt,
                                    parse_mode='Markdown', reply_markup=keyboard
                                )
                                o['alert_message_id'] = sent.message_id
                                o['last_notify_ts']   = now_ts
                                monitor.save_conditional_orders(cid)
                        except Exception as e:
                            if 'message to edit not found' in str(e).lower():
                                o['alert_message_id'] = None
                            else:
                                logger.error(f"Errore cond alert {oid} chat {cid}: {e}")


            # --- PRICESPIKE: confronto poll-to-poll su lista fissa ---
            if monitor.spike_subscribers:
                # Costruisce la lista spike: extra fissi + tutti i simboli xyz dal batch
                xyz_syms = set()
                if all_prices:
                    xyz_syms = {sym for sym, (_, _, dex) in all_prices.items() if dex and dex.upper() == 'XYZ'}
                spike_symbols_all   = xyz_syms | set(SPIKE_EXTRA_SYMBOLS)           # tutti: prev prices + snapshot
                spike_symbols_alert = spike_symbols_all - SPIKE_EXCLUDE_SYMBOLS  # solo questi generano alert

                # Usa all_prices se già fetchato, altrimenti fetcha ora
                prices_batch = all_prices if all_prices else await monitor.fetch_all_prices()

                # Accumula spike per chat_id: {chat_id: [righe]}
                spike_alerts: Dict[int, list] = {}

                for sym in spike_symbols_all:
                    if sym not in prices_batch:
                        continue
                    price_val, mtype, dex = prices_batch[sym]
                    prev = monitor.spike_prev_prices.get(sym)

                    # Alert solo per i simboli non esclusi
                    if prev is not None and prev > 0 and sym in spike_symbols_alert:
                        pct = (price_val - prev) / prev * 100
                        for chat_id in monitor.spike_subscribers:
                            thresh = monitor.get_spike_threshold(chat_id)
                            if abs(pct) >= thresh:
                                emoji, label = market_label(mtype, dex)
                                arrow = "📈" if pct > 0 else "📉"
                                logger.info(f"SPIKE {sym}: {prev:.6f} -> {price_val:.6f} ({pct:+.2f}%)")
                                spike_alerts.setdefault(chat_id, []).append(
                                    f"{arrow} *{sym}* {emoji} {label}\n"
                                    f"  ${prev:,.6f}  →  ${price_val:,.6f}  ({pct:+.2f}%)"
                                )

                    # Aggiorna sempre il prezzo precedente (anche per gli esclusi)
                    monitor.spike_prev_prices[sym] = price_val

                # Invia un unico messaggio per chat con tutti gli spike
                now = datetime.now().strftime('%H:%M:%S')
                for chat_id, lines in spike_alerts.items():
                    thresh = monitor.get_spike_threshold(chat_id)
                    msg = f"⚡ *PriceSpike* — {now} (soglia ±{thresh}% / {POLL_INTERVAL}s)\n\n" + "\n".join(lines)
                    try:
                        await application.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
                    except Exception as e:
                        logger.error(f"Errore invio spike a {chat_id}: {e}")

            # --- SNAPSHOT GIORNALIERO: scrivi prezzi dopo le PRICES_TIME ---
            now_dt = datetime.now()
            today  = now_dt.date()
            if (now_dt.hour >= PRICES_TIME
                    and monitor.last_snapshot_date != today):
                # Fetch dedicato: all_prices potrebbe essere vuoto se non ci sono subscriber
                prices_batch = all_prices.copy() if all_prices else await monitor.fetch_all_prices()
                # Snapshot solo dei simboli monitorati da pricespike (xyz + extra)
                xyz_syms = {sym for sym, (_, _, dex) in prices_batch.items() if dex and dex.upper() == 'XYZ'}
                spike_symbols_all = xyz_syms | set(SPIKE_EXTRA_SYMBOLS)  # snapshot include anche gli esclusi
                prices_to_snap = {sym: v for sym, v in prices_batch.items() if sym in spike_symbols_all}
                if prices_to_snap:
                    written = save_daily_snapshot(prices_to_snap)
                    monitor.last_snapshot_date = today
                    logger.info(f"Snapshot giornaliero: {written} simboli scritti in {PRICES_DIR}")
                else:
                    logger.warning("Snapshot giornaliero: nessun prezzo disponibile, riprovo al prossimo ciclo")

            await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            logger.error(f"Errore nel polling: {e}", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def post_init(application: Application):
    logger.info(f"Bot avviato. POLL={POLL_INTERVAL}s THRESHOLD={PRICE_CHANGE_THRESHOLD}%")

    # Riattiva tracking per tutti gli utenti che hanno già un address su disco
    wallet_dir = DATA_DIR / 'wallet'
    if wallet_dir.exists():
        for user_dir in wallet_dir.iterdir():
            if user_dir.is_dir() and (user_dir / 'address.txt').exists():
                try:
                    chat_id = int(user_dir.name)
                    monitor.tracking_enabled.add(chat_id)
                    logger.info(f"Position tracking riattivato per chat {chat_id}")
                except ValueError:
                    pass

    # Carica ordini condizionali persistiti su disco
    monitor.load_all_conditional_orders()
    total_conds = sum(len(v) for v in monitor.conditional_orders.values())
    if total_conds:
        logger.info(f"Ordini condizionali caricati: {total_conds}")

    asyncio.create_task(price_polling_task(application))
    asyncio.create_task(position_tracking_task(application))
    asyncio.create_task(candle_task(application))

async def post_shutdown(application: Application):
    await monitor.close_session()

def main():
    logger.info("=" * 60)
    logger.info("Avvio Hyperliquid Price Monitor Bot")
    logger.info(f"Config: {env_file}")
    logger.info("=" * 60)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("help",        help_command))
    app.add_handler(CommandHandler("price",       price))
    app.add_handler(CommandHandler("subscribe",   subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("threshold",   threshold_command))
    app.add_handler(CommandHandler("pricespike",  pricespike))
    app.add_handler(CommandHandler("list",        list_subscriptions))
    app.add_handler(CommandHandler("symbols",     symbols_command))
    app.add_handler(CommandHandler("stats",       stats))
    app.add_handler(CommandHandler("setaddress",  setaddress))
    app.add_handler(CommandHandler("setkey",      setkey))
    app.add_handler(CommandHandler("walletinfo",  walletinfo))
    app.add_handler(CommandHandler("positions",      positions))
    app.add_handler(CommandHandler("trackpositions", trackpositions))
    app.add_handler(CommandHandler("setleverage",    setleverage))
    app.add_handler(CommandHandler("long",           long_command))
    app.add_handler(CommandHandler("short",          short_command))
    app.add_handler(CommandHandler("close",          close_command))
    app.add_handler(CommandHandler("confirm",        confirm_command))
    app.add_handler(CommandHandler("cancelorder",    cancelorder_command))
    app.add_handler(CallbackQueryHandler(order_callback, pattern="^order_"))
    app.add_handler(CallbackQueryHandler(cond_callback,  pattern="^cond_"))
    app.add_handler(CommandHandler("stoploss",     stoploss_command))
    app.add_handler(CommandHandler("takeprofit",   takeprofit_command))
    app.add_handler(CommandHandler("orders",       orders_command))
    app.add_handler(CommandHandler("cancelcond",   cancelcond_command))
    app.add_handler(CommandHandler("chart", chart_command))

    app.post_init     = post_init
    app.post_shutdown = post_shutdown

    logger.info("🤖 Bot in esecuzione!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
