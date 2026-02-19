import asyncio
import logging
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
    env_file = 'miobot.env'

env_path = Path('.') / env_file

if not env_path.exists():
    print(f"❌ Errore: File {env_file} non trovato!")
    sys.exit(1)

load_dotenv(dotenv_path=env_path)
print(f"✅ Caricato file di configurazione: {env_file}")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN         = os.getenv('TELEGRAM_TOKEN')
HYPERLIQUID_API        = os.getenv('HYPERLIQUID_API', 'https://api.hyperliquid.xyz/info')
POLL_INTERVAL          = int(os.getenv('POLL_INTERVAL', '10'))
PRICE_CHANGE_THRESHOLD = float(os.getenv('PRICE_CHANGE_THRESHOLD', '0.5'))
MAX_SYMBOLS_DISPLAY    = int(os.getenv('MAX_SYMBOLS_DISPLAY', '20'))

# DEX aggiuntivi da interrogare con allMids.
# I perp standard usano dex="", quelli xyz usano dex="xyz".
# Aggiungi qui altri dex se necessario: ['xyz', 'flx', 'vntl']
SUPPORTED_DEXS = ['xyz']

if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_TOKEN non trovato nel file .env")
    sys.exit(1)

logger.info(f"Config: POLL={POLL_INTERVAL}s THRESHOLD={PRICE_CHANGE_THRESHOLD}% DEX={SUPPORTED_DEXS}")


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class HyperliquidPriceMonitor:
    def __init__(self):
        self.subscribers: Dict[int, Set[str]] = {}
        self.last_prices: Dict[str, float] = {}
        self.session: Optional[aiohttp.ClientSession] = None

    async def init_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
            logger.info("Sessione HTTP inizializzata")

    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None
            logger.info("Sessione HTTP chiusa")

    async def _allMids(self, dex: str) -> dict:
        """
        Chiama POST /info  {"type": "allMids", "dex": dex}
          dex=""    -> perp standard  (mids + spotMids)
          dex="xyz" -> perp xyz       (mids)
        Ritorna il dict della risposta, vuoto in caso di errore.
        """
        await self.init_session()
        payload = {"type": "allMids", "dex": dex}
        try:
            async with self.session.post(HYPERLIQUID_API, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.debug(f"allMids dex='{dex}' -> {list(data.keys())}")
                    return data
                logger.error(f"allMids dex='{dex}' HTTP {resp.status}")
                return {}
        except asyncio.TimeoutError:
            logger.error(f"allMids dex='{dex}' timeout")
            return {}
        except Exception as e:
            logger.error(f"allMids dex='{dex}' errore: {e}")
            return {}

    async def get_price(self, symbol: str) -> Optional[Tuple[float, str, Optional[str]]]:
        """
        Cerca il prezzo di symbol in tutti i mercati.
        Ritorna: (prezzo, tipo, dex_label)
          tipo      = 'PERP' | 'SPOT'
          dex_label = None per perp standard, 'XYZ' ecc. per dex perp
        """
        sym = symbol.upper()

        # 1. Perp standard + spot -> dex=""
        # Risposta: dict piatto {"BTC": "66873.5", "PURR/USDC": "0.067", ...}
        # - perp standard: chiave senza "/"
        # - spot: chiave con "/USDC"
        data = await self._allMids("")

        # Perp standard (es: "BTC")
        if sym in data:
            return (float(data[sym]), 'PERP', None)

        # Spot: prova "PURR" -> cerca "PURR/USDC", oppure direttamente "PURR/USDC"
        if f"{sym}/USDC" in data:
            return (float(data[f"{sym}/USDC"]), 'SPOT', None)
        if sym in data:  # chiave esatta con slash già incluso (es: utente scrive "PURR/USDC")
            return (float(data[sym]), 'SPOT', None)

        # 2. Perp DEX -> dex="xyz" ecc.
        # La risposta è un dict piatto con chiavi "xyz:MSTR", "xyz:TSLA" ecc.
        for dex in SUPPORTED_DEXS:
            dex_mids = await self._allMids(dex)
            full_key = f"{dex}:{sym}"
            if full_key in dex_mids:
                return (float(dex_mids[full_key]), 'PERP', dex.upper())

        logger.warning(f"Simbolo {sym} non trovato in nessun mercato")
        return None

    async def get_all_symbols(self) -> dict:
        """
        Ritorna: {'perps': [...], 'spot': [...], 'dex_perps': {dex: [...]}}
        """
        result: dict = {'perps': [], 'spot': [], 'dex_perps': {}}

        # dex="" -> dict piatto {"BTC": "price", "PURR/USDC": "price", "@1": "price", ...}
        # perp standard: chiavi senza "/" e senza ":"
        # spot: chiavi con "/"
        data = await self._allMids("")
        result['perps'] = sorted(k for k in data if '/' not in k and ':' not in k)
        result['spot']  = sorted(k for k in data if '/' in k)

        for dex in SUPPORTED_DEXS:
            dex_mids = await self._allMids(dex)
            if dex_mids:
                # Rimuove il prefisso "xyz:" per mostrare solo il nome della coin
                clean = sorted(k.split(':', 1)[1] for k in dex_mids.keys() if ':' in k)
                result['dex_perps'][dex.upper()] = clean

        return result

    # --- Subscriber management ---

    def add_subscriber(self, chat_id: int, symbol: str):
        self.subscribers.setdefault(chat_id, set()).add(symbol.upper())
        logger.info(f"Chat {chat_id} sottoscritta a {symbol}")

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
    """Ritorna (emoji, label)."""
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
        "/symbols — simboli disponibili\n"
        "/stats — statistiche bot\n"
        "/help — questo messaggio\n"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /price SYMBOL  (es: /price BTC  /price MSTR  /price PURR)")
        return

    sym = context.args[0].upper()
    await update.message.reply_text(f"🔍 Recupero prezzo di {sym}...")
    result = await monitor.get_price(sym)

    if not result:
        await update.message.reply_text(
            f"❌ Simbolo *{sym}* non trovato.\nUsa /symbols per vedere i simboli disponibili.",
            parse_mode='Markdown'
        )
        return

    price_val, mtype, dex = result
    emoji, label = market_label(mtype, dex)

    variation_msg = ""
    if sym in monitor.last_prices:
        old = monitor.last_prices[sym]
        pct = ((price_val - old) / old * 100) if old else 0
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
            f"❌ Simbolo *{sym}* non trovato.\nUsa /symbols per vedere i simboli disponibili.",
            parse_mode='Markdown'
        )
        return

    price_val, mtype, dex = result
    emoji, label = market_label(mtype, dex)
    monitor.add_subscriber(chat_id, sym)

    await update.message.reply_text(
        f"✅ Monitoro *{sym}* per te!\n"
        f"{emoji} Tipo: {label}\n"
        f"Prezzo attuale: ${price_val:,.6f}\n"
        f"Soglia notifiche: ±{PRICE_CHANGE_THRESHOLD}%",
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
            f"❌ Non stai monitorando *{sym}*. Usa /list per vedere le tue sottoscrizioni.",
            parse_mode='Markdown'
        )
        return

    monitor.remove_subscriber(chat_id, sym)
    await update.message.reply_text(f"✅ Non monitoro più *{sym}*", parse_mode='Markdown')

