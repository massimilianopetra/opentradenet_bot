#!/usr/bin/env python3
"""
ml_scanner.py — Scanner opportunità intraday per OpenTradeNet
=============================================================

Analizza le candele 15m di tutti i simboli e invia alert Telegram
quando rileva condizioni tecniche anomale che suggeriscono un movimento
imminente (momentum, spike volume, setup tecnici).

NON usa modelli ML pre-allenati — calcola uno score di opportunità
in tempo reale basato su:
  - Volume anomalo (spike vs media storica)
  - Momentum direzionale (velocità e coerenza del movimento)
  - Indicatori tecnici estremi (RSI, BB, EMA cross)
  - Coerenza multi-indicatore (tutti puntano nella stessa direzione?)

Target e stop sono calcolati in base all'ATR del simbolo.

USO
---
  # Scan manuale una tantum
  python3 ml_scanner.py

  # Scan su simbolo specifico
  python3 ml_scanner.py --symbol BTC

  # Modalità daemon: gira ogni 15 min in autonomia
  python3 ml_scanner.py --daemon

  # Soglia score personalizzata (default 65)
  python3 ml_scanner.py --min-score 70

  # Solo output testo, nessun alert Telegram
  python3 ml_scanner.py --dry-run

FORMATO ALERT TELEGRAM
----------------------
  ⚡ OPPORTUNITÀ — PLTR  [LONG]
  ━━━━━━━━━━━━━━━━━━━━━━━━━
  Score:  87/100  🟢🟢🟢🟢
  Prezzo: $24.31

  🎯 Target:  $25.06  (+3.1%)
  🛑 Stop:    $23.85  (-1.9%)
  📊 R/R:     1 : 1.6

  Segnali attivi:
  • Volume 3.2× la media  🔥
  • RSI 28 — zona oversold
  • Rottura banda BB inferiore
  • EMA9 in rialzo da 3 candele

  ⏰ 15:45 UTC  |  candela 15m
"""

import os
import sys
import csv
import math
import time
import json
import asyncio
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

SCRIPT_DIR   = Path(__file__).parent

def _load_env():
    for candidate in ['opentradenet.env', '.env']:
        p = SCRIPT_DIR / candidate
        if p.exists():
            load_dotenv(dotenv_path=p)
            return
    load_dotenv()

_load_env()

CANDLES_DIR        = SCRIPT_DIR / os.getenv('CANDLES_DIR', 'data/candles')
TELEGRAM_TOKEN     = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_IDS  = [x.strip() for x in os.getenv('SCANNER_CHAT_IDS',
                       os.getenv('WALLET_ALLOWED_CHATS', '')).split(',') if x.strip()]

# Score minimo per inviare un alert (0-100)
DEFAULT_MIN_SCORE  = 65

# Candele da analizzare per ogni simbolo (ultime N)
LOOKBACK           = 150    # ~37 ore di storia per calcolare medie

# ATR multiplier per target e stop
ATR_TARGET_MULT    = 2.0    # target = prezzo ± ATR × 2.0
ATR_STOP_MULT      = 1.0    # stop   = prezzo ∓ ATR × 1.0

# Cooldown: non risegnalare lo stesso simbolo se il nuovo score
# non supera il precedente (persistenza in memoria durante il daemon)
_last_signals: dict = {}   # {symbol: {'score': X, 'time': datetime, 'direction': str}}

# ---------------------------------------------------------------------------
# 1. LETTURA CANDELE
# ---------------------------------------------------------------------------

