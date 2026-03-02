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
CONDITIONAL_SNOOZE_SECS   = int(os.getenv('CONDITIONAL_SNOOZE_SECS',   '300'))
# Intervallo re-notifica ordini condizionali (nuovo messaggio con bip, default 2 minuti)
CONDITIONAL_RENOTIFY_SECS = int(os.getenv('CONDITIONAL_RENOTIFY_SECS', '120'))
# Trailing stop per takeprofit: se il prezzo ritraccia di questa % dal picco → chiude automaticamente
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


# ---------------------------------------------------------------------------
# Istanze globali (importate dagli altri moduli)
# ---------------------------------------------------------------------------
from pathlib import Path as _Path
_DATA_DIR = _Path(os.getenv('DATA_DIR', 'data'))
wallet_store = WalletStore(_DATA_DIR, WALLET_ENCRYPTION_KEY)
monitor      = HyperliquidPriceMonitor()