async def list_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs = monitor.get_subscriptions(chat_id)

    if not subs:
        await update.message.reply_text("📋 Nessuna sottoscrizione attiva.\nUsa /subscribe SYMBOL per iniziare!")
        return

    msg = "📋 *Le tue sottoscrizioni:*\n\n"
    for sym in sorted(subs):
        p = monitor.last_prices.get(sym)
        msg += f"• {sym} — ${p:,.6f}\n" if p else f"• {sym}\n"
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
    msg = (
        "📊 *Statistiche Bot*\n\n"
        f"👥 Utenti attivi: {len(monitor.subscribers)}\n"
        f"📈 Simboli monitorati: {len(monitor.get_all_monitored_symbols())}\n"
        f"💾 Prezzi in cache: {len(monitor.last_prices)}\n"
        f"⏱️ Intervallo polling: {POLL_INTERVAL}s\n"
        f"📊 Soglia notifiche: ±{PRICE_CHANGE_THRESHOLD}%\n"
        f"🔗 DEX: {', '.join(d.upper() for d in SUPPORTED_DEXS)}\n"
        f"⚙️ Config: {env_file}\n"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')


# ---------------------------------------------------------------------------
# Polling task
# ---------------------------------------------------------------------------

async def price_polling_task(application: Application):
    logger.info("🚀 Task di polling prezzi avviato")

    while True:
        try:
            all_symbols = monitor.get_all_monitored_symbols()

            if all_symbols:
                for sym in all_symbols:
                    result = await monitor.get_price(sym)
                    if not result:
                        await asyncio.sleep(0.5)
                        continue

                    price_val, mtype, dex = result
                    emoji, label = market_label(mtype, dex)
                    old = monitor.last_prices.get(sym)

                    if old is not None:
                        pct = ((price_val - old) / old * 100) if old else 0
                        if abs(pct) >= PRICE_CHANGE_THRESHOLD:
                            logger.info(f"{sym} ({label}): {old:.6f} -> {price_val:.6f} ({pct:+.2f}%)")
                            arrow = "📈" if price_val > old else "📉"
                            alert = (
                                f"{arrow} *{sym}* Alert! {emoji}\n\n"
                                f"Tipo: {label}\n"
                                f"Vecchio: ${old:,.6f}\n"
                                f"Nuovo: ${price_val:,.6f}\n"
                                f"Cambio: {pct:+.2f}%\n"
                                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                            )
                            for chat_id, syms in monitor.subscribers.items():
                                if sym in syms:
                                    try:
                                        await application.bot.send_message(
                                            chat_id=chat_id,
                                            text=alert,
                                            parse_mode='Markdown'
                                        )
                                    except Exception as e:
                                        logger.error(f"Errore invio a {chat_id}: {e}")

                    monitor.last_prices[sym] = price_val
                    await asyncio.sleep(0.5)

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
    app.add_handler(CommandHandler("list",        list_subscriptions))
    app.add_handler(CommandHandler("symbols",     symbols_command))
    app.add_handler(CommandHandler("stats",       stats))

    app.post_init     = post_init
    app.post_shutdown = post_shutdown

    logger.info("🤖 Bot in esecuzione!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
