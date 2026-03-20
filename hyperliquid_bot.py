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
from bot_monitor import HyperliquidPriceMonitor, MonitorConfig
import importlib, ml_scanner as _mls
import ml_analyst
import ml_journal
from bot_tasks import init_tasks, TaskContext, price_polling_task, position_tracking_task, candle_task

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

SUPPORTED_DEXS = [d.strip() for d in os.getenv('SUPPORTED_DEXS', 'xyz').split(',') if d.strip()]

SPIKE_EXTRA_SYMBOLS    = [s.strip().upper() for s in os.getenv('SPIKE_EXTRA_SYMBOLS', 'BTC,SOL,ETH,XRP,SUI,HYPE').split(',') if s.strip()]
SPIKE_THRESHOLD        = float(os.getenv('SPIKE_THRESHOLD', '1.0'))
SPIKE_EXCLUDE_SYMBOLS  = {s.strip().upper() for s in os.getenv('SPIKE_EXCLUDE_SYMBOLS', '').split(',') if s.strip()}

PRICES_DIR    = Path(os.getenv('PRICES_DIR', 'data/prices'))
COND_DIR      = Path(os.getenv('COND_DIR',   'data/conditional_orders'))
CANDLES_DIR   = Path(os.getenv('CANDLES_DIR', 'data/candles'))
CANDLES_INTERVAL_SECS = int(os.getenv('CANDLES_INTERVAL_SECS', '900'))
PRICES_TIME   = int(os.getenv('PRICES_TIME', '9'))

POSITION_TRACK_INTERVAL = int(os.getenv('POSITION_TRACK_INTERVAL', '300'))

CONDITIONAL_SNOOZE_SECS   = int(os.getenv('CONDITIONAL_SNOOZE_SECS',      '300'))
CONDITIONAL_RENOTIFY_SECS  = int(os.getenv('CONDITIONAL_RENOTIFY_SECS',  '120'))
CONDITIONAL_TP_TRAILING_PCT = float(os.getenv('CONDITIONAL_TP_TRAILING_PCT', '0.5'))

WALLET_ALLOWED_CHATS = {int(x.strip()) for x in os.getenv('WALLET_ALLOWED_CHATS', '').split(',') if x.strip()}

ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')

ANALYZE_ALLOWED_CHATS = {int(x.strip()) for x in os.getenv('ANALYZE_ALLOWED_CHATS', '').split(',') if x.strip()}

ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0')) or None

WALLET_ENCRYPTION_KEY = os.getenv('WALLET_ENCRYPTION_KEY', '')


if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_TOKEN non trovato nel file .env")
    sys.exit(1)

logger.info(f"Config: POLL={POLL_INTERVAL}s THRESHOLD={PRICE_CHANGE_THRESHOLD}% DEX={SUPPORTED_DEXS}")
logger.info(f"Log file: {LOG_FILE.resolve()}")

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

monitor = HyperliquidPriceMonitor(MonitorConfig(
    api_url                = HYPERLIQUID_API,
    supported_dexs         = SUPPORTED_DEXS,
    price_change_threshold = PRICE_CHANGE_THRESHOLD,
    spike_threshold        = SPIKE_THRESHOLD,
    cond_dir               = COND_DIR,
    data_dir               = DATA_DIR,
))


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
        "/spread SYMBOL — bid/ask/spread e liquidità\n"
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
        "/stoploss SYM PX           — stop loss nativo su Hyperliquid\n"
        "/cancelsl SYM              — cancella stop loss nativo\n"
        "/takeprofit SYM PX [sz|%]  — take profit con trailing stop\n"
        "/orders                    — ordini condizionali attivi (TP)\n"
        "/cancelcond ID|all         — cancella ordine take profit\n"
        "/chart SYM [N] [TF]       — grafico candele (es: /chart GOLD 72 1H)\n"
        "/analyze SYM              — analisi tecnica AI daily+15m con setup suggerito\n"
        "/scan [SYM] [SCORE]       — scan VSA opportunità\n"
        "/scan daemon [SCORE]      — attiva scan automatico\n"
        "/scanstop                 — ferma scan automatico\n"
        "/scanstatus               — stato scanner\n"
        "/stats — statistiche bot\n"
        "/help — questo messaggio\n"
        f"\n📌 Il tuo chat\\_id: `{update.effective_chat.id}`"
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

    sub_msg = ""
    base = monitor.subscribe_base_prices.get(sym)
    if base:
        pct_s = (price_val - base) / base * 100
        arrow_s = "📈" if pct_s > 0 else "📉" if pct_s < 0 else "➡️"
        sub_msg = f"\n{arrow_s} Da subscribe ({base:,.6f}): {pct_s:+.2f}%"

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