def find_csv(symbol: str) -> Path | None:
    """Cerca il CSV candele per il simbolo con i percorsi standard."""
    sym = symbol.upper()
    candidates = [
        CANDLES_DIR / sym / f"{sym}_15m.csv",
        CANDLES_DIR / f"{sym}_15m.csv",
        CANDLES_DIR / sym / f"{sym}.csv",
        CANDLES_DIR / f"{sym}.csv",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def load_last_candles(symbol: str, n: int = LOOKBACK) -> dict | None:
    """
    Legge le ultime N candele dal CSV.
    Restituisce dict con liste: opens, highs, lows, closes, volumes, timestamps
    o None se il file non esiste o ha troppo pochi dati.
    """
    csv_path = find_csv(symbol)
    if not csv_path:
        return None

    rows = []
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception:
        return None

    if len(rows) < 60:   # minimo assoluto per calcolare gli indicatori
        return None

    # Prendi le ultime N righe
    rows = rows[-n:]

    try:
        return {
            'opens':      [float(r['open'])   for r in rows],
            'highs':      [float(r['high'])   for r in rows],
            'lows':       [float(r['low'])    for r in rows],
            'closes':     [float(r['close'])  for r in rows],
            'volumes':    [float(r['volume']) for r in rows],
            'timestamps': [r['timestamp']     for r in rows],
            'symbol':     symbol,
        }
    except (ValueError, KeyError):
        return None


# ---------------------------------------------------------------------------
# 2. INDICATORI TECNICI (stesso codice di ml_features.py — standalone)
# ---------------------------------------------------------------------------

def _ema(values: list, period: int) -> list:
    result = [None] * len(values)
    k = 2 / (period + 1)
    for i, v in enumerate(values):
        if i < period - 1:
            continue
        if result[i-1] is None:
            result[i] = sum(values[i-period+1:i+1]) / period
        else:
            result[i] = v * k + result[i-1] * (1 - k)
    return result


def _rsi(closes: list, period: int = 14) -> list:
    result = [None] * len(closes)
    for i in range(period, len(closes)):
        gains = [max(closes[j] - closes[j-1], 0) for j in range(i-period+1, i+1)]
        losses= [max(closes[j-1] - closes[j], 0) for j in range(i-period+1, i+1)]
        ag = sum(gains)  / period
        al = sum(losses) / period
        result[i] = 100 - (100 / (1 + ag/al)) if al > 0 else 100
    return result


def _atr(highs: list, lows: list, closes: list, period: int = 14) -> list:
    result = [None] * len(closes)
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i]  - closes[i-1]))
        trs.append(tr)
    for i in range(period - 1, len(trs)):
        result[i + 1] = sum(trs[i-period+1:i+1]) / period
    return result


def _bollinger(closes: list, period: int = 20, dev: float = 2.0):
    mid = [None] * len(closes)
    upper = [None] * len(closes)
    lower = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i-period+1:i+1]
        m = sum(window) / period
        s = math.sqrt(sum((x - m)**2 for x in window) / period)
        mid[i]   = m
        upper[i] = m + dev * s
        lower[i] = m - dev * s
    return mid, upper, lower


def _sma(values: list, period: int) -> list:
    result = [None] * len(values)
    for i in range(period - 1, len(values)):
        result[i] = sum(values[i-period+1:i+1]) / period
    return result


# ---------------------------------------------------------------------------
# 3. SCORE ENGINE
# ---------------------------------------------------------------------------

