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
def get_yesterday_price(sym: str) -> Optional[float]:
    """Legge il prezzo del giorno precedente dal CSV dello storico."""
    csv_path = PRICES_DIR / f"{sym}.csv"
    if not csv_path.exists():
        return None
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            rows = [r for r in csv.reader(f) if r and r[0] != 'date']
        # Cerca il giorno precedente: l'ultima riga con data < oggi
        today = date.today().isoformat()
        past = [r for r in rows if r[0] < today]
        if past:
            return float(past[-1][1])
    except Exception:
        pass
    return None

def save_daily_snapshot(prices: Dict[str, Tuple[float, str, Optional[str]]]) -> int:
    """
    Scrive la quotazione odierna di ogni simbolo nel proprio CSV.
    Formato: data/prices/BTC.csv  con colonne date,price,type,dex
    Salta il simbolo se la data di oggi è già presente nel file.
    Ritorna il numero di simboli scritti.
    """
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    today     = date.today().isoformat()   # es: 2026-01-15
    written   = 0

    for sym, (price, mtype, dex) in prices.items():
        csv_path = PRICES_DIR / f"{sym}.csv"

        # Controlla se oggi è già stato scritto
        if csv_path.exists():
            try:
                with open(csv_path, 'r', encoding='utf-8') as f:
                    last_line = f.readlines()[-1].strip()
                if last_line.startswith(today):
                    continue   # già registrato oggi
            except Exception:
                pass  # file vuoto o corrotto: riscrivi comunque

        # Scrivi (o crea) il CSV
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
    """
    Fetcha le ultime N candele a 15 minuti per un simbolo da Hyperliquid.
    Ritorna lista di dict: {timestamp, open, high, low, close, volume}
    """
    import aiohttp
    # Hyperliquid vuole il nome interno: 'xyz:GOLD' per i dex, 'GOLD' per perp standard
    internal_name = f"{dex}:{symbol}" if dex else symbol

    # Calcola window: ultime 2 candele (coprono l'intervallo corrente + precedente)
    import time
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - 90 * 60 * 1000  # 90 minuti fa = 6 candele da 15m

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
    """
    Salva le candele in data/candles/SIMBOLO/SIMBOLO_15m.csv
    Deduplicazione + merge ordinato cronologicamente (no append cieco).
    Ritorna il numero di righe nuove scritte.
    """
    if not candles:
        return 0

    sym_dir  = CANDLES_DIR / symbol
    sym_dir.mkdir(parents=True, exist_ok=True)
    csv_path = sym_dir / f"{symbol}_15m.csv"

    # Legge righe esistenti
    existing_rows = []
    existing_ts   = set()
    if csv_path.exists():
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                next(reader, None)  # salta header
                for row in reader:
                    if row:
                        existing_rows.append(row)
                        existing_ts.add(row[0])
        except Exception:
            pass

    # Aggiunge solo le candele con timestamp nuovo
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

    # Merge + ordinamento cronologico + riscrittura completa
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


async def candle_task(application: Application):
    """
    Task che ogni 15 minuti fetcha e salva le candele per tutti i simboli
    monitorati (stessi del daily snapshot: xyz + SPIKE_EXTRA_SYMBOLS).
    """
    logger.info(f"🕯 Task candele avviato (intervallo: {CANDLES_INTERVAL_SECS}s)")
    CANDLES_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            # Fetch prezzi per ricavare l'universo simboli + info dex
            all_prices = await monitor.fetch_all_prices()

            xyz_syms      = {sym: v for sym, (_, _, dex) in all_prices.items()
                             if dex and dex.upper() == 'XYZ'
                             for sym, v in [(sym, all_prices[sym])]}
            # Ricostruisce correttamente
            xyz_set       = {sym for sym, (_, _, dex) in all_prices.items() if dex and dex.upper() == 'XYZ'}
            extra_set     = set(SPIKE_EXTRA_SYMBOLS)
            all_symbols   = xyz_set | extra_set

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
                # Piccola pausa tra simboli per non martellare l'API
                await asyncio.sleep(0.3)

            logger.info(
                f"🕯 Candele 15m: {total_written} righe scritte su {len(all_symbols)} simboli"
                + (f" ({errors} errori)" if errors else "")
            )

        except Exception as e:
            logger.error(f"Errore nel candle task: {e}", exc_info=True)

        await asyncio.sleep(CANDLES_INTERVAL_SECS)


# ---------------------------------------------------------------------------
# Position tracking
# ---------------------------------------------------------------------------