async def spread_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Uso: /spread SIMBOLO  (es: /spread BTC, /spread GOLD)"
        )
        return

    symbol = context.args[0].upper()
    await update.message.reply_text(f"🔍 Recupero spread di {symbol}...")

    try:
        client = HyperliquidClient('0x0000000000000000000000000000000000000000')
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: client.get_spread(symbol)
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Errore API: {e}")
        return

    if not result.get('found'):
        await update.message.reply_text(
            f"❌ *{symbol}* non trovato su XYZ né su PERP standard.",
            parse_mode='Markdown'
        )
        return

    spread_pct = result['spread_pct']
    dex_label  = result['dex']

    if spread_pct < 0.05:
        liq_emoji = "🟢"
        liq_label = "Alta liquidità"
    elif spread_pct < 0.15:
        liq_emoji = "🟡"
        liq_label = "Liquidità media"
    else:
        liq_emoji = "🔴"
        liq_label = "Bassa liquidità"

    await update.message.reply_text(
        f"📊 *Spread {symbol}* — {dex_label}\n\n"
        f"Bid: ${_fmt(result['bid'])}\n"
        f"Ask: ${_fmt(result['ask'])}\n"
        f"Mid: ${_fmt(result['mid'])}\n"
        f"─────────────\n"
        f"Spread: ${_fmt(result['spread_abs'])}  ({spread_pct:.4f}%)\n"
        f"{liq_emoji} {liq_label}\n"
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
    monitor.last_prices[sym]       = price_val
    monitor.alert_base_prices[sym] = price_val
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

    all_prices = await monitor.fetch_all_prices()
    threshold  = monitor.get_threshold(chat_id)
    msg        = ""

    if tracks:
        msg += f"📊 *Position Tracking* (soglia: ±{threshold}%):\n\n"
        for coin, track in sorted(tracks.items()):
            price_info    = all_prices.get(coin)
            current_price = price_info[0] if price_info else None
            entry         = track['entry_px']
            base          = track['alert_base']
            is_long       = track['is_long']
            liq           = track['liq_px']
            cp          = current_price or entry
            pnl_rt      = (cp - entry) * track['size']
            pnl_s       = "+" if pnl_rt >= 0 else ""
            try:    _lev = float(str(track.get('leverage',1)).replace('x',''))
            except: _lev = 1
            _margin_i   = abs(entry * track['size'] / _lev) if _lev else track['margin'] or 1
            pnl_pct     = pnl_rt / _margin_i * 100 if _margin_i else 0

            arrow_dir = "📈" if is_long else "📉"
            dir_s     = "LONG" if is_long else "SHORT"

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
    query    = context.args[0].upper() if context.args else ''
    await update.message.reply_text("🔍 Recupero simboli...")
    all_syms  = await monitor.get_all_symbols()

    perps     = all_syms.get('perps', [])
    spots     = all_syms.get('spot', [])
    dex_perps = all_syms.get('dex_perps', {})

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
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return
    monitored = monitor.get_all_monitored_symbols()
    _utenti_noti = (
        set(monitor.subscribers.keys()) |
        monitor.tracking_enabled |
        monitor.spike_subscribers |
        set(monitor.conditional_orders.keys())
    )
    msg = (
        "📊 Statistiche Bot\n\n"
        f"👥 Utenti noti: {len(_utenti_noti)}\n"
        f"   📋 Subscribe: {len(monitor.subscribers)}\n"
        f"   🎯 Tracking:  {len(monitor.tracking_enabled)}\n"
        f"   ⚡ Spike:     {len(monitor.spike_subscribers)}\n"
        f"   📌 Cond.ord.: {len(monitor.conditional_orders)}\n"
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
# Timeframe validi (allineati con candle_chart.py)
# ---------------------------------------------------------------------------
VALID_TIMEFRAMES = {'15m', '1H', '1D'}

DEFAULT_BARS_PER_TF = {
    '15m': 120,
    '1H':  120,
    '1D':  120,
}


async def chart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "📊 *Chart candlestick multi-timeframe*\n\n"
            "Uso: `/chart SIMBOLO [BARRE] [TIMEFRAME]`\n\n"
            "Timeframe disponibili: `15m` (default) · `1H` · `1D`\n\n"
            "Esempi:\n"
            "  `/chart GOLD`          — 15m, ~30h\n"
            "  `/chart GOLD 200`      — 15m, 200 candele\n"
            "  `/chart GOLD 1H`       — orario, ~5 giorni\n"
            "  `/chart GOLD 72 1H`    — orario, 3 giorni\n"
            "  `/chart GOLD 1D`       — giornaliero, ~4 mesi\n"
            "  `/chart GOLD 60 1D`    — giornaliero, 2 mesi",
            parse_mode='Markdown'
        )
        return

    symbol    = context.args[0].upper()
    timeframe = '15m'
    bars      = None

    for arg in context.args[1:]:
        if arg.upper() in VALID_TIMEFRAMES:
            timeframe = arg.upper()
        else:
            try:
                bars = int(arg)
            except ValueError:
                await update.message.reply_text(
                    f"❌ Argomento non riconosciuto: `{arg}`\n"
                    f"Timeframe validi: `15m`, `1H`, `1D`",
                    parse_mode='Markdown'
                )
                return

    if bars is None:
        bars = DEFAULT_BARS_PER_TF[timeframe]

    max_bars = {'15m': 500, '1H': 500, '1D': 365}
    if bars < 10 or bars > max_bars[timeframe]:
        await update.message.reply_text(
            f"❌ Numero barre deve essere tra 10 e {max_bars[timeframe]} per timeframe {timeframe}."
        )
        return

    try:
        csv_path = cc.find_csv(symbol, Path(CANDLES_DIR))
    except FileNotFoundError:
        await update.message.reply_text(
            f"❌ Nessuna candela disponibile per *{symbol}*.\n"
            f"Il simbolo deve essere monitorato dal bot (presente in `data/candles/`).",
            parse_mode='Markdown'
        )
        return

    tf_desc = {'15m': '15 minuti', '1H': 'orario', '1D': 'giornaliero'}
    wait_msg = await update.message.reply_text(
        f"⏳ Generazione grafico *{symbol}* ({tf_desc[timeframe]}, {bars} barre)...",
        parse_mode='Markdown'
    )

    tmp_path = None
    try:
        data = cc.load_csv(csv_path, bars, timeframe)
        n    = len(data['closes'])

        if n < 10:
            await wait_msg.edit_text(
                f"❌ Dati insufficienti per *{symbol}* "
                f"({n} candele {timeframe} disponibili, minimo 10).",
                parse_mode='Markdown'
            )
            return

        fig = cc.plot_chart(data, symbol, timeframe)

        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            tmp_path = tmp.name

        fig.savefig(tmp_path, dpi=110, bbox_inches='tight', facecolor='#0d1117')
        plt.close(fig)

        tf_label_map = {'15m': '15m', '1H': '1H', '1D': '1D'}
        caption = (
            f"📊 *{symbol}* — {tf_label_map[timeframe]} — {n} candele\n"
            f"Ultimo: `{data['closes'][-1]:.6g}`"
        )

        with open(tmp_path, 'rb') as img:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=img,
                caption=caption,
                parse_mode='Markdown'
            )
        await wait_msg.delete()

    except Exception as e:
        logger.error(f"chart_command error {symbol} {timeframe}: {e}", exc_info=True)
        await wait_msg.edit_text(
            f"❌ Errore generando il grafico per *{symbol}*: {e}",
            parse_mode='Markdown'
        )
    finally:
        if tmp_path:
            try:
                import os
                os.unlink(tmp_path)
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Snapshot giornaliero prezzi
# ---------------------------------------------------------------------------

