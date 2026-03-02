import asyncio
import logging
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

from bot_config import TELEGRAM_TOKEN, logger, monitor, wallet_store
from bot_utils import (start, subscribe, unsubscribe, price_command,
    list_command, setalert, removealert, alertstatus, market_label)
from bot_data import trackpositions, candle_task
from bot_trading import (stoploss_command, takeprofit_command, orders_command,
    cancelcond_command, order_callback, cond_callback,
    long_command, short_command, close_command, confirm_command, cancelorder_command)
from bot_wallet import setaddress_command, setkey_command, walletinfo_command, positions_command
from bot_tasks import position_tracking_task, price_polling_task

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

    app.post_init     = post_init
    app.post_shutdown = post_shutdown

    logger.info("🤖 Bot in esecuzione!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
