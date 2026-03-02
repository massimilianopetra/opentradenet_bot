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
    /walletinfo  — mostra stato credenziali + saldo wallet.
    """
    chat_id = update.effective_chat.id
    if not _is_wallet_allowed(chat_id):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    addr    = wallet_store.get_address(chat_id)
    has_key = wallet_store.has_key(chat_id)
    addr_str = f"`{addr[:6]}...{addr[-4:]}`" if addr else "❌ non impostato"

    # Sezione credenziali
    msg = (
        f"🔐 *Wallet Info*\n\n"
        f"📍 Address: {addr_str}\n"
        f"🔑 Chiave API: {'✅ impostata' if has_key else '❌ non impostata'}\n"
    )

    # Saldo — richiede solo address
    if addr:
        await update.message.reply_text("⏳ Recupero saldo...")
        try:
            client  = HyperliquidClient(addr)
            summary = await asyncio.get_event_loop().run_in_executor(
                None, client.get_account_summary
            )
            equity    = summary['account_value']
            margin    = summary['total_margin']
            available = max(equity - margin, 0)
            ntl_perp  = summary['total_ntl_pos']  # solo perp standard dall'API

            # Esposizione XYZ dalle posizioni tracciate in memoria
            ntl_xyz = 0.0
            tracks  = monitor.position_tracks.get(chat_id, {})
            all_px  = await monitor.fetch_all_prices() if tracks else {}
            for coin, track in tracks.items():
                if not track.get('dex'):
                    continue  # skip perp standard, già in ntl_perp
                price_info = all_px.get(coin)
                if price_info:
                    ntl_xyz += abs(track['size'] * price_info[0])

            ntl_tot = ntl_perp + ntl_xyz

            # Ordini condizionali attivi
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
# Position tracking task — aggiorna ogni POSITION_TRACK_INTERVAL secondi
# ---------------------------------------------------------------------------