def get_yesterday_price(sym: str) -> Optional[float]:
    csv_path = PRICES_DIR / f"{sym}.csv"
    if not csv_path.exists():
        return None
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            rows = [r for r in csv.reader(f) if r and r[0] != 'date']
        today = date.today().isoformat()
        past = [r for r in rows if r[0] < today]
        if past:
            return float(past[-1][1])
    except Exception:
        pass
    return None

def save_daily_snapshot(prices: Dict[str, Tuple[float, str, Optional[str]]]) -> int:
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    today     = date.today().isoformat()
    written   = 0

    for sym, (price, mtype, dex) in prices.items():
        csv_path = PRICES_DIR / f"{sym}.csv"

        if csv_path.exists():
            try:
                with open(csv_path, 'r', encoding='utf-8') as f:
                    last_line = f.readlines()[-1].strip()
                if last_line.startswith(today):
                    continue
            except Exception:
                pass

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
    import aiohttp
    internal_name = f"{dex}:{symbol}" if dex else symbol

    import time
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - 90 * 60 * 1000

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
    if not candles:
        return 0

    sym_dir  = CANDLES_DIR / symbol
    sym_dir.mkdir(parents=True, exist_ok=True)
    csv_path = sym_dir / f"{symbol}_15m.csv"

    existing_rows = []
    existing_ts   = set()
    if csv_path.exists():
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                next(reader, None)
                for row in reader:
                    if row:
                        existing_rows.append(row)
                        existing_ts.add(row[0])
        except Exception:
            pass

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

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    args = context.args or []
    daemon_mode = False
    symbol      = None
    min_score   = monitor.scanner_min_score

    for arg in args:
        if arg.lower() == 'daemon':
            daemon_mode = True
        elif arg.isdigit():
            min_score = int(arg)
        else:
            symbol = arg.upper()

    recipients = list(monitor.spike_subscribers | monitor.tracking_enabled) or [chat_id]

    if daemon_mode:
        monitor.scanner_enabled   = True
        monitor.scanner_min_score = min_score
        monitor.scanner_chat_ids  = set(recipients)
        score_str = f" (soglia {min_score})" if min_score != 60 else ""
        await update.message.reply_text(
            f"🤖 *Scanner daemon attivato*{score_str}\n"
            f"Scan automatico dopo ogni chiusura candela 15m.\n"
            f"Alert inviati a {len(recipients)} utente/i.\n"
            f"Usa /scanstop per fermare.",
            parse_mode='Markdown'
        )
        return

    symbols = [symbol] if symbol else _mls.discover_symbols()
    if not symbols:
        await update.message.reply_text("❌ Nessun simbolo trovato.")
        return

    sym_str = symbol if symbol else f"tutti i simboli ({len(symbols)})"
    await update.message.reply_text(f"🔍 Scan in corso su {sym_str}...")

    live_prices, funding_map = await asyncio.gather(
        _mls.fetch_live_prices(),
        _mls._fetch_all_funding_rates(),
    )
    opportunities = []
    for sym in symbols:
        data = _mls.load_last_candles(sym, _mls.LOOKBACK)
        if data is None:
            continue
        lp   = live_prices.get(sym)
        fr   = funding_map.get(sym.upper())
        result = _mls.compute_opportunity(data, live_price=lp, funding_rate=fr)
        if result and result['score'] >= min_score:
            opportunities.append(result)

    opportunities.sort(key=lambda r: r['score'], reverse=True)

    if not opportunities:
        await update.message.reply_text(
            f"📭 Nessuna opportunità trovata (score < {min_score})."
        )
        return

    for r in opportunities:
        msg = _mls.format_alert(r)
        try:
            await update.message.reply_text(msg, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Errore invio alert scan: {e}")


async def scanstop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    if monitor.scanner_enabled:
        monitor.scanner_enabled = False
        monitor.scanner_chat_ids.clear()
        await update.message.reply_text("⏸ *Scanner daemon fermato.*", parse_mode='Markdown')
    else:
        await update.message.reply_text("ℹ️ Scanner daemon non era attivo.")


async def scanstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    stato   = "✅ ATTIVO" if monitor.scanner_enabled else "⏸ NON ATTIVO"
    score   = monitor.scanner_min_score
    n_users = len(monitor.scanner_chat_ids)
    last    = _mls._last_signals

    last_str = ""
    if last:
        recenti = sorted(last.items(), key=lambda x: x[1]['time'], reverse=True)[:5]
        last_str = "\n\nUltimi segnali:\n" + "\n".join(
            f"  {sym}: {v['direction']} score={v['score']} @ {v['time'].strftime('%H:%M')}"
            for sym, v in recenti
        )

    await update.message.reply_text(
        f"📡 *ML Scanner* — {stato}\n"
        f"Soglia score: {score}/100\n"
        f"Utenti: {n_users}\n"
        f"Simboli disponibili: {len(_mls.discover_symbols())}"
        f"{last_str}",
        parse_mode='Markdown'
    )


# ---------------------------------------------------------------------------
# Position tracking
# ---------------------------------------------------------------------------

async def trackpositions(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    return monitor.new_order_id()


def _cond_label(order: dict) -> str:
    is_sl   = order['type'] == 'stoploss'
    is_long = order.get('is_long', True)
    t       = "🛑 SL" if is_sl else "🎯 TP"
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
    chat_id = update.effective_chat.id
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "🛑 *Stop Loss Nativo*\n\n"
            "Uso: /stoploss SIMBOLO VALORE\n\n"
            "Il VALORE può essere:\n"
            "  `4600`   → prezzo esplicito\n"
            "  `2%`     → distanza % dall'entry\n"
            "  `50$`    → perdita massima in dollari\n\n"
            "L'ordine viene inserito direttamente su Hyperliquid.\n"
            "Rimane attivo anche se il bot è offline.\n\n"
            "Esempi:\n"
            "  /stoploss GOLD 4600\n"
            "  /stoploss GOLD 2%\n"
            "  /stoploss GOLD 50$",
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

    symbol = context.args[0].upper()
    raw    = context.args[1].replace(',', '.')

    # Recupera prezzo corrente e posizione (necessari per calcoli % e $)
    track        = monitor.position_tracks.get(chat_id, {}).get(symbol)
    price_result = await monitor.get_price(symbol)
    current_px   = price_result[0] if price_result else (track['entry_px'] if track else None)
    is_long      = track['is_long'] if track else True
    entry_px     = track['entry_px'] if track else current_px

    # ── Parsing del valore ──────────────────────────────────────────────────
    trigger_px  = None
    mode_label  = ""

    if raw.endswith('%'):
        # Distanza percentuale dall'entry
        if entry_px is None:
            await update.message.reply_text(
                "❌ Nessuna posizione trovata per calcolare la % — usa un prezzo esplicito.")
            return
        try:
            pct = float(raw[:-1])
            if pct <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Percentuale non valida.")
            return
        if is_long:
            trigger_px = entry_px * (1 - pct / 100)
        else:
            trigger_px = entry_px * (1 + pct / 100)
        mode_label = f"entry {_fmt(entry_px)} -{pct}%"

    elif raw.endswith('$'):
        # Perdita massima in dollari
        if entry_px is None or not track:
            await update.message.reply_text(
                "❌ Nessuna posizione trovata per calcolare il $ — usa un prezzo esplicito.")
            return
        try:
            loss_usd = float(raw[:-1])
            if loss_usd <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Importo $ non valido.")
            return
        size = abs(track['size'])
        if size == 0:
            await update.message.reply_text("❌ Size posizione zero, impossibile calcolare.")
            return
        delta_px = loss_usd / size
        if is_long:
            trigger_px = entry_px - delta_px
        else:
            trigger_px = entry_px + delta_px
        mode_label = f"entry {_fmt(entry_px)} -${loss_usd}"

    else:
        # Prezzo esplicito
        try:
            trigger_px = float(raw)
        except ValueError:
            await update.message.reply_text(
                "❌ Valore non valido. Usa un prezzo (es. `4600`), una % (es. `2%`) "
                "o un importo $ (es. `50$`).",
                parse_mode='Markdown')
            return
        mode_label = f"prezzo esplicito"

    # ── Validazione direzione ───────────────────────────────────────────────
    if current_px:
        if is_long and trigger_px >= current_px:
            await update.message.reply_text(
                f"❌ Stop Loss LONG deve essere *sotto* il prezzo attuale\n"
                f"Calcolato: ${_fmt(trigger_px)}  Attuale: ${_fmt(current_px)}",
                parse_mode='Markdown')
            return
        if not is_long and trigger_px <= current_px:
            await update.message.reply_text(
                f"❌ Stop Loss SHORT deve essere *sopra* il prezzo attuale\n"
                f"Calcolato: ${_fmt(trigger_px)}  Attuale: ${_fmt(current_px)}",
                parse_mode='Markdown')
            return

    dex      = await _detect_dex(symbol)
    market_s = f"_{dex.upper()}_" if dex else "PERP"

    await update.message.reply_text(
        f"⏳ Inserimento Stop Loss nativo su Hyperliquid...\n"
        f"Trigger calcolato: ${_fmt(trigger_px)} ({mode_label})"
    )

    try:
        client = HyperliquidClient(addr, private_key=key)
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: client.set_stop_loss_native(symbol, trigger_px, dex)
        )
        logger.info(f"SL NATIVE {symbol} trigger=${trigger_px} ({mode_label}) chat {chat_id}: {result}")

        if isinstance(result, dict) and result.get('status') == 'err':
            await update.message.reply_text(
                f"❌ Errore exchange: {result.get('response', result)}")
            return

        size      = result.get('_size', '?')
        direction = "LONG" if result.get('_is_long') else "SHORT"
        oid       = result.get('_oid')

        if oid is not None:
            monitor.native_sl_orders.setdefault(chat_id, {})[symbol] = {
                'oid':        oid,
                'dex':        dex,
                'trigger_px': trigger_px,
                'is_long':    result.get('_is_long', True),
            }
            monitor.save_native_sl_orders(chat_id)
            oid_note = f"\noid: `{oid}` (usa /cancelsl {symbol} per rimuovere)"
        else:
            oid_note = "\n⚠️ oid non ricevuto — per cancellare usa la UI Hyperliquid"

        await update.message.reply_text(
            f"✅ *Stop Loss nativo inserito*\n\n"
            f"🛑 SL *{symbol}* {market_s} ({direction})\n"
            f"Trigger: ${_fmt(trigger_px)}  _({mode_label})_\n"
            f"Size: {size}\n"
            f"⚙️ Gestito da Hyperliquid — attivo anche offline"
            f"{oid_note}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Errore set_stop_loss_native {symbol} chat {chat_id}: {e}")
        await update.message.reply_text(f"❌ Errore: {e}")

async def cancelsl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    if not context.args:
        sl_map = monitor.native_sl_orders.get(chat_id, {})
        if not sl_map:
            await update.message.reply_text(
                "ℹ️ Nessun Stop Loss nativo attivo registrato dal bot.\n\n"
                "Nota: SL inseriti direttamente da UI Hyperliquid non sono visibili qui.",
                parse_mode='Markdown'
            )
            return
        lines = []
        for sym, info in sl_map.items():
            mkt   = f"_{info['dex'].upper()}_" if info['dex'] else "PERP"
            dir_s = "L" if info['is_long'] else "S"
            lines.append(f"🛑 *{sym}* {mkt} ({dir_s}) trigger ${_fmt(info['trigger_px'])}  oid `{info['oid']}`")
        await update.message.reply_text(
            "🛑 *Stop Loss nativi attivi*\n\n" + "\n".join(lines) +
            "\n\nUsa /cancelsl SIMBOLO per cancellarne uno.",
            parse_mode='Markdown'
        )
        return

    addr = wallet_store.get_address(chat_id)
    key  = wallet_store.get_key(chat_id)
    if not addr:
        await update.message.reply_text("❌ Imposta prima /setaddress")
        return
    if not key:
        await update.message.reply_text("❌ Imposta prima /setkey")
        return

    symbol = context.args[0].upper()
    sl_map = monitor.native_sl_orders.get(chat_id, {})
    sl_info = sl_map.get(symbol)

    if not sl_info:
        await update.message.reply_text(
            f"❌ Nessun Stop Loss nativo registrato per *{symbol}*.\n\n"
            f"Se l'hai inserito da un'altra sessione o dalla UI, cancellalo direttamente su Hyperliquid.",
            parse_mode='Markdown'
        )
        return

    oid = sl_info['oid']
    dex = sl_info['dex']
    market_s = f"_{dex.upper()}_" if dex else "PERP"

    await update.message.reply_text(f"⏳ Cancellazione Stop Loss *{symbol}* {market_s}...", parse_mode='Markdown')

    try:
        client = HyperliquidClient(addr, private_key=key)
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: client.cancel_stop_loss_native(symbol, oid, dex)
        )
        logger.info(f"CANCEL SL NATIVE {symbol} oid={oid} chat {chat_id}: {result}")

        if isinstance(result, dict) and result.get('status') == 'err':
            await update.message.reply_text(
                f"❌ Errore exchange: {result.get('response', result)}")
            return

        sl_map.pop(symbol, None)
        monitor.save_native_sl_orders(chat_id)

        await update.message.reply_text(
            f"✅ *Stop Loss cancellato*\n\n"
            f"🛑 SL *{symbol}* {market_s} rimosso dall'exchange\n"
            f"Trigger era: ${_fmt(sl_info['trigger_px'])}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Errore cancel_stop_loss_native {symbol} oid={oid} chat {chat_id}: {e}")
        await update.message.reply_text(f"❌ Errore: {e}")

