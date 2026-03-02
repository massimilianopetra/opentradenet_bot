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


# ---------------------------------------------------------------------------
# Snapshot giornaliero prezzi
# ---------------------------------------------------------------------------