def compute_score(data: dict) -> dict:
    """
    Calcola lo score di opportunità (0-100) e la direzione suggerita.

    Lo score è la somma pesata di componenti indipendenti:

    COMPONENTE              PESO MAX   DESCRIZIONE
    ─────────────────────────────────────────────────────────────
    volume_spike            25 pt      Volume vs SMA20: >2x=25, >1.5x=15
    momentum_strength       20 pt      Velocità e consistenza del movimento
    rsi_extreme             20 pt      RSI <30 (long) o >70 (short)
    bb_breakout             15 pt      Prezzo vicino/fuori banda BB
    ema_alignment           10 pt      EMA9>EMA21>EMA50 (long) o inverso
    candle_structure        10 pt      Body ratio, wick direction

    Totale max: 100 pt

    La direzione (LONG/SHORT) è determinata dalla maggioranza dei
    segnali direzionali. Se i segnali sono contrastanti, il score
    viene penalizzato (incoerenza → opportunità meno affidabile).
    """
    closes  = data['closes']
    highs   = data['highs']
    lows    = data['lows']
    opens   = data['opens']
    volumes = data['volumes']
    n       = len(closes)

    # Calcola indicatori
    ema9   = _ema(closes, 9)
    ema21  = _ema(closes, 21)
    ema50  = _ema(closes, 50)
    rsi    = _rsi(closes, 14)
    atr_v  = _atr(highs, lows, closes, 14)
    _, bb_up, bb_lo = _bollinger(closes, 20)
    vol_sma20 = _sma(volumes, 20)
    vol_sma5  = _sma(volumes, 5)

    i   = n - 1   # candela corrente (ultima)
    c   = closes[i]
    o   = opens[i]
    h   = highs[i]
    lo  = lows[i]
    v   = volumes[i]
    rng = max(h - lo, 1e-10)

    # Raccoglie i voti direzionali: +1 = long, -1 = short, 0 = neutro
    direction_votes = []
    score_components = {}
    signals_text = []

    # ── 1. VOLUME SPIKE (25 pt) ───────────────────────────────────────────
    vol_score = 0
    vol_ratio = 1.0
    if vol_sma20[i] and vol_sma20[i] > 0:
        vol_ratio = v / vol_sma20[i]
        if vol_ratio >= 3.0:
            vol_score = 25
            signals_text.append(f"Volume {vol_ratio:.1f}× la media 🔥🔥")
        elif vol_ratio >= 2.0:
            vol_score = 20
            signals_text.append(f"Volume {vol_ratio:.1f}× la media 🔥")
        elif vol_ratio >= 1.5:
            vol_score = 12
            signals_text.append(f"Volume {vol_ratio:.1f}× la media")
        elif vol_ratio >= 1.2:
            vol_score = 5

    score_components['volume'] = vol_score

    # Il volume da solo non ha direzione — rinforza gli altri segnali
    # ma non vota da solo

    # ── 2. MOMENTUM (20 pt) ───────────────────────────────────────────────
    mom_score = 0
    mom_dir   = 0

    # Return sulle ultime 3 candele
    if i >= 3:
        ret1 = (closes[i]   - closes[i-1]) / closes[i-1]
        ret3 = (closes[i]   - closes[i-3]) / closes[i-3]
        ret5 = (closes[i]   - closes[i-5]) / closes[i-5] if i >= 5 else 0

        # Forza del movimento corrente
        body_ratio = abs(c - o) / rng
        is_bull    = c > o

        # Candele consecutive nella stessa direzione
        consec = 0
        for j in range(1, 5):
            if i - j < 0: break
            if (closes[i-j] > opens[i-j]) == is_bull:
                consec += 1
            else:
                break

        # Score momentum
        abs_ret3 = abs(ret3)
        if abs_ret3 > 0.015:   # >1.5% in 3 candele
            mom_score = 20
        elif abs_ret3 > 0.010:
            mom_score = 15
        elif abs_ret3 > 0.005:
            mom_score = 10
        elif abs_ret3 > 0.002:
            mom_score = 5

        # Bonus candele consecutive
        if consec >= 3:
            mom_score = min(mom_score + 5, 20)

        mom_dir = 1 if ret3 > 0 else -1

        if mom_score >= 15:
            dir_str = "rialzista" if mom_dir > 0 else "ribassista"
            signals_text.append(f"Momentum {dir_str} forte ({ret3*100:+.1f}% in 3 candele)")
        elif mom_score >= 10:
            dir_str = "rialzista" if mom_dir > 0 else "ribassista"
            signals_text.append(f"Momentum {dir_str} ({ret3*100:+.1f}% in 3 candele)")

        if mom_score > 0:
            direction_votes.append(mom_dir)

    score_components['momentum'] = mom_score

    # ── 3. RSI ESTREMO (20 pt) ────────────────────────────────────────────
    rsi_score = 0
    rsi_dir   = 0
    rsi_val   = rsi[i]

    if rsi_val is not None:
        if rsi_val <= 20:
            rsi_score = 20
            rsi_dir   = 1
            signals_text.append(f"RSI {rsi_val:.0f} — fortemente oversold 📉")
        elif rsi_val <= 30:
            rsi_score = 15
            rsi_dir   = 1
            signals_text.append(f"RSI {rsi_val:.0f} — zona oversold")
        elif rsi_val <= 40:
            rsi_score = 7
            rsi_dir   = 1
        elif rsi_val >= 80:
            rsi_score = 20
            rsi_dir   = -1
            signals_text.append(f"RSI {rsi_val:.0f} — fortemente overbought 📈")
        elif rsi_val >= 70:
            rsi_score = 15
            rsi_dir   = -1
            signals_text.append(f"RSI {rsi_val:.0f} — zona overbought")
        elif rsi_val >= 60:
            rsi_score = 7
            rsi_dir   = -1

        if rsi_score > 0:
            direction_votes.append(rsi_dir)

    score_components['rsi'] = rsi_score

    # ── 4. BOLLINGER BANDS (15 pt) ────────────────────────────────────────
    bb_score = 0
    bb_dir   = 0

    if bb_up[i] and bb_lo[i]:
        bb_width = bb_up[i] - bb_lo[i]
        bb_pos   = (c - bb_lo[i]) / bb_width if bb_width > 0 else 0.5

        # Squeeze: bande strette = energia compressa, breakout imminente
        # Calcoliamo la larghezza relativa vs media ultimi 20 periodi
        recent_widths = []
        for j in range(max(0, i-20), i+1):
            if bb_up[j] and bb_lo[j]:
                recent_widths.append(bb_up[j] - bb_lo[j])

        if recent_widths:
            avg_width  = sum(recent_widths) / len(recent_widths)
            width_ratio = bb_width / avg_width if avg_width > 0 else 1.0
        else:
            width_ratio = 1.0

        if bb_pos <= 0.05:          # prezzo toccato/rotto banda inferiore
            bb_score = 15
            bb_dir   = 1
            signals_text.append("Rottura banda BB inferiore")
        elif bb_pos <= 0.15:
            bb_score = 10
            bb_dir   = 1
            signals_text.append("Prezzo vicino banda BB inferiore")
        elif bb_pos >= 0.95:        # prezzo toccato/rotto banda superiore
            bb_score = 15
            bb_dir   = -1
            signals_text.append("Rottura banda BB superiore")
        elif bb_pos >= 0.85:
            bb_score = 10
            bb_dir   = -1
            signals_text.append("Prezzo vicino banda BB superiore")

        # Bonus squeeze (bande più strette del 30% vs media → breakout imminente)
        if width_ratio < 0.7 and bb_score > 0:
            bb_score = min(bb_score + 5, 15)
            signals_text[-1] += " + squeeze BB"

        if bb_score > 0:
            direction_votes.append(bb_dir)

    score_components['bb'] = bb_score

    # ── 5. EMA ALIGNMENT (10 pt) ─────────────────────────────────────────
    ema_score = 0
    ema_dir   = 0

    if ema9[i] and ema21[i] and ema50[i]:
        bull_align = ema9[i] > ema21[i] > ema50[i]
        bear_align = ema9[i] < ema21[i] < ema50[i]

        # Cross recente EMA9/EMA21?
        cross_bull = (ema9[i] > ema21[i] and
                      i >= 1 and ema9[i-1] and ema21[i-1] and
                      ema9[i-1] <= ema21[i-1])
        cross_bear = (ema9[i] < ema21[i] and
                      i >= 1 and ema9[i-1] and ema21[i-1] and
                      ema9[i-1] >= ema21[i-1])

        if cross_bull:
            ema_score = 10
            ema_dir   = 1
            signals_text.append("Cross rialzista EMA9/EMA21 🔀")
        elif cross_bear:
            ema_score = 10
            ema_dir   = -1
            signals_text.append("Cross ribassista EMA9/EMA21 🔀")
        elif bull_align:
            ema_score = 6
            ema_dir   = 1
            signals_text.append("EMA allineate al rialzo")
        elif bear_align:
            ema_score = 6
            ema_dir   = -1
            signals_text.append("EMA allineate al ribasso")
        elif ema9[i] > ema21[i]:
            ema_score = 3
            ema_dir   = 1
        elif ema9[i] < ema21[i]:
            ema_score = 3
            ema_dir   = -1

        if ema_score > 0:
            direction_votes.append(ema_dir)

    score_components['ema'] = ema_score

    # ── 6. STRUTTURA CANDELA (10 pt) ─────────────────────────────────────
    candle_score = 0
    candle_dir   = 0

    body_ratio       = abs(c - o) / rng
    upper_wick_ratio = (h - max(c, o)) / rng
    lower_wick_ratio = (min(c, o) - lo) / rng

    # Hammer: wick inferiore lungo → reversal rialzista
    if lower_wick_ratio > 0.5 and body_ratio < 0.3:
        candle_score = 10
        candle_dir   = 1
        signals_text.append("Pattern hammer (reversal rialzista)")
    # Inverted hammer / shooting star: wick superiore lungo → reversal ribassista
    elif upper_wick_ratio > 0.5 and body_ratio < 0.3:
        candle_score = 10
        candle_dir   = -1
        signals_text.append("Pattern shooting star (reversal ribassista)")
    # Marubozu: corpo pieno → continuazione
    elif body_ratio > 0.85:
        candle_score = 8
        candle_dir   = 1 if c > o else -1
        dir_str = "rialzista" if candle_dir > 0 else "ribassista"
        signals_text.append(f"Candela marubozu {dir_str} (forza pura)")
    # Doji: indecisione — abbassa leggermente lo score totale
    elif body_ratio < 0.1:
        candle_score = -3   # penalità indecisione
        candle_dir   = 0

    if candle_score > 0:
        direction_votes.append(candle_dir)

    score_components['candle'] = max(candle_score, 0)

    # ── DIREZIONE FINALE ─────────────────────────────────────────────────
    long_votes  = sum(1 for v in direction_votes if v > 0)
    short_votes = sum(1 for v in direction_votes if v < 0)
    total_votes = len(direction_votes)

    if total_votes == 0:
        return None   # nessun segnale

    if long_votes > short_votes:
        direction = 'LONG'
        agreement = long_votes / total_votes
    elif short_votes > long_votes:
        direction = 'SHORT'
        agreement = short_votes / total_votes
    else:
        return None   # parità → segnale ambiguo

    # ── SCORE FINALE ─────────────────────────────────────────────────────
    raw_score = (score_components['volume']   +
                 score_components['momentum'] +
                 score_components['rsi']      +
                 score_components['bb']       +
                 score_components['ema']      +
                 score_components['candle'])

    # Bonus coerenza: tutti i segnali concordano → +10%
    # Penalità incoerenza: segnali contrastanti → -20%
    if agreement >= 1.0 and total_votes >= 3:
        raw_score = int(raw_score * 1.10)
        signals_text.append("✅ Tutti i segnali concordano")
    elif agreement < 0.6:
        raw_score = int(raw_score * 0.80)

    final_score = max(0, min(100, raw_score))

    # ── ATR per target e stop ─────────────────────────────────────────────
    atr_current = None
    for j in range(i, max(i-5, 0), -1):
        if atr_v[j] is not None:
            atr_current = atr_v[j]
            break

    if atr_current:
        if direction == 'LONG':
            target = c + atr_current * ATR_TARGET_MULT
            stop   = c - atr_current * ATR_STOP_MULT
        else:
            target = c - atr_current * ATR_TARGET_MULT
            stop   = c + atr_current * ATR_STOP_MULT

        target_pct = abs(target - c) / c * 100
        stop_pct   = abs(stop   - c) / c * 100
        rr_ratio   = target_pct / stop_pct if stop_pct > 0 else 0
    else:
        target = stop = None
        target_pct = stop_pct = rr_ratio = 0

    return {
        'symbol':      data['symbol'],
        'direction':   direction,
        'score':       final_score,
        'price':       c,
        'atr':         atr_current,
        'target':      target,
        'stop':        stop,
        'target_pct':  target_pct,
        'stop_pct':    stop_pct,
        'rr_ratio':    rr_ratio,
        'signals':     signals_text,
        'components':  score_components,
        'agreement':   agreement,
        'vol_ratio':   vol_ratio,
        'rsi_val':     rsi_val,
        'timestamp':   data['timestamps'][-1] if data['timestamps'] else '',
    }