async def takeprofit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_conditional(update, context, 'takeprofit')



def _resolve_trigger(raw: str, ctype: str, is_long: bool,
                     entry_px: float, size_contracts: float) -> tuple:
    """
    Converte l'argomento trigger in prezzo assoluto.

    Formati accettati:
      "5600"    → prezzo assoluto (comportamento storico)
      "1.3%"    → % sul prezzo di carico  → entry_px ± 1.3%
      "12$"     → delta P&L in USD        → entry_px ± (12 / abs(size_contracts))

    Segno automatico:
      TP LONG  → +  (trigger sopra entry)
      TP SHORT → -  (trigger sotto entry)
      SL LONG  → -  (trigger sotto entry)
      SL SHORT → +  (trigger sopra entry)

    Ritorna (trigger_px: float, origin_desc: str) oppure
    solleva ValueError se il formato non è riconosciuto.
    """
    raw = raw.strip().replace(',', '.')

    # Determina se il trigger deve essere sopra (+1) o sotto (-1) l'entry
    if ctype == 'takeprofit':
        sign = +1 if is_long else -1
    else:  # stoploss
        sign = -1 if is_long else +1

    # --- Percentuale sul prezzo di carico ---
    if raw.endswith('%'):
        pct_val = float(raw[:-1])
        if pct_val <= 0:
            raise ValueError("La percentuale deve essere positiva")
        delta      = entry_px * pct_val / 100
        trigger_px = round(entry_px + sign * delta, 8)
        desc       = f"{pct_val}% su entry ${_fmt(entry_px)}"
        return trigger_px, desc

    # --- Delta P&L in dollari ---
    if raw.endswith('$'):
        usd_val = float(raw[:-1])
        if usd_val <= 0:
            raise ValueError("Il valore in $ deve essere positivo")
        if abs(size_contracts) < 1e-12:
            raise ValueError("Size posizione zero, impossibile calcolare il trigger")
        delta      = usd_val / abs(size_contracts)
        trigger_px = round(entry_px + sign * delta, 8)
        desc       = f"${usd_val} P&L su entry ${_fmt(entry_px)}"
        return trigger_px, desc

    # --- Prezzo assoluto (default) ---
    trigger_px = float(raw)
    return trigger_px, None


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
            f"Uso: /{ctype} SIMBOLO TRIGGER [size|%]\n\n"
            f"TRIGGER può essere:\n"
            f"  `5600`    — prezzo assoluto\n"
            f"  `1.3%`    — % sul prezzo di carico\n"
            f"  `12$`     — delta P&L in dollari\n\n"
            f"Esempi:\n"
            f"  /{ctype} GOLD 5100       — prezzo assoluto\n"
            f"  /{ctype} GOLD 1.5%       — 1.5% dal carico\n"
            f"  /{ctype} GOLD 15$        — $15 di guadagno/perdita\n"
            f"  /{ctype} GOLD 1.5% 50%  — 1.5% dal carico, chiudi 50%",
            parse_mode='Markdown'
        )
        return

    symbol = context.args[0].upper()
    trigger_raw = context.args[1]

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

    track = monitor.position_tracks.get(chat_id, {}).get(symbol)
    if not track:
        await update.message.reply_text(
            f"❌ Nessuna posizione aperta su *{symbol}*.\n"
            f"Apri prima una posizione con /long o /short.",
            parse_mode='Markdown'
        )
        return

    is_long      = track['is_long']
    price_result = await monitor.get_price(symbol)
    current_px   = price_result[0] if price_result else monitor.last_prices.get(symbol, track['entry_px'])

    # Risolvi trigger (assoluto / % / $)
    trigger_origin = None
    try:
        trigger_px, trigger_origin = _resolve_trigger(
            trigger_raw, ctype, is_long,
            entry_px=track['entry_px'],
            size_contracts=track['size'],
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ Trigger non valido: {e}")
        return

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
    else:
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
        'is_long':          is_long,
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

    if ctype == 'stoploss':
        sym_arrow = "🔽 ≤" if is_long else "🔼 ≥"
    else:
        sym_arrow = "🔼 ≥" if is_long else "🔽 ≤"

    origin_line = f"\n📐 Calcolato da: {trigger_origin}" if trigger_origin else ""
    await update.message.reply_text(
        f"✅ *{label} impostato* — `{oid}`{upd_note}\n\n"
        f"{'📈' if is_long else '📉'} {dir_s} *{symbol}* {market_s}\n"
        f"{sym_arrow} ${_fmt(trigger_px)}{size_s}\n"
        f"Entry: ${_fmt(track['entry_px'])}{origin_line}\n"
        f"Prezzo attuale: ${_fmt(current_px)}\n\n"
        f"Usa /orders per vedere gli ordini\n"
        f"/cancelcond {oid} per cancellare",
        parse_mode='Markdown'
    )


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    monitor.pending_orders[chat_id] = {
        'symbol':     symbol,
        'dex':        dex,
        'is_buy':     is_buy,
        'usd':        usd,
        'price':      price,
        'size':       size,
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

    tracks = monitor.position_tracks.get(chat_id, {})
    pos    = tracks.get(symbol)
    if not pos:
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
        'is_buy':     None,
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
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.market_close(
                    symbol, dex,
                    usd_amount=order.get('usd'),
                    pct=order.get('pct')
                )
            )
            direction = "🔴 CLOSE"
        else:
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.market_open(
                    symbol, order['is_buy'], order['usd'], dex
                )
            )
            direction = "📈 LONG" if order['is_buy'] else "📉 SHORT"

        logger.info(f"ORDER {direction} {symbol} chat {chat_id}: {result}")

        if isinstance(result, dict) and result.get('status') == 'err':
            await update.message.reply_text(f"❌ Errore exchange: {result.get('response', result)}")
            return

        # --- Journal log_open ---
        try:
            fill_price = result.get('_price') or order.get('price')
            fill_usd   = result.get('_usd')   or order.get('usd', 0.0)
            if order['is_buy'] is not None and fill_price:
                _ml_score, _ml_signal = None, None
                try:
                    _scores = _mls.get_latest_scores()
                    if symbol in _scores:
                        _ml_score  = _scores[symbol].get('score')
                        _ml_signal = _scores[symbol].get('signal')
                except Exception:
                    pass
                ml_journal.log_open(
                    symbol=symbol,
                    direction="long" if order['is_buy'] else "short",
                    entry_price=fill_price,
                    size_usd=fill_usd,
                    ml_score=_ml_score,
                    ml_signal=_ml_signal,
                    candles_dir=CANDLES_DIR,
                    chat_id=chat_id,
                )
        except Exception as _je:
            logger.warning(f"ml_journal log_open error: {_je}")

        market_s = f"_{dex.upper()}_" if dex else "PERP"
        size     = result.get('_size', '?')
        price    = result.get('_price')
        usd      = result.get('_usd') or order.get('usd')

        price_line = f"Prezzo: ${_fmt(price)}\n" if isinstance(price, float) else ""
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
    chat_id = update.effective_chat.id
    if monitor.pending_orders.pop(chat_id, None):
        await update.message.reply_text("🚫 Ordine annullato.")
    else:
        await update.message.reply_text("ℹ️ Nessun ordine in attesa.")


