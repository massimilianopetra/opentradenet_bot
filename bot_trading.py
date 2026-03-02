import asyncio
import csv
import logging
import os
from typing import Dict, Optional, Tuple
from datetime import datetime, date
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from hl_wallet import HyperliquidClient
from bot_config import (
    monitor, wallet_store, logger,
    SUPPORTED_DEXS, SPIKE_EXTRA_SYMBOLS, SPIKE_EXCLUDE_SYMBOLS, SPIKE_THRESHOLD,
    POLL_INTERVAL, PRICE_CHANGE_THRESHOLD, MAX_SYMBOLS_DISPLAY,
    PRICES_DIR, PRICES_TIME, COND_DIR, CANDLES_DIR, CANDLES_INTERVAL_SECS,
    POSITION_TRACK_INTERVAL, CONDITIONAL_SNOOZE_SECS, CONDITIONAL_RENOTIFY_SECS,
    CONDITIONAL_TP_TRAILING_PCT, WALLET_ALLOWED_CHATS,
)
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
    t     = "🛑 SL" if order['type'] == 'stoploss' else "🎯 TP"
    arrow = "≤" if order['type'] == 'stoploss' else "≥"
    size_s = ""
    if order.get('pct'):
        size_s = f" {order['pct']*100:.0f}%"
    elif order.get('usd'):
        size_s = f" ${order['usd']:,.0f}"
    else:
        size_s = " tutto"
    coin   = order['coin']
    dex_s  = f" _{order['dex'].upper()}_" if order['dex'] else " PERP"
    return f"{t} *{coin}*{dex_s} {arrow} ${_fmt(order['trigger_px'])}{size_s}"


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

    dex    = await _detect_dex(symbol)
    oid    = _next_cond_id()
    order  = {
        'coin':       symbol,
        'dex':        dex,
        'type':       ctype,
        'trigger_px': trigger_px,
        'usd':        usd_amount,
        'pct':        pct,
        'created_at': datetime.now(),
        'snoozed_until':    None,
        'alert_message_id': None,   # ID messaggio Telegram attivo — per edit silenzioso
        'last_notify_ts':   None,   # timestamp ultimo send_message con bip
        'tp_peak_price':    None,   # picco prezzo raggiunto dopo trigger TP (per trailing)
    }
    monitor.conditional_orders.setdefault(chat_id, {})[oid] = order
    monitor.save_conditional_orders(chat_id)

    size_s = ""
    if pct:        size_s = f" ({pct*100:.0f}%)"
    elif usd_amount: size_s = f" (${usd_amount:,.0f})"
    else:          size_s = " (tutto)"
    market_s = f"_{dex.upper()}_" if dex else "PERP"
    sym_arrow = "🔽 ≤" if ctype == 'stoploss' else "🔼 ≥"

    await update.message.reply_text(
        f"✅ *{label} impostato* — ID: `{oid}`\n\n"
        f"*{symbol}* {market_s}  {sym_arrow}  ${_fmt(trigger_px)}{size_s}\n\n"
        f"Usa /orders per vedere gli ordini attivi\n"
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