# ---------------------------------------------------------------------------
# 4. FORMATO ALERT TELEGRAM
# ---------------------------------------------------------------------------

def format_alert(result: dict) -> str:
    """Formatta il messaggio Telegram per un'opportunità rilevata."""
    sym   = result['symbol']
    dirn  = result['direction']
    score = result['score']
    price = result['price']

    # Emoji direzione
    dir_emoji = '🟢' if dirn == 'LONG' else '🔴'

    # Barra score visiva
    filled = score // 20
    bar    = '🟩' * filled + '⬜' * (5 - filled)

    # Header
    lines = [
        f"⚡ <b>OPPORTUNITÀ — {sym}</b>  [{dirn}] {dir_emoji}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Score:  <b>{score}/100</b>  {bar}",
        f"Prezzo: <b>{_fmt_price(price)}</b>",
        "",
    ]

    # Target e stop
    if result['target'] and result['stop']:
        if dirn == 'LONG':
            target_sign = '+'
            stop_sign   = '-'
        else:
            target_sign = '-'
            stop_sign   = '+'
        lines += [
            f"🎯 Target:  {_fmt_price(result['target'])}  ({target_sign}{result['target_pct']:.1f}%)",
            f"🛑 Stop:    {_fmt_price(result['stop'])}  ({stop_sign}{result['stop_pct']:.1f}%)",
            f"📊 R/R:     1 : {result['rr_ratio']:.1f}",
            "",
        ]

    # Segnali attivi
    if result['signals']:
        lines.append("Segnali attivi:")
        for sig in result['signals']:
            lines.append(f"  • {sig}")

    # Footer
    lines += [
        "",
        f"⏰ {result['timestamp']}  |  candela 15m",
    ]

    return '\n'.join(lines)