async def order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce i bottoni inline Conferma / Annulla degli ordini."""
    query   = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()

    if query.data == "order_confirm":
        await query.edit_message_reply_markup(reply_markup=None)
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

            # --- Fetch immediato posizioni per aggiornare position_tracks ---
            try:
                _pos_list = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: client.get_positions(extra_dexs=SUPPORTED_DEXS)
                )
                _current = monitor.position_tracks.get(chat_id, {})
                _new_tracks = {}
                for _p in _pos_list:
                    _coin = _p['coin']
                    if _coin in _current:
                        _t = _current[_coin].copy()
                        _t.update({
                            'entry_px':   _p['entry_px'],
                            'size':       _p['size'],
                            'margin':     _p['margin'],
                            'leverage':   _p['leverage'],
                            'unrealized': _p['unrealized'],
                            'liq_px':     _p['liq_px'],
                            'is_long':    _p['is_long'],
                        })
                        _new_tracks[_coin] = _t
                    else:
                        _new_tracks[_coin] = {
                            'entry_px':   _p['entry_px'],
                            'is_long':    _p['is_long'],
                            'size':       _p['size'],
                            'margin':     _p['margin'],
                            'leverage':   _p['leverage'],
                            'dex':        _p.get('dex', 'PERP'),
                            'unrealized': _p['unrealized'],
                            'liq_px':     _p['liq_px'],
                            'alert_base': monitor.last_prices.get(_p['coin'], _p['entry_px']),
                            'added_at':   datetime.now(),
                        }
                monitor.position_tracks[chat_id] = _new_tracks
                logger.info(f"ORDER (btn) position_tracks aggiornato immediatamente: {list(_new_tracks.keys())} chat {chat_id}")
            except Exception as _pe:
                logger.warning(f"ORDER (btn) fetch immediato posizioni fallito chat {chat_id}: {_pe}")

            # --- Journal log_open ---
            try:
                fill_price = result.get('_price') or order.get('price')
                fill_usd   = result.get('_usd')   or order.get('usd', 0.0)
                if order['is_buy'] is not None and fill_price:
                    _ml_score, _ml_signal = None, None
                    try:
                        _scores = _mls.get_latest_scores()
                        if symbol in _scores:
                            _ml_score  = _scores[symbol].get('score')
                            _ml_signal = _scores[symbol].get('signal')
                    except Exception:
                        pass
                    ml_journal.log_open(
                        symbol=symbol,
                        direction="long" if order['is_buy'] else "short",
                        entry_price=fill_price,
                        size_usd=fill_usd,
                        ml_score=_ml_score,
                        ml_signal=_ml_signal,
                        candles_dir=CANDLES_DIR,
                        chat_id=chat_id,
                    )
            except Exception as _je:
                logger.warning(f"ml_journal log_open error: {_je}")

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
    data  = query.data

    parts  = data.split('_')
    action = parts[1]
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
        order['snoozed_until']    = datetime.now().timestamp() + CONDITIONAL_SNOOZE_SECS
        order['alert_message_id'] = None
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

    extra_args = [a.lower() for a in context.args[2:]]
    is_cross   = 'cross' in extra_args
    force_xyz  = 'xyz' in extra_args
    margin_s   = "cross" if is_cross else "isolated"

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
    return _is_admin(chat_id) or not WALLET_ALLOWED_CHATS or chat_id in WALLET_ALLOWED_CHATS

def _is_admin(chat_id: int) -> bool:
    return ADMIN_CHAT_ID is not None and chat_id == ADMIN_CHAT_ID

async def setaddress(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    monitor.tracking_enabled.add(chat_id)
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
    chat_id = update.effective_chat.id
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    addr    = wallet_store.get_address(chat_id)
    has_key = wallet_store.has_key(chat_id)
    addr_str = f"`{addr[:6]}...{addr[-4:]}`" if addr else "❌ non impostato"

    msg = (
        f"🔐 *Wallet Info*\n\n"
        f"📍 Address: {addr_str}\n"
        f"🔑 Chiave API: {'✅ impostata' if has_key else '❌ non impostata'}\n"
    )

    if addr:
        await update.message.reply_text("⏳ Recupero saldo...")
        try:
            client  = HyperliquidClient(addr)
            summary = await asyncio.get_event_loop().run_in_executor(
                None, client.get_account_summary
            )
            equity    = summary['account_value']
            margin    = summary['total_margin']
            logger.info(f"DIAG /walletinfo chat={chat_id} addr={addr[:10]}... equity={equity:.2f} margin={margin:.2f}")
            available = max(equity - margin, 0)
            ntl_perp  = summary['total_ntl_pos']

            ntl_xyz = 0.0
            tracks  = monitor.position_tracks.get(chat_id, {})
            all_px  = await monitor.fetch_all_prices() if tracks else {}
            for coin, track in tracks.items():
                if not track.get('dex'):
                    continue
                price_info = all_px.get(coin)
                if price_info:
                    ntl_xyz += abs(track['size'] * price_info[0])

            ntl_tot = ntl_perp + ntl_xyz

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
        logger.info(f"DIAG /positions chat={chat_id} addr={addr[:10]}... found={len(pos_list)} supported_dexs={SUPPORTED_DEXS}")
        for p in pos_list:
            logger.info(f"  DIAG POS: coin={p['coin']} {'L' if p['is_long'] else 'S'} size={p['size']} dex={p.get('dex','PERP')} entry={p['entry_px']}")
        summary = await asyncio.get_event_loop().run_in_executor(
            None, client.get_account_summary
        )
        logger.info(f"  DIAG SUMMARY: equity={summary.get('account_value',0):.2f} margin={summary.get('total_margin',0):.2f}")
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
# Lifecycle
# ---------------------------------------------------------------------------

async def post_init(application: Application):
    logger.info(f"Bot avviato. POLL={POLL_INTERVAL}s THRESHOLD={PRICE_CHANGE_THRESHOLD}%")

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

    monitor.load_all_conditional_orders()
    total_conds = sum(len(v) for v in monitor.conditional_orders.values())
    if total_conds:
        logger.info(f"Ordini condizionali caricati: {total_conds}")

    ctx = TaskContext(
        monitor=monitor,
        wallet_store=wallet_store,
        constants={
            'POLL_INTERVAL':               POLL_INTERVAL,
            'PRICE_CHANGE_THRESHOLD':      PRICE_CHANGE_THRESHOLD,
            'POSITION_TRACK_INTERVAL':     POSITION_TRACK_INTERVAL,
            'CANDLES_INTERVAL_SECS':       CANDLES_INTERVAL_SECS,
            'PRICES_TIME':                 PRICES_TIME,
            'PRICES_DIR':                  PRICES_DIR,
            'CANDLES_DIR':                 CANDLES_DIR,
            'SPIKE_EXTRA_SYMBOLS':         SPIKE_EXTRA_SYMBOLS,
            'SPIKE_EXCLUDE_SYMBOLS':       SPIKE_EXCLUDE_SYMBOLS,
            'CONDITIONAL_RENOTIFY_SECS':   CONDITIONAL_RENOTIFY_SECS,
            'CONDITIONAL_SNOOZE_SECS':     CONDITIONAL_SNOOZE_SECS,
            'CONDITIONAL_TP_TRAILING_PCT': CONDITIONAL_TP_TRAILING_PCT,
            'SUPPORTED_DEXS':              SUPPORTED_DEXS,
        },
        helpers={
            'market_label':        market_label,
            'save_daily_snapshot': save_daily_snapshot,
            'fetch_candles_15m':   fetch_candles_15m,
            'save_candles':        save_candles,
            'get_yesterday_price': get_yesterday_price,
            '_fmt':                _fmt,
            '_mls':                _mls,
        }
    )
    init_tasks(ctx)

    asyncio.create_task(price_polling_task(application))
    asyncio.create_task(position_tracking_task(application))
    asyncio.create_task(candle_task(application))

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/analyze SIMBOLO — Analisi tecnica AI con chart daily + 15m."""
    chat_id = update.effective_chat.id

    if ANALYZE_ALLOWED_CHATS and chat_id not in ANALYZE_ALLOWED_CHATS:
        await update.message.reply_text("⛔ Non sei autorizzato a usare /analyze.")
        return

    if not ANTHROPIC_API_KEY:
        await update.message.reply_text("❌ ANTHROPIC_API_KEY non configurata nel .env")
        return

    if not context.args:
        await update.message.reply_text(
            "📊 Uso: /analyze SIMBOLO\nEsempio: /analyze SOL\n\n"
            "Genera analisi tecnica AI con chart daily + 15m e setup suggerito."
        )
        return

    symbol = context.args[0].upper()
    thinking_msg = await update.message.reply_text(
        f"🔍 Analisi {symbol} in corso... (~15s)"
    )

    ml_score, ml_signal = None, None
    try:
        scores = _mls.get_latest_scores()
        if symbol in scores:
            ml_score  = scores[symbol].get('score')
            ml_signal = scores[symbol].get('signal')
    except Exception:
        pass

    analysis_text, chart_paths = await ml_analyst.analyze_symbol(
        symbol=symbol,
        ml_score=ml_score,
        ml_signal=ml_signal,
        candles_dir=CANDLES_DIR,
        anthropic_api_key=ANTHROPIC_API_KEY
    )

    try:
        await thinking_msg.delete()
    except Exception:
        pass

    if chart_paths:
        from telegram import InputMediaPhoto
        media = []
        for i, path in enumerate(chart_paths):
            try:
                with open(path, 'rb') as f:
                    img_bytes = f.read()
                caption = analysis_text if i == len(chart_paths) - 1 else None
                media.append(InputMediaPhoto(media=img_bytes, caption=caption))
            except Exception as e:
                logger.error(f"Errore lettura chart {path}: {e}")
        if media:
            try:
                await update.message.reply_media_group(media=media)
            except Exception as e:
                logger.error(f"Errore invio media group: {e}")
                await update.message.reply_text(analysis_text)
        else:
            await update.message.reply_text(analysis_text)
        ml_analyst.cleanup_charts(chart_paths)
    else:
        await update.message.reply_text(analysis_text)

