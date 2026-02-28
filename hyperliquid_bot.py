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
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
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
PRICES_TIME   = int(os.getenv('PRICES_TIME', '9'))  # ora intera (0-23)

# Intervallo aggiornamento position tracking (default 5 minuti)
POSITION_TRACK_INTERVAL = int(os.getenv('POSITION_TRACK_INTERVAL', '300'))

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
            pnl           = track['unrealized']
            pnl_s         = "+" if pnl >= 0 else ""
            pnl_pct       = pnl / track['margin'] * 100 if track['margin'] else 0
            liq           = track['liq_px']
            fmt           = lambda x: f"{x:,.5g}"

            arrow_dir = "📈" if is_long else "📉"
            dir_s     = "LONG" if is_long else "SHORT"

            # Formatter prezzi: decimali adattivi (2 per grandi, 4 per piccoli)
            def fmt(x):
                if x == 0: return "0"
                if x >= 100:  return f"{x:,.2f}"
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
                f"  PnL: {pnl_s}${pnl:.2f} ({pnl_s}{pnl_pct:.1f}%)\n"
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
            "Uso: /setleverage SIMBOLO LEVA [cross]\n\n"
            "Esempi:\n"
            "  /setleverage NFLX 5       — NFLX xyz, isolated, 5x\n"
            "  /setleverage BTC 10       — BTC perp, isolated, 10x\n"
            "  /setleverage MSTR 3 cross — MSTR xyz, cross, 3x\n\n"
            "Default: isolated. Aggiungi 'cross' per cross margin.",
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

    is_cross = len(context.args) > 2 and context.args[2].lower() == 'cross'
    margin_s = "cross" if is_cross else "isolated"

    # Determina se è un simbolo xyz interrogando direttamente il meta del dex
    # (più affidabile della lista locale che potrebbe avere formati diversi)
    import aiohttp as _aiohttp
    dex      = ''
    market_s = 'PERP'
    try:
        async with _aiohttp.ClientSession() as _sess:
            async with _sess.post(
                'https://api.hyperliquid.xyz/info',
                json={'type': 'meta', 'dex': 'xyz'},
                timeout=_aiohttp.ClientTimeout(total=10)
            ) as _r:
                _meta = await _r.json()
        _xyz_names = {a.get('name', '').upper() for a in _meta.get('universe', [])}
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
    /walletinfo  — mostra lo stato delle credenziali salvate.
    """
    chat_id = update.effective_chat.id
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    addr = wallet_store.get_address(chat_id)
    has_key = wallet_store.has_key(chat_id)
    addr_str = f"`{addr[:6]}...{addr[-4:]}`" if addr else "❌ non impostato"

    await update.message.reply_text(
        f"🔐 *Wallet Info*\n\n"
        f"📍 Address: {addr_str}\n"
        f"🔑 Chiave API: {'✅ impostata' if has_key else '❌ non impostata'}\n\n"
        f"Comandi:\n"
        f"/setaddress 0x... — imposta address\n"
        f"/setkey 0x...     — imposta chiave API\n"
        f"/positions        — posizioni aperte",
        parse_mode='Markdown'
    )


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
        if x >= 100: return f"{x:,.2f}"
        if x >= 1:   return f"{x:,.3f}"
        return f"{x:,.5f}"

    for p in pos_list:
        arrow    = "📈 LONG" if p['is_long'] else "📉 SHORT"
        dex_tag  = f" _{p['dex']}_" if p.get('dex', 'PERP') != 'PERP' else ""
        pnl_col  = p['unrealized']
        pnl_sign = "+" if pnl_col >= 0 else ""
        pnl_pct  = pnl_col / p['margin'] * 100 if p['margin'] else 0

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

                # Simboli attualmente aperti dall'API
                for p in pos_list:
                    coin = p['coin']

                    if coin in current_tracks:
                        # Posizione già tracciata — aggiorna solo dati da API (size/margin/liq/pnl)
                        # Il check soglia è gestito dal polling 10s tramite last_prices
                        track = current_tracks[coin].copy()
                        track['size']       = p['size']
                        track['margin']     = p['margin']
                        track['unrealized'] = p['unrealized']
                        track['liq_px']     = p['liq_px']
                        new_tracks[coin]    = track

                    else:
                        # Nuova posizione rilevata — aggiungi al tracking
                        new_tracks[coin] = {
                            'entry_px':   p['entry_px'],
                            'is_long':    p['is_long'],
                            'size':       p['size'],
                            'margin':     p['margin'],
                            'leverage':   p['leverage'],
                            'dex':        p.get('dex', 'PERP'),
                            'unrealized': p['unrealized'],
                            'liq_px':     p['liq_px'],
                            'alert_base': monitor.last_prices.get(p['coin'], p['entry_px']),  # prezzo corrente al rilevamento
                            'added_at':   datetime.now(),
                        }
                        logger.info(f"TRACK NEW {coin} ({'L' if p['is_long'] else 'S'}) -> chat {chat_id}")
                        try:
                            await application.bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    f"🎯 *Nuova posizione rilevata*\n\n"
                                    f"{'📈 LONG' if p['is_long'] else '📉 SHORT'} *{coin}* "
                                    f"_{p.get('dex','PERP')}_ {p['leverage']}x\n"
                                    f"Entry: ${p['entry_px']:,.2f}  Size: {abs(p['size'])}\n"
                                    f"Margin: ${p['margin']:.2f}  Liq: ${p['liq_px']:,.2f}\n"
                                    f"Soglia alert: ±{threshold}%"
                                ),
                                parse_mode='Markdown'
                            )
                        except Exception as e:
                            logger.error(f"Errore notifica nuova posizione {chat_id}: {e}")

                # Posizioni chiuse: erano in track ma non più nell'API
                closed = set(current_tracks.keys()) - set(new_tracks.keys())
                for coin in closed:
                    track = current_tracks[coin]
                    logger.info(f"TRACK CLOSED {coin} -> chat {chat_id}")
                    try:
                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"🏁 *Posizione chiusa*: *{coin}*\n"
                                f"{'LONG' if track['is_long'] else 'SHORT'} _{track['dex']}_ {track['leverage']}x\n"
                                f"Entry: ${track['entry_px']:,.2f}\n"
                                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                            ),
                            parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.error(f"Errore notifica chiusura {chat_id}: {e}")

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
                            pct_entry = (current_price - entry_px) / entry_px * 100 if entry_px else 0
                            pnl_pct   = pnl / track['margin'] * 100 if track['margin'] else 0
                            track_alerts.setdefault(chat_id, []).append(
                                f"{arrow} *{coin}* {'LONG' if is_long else 'SHORT'} "
                                f"_{track['dex']}_ {track['leverage']}x  {favor_s}\n"
                                f"  Rif: ${base:,.2f}  →  ${current_price:,.2f}  ({pct:+.2f}%)\n"
                                f"  Entry: ${entry_px:,.2f}  →  ${current_price:,.2f}  ({pct_entry:+.2f}%)\n"
                                f"  PnL: {pnl_s}${pnl:.2f} ({pnl_s}{pnl_pct:.1f}%)\n"
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

    asyncio.create_task(price_polling_task(application))
    asyncio.create_task(position_tracking_task(application))

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

    app.post_init     = post_init
    app.post_shutdown = post_shutdown

    logger.info("🤖 Bot in esecuzione!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
