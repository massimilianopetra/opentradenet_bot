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

                        # Verifica trigger iniziale
                        is_tp = o['type'] == 'takeprofit'
                        is_sl = o['type'] == 'stoploss'
                        triggered = (
                            (is_sl and current_px <= o['trigger_px']) or
                            (is_tp and current_px >= o['trigger_px'])
                        )

                        # --- TRAILING STOP per TAKEPROFIT ---
                        # Una volta triggerato, aggiorna il picco e controlla ritracciamento
                        if is_tp and triggered:
                            track    = monitor.position_tracks.get(cid, {}).get(coin)
                            is_long  = track['is_long'] if track else True
                            peak     = o.get('tp_peak_price')

                            if is_long:
                                # Long: picco = massimo raggiunto
                                if peak is None or current_px > peak:
                                    o['tp_peak_price'] = current_px
                                    peak = current_px
                                # Ritracciamento dal picco
                                drop_pct = (peak - current_px) / peak * 100
                                if drop_pct >= CONDITIONAL_TP_TRAILING_PCT:
                                    # Chiudi automaticamente
                                    logger.info(f"TP TRAILING AUTO-CLOSE {coin}: picco={peak} attuale={current_px} drop={drop_pct:.2f}%")
                                    try:
                                        key    = wallet_store.get_key(cid)
                                        addr   = wallet_store.get_address(cid)
                                        client = HyperliquidClient(addr, private_key=key)
                                        result = await asyncio.get_event_loop().run_in_executor(
                                            None, lambda: client.market_close(coin, o['dex'],
                                                usd_amount=o.get('usd'), pct=o.get('pct'))
                                        )
                                        conds.pop(oid, None)
                                        monitor.save_conditional_orders(cid)
                                        # Aggiorna o cancella messaggio attivo
                                        mid = o.get('alert_message_id')
                                        close_txt = (
                                            f"🎯✅ *Take Profit — Chiusura automatica trailing*\n\n"
                                            f"*{coin}* {'_'+o['dex'].upper()+'_' if o['dex'] else 'PERP'}\n"
                                            f"Picco: ${_fmt(peak)}  →  Attuale: ${_fmt(current_px)}\n"
                                            f"Ritracciamento: -{drop_pct:.2f}% (soglia {CONDITIONAL_TP_TRAILING_PCT}%)\n"
                                            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                        )
                                        if mid:
                                            try:
                                                await application.bot.edit_message_text(
                                                    chat_id=cid, message_id=mid,
                                                    text=close_txt, parse_mode='Markdown'
                                                )
                                            except Exception:
                                                await application.bot.send_message(chat_id=cid, text=close_txt, parse_mode='Markdown')
                                        else:
                                            await application.bot.send_message(chat_id=cid, text=close_txt, parse_mode='Markdown')
                                    except Exception as e:
                                        logger.error(f"Errore trailing auto-close {coin} chat {cid}: {e}")
                                        await application.bot.send_message(
                                            chat_id=cid,
                                            text=f"⚠️ Trailing TP *{coin}*: errore chiusura automatica\n{e}\nChiudi manualmente!",
                                            parse_mode='Markdown'
                                        )
                                    continue  # ordine già rimosso

                            else:
                                # Short: picco = minimo raggiunto
                                if peak is None or current_px < peak:
                                    o['tp_peak_price'] = current_px
                                    peak = current_px
                                rise_pct = (current_px - peak) / peak * 100
                                if rise_pct >= CONDITIONAL_TP_TRAILING_PCT:
                                    logger.info(f"TP TRAILING SHORT AUTO-CLOSE {coin}: picco={peak} attuale={current_px} rise={rise_pct:.2f}%")
                                    try:
                                        key    = wallet_store.get_key(cid)
                                        addr   = wallet_store.get_address(cid)
                                        client = HyperliquidClient(addr, private_key=key)
                                        result = await asyncio.get_event_loop().run_in_executor(
                                            None, lambda: client.market_close(coin, o['dex'],
                                                usd_amount=o.get('usd'), pct=o.get('pct'))
                                        )
                                        conds.pop(oid, None)
                                        monitor.save_conditional_orders(cid)
                                        mid = o.get('alert_message_id')
                                        close_txt = (
                                            f"🎯✅ *Take Profit — Chiusura automatica trailing*\n\n"
                                            f"*{coin}* {'_'+o['dex'].upper()+'_' if o['dex'] else 'PERP'}\n"
                                            f"Picco: ${_fmt(peak)}  →  Attuale: ${_fmt(current_px)}\n"
                                            f"Rimbalzo: +{rise_pct:.2f}% (soglia {CONDITIONAL_TP_TRAILING_PCT}%)\n"
                                            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                        )
                                        if mid:
                                            try:
                                                await application.bot.edit_message_text(
                                                    chat_id=cid, message_id=mid,
                                                    text=close_txt, parse_mode='Markdown'
                                                )
                                            except Exception:
                                                await application.bot.send_message(chat_id=cid, text=close_txt, parse_mode='Markdown')
                                        else:
                                            await application.bot.send_message(chat_id=cid, text=close_txt, parse_mode='Markdown')
                                    except Exception as e:
                                        logger.error(f"Errore trailing short auto-close {coin} chat {cid}: {e}")
                                        await application.bot.send_message(
                                            chat_id=cid,
                                            text=f"⚠️ Trailing TP SHORT *{coin}*: errore chiusura automatica\n{e}\nChiudi manualmente!",
                                            parse_mode='Markdown'
                                        )
                                    continue

                        if not triggered:
                            # Uscito dalla zona — resetta picco e pulisci messaggio
                            if o.get('tp_peak_price') is not None:
                                o['tp_peak_price'] = None
                            if o.get('alert_message_id'):
                                try:
                                    await application.bot.edit_message_text(
                                        chat_id=cid,
                                        message_id=o['alert_message_id'],
                                        text=(
                                            f"{'🛑 Stop Loss' if is_sl else '🎯 Take Profit'}"
                                            f" — `{oid}` ✅ uscito dalla zona\n"
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

                        # Costruisce testo alert con PnL e picco trailing se TP
                        t_label  = "🛑 Stop Loss" if is_sl else "🎯 Take Profit"
                        arrow    = "🔽" if is_sl else "🔼"
                        dex_s    = f"_{o['dex'].upper()}_" if o['dex'] else "PERP"
                        size_s   = ""
                        if o.get('pct'):   size_s = f"  ({o['pct']*100:.0f}%)"
                        elif o.get('usd'): size_s = f"  (${o['usd']:,.0f})"
                        else:              size_s = "  (tutto)"

                        # PnL dalla posizione tracciata
                        pnl_line = ""
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

                        # Linea trailing (solo per TP già triggerato)
                        trailing_line = ""
                        if is_tp and o.get('tp_peak_price'):
                            peak = o['tp_peak_price']
                            trailing_line = f"\n📍 Picco: ${_fmt(peak)}  (trailing -{CONDITIONAL_TP_TRAILING_PCT}%)"

                        now_str = datetime.now().strftime('%H:%M:%S')
                        txt = (
                            f"{t_label} — `{oid}`  _{now_str}_\n\n"
                            f"{arrow} *{coin}* {dex_s}\n"
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
                                await application.bot.edit_message_text(
                                    chat_id=cid, message_id=mid,
                                    text=txt, parse_mode='Markdown',
                                    reply_markup=keyboard
                                )
                            else:
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
