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

# Carica file .env specifico
# Puoi passare il nome del file come argomento: python hyperliquid_bot.py miobot.env
if len(sys.argv) > 1:
    env_file = sys.argv[1]
else:
    env_file = 'miobot.env'  # Default

env_path = Path('.') / env_file

if not env_path.exists():
    print(f"❌ Errore: File {env_file} non trovato!")
    print(f"Crea il file {env_file} con le tue configurazioni.")
    sys.exit(1)

load_dotenv(dotenv_path=env_path)
print(f"✅ Caricato file di configurazione: {env_file}")

# Configurazione logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Variabili di configurazione da .env
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
HYPERLIQUID_API = os.getenv('HYPERLIQUID_API', 'https://api.hyperliquid.xyz/info')
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL', '10'))
PRICE_CHANGE_THRESHOLD = float(os.getenv('PRICE_CHANGE_THRESHOLD', '0.5'))
MAX_SYMBOLS_DISPLAY = int(os.getenv('MAX_SYMBOLS_DISPLAY', '20'))

# Validazione configurazione
if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_TOKEN non trovato nel file .env")
    sys.exit(1)

logger.info(f"Configurazione caricata da: {env_file}")

class HyperliquidPriceMonitor:
    def __init__(self):
        self.subscribers: Dict[int, Set[str]] = {}  # chat_id -> set di simboli
        self.last_prices: Dict[str, float] = {}
        self.session: aiohttp.ClientSession = None
        self.spot_tokens_cache: Dict[str, dict] = {}  # Cache per i token spot
        
    async def init_session(self):
        """Inizializza la sessione HTTP"""
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)
            logger.info("Sessione HTTP inizializzata")
    
    async def close_session(self):
        """Chiude la sessione HTTP"""
        if self.session:
            await self.session.close()
            logger.info("Sessione HTTP chiusa")
    
    async def get_spot_meta(self) -> dict:
        """Recupera i metadata degli spot tokens"""
        try:
            await self.init_session()
            
            payload = {"type": "spotMeta"}
            
            async with self.session.post(HYPERLIQUID_API, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    # Costruisce un dizionario name -> token info
                    spot_meta = {}
                    for token in data.get('tokens', []):
                        name = token.get('name', '')
                        spot_meta[name] = token
                    return spot_meta
                else:
                    logger.error(f"Errore nel recupero spot meta: {response.status}")
                    return {}
        except Exception as e:
            logger.error(f"Errore nel recupero spot meta: {e}")
            return {}
    
    async def get_price(self, symbol: str) -> Optional[Tuple[float, str]]:
        """
        Recupera il prezzo di un simbolo da Hyperliquid
        Ritorna: (prezzo, tipo, dex) dove tipo = 'PERP' o 'SPOT', dex = nome del dex
        """
        try:
            await self.init_session()
            
            # Prova prima con i perpetuals standard
            payload = {"type": "metaAndAssetCtxs"}
            
            async with self.session.post(HYPERLIQUID_API, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Cerca il simbolo nei perpetuals standard
                    for i, asset in enumerate(data[0]['universe']):
                        if asset['name'] == symbol:
                            price = float(data[1][i]['markPx'])
                            return (price, 'PERP', None)
                    
                    logger.debug(f"{symbol} non trovato nei perpetuals standard, cerco nei DEX...")
            
            # Prova con perpDexs per i PERP xyz, flx, vntl, ecc.
            payload_dexs = {"type": "perpDexs"}
            async with self.session.post(HYPERLIQUID_API, json=payload_dexs) as resp_dexs:
                if resp_dexs.status == 200:
                    dexs_data = await resp_dexs.json()
                    
                    # Salta il primo elemento (null) e itera sui DEX
                    for dex in dexs_data[1:]:
                        if dex is None:
                            continue
                        
                        dex_name = dex.get('name', '')
                        
                        # Cerca il simbolo con prefisso (es: xyz:MSTR)
                        full_symbol = f"{dex_name}:{symbol}"
                        
                        # Verifica se il simbolo esiste in questo DEX
                        assets = dex.get('assetToStreamingOiCap', [])
                        symbol_exists = any(asset[0] == full_symbol for asset in assets)
                        
                        if symbol_exists:
                            # Ottieni il prezzo da allMids
                            payload_mids = {"type": "allMids"}
                            async with self.session.post(HYPERLIQUID_API, json=payload_mids) as resp_mids:
                                if resp_mids.status == 200:
                                    mids_data = await resp_mids.json()
                                    perp_mids = mids_data.get('mids', {})
                                    
                                    if full_symbol in perp_mids:
                                        price = float(perp_mids[full_symbol])
                                        return (price, 'PERP', dex_name.upper())
            
            # Se non trovato nei PERP, cerca negli spot
            payload_mids = {"type": "allMids"}
            async with self.session.post(HYPERLIQUID_API, json=payload_mids) as resp_mids:
                if resp_mids.status == 200:
                    mids_data = await resp_mids.json()
                    spot_mids = mids_data.get('spotMids', {})
                    
                    # Prova con il nome diretto
                    if symbol in spot_mids:
                        price = float(spot_mids[symbol])
                        return (price, 'SPOT', None)
                    
                    # Prova con /USDC
                    spot_symbol = f"{symbol}/USDC"
                    if spot_symbol in spot_mids:
                        price = float(spot_mids[spot_symbol])
                        return (price, 'SPOT', None)
            
            logger.warning(f"Simbolo {symbol} non trovato")
            return None
                    
        except asyncio.TimeoutError:
            logger.error(f"Timeout nel recupero prezzo per {symbol}")
            return None
        except Exception as e:
            logger.error(f"Errore nel recupero prezzo per {symbol}: {e}")
            return None
    
    async def get_all_symbols(self) -> dict:
        """
        Recupera tutti i simboli disponibili
        Ritorna: {'perps': [...], 'dex_perps': {dex_name: [...]}, 'spot': [...]}
        """
        try:
            await self.init_session()
            
            symbols = {'perps': [], 'dex_perps': {}, 'spot': []}
            
            # Recupera perpetuals standard
            payload = {"type": "metaAndAssetCtxs"}
            async with self.session.post(HYPERLIQUID_API, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    symbols['perps'] = [asset['name'] for asset in data[0]['universe']]
            
            # Recupera i PERP dai vari DEX (xyz, flx, vntl, ecc.)
            payload_dexs = {"type": "perpDexs"}
            async with self.session.post(HYPERLIQUID_API, json=payload_dexs) as resp_dexs:
                if resp_dexs.status == 200:
                    dexs_data = await resp_dexs.json()
                    
                    # Salta il primo elemento (null) e itera sui DEX
                    for dex in dexs_data[1:]:
                        if dex is None:
                            continue
                        
                        dex_name = dex.get('name', '').upper()
                        full_name = dex.get('fullName', dex_name)
                        
                        # Estrai i simboli da assetToStreamingOiCap
                        assets = dex.get('assetToStreamingOiCap', [])
                        dex_symbols = []
                        
                        for asset in assets:
                            if len(asset) >= 1:
                                # asset[0] è nel formato "xyz:MSTR"
                                full_symbol = asset[0]
                                # Estrai solo il simbolo senza prefisso
                                if ':' in full_symbol:
                                    symbol_only = full_symbol.split(':', 1)[1]
                                    dex_symbols.append(symbol_only)
                        
                        if dex_symbols:
                            symbols['dex_perps'][dex_name] = {
                                'name': full_name,
                                'symbols': dex_symbols
                            }
            
            # Recupera spot tokens
            payload_mids = {"type": "allMids"}
            async with self.session.post(HYPERLIQUID_API, json=payload_mids) as resp_mids:
                if resp_mids.status == 200:
                    mids_data = await resp_mids.json()
                    spot_mids = mids_data.get('spotMids', {})
                    symbols['spot'] = list(spot_mids.keys())
            
            return symbols
                    
        except Exception as e:
            logger.error(f"Errore nel recupero simboli: {e}")
            return {'perps': [], 'dex_perps': {}, 'spot': []}
    
    def add_subscriber(self, chat_id: int, symbol: str):
        """Aggiunge un subscriber per un simbolo"""
        if chat_id not in self.subscribers:
            self.subscribers[chat_id] = set()
        self.subscribers[chat_id].add(symbol.upper())
        logger.info(f"Chat {chat_id} sottoscritta a {symbol}")
    
    def remove_subscriber(self, chat_id: int, symbol: str):
        """Rimuove un subscriber per un simbolo"""
        if chat_id in self.subscribers:
            self.subscribers[chat_id].discard(symbol.upper())
            if not self.subscribers[chat_id]:
                del self.subscribers[chat_id]
            logger.info(f"Chat {chat_id} non più sottoscritta a {symbol}")
    
    def get_subscriptions(self, chat_id: int) -> Set[str]:
        """Restituisce i simboli sottoscritti da una chat"""
        return self.subscribers.get(chat_id, set())
    
    def get_all_monitored_symbols(self) -> Set[str]:
        """Restituisce tutti i simboli monitorati"""
        all_symbols = set()
        for symbols in self.subscribers.values():
            all_symbols.update(symbols)
        return all_symbols

# Istanza globale del monitor
monitor = HyperliquidPriceMonitor()

# Comandi del bot
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    welcome_msg = (
        "🤖 *Bot Hyperliquid Price Monitor*\n\n"
        "Monitora:\n"
        "⚡ Perpetuals standard\n"
        "🔥 Perpetuals DEX (XYZ, FLX, VNTL, ecc.)\n"
        "💎 Spot tokens\n\n"
        "Comandi disponibili:\n"
        "/price SYMBOL - Ottieni il prezzo corrente\n"
        "/subscribe SYMBOL - Monitora un simbolo\n"
        "/unsubscribe SYMBOL - Smetti di monitorare\n"
        "/list - Mostra le tue sottoscrizioni\n"
        "/symbols - Mostra simboli disponibili\n"
        "/stats - Statistiche del bot\n"
        "/help - Mostra questo messaggio\n"
    )
    await update.message.reply_text(welcome_msg, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /help"""
    await start(update, context)

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /price SYMBOL"""
    if not context.args:
        await update.message.reply_text("Uso: /price SYMBOL (es: /price BTC, /price MSTR, /price PURR)")
        return
    
    symbol = context.args[0].upper()
    await update.message.reply_text(f"🔍 Recupero prezzo di {symbol}...")
    
    result = await monitor.get_price(symbol)
    
    if result:
        price, market_type, dex_name = result
        
        # Emoji per tipo di mercato
        if market_type == 'PERP':
            if dex_name:
                type_emoji = "🔥"
                type_label = f"PERP ({dex_name})"
            else:
                type_emoji = "⚡"
                type_label = "PERP"
        else:  # SPOT
            type_emoji = "💎"
            type_label = "SPOT"
        
        # Calcola variazione se abbiamo il prezzo precedente
        variation_msg = ""
        if symbol in monitor.last_prices:
            old_price = monitor.last_prices[symbol]
            change = price - old_price
            change_pct = (change / old_price * 100) if old_price != 0 else 0
            direction = "📈" if change > 0 else "📉" if change < 0 else "➡️"
            variation_msg = f"\n{direction} Variazione: {change_pct:+.2f}%"
        
        await update.message.reply_text(
            f"{type_emoji} *{symbol}* ({type_label})\n"
            f"Prezzo: ${price:,.6f}"
            f"{variation_msg}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            f"❌ Impossibile recuperare il prezzo di {symbol}\n"
            f"Verifica che il simbolo sia corretto con /symbols"
        )

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /subscribe SYMBOL"""
    if not context.args:
        await update.message.reply_text("Uso: /subscribe SYMBOL (es: /subscribe BTC, /subscribe MSTR, /subscribe PURR)")
        return
    
    symbol = context.args[0].upper()
    chat_id = update.effective_chat.id
    
    # Verifica che il simbolo esista
    await update.message.reply_text(f"🔍 Verifico {symbol}...")
    result = await monitor.get_price(symbol)
    
    if result is None:
        await update.message.reply_text(
            f"❌ Simbolo *{symbol}* non trovato!\n"
            f"Usa /symbols per vedere i simboli disponibili.",
            parse_mode='Markdown'
        )
        return
    
    price, market_type, dex_name = result
    
    # Emoji per tipo di mercato
    if market_type == 'PERP':
        if dex_name:
            type_emoji = "🔥"
            type_label = f"PERP ({dex_name})"
        else:
            type_emoji = "⚡"
            type_label = "PERP"
    else:  # SPOT
        type_emoji = "💎"
        type_label = "SPOT"
    
    monitor.add_subscriber(chat_id, symbol)
    
    await update.message.reply_text(
        f"✅ Ora monitoro *{symbol}* per te!\n"
        f"{type_emoji} Tipo: {type_label}\n"
        f"Prezzo attuale: ${price:,.6f}\n"
        f"Soglia notifiche: ±{PRICE_CHANGE_THRESHOLD}%\n\n"
        f"Riceverai aggiornamenti sui cambiamenti significativi.",
        parse_mode='Markdown'
    )

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /unsubscribe SYMBOL"""
    if not context.args:
        await update.message.reply_text("Uso: /unsubscribe SYMBOL")
        return
    
    symbol = context.args[0].upper()
    chat_id = update.effective_chat.id
    
    if symbol not in monitor.get_subscriptions(chat_id):
        await update.message.reply_text(
            f"❌ Non stai monitorando *{symbol}*\n"
            f"Usa /list per vedere le tue sottoscrizioni.",
            parse_mode='Markdown'
        )
        return
    
    monitor.remove_subscriber(chat_id, symbol)
    await update.message.reply_text(f"✅ Non monitoro più *{symbol}*", parse_mode='Markdown')

async def list_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /list"""
    chat_id = update.effective_chat.id
    subs = monitor.get_subscriptions(chat_id)
    
    if subs:
        msg = "📋 *Le tue sottoscrizioni:*\n\n"
        for symbol in sorted(subs):
            price = monitor.last_prices.get(symbol)
            if price:
                msg += f"• {symbol} - ${price:,.6f}\n"
            else:
                msg += f"• {symbol}\n"
    else:
        msg = "📋 Non hai sottoscrizioni attive.\nUsa /subscribe SYMBOL per iniziare!"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def symbols(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /symbols"""
    await update.message.reply_text("🔍 Recupero simboli disponibili...")
    
    symbols_dict = await monitor.get_all_symbols()
    
    perps = symbols_dict.get('perps', [])
    dex_perps = symbols_dict.get('dex_perps', {})
    spots = symbols_dict.get('spot', [])
    
    if perps or dex_perps or spots:
        msg = "📊 *Simboli disponibili*\n\n"
        
        # Perpetuals standard
        if perps:
            msg += f"⚡ *PERPETUALS* ({len(perps)} totali):\n"
            msg += ", ".join(perps[:MAX_SYMBOLS_DISPLAY])
            if len(perps) > MAX_SYMBOLS_DISPLAY:
                msg += f"\n... e altri {len(perps) - MAX_SYMBOLS_DISPLAY}"
            msg += "\n\n"
        
        # Perpetuals dai vari DEX
        if dex_perps:
            for dex_code, dex_info in dex_perps.items():
                dex_symbols = dex_info['symbols']
                dex_full_name = dex_info['name']
                
                msg += f"🔥 *{dex_code}* - {dex_full_name} ({len(dex_symbols)} totali):\n"
                msg += ", ".join(dex_symbols[:MAX_SYMBOLS_DISPLAY])
                if len(dex_symbols) > MAX_SYMBOLS_DISPLAY:
                    msg += f"\n... e altri {len(dex_symbols) - MAX_SYMBOLS_DISPLAY}"
                msg += "\n\n"
        
        # Spot tokens
        if spots:
            msg += f"💎 *SPOT TOKENS* ({len(spots)} totali):\n"
            clean_spots = [s.replace('/USDC', '') for s in spots]
            msg += ", ".join(clean_spots[:MAX_SYMBOLS_DISPLAY])
            if len(spots) > MAX_SYMBOLS_DISPLAY:
                msg += f"\n... e altri {len(spots) - MAX_SYMBOLS_DISPLAY}"
        
        # Calcola totale
        total_dex_symbols = sum(len(info['symbols']) for info in dex_perps.values())
        total = len(perps) + total_dex_symbols + len(spots)
        msg += f"\n\n*Totale: {total} simboli*"
    else:
        msg = "❌ Impossibile recuperare i simboli"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /stats"""
    total_subscribers = len(monitor.subscribers)
    total_symbols = len(monitor.get_all_monitored_symbols())
    total_prices = len(monitor.last_prices)
    
    msg = (
        "📊 *Statistiche Bot*\n\n"
        f"👥 Utenti attivi: {total_subscribers}\n"
        f"📈 Simboli monitorati: {total_symbols}\n"
        f"💾 Prezzi in cache: {total_prices}\n"
        f"⏱️ Intervallo polling: {POLL_INTERVAL}s\n"
        f"📊 Soglia notifiche: ±{PRICE_CHANGE_THRESHOLD}%\n"
        f"⚙️ Config: {env_file}\n"
    )
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def price_polling_task(application: Application):
    """Task in background per il polling dei prezzi"""
    logger.info("🚀 Task di polling prezzi avviato")
    
    while True:
        try:
            # Raccogli tutti i simboli unici sottoscritti
            all_symbols = monitor.get_all_monitored_symbols()
            
            if all_symbols:
                logger.info(f"Polling di {len(all_symbols)} simboli...")
                
                # Per ogni simbolo, controlla il prezzo
                for symbol in all_symbols:
                    result = await monitor.get_price(symbol)
                    
                    if result:
                        price, market_type, dex_name = result
                        
                        # Emoji per tipo di mercato
                        if market_type == 'PERP':
                            if dex_name:
                                type_emoji = "🔥"
                                type_label = f"PERP ({dex_name})"
                            else:
                                type_emoji = "⚡"
                                type_label = "PERP"
                        else:  # SPOT
                            type_emoji = "💎"
                            type_label = "SPOT"
                        
                        # Controlla se il prezzo è cambiato significativamente
                        old_price = monitor.last_prices.get(symbol)
                        
                        if old_price:
                            change_pct = ((price - old_price) / old_price * 100)
                            
                            if abs(change_pct) >= PRICE_CHANGE_THRESHOLD:
                                # Notifica tutti i subscriber di questo simbolo
                                logger.info(f"{symbol} ({type_label}): {old_price:.6f} -> {price:.6f} ({change_pct:+.2f}%)")
                                
                                for chat_id, symbols in monitor.subscribers.items():
                                    if symbol in symbols:
                                        direction = "📈" if price > old_price else "📉"
                                        msg = (
                                            f"{direction} *{symbol}* Alert! {type_emoji}\n\n"
                                            f"Tipo: {type_label}\n"
                                            f"Vecchio: ${old_price:,.6f}\n"
                                            f"Nuovo: ${price:,.6f}\n"
                                            f"Cambio: {change_pct:+.2f}%\n"
                                            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                        )
                                        
                                        try:
                                            await application.bot.send_message(
                                                chat_id=chat_id,
                                                text=msg,
                                                parse_mode='Markdown'
                                            )
                                        except Exception as e:
                                            logger.error(f"Errore nell'invio messaggio a {chat_id}: {e}")
                        
                        # Aggiorna il prezzo
                        monitor.last_prices[symbol] = price
                    
                    # Piccolo delay tra le richieste per non sovraccaricare l'API
                    await asyncio.sleep(0.5)
            else:
                logger.debug("Nessun simbolo da monitorare")
            
            # Aspetta prima del prossimo polling
            await asyncio.sleep(POLL_INTERVAL)
            
        except Exception as e:
            logger.error(f"Errore nel task di polling: {e}", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)

async def post_init(application: Application):
    """Inizializzazione post-startup"""
    logger.info("Inizializzazione completata")
    logger.info(f"Configurazione: POLL_INTERVAL={POLL_INTERVAL}s, THRESHOLD={PRICE_CHANGE_THRESHOLD}%")
    # Avvia il task di polling in background
    asyncio.create_task(price_polling_task(application))

async def post_shutdown(application: Application):
    """Cleanup alla chiusura"""
    logger.info("Chiusura bot...")
    await monitor.close_session()

def main():
    """Funzione principale"""
    logger.info("=" * 60)
    logger.info("Avvio Hyperliquid Price Monitor Bot")
    logger.info(f"File configurazione: {env_file}")
    logger.info("=" * 60)
    
    # Crea l'applicazione
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Aggiungi i gestori dei comandi
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("price", price))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe))
    application.add_handler(CommandHandler("list", list_subscriptions))
    application.add_handler(CommandHandler("symbols", symbols))
    application.add_handler(CommandHandler("stats", stats))
    
    # Aggiungi callback per init e shutdown
    application.post_init = post_init
    application.post_shutdown = post_shutdown
    
    # Avvia il bot
    logger.info("🤖 Bot in esecuzione!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