def _fmt_price(price: float) -> str:
    """Formatta il prezzo con il numero giusto di decimali."""
    if price is None:
        return '—'
    if price >= 1000:
        return f"${price:,.2f}"
    elif price >= 1:
        return f"${price:.3f}"
    else:
        return f"${price:.6f}"


# ---------------------------------------------------------------------------
# 5. INVIO TELEGRAM
# ---------------------------------------------------------------------------

async def send_telegram(message: str, chat_ids: list):
    """Invia il messaggio a tutti i chat_id configurati."""
    if not TELEGRAM_TOKEN or not chat_ids:
        return

    try:
        import aiohttp
    except ImportError:
        print("  ⚠️  aiohttp non disponibile — alert Telegram saltato")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as session:
        for chat_id in chat_ids:
            try:
                await session.post(url, json={
                    'chat_id':    chat_id,
                    'text':       message,
                    'parse_mode': 'HTML',
                })
            except Exception as e:
                print(f"  ⚠️  Errore Telegram (chat {chat_id}): {e}")


# ---------------------------------------------------------------------------
# 6. DISCOVERY SIMBOLI
# ---------------------------------------------------------------------------

def discover_symbols() -> list:
    """Trova tutti i simboli disponibili in CANDLES_DIR."""
    symbols = set()
    if not CANDLES_DIR.exists():
        return []
    for item in CANDLES_DIR.iterdir():
        if item.is_dir():
            # Cerca CSV nella sottocartella
            for csv_file in item.glob('*.csv'):
                symbols.add(item.name.upper())
        elif item.suffix == '.csv':
            sym = item.stem.replace('_15m', '').replace('_15M', '').upper()
            symbols.add(sym)
    return sorted(symbols)


