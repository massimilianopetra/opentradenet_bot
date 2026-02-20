import asyncio
import logging
import logging.handlers
import os
import sys
from typing import Dict, Set, Optional, Tuple
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------------------------------------------------------------------
# Configurazione ambiente
# ---------------------------------------------------------------------------

if len(sys.argv) > 1:
    env_file = sys.argv[1]
else:
    env_file = 'opentradenet.env'

env_path = Path('.') / env_file

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
root_logger.setLevel(logging.INFO)
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
SPIKE_EXTRA_SYMBOLS    = [s.strip().upper() for s in os.getenv('SPIKE_EXTRA_SYMBOLS', 'BTC,SOL,ETH,XRP,SUI,HYPER').split(',') if s.strip()]
SPIKE_THRESHOLD        = float(os.getenv('SPIKE_THRESHOLD', '1.0'))   # soglia % poll-to-poll per pricespike

if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_TOKEN non trovato nel file .env")
    sys.exit(1)

logger.info(f"Config: POLL={POLL_INTERVAL}s THRESHOLD={PRICE_CHANGE_THRESHOLD}% DEX={SUPPORTED_DEXS}")
logger.info(f"Log file: {LOG_FILE.resolve()}")


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class HyperliquidPriceMonitor:
    def __init__(self):
        self.subscribers:       Dict[int, Set[str]] = {}
        self.last_prices:       Dict[str, float]    = {}  # aggiornato ogni poll
        self.alert_base_prices: Dict[str, float]    = {}  # riferimento alert: impostato al subscribe, aggiornato dopo ogni alert
        self.user_thresholds:   Dict[int, float]    = {}  # soglia per-utente, fallback a PRICE_CHANGE_THRESHOLD globale
        self.spike_subscribers: Set[int]             = set()  # chat_id iscritti a pricespike
        self.spike_prev_prices: Dict[str, float]     = {}     # prezzi poll precedente per pricespike
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
        """Restituisce la soglia dell'utente, o quella globale se non impostata."""
        return self.user_thresholds.get(chat_id, PRICE_CHANGE_THRESHOLD)

    def set_threshold(self, chat_id: int, threshold: float):
        self.user_thresholds[chat_id] = threshold
        logger.info(f"Chat {chat_id} soglia impostata a {threshold}%")

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

    variation_msg = ""
    old = monitor.last_prices.get(sym)
    if old:
        pct = (price_val - old) / old * 100
        arrow = "📈" if price_val > old else "📉" if price_val < old else "➡️"
        variation_msg = f"\n{arrow} Variazione: {pct:+.2f}%"

    await update.message.reply_text(
        f"{emoji} *{sym}* ({label})\n"
        f"Prezzo: ${price_val:,.6f}"
        f"{variation_msg}\n"
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
        thresh = monitor.user_thresholds.get(chat_id, SPIKE_THRESHOLD)
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
            monitor.user_thresholds[chat_id] = value
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
        thresh = monitor.user_thresholds.get(chat_id, SPIKE_THRESHOLD)
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
        thresh = monitor.user_thresholds.get(chat_id, SPIKE_THRESHOLD)
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

    if not subs:
        await update.message.reply_text("📋 Nessuna sottoscrizione attiva.\nUsa /subscribe SYMBOL per iniziare!")
        return

    # Fetch prezzi correnti in batch
    all_prices = await monitor.fetch_all_prices()

    threshold = monitor.get_threshold(chat_id)
    msg = f"📋 *Le tue sottoscrizioni* (soglia: ±{threshold}%):\n\n"
    for sym in sorted(subs):
        base    = monitor.alert_base_prices.get(sym)   # prezzo al subscribe (o ultimo alert)
        current_info = all_prices.get(sym)
        current = current_info[0] if current_info else monitor.last_prices.get(sym)

        if base and current:
            pct   = (current - base) / base * 100
            arrow = "📈" if pct > 0 else "📉" if pct < 0 else "➡️"
            msg += (
                f"• *{sym}*\n"
                f"  Riferimento: ${base:,.6f}\n"
                f"  Attuale:     ${current:,.6f}  {arrow} {pct:+.2f}%\n\n"
            )
        elif current:
            msg += f"• *{sym}* — ${current:,.6f}\n\n"
        else:
            msg += f"• *{sym}* — prezzo non ancora disponibile\n\n"

    await update.message.reply_text(msg, parse_mode='Markdown')

async def symbols_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Recupero simboli...")
    all_syms = await monitor.get_all_symbols()

    perps     = all_syms.get('perps', [])
    spots     = all_syms.get('spot', [])
    dex_perps = all_syms.get('dex_perps', {})

    msg = "📊 *Simboli disponibili*\n\n"

    if perps:
        msg += f"⚡ *PERPETUALS* ({len(perps)} totali):\n"
        msg += ", ".join(perps[:MAX_SYMBOLS_DISPLAY])
        if len(perps) > MAX_SYMBOLS_DISPLAY:
            msg += f"\n... e altri {len(perps) - MAX_SYMBOLS_DISPLAY}"
        msg += "\n\n"

    for dex_code, dex_syms in dex_perps.items():
        msg += f"🔥 *{dex_code}* ({len(dex_syms)} totali):\n"
        msg += ", ".join(dex_syms[:MAX_SYMBOLS_DISPLAY])
        if len(dex_syms) > MAX_SYMBOLS_DISPLAY:
            msg += f"\n... e altri {len(dex_syms) - MAX_SYMBOLS_DISPLAY}"
        msg += "\n\n"

    if spots:
        clean = [s.replace('/USDC', '') for s in spots]
        msg += f"💎 *SPOT* ({len(spots)} totali):\n"
        msg += ", ".join(clean[:MAX_SYMBOLS_DISPLAY])
        if len(spots) > MAX_SYMBOLS_DISPLAY:
            msg += f"\n... e altri {len(spots) - MAX_SYMBOLS_DISPLAY}"
        msg += "\n\n"

    total = len(perps) + len(spots) + sum(len(v) for v in dex_perps.values())
    msg += f"*Totale: {total} simboli*"
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
        f"⚡ PriceSpike: {'attivo' if update.effective_chat.id in monitor.spike_subscribers else 'non attivo'} (soglia ±{monitor.user_thresholds.get(update.effective_chat.id, SPIKE_THRESHOLD)}% per poll)\n"
        f"📁 Log: {LOG_FILE}\n"
        f"⚙️ Config: {env_file}\n"
    )
    await update.message.reply_text(msg)


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

                for sym in monitored:
                    if sym not in all_prices:
                        logger.debug(f"Simbolo {sym} non trovato nel batch")
                        continue

                    price_val, mtype, dex = all_prices[sym]
                    emoji, label = market_label(mtype, dex)
                    base = monitor.alert_base_prices.get(sym)  # prezzo di riferimento alert

                    if base is not None:
                        pct = (price_val - base) / base * 100
                        alert_sent = False
                        for chat_id, syms in monitor.subscribers.items():
                            if sym not in syms:
                                continue
                            threshold = monitor.get_threshold(chat_id)
                            if abs(pct) >= threshold:
                                if not alert_sent:
                                    logger.info(f"ALERT {sym} ({label}): {base:.6f} -> {price_val:.6f} ({pct:+.2f}%)")
                                arrow = "📈" if price_val > base else "📉"
                                alert = (
                                    f"{arrow} *{sym}* Alert! {emoji}\n\n"
                                    f"Tipo: {label}\n"
                                    f"Riferimento: ${base:,.6f}\n"
                                    f"Attuale: ${price_val:,.6f}\n"
                                    f"Cambio: {pct:+.2f}%\n"
                                    f"Soglia: ±{threshold}%\n"
                                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                )
                                try:
                                    await application.bot.send_message(
                                        chat_id=chat_id,
                                        text=alert,
                                        parse_mode='Markdown'
                                    )
                                    alert_sent = True
                                except Exception as e:
                                    logger.error(f"Errore invio a {chat_id}: {e}")
                        # Aggiorna il riferimento SOLO se almeno un alert è stato inviato
                        if alert_sent:
                            monitor.alert_base_prices[sym] = price_val

                    monitor.last_prices[sym] = price_val  # aggiornato sempre

            # --- PRICESPIKE: confronto poll-to-poll su lista fissa ---
            if monitor.spike_subscribers:
                # Costruisce la lista spike: extra fissi + tutti i simboli xyz dal batch
                xyz_syms = set()
                if all_prices:
                    xyz_syms = {sym for sym, (_, _, dex) in all_prices.items() if dex and dex.upper() == 'XYZ'}
                spike_symbols = xyz_syms | set(SPIKE_EXTRA_SYMBOLS)

                # Usa all_prices se già fetchato, altrimenti fetcha ora
                prices_batch = all_prices if all_prices else await monitor.fetch_all_prices()

                for sym in spike_symbols:
                    if sym not in prices_batch:
                        continue
                    price_val, mtype, dex = prices_batch[sym]
                    prev = monitor.spike_prev_prices.get(sym)

                    if prev is not None and prev > 0:
                        pct = (price_val - prev) / prev * 100
                        for chat_id in monitor.spike_subscribers:
                            thresh = monitor.user_thresholds.get(chat_id, SPIKE_THRESHOLD)
                            if abs(pct) >= thresh:
                                emoji, label = market_label(mtype, dex)
                                arrow = "📈" if pct > 0 else "📉"
                                spike_msg = (
                                    f"{arrow} *SPIKE {sym}* {emoji}\n\n"
                                    f"Tipo: {label}\n"
                                    f"Precedente: ${prev:,.6f}\n"
                                    f"Attuale:    ${price_val:,.6f}\n"
                                    f"Variazione: {pct:+.2f}% in {POLL_INTERVAL}s\n"
                                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                )
                                try:
                                    await application.bot.send_message(
                                        chat_id=chat_id,
                                        text=spike_msg,
                                        parse_mode='Markdown'
                                    )
                                    logger.info(f"SPIKE {sym}: {prev:.6f} -> {price_val:.6f} ({pct:+.2f}%) -> chat {chat_id}")
                                except Exception as e:
                                    logger.error(f"Errore invio spike a {chat_id}: {e}")

                    monitor.spike_prev_prices[sym] = price_val

            await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            logger.error(f"Errore nel polling: {e}", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def post_init(application: Application):
    logger.info(f"Bot avviato. POLL={POLL_INTERVAL}s THRESHOLD={PRICE_CHANGE_THRESHOLD}%")
    asyncio.create_task(price_polling_task(application))

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

    app.post_init     = post_init
    app.post_shutdown = post_shutdown

    logger.info("🤖 Bot in esecuzione!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