async def message_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Solo admin: /message all TESTO  oppure  /message <chat_id> TESTO"""
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("❌ Comando riservato all'admin.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "ℹ️ Sintassi:\n"
            "  /message all <testo>          → broadcast a tutti gli utenti noti\n"
            "  /message <chat_id> <testo>    → messaggio a una chat specifica"
        )
        return

    target = context.args[0]
    text   = " ".join(context.args[1:])

    known_users: set[int] = (
        monitor.spike_subscribers |
        monitor.tracking_enabled |
        set(monitor.conditional_orders.keys())
    )

    if target.lower() == "all":
        if not known_users:
            await update.message.reply_text("⚠️ Nessun utente noto a cui inviare il messaggio.")
            return
        ok, fail = 0, 0
        for uid in known_users:
            try:
                await context.bot.send_message(chat_id=uid, text=text)
                ok += 1
            except Exception as e:
                logger.warning(f"message_command: impossibile inviare a {uid}: {e}")
                fail += 1
        await update.message.reply_text(
            f"✅ Broadcast completato: {ok} inviati, {fail} falliti."
        )
    else:
        try:
            target_id = int(target)
        except ValueError:
            await update.message.reply_text("❌ chat_id non valido. Usa un numero o 'all'.")
            return
        try:
            await context.bot.send_message(chat_id=target_id, text=text)
            await update.message.reply_text(f"✅ Messaggio inviato a {target_id}.")
        except Exception as e:
            logger.warning(f"message_command: impossibile inviare a {target_id}: {e}")
            await update.message.reply_text(f"❌ Errore invio a {target_id}: {e}")


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
    app.add_handler(CommandHandler("spread",      spread_command))
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
    app.add_handler(CommandHandler("scan",         scan_command))
    app.add_handler(CommandHandler("analyze",      analyze_command))
    app.add_handler(CommandHandler("scanstop",     scanstop_command))
    app.add_handler(CommandHandler("scanstatus",   scanstatus_command))
    app.add_handler(CommandHandler("cancelsl", cancelsl_command))
    app.add_handler(CommandHandler("message",  message_command))

    app.post_init     = post_init
    app.post_shutdown = post_shutdown

    logger.info("🤖 Bot in esecuzione!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
