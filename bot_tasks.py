"""
bot_tasks.py — Background task asincroni per OpenTradeNet bot

Contiene i loop che girano in background, estratti da hyperliquid_bot.py:
  - price_polling_task      : alert subscribe, position alert, ordini condizionali, spike, snapshot
  - position_tracking_task  : rileva apertura/chiusura/modifica posizioni
  - candle_task             : fetch e salvataggio candele 15m
  - scanner_daemon_task     : check periodico dei plugin ML scanner (ml_scanner.SCANNER_PLUGINS)
                              sulla candela live, indipendente dalla chiusura candela 15m

Inizializzazione obbligatoria prima di lanciare i task:
    from bot_tasks import init_tasks, TaskContext
    init_tasks(TaskContext(monitor, wallet_store, constants, helpers))
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Set, Optional, Tuple

from hl_wallet import HyperliquidClient
import ml_journal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Iniezione dipendenze
# ---------------------------------------------------------------------------

class TaskContext:
    """
    Contenitore di dipendenze iniettato dal bot principale tramite init_tasks().
    Evita importazioni circolari: bot_tasks non importa nulla da hyperliquid_bot.
    """
    def __init__(self, monitor, wallet_store, constants: dict, helpers: dict):
        self.monitor      = monitor
        self.wallet_store = wallet_store
        self.c            = constants   # costanti di configurazione
        self.h            = helpers     # funzioni helper


_ctx: Optional[TaskContext] = None


def init_tasks(ctx: TaskContext) -> None:
    """Deve essere chiamato in post_init() prima di create_task()."""
    global _ctx
    _ctx = ctx
    logger.info("TaskContext inizializzato correttamente")


# ---------------------------------------------------------------------------
# Task 1 — Price polling
# ---------------------------------------------------------------------------

async def price_polling_task(application) -> None:
    """
    Loop principale di polling prezzi. Ogni POLL_INTERVAL secondi:
      1. Fetch batch di tutti i prezzi monitorati
      2. Alert subscribe (soglia per-utente)
      3. Alert position tracking (soglia su alert_base)
      4. Check ordini condizionali (stoploss / takeprofit con trailing)
      5. Pricespike (variazione poll-to-poll)
      6. Snapshot giornaliero prezzi
    """
    c       = _ctx.c
    h       = _ctx.h
    monitor = _ctx.monitor

    POLL_INTERVAL             = c['POLL_INTERVAL']
    PRICE_CHANGE_THRESHOLD    = c['PRICE_CHANGE_THRESHOLD']
    PRICES_TIME               = c['PRICES_TIME']
    PRICES_DIR                = c['PRICES_DIR']
    SPIKE_EXTRA_SYMBOLS       = c['SPIKE_EXTRA_SYMBOLS']
    SPIKE_EXCLUDE_SYMBOLS     = c['SPIKE_EXCLUDE_SYMBOLS']
    CONDITIONAL_RENOTIFY_SECS = c['CONDITIONAL_RENOTIFY_SECS']
    CONDITIONAL_SNOOZE_SECS   = c['CONDITIONAL_SNOOZE_SECS']
    CONDITIONAL_TP_TRAILING_PCT = c['CONDITIONAL_TP_TRAILING_PCT']

    market_label        = h['market_label']
    save_daily_snapshot = h['save_daily_snapshot']
    _fmt                = h['_fmt']
    wallet_store        = _ctx.wallet_store

    logger.info("🚀 Task di polling avviato (modalità batch)")

    while True:
        try:
            monitored = monitor.get_all_monitored_symbols()

            all_prices: Dict[str, Tuple[float, str, Optional[str]]] = {}
            if monitored:
                all_prices = await monitor.fetch_all_prices()
                logger.debug(
                    f"Batch fetch: {len(all_prices)} prezzi ricevuti, "
                    f"monitoro {len(monitored)} simboli"
                )

                # ── 1. ALERT SUBSCRIBE ──────────────────────────────────────────
                subscribe_alerts: Dict[int, list] = {}
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
                                logger.info(
                                    f"ALERT {sym} ({label}): "
                                    f"{base:.6f} -> {price_val:.6f} ({pct:+.2f}%)"
                                )
                                arrow = "📈" if price_val > base else "📉"
                                subscribe_alerts.setdefault(chat_id, []).append(
                                    f"{arrow} *{sym}* {emoji} {label}\n"
                                    f"  Rif: ${base:,.6f}  →  ${price_val:,.6f}  ({pct:+.2f}%)"
                                )
                                triggered_syms.add(sym)

                    monitor.last_prices[sym] = price_val

                # ── Aggiorna candela 15m in-memory ──────────────────────────────
                now      = datetime.now()
                slot_dt  = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
                slot_str = slot_dt.strftime('%Y-%m-%d %H:%M')

                for symbol, price_info in all_prices.items():
                    px = price_info[0] if isinstance(price_info, (list, tuple)) else price_info
                    if not isinstance(px, (int, float)) or px <= 0:
                        continue
                    existing = monitor.live_candle_ohlc.get(symbol)
                    if existing is None or existing['slot'] != slot_str:
                        monitor.live_candle_ohlc[symbol] = {
                            'slot':  slot_str,
                            'open':  px,
                            'high':  px,
                            'low':   px,
                            'close': px,
                        }
                    else:
                        existing['high']  = max(existing['high'], px)
                        existing['low']   = min(existing['low'],  px)
                        existing['close'] = px

                now = now.strftime('%H:%M:%S')
                for chat_id, lines in subscribe_alerts.items():
                    threshold = monitor.get_threshold(chat_id)
                    msg = (
                        f"🔔 *Alert sottoscrizioni* — {now} (soglia ±{threshold}%)\n\n"
                        + "\n\n".join(lines)
                    )
                    try:
                        await application.bot.send_message(
                            chat_id=chat_id, text=msg, parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.error(f"Errore invio subscribe alert a {chat_id}: {e}")

                for sym in triggered_syms:
                    if sym in all_prices:
                        monitor.alert_base_prices[sym] = all_prices[sym][0]

                # ── 2. POSITION TRACKING ALERTS ─────────────────────────────────
                track_alerts: Dict[int, list] = {}
                track_triggered: list = []

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
                            liq      = track['liq_px']
                            liq_dist = (
                                abs((liq - current_price) / current_price * 100)
                                if liq else 0
                            )
                            logger.info(f"TRACK ALERT {coin}: {pct:+.2f}% -> chat {chat_id}")
                            entry_px  = track['entry_px']
                            size_val  = track['size']
                            pct_entry = (
                                (current_price - entry_px) / entry_px * 100
                                if entry_px else 0
                            )
                            pnl_rt    = (current_price - entry_px) * size_val
                            pnl_rt_s  = "+" if pnl_rt >= 0 else ""
                            lev_val   = track.get('leverage', 1)
                            try:
                                lev_num = float(str(lev_val).replace('x', ''))
                            except Exception:
                                lev_num = 1
                            margin_calc = (
                                abs(entry_px * size_val / lev_num)
                                if lev_num else track['margin']
                            )
                            pnl_pct = pnl_rt / margin_calc * 100 if margin_calc else 0
                            track_alerts.setdefault(chat_id, []).append(
                                f"{arrow} *{coin}* {'LONG' if is_long else 'SHORT'} "
                                f"_{track['dex']}_ {track['leverage']}x  "
                                f"size: {abs(size_val)}  {favor_s}\n"
                                f"  Rif: ${_fmt(base)}  →  ${_fmt(current_price)}  ({pct:+.2f}%)\n"
                                f"  Entry: ${_fmt(entry_px)}  →  ${_fmt(current_price)}"
                                f"  ({pct_entry:+.2f}%)\n"
                                f"  PnL: {pnl_rt_s}${pnl_rt:.2f} ({pnl_rt_s}{pnl_pct:.1f}%)\n"
                                f"  Liq dist: {liq_dist:.1f}%"
                            )
                            track_triggered.append((chat_id, coin, current_price))

                now = datetime.now().strftime('%H:%M:%S')
                for chat_id, lines in track_alerts.items():
                    threshold = monitor.get_threshold(chat_id)
                    msg = (
                        f"🎯 *Position Alert* — {now} (soglia ±{threshold}%)\n\n"
                        + "\n\n".join(lines)
                    )
                    try:
                        await application.bot.send_message(
                            chat_id=chat_id, text=msg, parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.error(f"Errore invio track alert {chat_id}: {e}")

                for chat_id, coin, new_base in track_triggered:
                    if coin in monitor.position_tracks.get(chat_id, {}):
                        monitor.position_tracks[chat_id][coin]['alert_base'] = new_base

                # ── 3. ORDINI CONDIZIONALI (stoploss / takeprofit) ───────────────
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

                            is_tp = o['type'] == 'takeprofit'
                            is_sl = o['type'] == 'stoploss'

                            _track = monitor.position_tracks.get(cid, {}).get(coin)
                            if _track:
                                is_long = _track['is_long']
                                if 'is_long' not in o:
                                    o['is_long'] = is_long
                                    monitor.save_conditional_orders(cid)
                            elif 'is_long' in o:
                                is_long = o['is_long']
                            else:
                                logger.warning(
                                    f"is_long sconosciuto per {coin} ordine {oid} — skip"
                                )
                                continue

                            if is_long:
                                triggered = (
                                    (is_sl and current_px <= o['trigger_px']) or
                                    (is_tp and current_px >= o['trigger_px'])
                                )
                            else:
                                triggered = (
                                    (is_sl and current_px >= o['trigger_px']) or
                                    (is_tp and current_px <= o['trigger_px'])
                                )

                            # ── Trailing stop per TAKEPROFIT ──────────────────────
                            if is_tp and triggered:
                                peak = o.get('tp_peak_price')
                                retrace_pct = 0.0
                                if is_long:
                                    if peak is None or current_px > peak:
                                        o['tp_peak_price'] = current_px
                                        peak = current_px
                                        logger.info(
                                            f"TP TRAILING {coin} {oid} — "
                                            f"nuovo picco LONG: ${peak:.4f}"
                                        )
                                    retrace_pct = (peak - current_px) / peak * 100 if peak else 0
                                else:
                                    if peak is None or current_px < peak:
                                        o['tp_peak_price'] = current_px
                                        peak = current_px
                                        logger.info(
                                            f"TP TRAILING {coin} {oid} — "
                                            f"nuovo picco SHORT: ${peak:.4f}"
                                        )
                                    retrace_pct = (current_px - peak) / peak * 100 if peak else 0

                                logger.info(
                                    f"TP TRAILING {coin} {oid} — "
                                    f"px=${current_px:.4f}  picco=${peak:.4f}  "
                                    f"retrace={retrace_pct:.3f}%  "
                                    f"soglia={CONDITIONAL_TP_TRAILING_PCT}%"
                                )
                                trailing_triggered = retrace_pct >= CONDITIONAL_TP_TRAILING_PCT

                                if trailing_triggered:
                                    logger.info(
                                        f"TP TRAILING AUTO-CLOSE {coin} chat {cid} "
                                        f"retrace={retrace_pct:.2f}%"
                                    )
                                    try:
                                        _key    = wallet_store.get_key(cid)
                                        _addr   = wallet_store.get_address(cid)
                                        _client = HyperliquidClient(_addr, private_key=_key)
                                        _dex    = o.get('dex', '')
                                        _result = await asyncio.get_event_loop().run_in_executor(
                                            None,
                                            lambda: _client.market_close(
                                                coin, _dex,
                                                usd_amount=o.get('usd'),
                                                pct=o.get('pct')
                                            )
                                        )
                                        conds.pop(oid, None)
                                        monitor.save_conditional_orders(cid)
                                        _size = _result.get('_size', '?')
                                        _dir  = "LONG" if is_long else "SHORT"
                                        _dex_s = f"_{_dex.upper()}_" if _dex else "PERP"
                                        await application.bot.send_message(
                                            chat_id=cid,
                                            text=(
                                                f"🎯 *Take Profit eseguito* (trailing)\n\n"
                                                f"{'📈' if is_long else '📉'} {_dir} "
                                                f"*{coin}* {_dex_s}\n"
                                                f"Size: {_size}\n"
                                                f"Picco: ${_fmt(peak)}"
                                                f"  ritracciamento: -{retrace_pct:.2f}%\n"
                                                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                            ),
                                            parse_mode='Markdown'
                                        )
                                    except Exception as e:
                                        logger.error(
                                            f"Errore trailing close {coin} chat {cid}: {e}"
                                        )
                                    continue

                                else:
                                    pass

                            if o.get('snoozed_until') and now_ts < o['snoozed_until']:
                                continue

                            if not triggered:
                                continue

                            # ── Costruisci messaggio alert ────────────────────────
                            t_label     = "🛑 Stop Loss" if is_sl else "🎯 Take Profit"
                            dir_label   = "LONG" if is_long else "SHORT"
                            dex_s       = f"_{o.get('dex','').upper()}_" if o.get('dex') else "PERP"
                            pct_o       = o.get('pct')
                            usd_o       = o.get('usd')
                            size_s      = (
                                f" ({pct_o*100:.0f}%)" if pct_o else
                                f" (${usd_o:,.0f})"    if usd_o else
                                " (tutto)"
                            )
                            arrow       = "📉" if is_sl else "📈"
                            pnl_line    = ""
                            trail_line  = ""

                            _trk = monitor.position_tracks.get(cid, {}).get(coin)
                            if _trk:
                                _entry = _trk['entry_px']
                                _sz    = _trk['size']
                                _pnl   = (current_px - _entry) * _sz
                                _pnl_s = "+" if _pnl >= 0 else ""
                                pnl_line = f"\nPnL stimato: {_pnl_s}${_pnl:.2f}"

                            if is_tp and o.get('tp_peak_price'):
                                trail_line = (
                                    f"\n📍 Picco: ${_fmt(o['tp_peak_price'])}"
                                    f"  trailing: -{CONDITIONAL_TP_TRAILING_PCT}%"
                                )

                            now_str = datetime.now().strftime('%H:%M:%S')
                            txt = (
                                f"{t_label} — `{oid}`  _{now_str}_\n\n"
                                f"{arrow} {dir_label} *{coin}* {dex_s}\n"
                                f"Trigger: ${_fmt(o['trigger_px'])}"
                                f"  Attuale: ${_fmt(current_px)}"
                                f"{pnl_line}{trail_line}\n"
                                f"Azione: chiudi posizione{size_s}"
                            )
                            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                            keyboard = InlineKeyboardMarkup([[
                                InlineKeyboardButton(
                                    "✅ Esegui",
                                    callback_data=f"cond_exec_{cid}_{oid}"
                                ),
                                InlineKeyboardButton(
                                    "⏭ Salta",
                                    callback_data=f"cond_snooze_{cid}_{oid}"
                                ),
                                InlineKeyboardButton(
                                    "🗑 Cancella",
                                    callback_data=f"cond_cancel_{cid}_{oid}"
                                ),
                            ]])

                            try:
                                mid         = o.get('alert_message_id')
                                last_notify = o.get('last_notify_ts') or 0
                                need_renotify = mid and (
                                    now_ts - last_notify >= CONDITIONAL_RENOTIFY_SECS
                                )

                                if need_renotify:
                                    try:
                                        await application.bot.delete_message(
                                            chat_id=cid, message_id=mid
                                        )
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

                # ── 4. PRICESPIKE ────────────────────────────────────────────────
                if monitor.spike_subscribers:
                    xyz_syms = set()
                    if all_prices:
                        xyz_syms = {
                            sym for sym, (_, _, dex) in all_prices.items()
                            if dex and dex.upper() == 'XYZ'
                        }
                    spike_symbols_all   = xyz_syms | set(SPIKE_EXTRA_SYMBOLS)
                    spike_symbols_alert = spike_symbols_all - SPIKE_EXCLUDE_SYMBOLS

                    if not all_prices:
                        all_prices = await monitor.fetch_all_prices()

                    spike_lines: Dict[int, list] = {}
                    for sym in spike_symbols_alert:
                        if sym not in all_prices:
                            continue
                        price_val = all_prices[sym][0]
                        prev      = monitor.spike_prev_prices.get(sym)
                        if prev is not None and prev > 0:
                            spike_pct = (price_val - prev) / prev * 100
                            for chat_id in monitor.spike_subscribers:
                                spike_thr = monitor.get_spike_threshold(chat_id)
                                if abs(spike_pct) >= spike_thr:
                                    arrow = "📈" if spike_pct > 0 else "📉"
                                    yest  = h.get('get_yesterday_price', lambda s: None)(sym) \
                                            if 'get_yesterday_price' in h else None
                                    yest_s = ""
                                    if yest:
                                        yest_pct = (price_val - yest) / yest * 100
                                        yest_s   = f"  (vs ieri: {yest_pct:+.1f}%)"
                                    spike_lines.setdefault(chat_id, []).append(
                                        f"{arrow} *{sym}*: "
                                        f"${prev:,.5g} → ${price_val:,.5g} "
                                        f"({spike_pct:+.2f}%){yest_s}"
                                    )

                    for sym in spike_symbols_all:
                        if sym in all_prices:
                            monitor.spike_prev_prices[sym] = all_prices[sym][0]

                    now = datetime.now().strftime('%H:%M:%S')
                    for chat_id, lines in spike_lines.items():
                        spike_thr = monitor.get_spike_threshold(chat_id)
                        msg = (
                            f"⚡ *PriceSpike* — {now} (soglia ±{spike_thr}%)\n\n"
                            + "\n".join(lines)
                        )
                        try:
                            await application.bot.send_message(
                                chat_id=chat_id, text=msg, parse_mode='Markdown'
                            )
                        except Exception as e:
                            logger.error(f"Errore invio spike a {chat_id}: {e}")

            # ── 5. SNAPSHOT GIORNALIERO ──────────────────────────────────────────
            now_dt = datetime.now()
            today  = now_dt.date()
            if now_dt.hour >= PRICES_TIME and monitor.last_snapshot_date != today:
                prices_batch = all_prices.copy() if all_prices else await monitor.fetch_all_prices()
                xyz_syms = {
                    sym for sym, (_, _, dex) in prices_batch.items()
                    if dex and dex.upper() == 'XYZ'
                }
                spike_symbols_all = xyz_syms | set(SPIKE_EXTRA_SYMBOLS)
                prices_to_snap    = {
                    sym: v for sym, v in prices_batch.items()
                    if sym in spike_symbols_all
                }
                if prices_to_snap:
                    written = save_daily_snapshot(prices_to_snap)
                    monitor.last_snapshot_date = today
                    logger.info(
                        f"Snapshot giornaliero: {written} simboli scritti in {PRICES_DIR}"
                    )
                else:
                    logger.warning(
                        "Snapshot giornaliero: nessun prezzo disponibile, "
                        "riprovo al prossimo ciclo"
                    )

            await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            logger.error(f"Errore nel polling: {e}", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Task 2 — Position tracking
# ---------------------------------------------------------------------------

async def position_tracking_task(application) -> None:
    """
    Ogni POSITION_TRACK_INTERVAL secondi:
      - Fetcha le posizioni aperte per ogni utente con tracking attivo
      - Notifica nuove posizioni, aggiornamenti (size/entry), chiusure
      - Cancella automaticamente gli ordini condizionali delle posizioni chiuse
      - Registra aperture/chiusure nel ml_journal
    """
    c            = _ctx.c
    h            = _ctx.h
    monitor      = _ctx.monitor
    wallet_store = _ctx.wallet_store

    POSITION_TRACK_INTERVAL = c['POSITION_TRACK_INTERVAL']
    SUPPORTED_DEXS          = c['SUPPORTED_DEXS']
    CANDLES_DIR             = c['CANDLES_DIR']
    _fmt                    = h['_fmt']

    logger.info(f"🎯 Task position tracking avviato (ogni {POSITION_TRACK_INTERVAL}s)")

    while True:
        try:
            for chat_id in list(monitor.tracking_enabled):
                addr = wallet_store.get_address(chat_id)
                if not addr:
                    continue

                try:
                    client   = HyperliquidClient(addr)
                    pos_list, _av, _wm = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: client.get_positions(extra_dexs=SUPPORTED_DEXS)
                    )
                    logger.info(f"DIAG TRACK FETCH chat={chat_id} addr={addr[:10]}... found={len(pos_list)} dexs={SUPPORTED_DEXS}")
                    for p in pos_list:
                        logger.info(f"  DIAG TRACK POS: coin={p['coin']} {'L' if p['is_long'] else 'S'} size={p['size']} dex={p.get('dex','PERP')} entry={p['entry_px']}")
                except Exception as e:
                    logger.error(f"Position tracking fetch error chat {chat_id}: {e}")
                    continue

                current_tracks = monitor.position_tracks.get(chat_id, {})
                new_tracks     = {}
                threshold      = monitor.get_threshold(chat_id)
                is_first_cycle = chat_id not in monitor._first_track_done

                for p in pos_list:
                    coin  = p['coin']
                    dex_s = (
                        f"_{p.get('dex','PERP').upper()}_"
                        if p.get('dex') else "PERP"
                    )
                    dir_s = "📈 LONG" if p['is_long'] else "📉 SHORT"

                    if coin in current_tracks:
                        old_track = current_tracks[coin]
                        old_size  = abs(old_track['size'])
                        new_size  = abs(p['size'])
                        old_entry = old_track['entry_px']
                        new_entry = p['entry_px']

                        size_changed  = (
                            abs(new_size - old_size) / old_size > 0.001
                            if old_size else False
                        )
                        entry_changed = (
                            abs(new_entry - old_entry) / old_entry > 0.001
                            if old_entry else False
                        )

                        if (size_changed or entry_changed) and not is_first_cycle:
                            change_parts = []
                            if size_changed:
                                change_parts.append(
                                    f"Size: {old_size} → {new_size} "
                                    f"({'▲' if new_size > old_size else '▼'})"
                                )
                            if entry_changed:
                                change_parts.append(
                                    f"Entry: ${_fmt(old_entry)} → ${_fmt(new_entry)}"
                                )
                            try:
                                await application.bot.send_message(
                                    chat_id=chat_id,
                                    text=(
                                        f"✏️ *Posizione aggiornata*: *{coin}*\n\n"
                                        f"{dir_s} {dex_s} {p['leverage']}x\n"
                                        + "\n".join(change_parts) +
                                        f"\nMargin: ${p['margin']:.2f}"
                                        f"  Liq: ${_fmt(p['liq_px'])}"
                                    ),
                                    parse_mode='Markdown'
                                )
                            except Exception as e:
                                logger.error(
                                    f"Errore notifica aggiornamento posizione {chat_id}: {e}"
                                )

                        track = old_track.copy()
                        track.update({
                            'entry_px':   p['entry_px'],
                            'size':       p['size'],
                            'margin':     p['margin'],
                            'leverage':   p['leverage'],
                            'unrealized': p['unrealized'],
                            'liq_px':     p['liq_px'],
                            'is_long':    p['is_long'],
                        })
                        new_tracks[coin] = track

                    else:
                        new_tracks[coin] = {
                            'entry_px':   p['entry_px'],
                            'is_long':    p['is_long'],
                            'size':       p['size'],
                            'margin':     p['margin'],
                            'leverage':   p['leverage'],
                            'dex':        p.get('dex', 'PERP'),
                            'unrealized': p['unrealized'],
                            'liq_px':     p['liq_px'],
                            'alert_base': monitor.last_prices.get(p['coin'], p['entry_px']),
                            'added_at':   datetime.now(),
                        }
                        logger.info(
                            f"TRACK NEW {coin} "
                            f"({'L' if p['is_long'] else 'S'}) -> chat {chat_id}"
                        )

                        # --- Journal log_open ---
                        # Solo se non esiste già un record open per questo simbolo
                        # (evita duplicati con trade aperti via /long /short o importati da CSV)
                        try:
                            open_trades  = ml_journal.get_open_trades(chat_id=chat_id)
                            already_open = any(
                                t['symbol'] == coin.upper()
                                for t in open_trades
                            )
                            if not already_open:
                                ml_journal.log_open(
                                    symbol=coin,
                                    direction="long" if p['is_long'] else "short",
                                    entry_price=p['entry_px'],
                                    size_usd=abs(p['size'] * p['entry_px']),
                                    candles_dir=CANDLES_DIR,
                                    chat_id=chat_id,
                                )
                        except Exception as _je:
                            logger.warning(f"ml_journal log_open (track) error {coin}: {_je}")

                        try:
                            label = 'Posizione attiva al boot' if is_first_cycle else 'Nuova posizione rilevata'
                            await application.bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    f"🎯 *{label}*\n\n"
                                    f"{dir_s} *{coin}* {dex_s} {p['leverage']}x\n"
                                    f"Entry: ${_fmt(p['entry_px'])}  Size: {abs(p['size'])}\n"
                                    f"Margin: ${p['margin']:.2f}  Liq: ${_fmt(p['liq_px'])}\n"
                                    f"Soglia alert: ±{threshold}%"
                                ),
                                parse_mode='Markdown'
                            )
                        except Exception as e:
                            logger.error(f"Errore notifica posizione {chat_id}: {e}")

                # Posizioni chiuse: erano in track ma non più nell'API
                closed = set(current_tracks.keys()) - set(new_tracks.keys())
                for coin in closed:
                    track = current_tracks[coin]
                    logger.info(f"TRACK CLOSED {coin} -> chat {chat_id}")

                    conds        = monitor.conditional_orders.get(chat_id, {})
                    removed_oids = [oid for oid, o in conds.items() if o['coin'] == coin]
                    for oid in removed_oids:
                        conds.pop(oid, None)
                    if removed_oids:
                        monitor.save_conditional_orders(chat_id)
                        logger.info(
                            f"Rimossi {len(removed_oids)} ordini condizionali "
                            f"per {coin} chiuso"
                        )

                    # Prezzo corrente per log_close
                    current_price = monitor.last_prices.get(coin, track['entry_px'])

                    # Stima P&L dalla posizione tracciata (unrealized al momento della chiusura)
                    # Usiamo unrealized dell'ultimo aggiornamento come approssimazione
                    pnl_estimate = track.get('unrealized', 0.0) or 0.0

                    # --- Journal log_close ---
                    try:
                        ml_journal.log_close(
                            symbol=coin,
                            exit_price=current_price,
                            pnl_usd=pnl_estimate,
                            chat_id=chat_id,
                        )
                    except Exception as _je:
                        logger.warning(f"ml_journal log_close error {coin}: {_je}")

                    try:
                        cond_note = (
                            f"\n🗑 Rimossi {len(removed_oids)} ordini condizionali"
                            if removed_oids else ""
                        )
                        dir_s = "LONG" if track['is_long'] else "SHORT"
                        dex_s = (
                            f"_{track['dex'].upper()}_"
                            if track.get('dex') else "PERP"
                        )
                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"🏁 *Posizione chiusa*: *{coin}*\n"
                                f"{dir_s} {dex_s} {track['leverage']}x\n"
                                f"Entry: ${_fmt(track['entry_px'])}\n"
                                f"⏰ {datetime.now().strftime('%H:%M:%S')}{cond_note}"
                            ),
                            parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.error(f"Errore notifica chiusura {chat_id}: {e}")

                monitor._first_track_done.add(chat_id)
                monitor.position_tracks[chat_id] = new_tracks

        except Exception as e:
            logger.error(f"Errore nel position tracking task: {e}", exc_info=True)

        await asyncio.sleep(POSITION_TRACK_INTERVAL)


# ---------------------------------------------------------------------------
# Task 3 — Candele 15m
# ---------------------------------------------------------------------------

def _sleep_until_next_candle(offset_secs: int = 30) -> float:
    """Calcola i secondi da attendere per il prossimo multiplo di 15m + offset."""
    import time
    now      = time.time()
    interval = 15 * 60  # 900s
    elapsed  = now % interval
    wait     = (interval - elapsed) + offset_secs
    return wait


async def candle_task(application) -> None:
    """
    Ogni CANDLES_INTERVAL_SECS:
      - Fetcha le ultime candele 15m per tutti i simboli (xyz + SPIKE_EXTRA_SYMBOLS)
      - Salva su CSV in data/candles/

    Solo scrittura candele: il trigger dello scanner daemon vive in
    scanner_daemon_task, disaccoppiato dal ciclo di chiusura candela.
    """
    c       = _ctx.c
    h       = _ctx.h
    monitor = _ctx.monitor
    _mls    = h['_mls']

    CANDLES_INTERVAL_SECS = c['CANDLES_INTERVAL_SECS']
    SPIKE_EXTRA_SYMBOLS   = c['SPIKE_EXTRA_SYMBOLS']
    CANDLES_DIR           = c['CANDLES_DIR']
    fetch_candles_15m     = h['fetch_candles_15m']
    save_candles          = h['save_candles']

    CANDLES_DIR.mkdir(parents=True, exist_ok=True)
    wait0 = _sleep_until_next_candle(offset_secs=30)
    logger.info(f"🕯 Task candele avviato — prima esecuzione in {wait0:.0f}s (allineato a :00/:15/:30/:45 + 30s)")
    await asyncio.sleep(wait0)

    while True:
        try:
            all_prices = await monitor.fetch_all_prices()

            xyz_set     = {
                sym for sym, (_, _, dex) in all_prices.items()
                if dex and dex.upper() == 'XYZ'
            }
            extra_set   = set(SPIKE_EXTRA_SYMBOLS)
            all_symbols = xyz_set | extra_set

            total_written = 0
            errors        = 0

            for sym in sorted(all_symbols):
                info = all_prices.get(sym)
                dex  = info[2].lower() if info and info[2] else ''
                try:
                    candles = await fetch_candles_15m(sym, dex)
                    n       = save_candles(sym, candles)
                    total_written += n
                    if n:
                        logger.debug(f"Candele {sym}: {n} nuove righe salvate")
                except Exception as e:
                    errors += 1
                    logger.warning(f"Errore fetch candele {sym}: {e}")
                await asyncio.sleep(0.3)

            logger.info(
                f"🕯 Candele 15m: {total_written} righe scritte su {len(all_symbols)} simboli"
                + (f" ({errors} errori)" if errors else "")
            )

        except Exception as e:
            logger.error(f"Errore nel candle task: {e}", exc_info=True)

        await asyncio.sleep(_sleep_until_next_candle(offset_secs=30))


# ---------------------------------------------------------------------------
# Task 4 — Scanner daemon (plugin-based, indipendente dalla chiusura candela)
# ---------------------------------------------------------------------------

async def scanner_daemon_task(application) -> None:
    """
    Ogni SCANNER_DAEMON_INTERVAL_SECS:
      - Se monitor.scanner_enabled, per ogni simbolo scoperto carica le candele
        chiuse (load_last_candles/LOOKBACK) e le arricchisce con la candela live
        (stessa logica di fetch_live_candle + append_live_candle usata da /chart).
      - Per ogni plugin in monitor.scanner_active_plugins chiama plugin.compute()
        e, se il risultato supera monitor.scanner_thresholds[plugin.name], invia
        plugin.format_alert(result) a tutti i chat_id in monitor.scanner_chat_ids.
    """
    c       = _ctx.c
    h       = _ctx.h
    monitor = _ctx.monitor
    _mls    = h['_mls']

    SCANNER_DAEMON_INTERVAL_SECS = c['SCANNER_DAEMON_INTERVAL_SECS']
    fetch_live_candle            = h['fetch_live_candle']
    append_live_candle           = h['append_live_candle']

    logger.info(f"📡 Task scanner daemon avviato (ogni {SCANNER_DAEMON_INTERVAL_SECS}s)")

    while True:
        await asyncio.sleep(SCANNER_DAEMON_INTERVAL_SECS)

        try:
            if not (monitor.scanner_enabled and monitor.scanner_active_plugins
                    and monitor.scanner_chat_ids):
                continue

            plugins = [
                _mls.SCANNER_PLUGINS[name]
                for name in monitor.scanner_active_plugins
                if name in _mls.SCANNER_PLUGINS
            ]
            if not plugins:
                continue

            symbols = _mls.discover_symbols()

            all_prices, funding_map = await asyncio.gather(
                monitor.fetch_all_prices(),
                _mls._fetch_all_funding_rates(),
            )
            xyz_set = {
                sym for sym, (_, _, dex) in all_prices.items()
                if dex and dex.upper() == 'XYZ'
            }

            def _load_and_enrich(sym: str):
                data = _mls.load_last_candles(sym, _mls.LOOKBACK)
                if data is None:
                    return None
                data['symbol'] = sym
                return data

            for sym in symbols:
                candles = await asyncio.get_event_loop().run_in_executor(
                    None, _load_and_enrich, sym
                )
                if candles is None:
                    continue

                is_xyz    = sym in xyz_set
                live_ohlc = await fetch_live_candle(sym, is_xyz=is_xyz)
                if live_ohlc is not None:
                    candles = append_live_candle(candles, live_ohlc)
                    candles['symbol'] = sym
                else:
                    fallback = monitor.last_prices.get(sym)
                    if not fallback:
                        try:
                            gp = await monitor.get_price(sym)
                            if gp:
                                fallback = gp[0]
                        except Exception:
                            pass
                    if fallback:
                        candles = append_live_candle(candles, float(fallback))
                        candles['symbol'] = sym

                live_price   = all_prices.get(sym, (None,))[0]
                funding_rate = funding_map.get(sym.upper())

                for plugin in plugins:
                    try:
                        result = await asyncio.get_event_loop().run_in_executor(
                            None, plugin.compute, sym, candles, live_price, funding_rate
                        )
                    except Exception as e:
                        logger.error(f"Errore compute plugin {plugin.name} {sym}: {e}")
                        continue

                    if result is None:
                        continue

                    threshold = monitor.scanner_thresholds.get(plugin.name, plugin.default_min_score)
                    if result['score'] < threshold:
                        continue

                    try:
                        msg = plugin.format_alert(result)
                    except Exception as e:
                        logger.error(f"Errore format_alert plugin {plugin.name} {sym}: {e}")
                        continue

                    for cid in list(monitor.scanner_chat_ids):
                        try:
                            await application.bot.send_message(
                                chat_id=cid, text=msg, parse_mode='HTML'
                            )
                        except Exception as e:
                            logger.error(f"Errore invio {plugin.name} daemon a {cid}: {e}")

            logger.info(
                f"📡 Scanner daemon: ciclo completato — plugin={sorted(monitor.scanner_active_plugins)} "
                f"simboli={len(symbols)} utenti={len(monitor.scanner_chat_ids)}"
            )

        except Exception as e:
            logger.error(f"Errore nello scanner daemon task: {e}", exc_info=True)