# ---------------------------------------------------------------------------
# 7. SCAN PRINCIPALE
# ---------------------------------------------------------------------------

async def run_scan(symbols: list, min_score: int, dry_run: bool,
                   chat_ids: list, verbose: bool = True) -> list:
    """
    Esegue lo scan su tutti i simboli e invia gli alert.
    Restituisce la lista dei risultati ordinati per score.
    """
    scan_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

    if verbose:
        print(f"\n{'═'*55}")
        print(f"  ml_scanner.py — OpenTradeNet Opportunity Scanner")
        print(f"{'═'*55}")
        print(f"  Ora:       {scan_time}")
        print(f"  Simboli:   {len(symbols)}")
        print(f"  Min score: {min_score}")
        print(f"  Dry-run:   {'sì' if dry_run else 'no'}")
        print()

    opportunities = []
    skipped = 0

    for sym in symbols:
        data = load_last_candles(sym, LOOKBACK)
        if data is None:
            skipped += 1
            continue

        result = compute_score(data)
        if result is None:
            continue

        if result['score'] >= min_score:
            opportunities.append(result)

    # Ordina per score decrescente
    opportunities.sort(key=lambda r: r['score'], reverse=True)

    if verbose:
        if opportunities:
            print(f"  {'SIMBOLO':<12} {'DIR':<6} {'SCORE':>5}  "
                  f"{'PREC_L':>6}  {'PREC_S':>6}  SEGNALI PRINCIPALI")
            print(f"  {'─'*70}")
            for r in opportunities:
                bar  = '█' * (r['score'] // 10)
                print(f"  {r['symbol']:<12} {r['direction']:<6} {r['score']:>4}/100  "
                      f"{r['price']:>10.3f}  {bar}")
                if r['signals']:
                    print(f"    → {r['signals'][0]}")
        else:
            print(f"  Nessuna opportunità trovata (score < {min_score})")

        if skipped:
            print(f"\n  ⚠️  {skipped} simboli saltati (dati insufficienti)")

        print(f"\n  Totale opportunità: {len(opportunities)}")
        print(f"{'═'*55}\n")

    # Invia alert Telegram
    sent = 0
    for r in opportunities:
        sym   = r['symbol']
        score = r['score']
        dirn  = r['direction']

        # Controlla cooldown: risegnala solo se score superiore al precedente
        last = _last_signals.get(sym)
        if last and last['direction'] == dirn and score <= last['score']:
            if verbose:
                print(f"  ⏭️  {sym} — già segnalato con score {last['score']}, skip")
            continue

        # Aggiorna ultimo segnale
        _last_signals[sym] = {
            'score':     score,
            'direction': dirn,
            'time':      datetime.utcnow(),
        }

        message = format_alert(r)

        if verbose:
            print(f"\n{'─'*55}")
            print(f"  ALERT: {sym} [{dirn}] score={score}")
            print(f"{'─'*55}")
            # Stampa il messaggio formattato (senza tag HTML)
            clean = message.replace('<b>', '').replace('</b>', '')
            for line in clean.split('\n'):
                print(f"  {line}")

        if not dry_run:
            await send_telegram(message, chat_ids)
            sent += 1

    if verbose and sent > 0:
        print(f"\n  📬 {sent} alert inviati su Telegram")

    return opportunities


# ---------------------------------------------------------------------------
# 8. DAEMON MODE
# ---------------------------------------------------------------------------

async def daemon_loop(symbols: list, min_score: int, chat_ids: list,
                      interval_minutes: int = 15):
    """
    Gira in loop ogni interval_minutes minuti, sincronizzato
    con i minuti fissi del candele 15m (00, 15, 30, 45).
    """
    print(f"\n  🤖 Modalità daemon avviata — scan ogni {interval_minutes} minuti")
    print(f"  Simboli: {len(symbols)}")
    print(f"  Premi Ctrl+C per fermare\n")

    # Pulizia _last_signals ogni 24 ore
    last_cleanup = datetime.utcnow()

    while True:
        # Calcola secondi al prossimo multiplo di 15 min
        now     = datetime.utcnow()
        minutes = now.minute
        secs    = now.second
        next_q  = ((minutes // interval_minutes) + 1) * interval_minutes
        if next_q >= 60:
            next_q -= 60
            wait = (60 - minutes - 1) * 60 + (60 - secs) + next_q * 60
        else:
            wait = (next_q - minutes) * 60 - secs

        # Aggiungi 30 secondi di ritardo per dare tempo al bot di scrivere le candele
        wait += 30

        print(f"  ⏳ Prossimo scan tra {wait//60}m {wait%60}s "
              f"(alle {(now + timedelta(seconds=wait)).strftime('%H:%M')} UTC)")

        await asyncio.sleep(wait)

        # Cleanup segnali vecchi ogni 24h
        if (datetime.utcnow() - last_cleanup).total_seconds() > 86400:
            _last_signals.clear()
            last_cleanup = datetime.utcnow()

        # Esegui scan
        try:
            await run_scan(symbols, min_score, dry_run=False,
                           chat_ids=chat_ids, verbose=True)
        except Exception as e:
            print(f"  ❌ Errore durante lo scan: {e}")
            import traceback
            traceback.print_exc()


# ---------------------------------------------------------------------------
# 9. MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='ml_scanner.py — Scanner opportunità intraday OpenTradeNet',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  # Scan manuale su tutti i simboli
  python3 ml_scanner.py

  # Scan solo su BTC
  python3 ml_scanner.py --symbol BTC

  # Soglia score più alta (più selettivo)
  python3 ml_scanner.py --min-score 75

  # Testa senza inviare alert Telegram
  python3 ml_scanner.py --dry-run

  # Modalità daemon: scan automatico ogni 15 min
  python3 ml_scanner.py --daemon
        """
    )
    parser.add_argument('--symbol',    default=None,
                        help='Analizza solo questo simbolo')
    parser.add_argument('--min-score', type=int, default=DEFAULT_MIN_SCORE,
                        help=f'Score minimo per segnalare (default: {DEFAULT_MIN_SCORE})')
    parser.add_argument('--dry-run',   action='store_true',
                        help='Non inviare alert Telegram')
    parser.add_argument('--daemon',    action='store_true',
                        help='Modalità automatica: scan ogni 15 minuti')
    parser.add_argument('--quiet',     action='store_true',
                        help='Output minimale')
    args = parser.parse_args()

    # Chat ID da usare
    chat_ids = TELEGRAM_CHAT_IDS
    if not chat_ids and not args.dry_run:
        print("  ⚠️  SCANNER_CHAT_IDS non configurato nel .env")
        print("      Aggiungi: SCANNER_CHAT_IDS=tuo_chat_id")
        print("      Per ora uso dry-run automatico")
        args.dry_run = True

    # Simboli
    if args.symbol:
        symbols = [args.symbol.upper()]
    else:
        symbols = discover_symbols()
        if not symbols:
            print(f"  ❌ Nessun simbolo trovato in {CANDLES_DIR}")
            sys.exit(1)

    # Esegui
    if args.daemon:
        asyncio.run(daemon_loop(
            symbols   = symbols,
            min_score = args.min_score,
            chat_ids  = chat_ids,
        ))
    else:
        asyncio.run(run_scan(
            symbols   = symbols,
            min_score = args.min_score,
            dry_run   = args.dry_run,
            chat_ids  = chat_ids,
            verbose   = not args.quiet,
        ))


if __name__ == '__main__':
    main()
