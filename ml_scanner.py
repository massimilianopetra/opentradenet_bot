#!/usr/bin/env python3
"""
ml_scanner.py — Scanner VSA + Bollinger + EMA per OpenTradeNet
==============================================================

Analizza le candele 15m cercando setup VSA (Volume Spread Analysis)
combinati con contesto Bollinger Bands ed EMA trend.

FILOSOFIA
---------
Le feature non vengono valutate singolarmente ma IN RELAZIONE tra loro.
VSA legge volume + range + posizione close INSIEME per capire l'intenzione
del mercato (smart money vs retail).

PATTERN VSA RICONOSCIUTI
------------------------

1. CLIMAX (volume estremo = fine del movimento)
   Buying Climax:  volume molto alto + range ampio + close in cima  → SHORT
   Selling Climax: volume molto alto + range ampio + close in fondo → LONG

2. EFFORT vs RESULT (sforzo senza risultato = debolezza nascosta)
   Volume alto + range stretto + close nel mezzo → segnale opposto al trend

3. STOPPING VOLUME (assorbimento)
   Candela bearish con close che risale (close_pos > 55%) → LONG
   Candela bullish con close che ricade (close_pos < 45%) → SHORT

4. NO SUPPLY / NO DEMAND (assenza di interesse = continuazione)
   Volume bassissimo in downtrend → No Supply → LONG
   Volume bassissimo in uptrend   → No Demand → SHORT

Tutte le soglie sono RELATIVE alla storia del simbolo (percentili),
non valori assoluti — adattivo a BTC come a INTC.

CONTESTO
--------
Ogni pattern VSA viene pesato in base a:
- Posizione nelle Bollinger Bands
- Allineamento EMA9/21/50 (trend dominante)
- Squeeze BB (volatilità compressa)

USO
---
  python3 ml_scanner.py                # scan manuale
  python3 ml_scanner.py --symbol BTC   # solo BTC
  python3 ml_scanner.py --daemon       # loop ogni 15 min
  python3 ml_scanner.py --dry-run      # no Telegram
  python3 ml_scanner.py --min-score 70 # più selettivo
"""

import os
import sys
import csv
import math
import asyncio
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent

def _load_env():
    for candidate in ['opentradenet.env', '.env']:
        p = SCRIPT_DIR / candidate
        if p.exists():
            load_dotenv(dotenv_path=p)
            return
    load_dotenv()

_load_env()

CANDLES_DIR       = SCRIPT_DIR / os.getenv('CANDLES_DIR', 'data/candles')
TELEGRAM_TOKEN    = os.getenv('TELEGRAM_TOKEN', '')
HYPERLIQUID_API   = os.getenv('HYPERLIQUID_API', 'https://api.hyperliquid.xyz/info')
SUPPORTED_DEXS    = [d.strip() for d in os.getenv('SUPPORTED_DEXS', 'xyz').split(',') if d.strip()]

# Chat IDs: SCANNER_CHAT_IDS ha priorità, poi cade su WALLET_ALLOWED_CHATS del bot principale
_raw_ids = os.getenv('SCANNER_CHAT_IDS', '') or os.getenv('WALLET_ALLOWED_CHATS', '')
TELEGRAM_CHAT_IDS = [x.strip() for x in _raw_ids.split(',') if x.strip()]

# Simboli 24/7 (crypto): letti dal .env, default = i perp standard più comuni
# Tutto ciò che NON è in questa lista viene trattato come XYZ (mercato con orari)
# Nel .env: SCANNER_24H_SYMBOLS=BTC,ETH,SOL,XRP,SUI,HYPE,KAS
_raw_24h = os.getenv('SCANNER_24H_SYMBOLS', 'BTC,ETH,SOL,XRP,SUI,HYPE,KAS,MSTR,COIN,XYZ100')
SYMBOLS_24H = {s.strip().upper() for s in _raw_24h.split(',') if s.strip()}

# Orario mercati XYZ: nessun filtro orario, solo weekend
# (sabato e domenica i simboli XYZ vengono saltati automaticamente)

DEFAULT_MIN_SCORE = 60
LOOKBACK          = 150

# Moltiplicatori ATR per categoria
_STOCKS  = {'AAPL','AMD','AMZN','BABA','COIN','GOOGL','HOOD','INTC','META','MSFT',
            'MU','NFLX','NVDA','ORCL','PLTR','RIVN','SNDK','TSLA','TSM','CRCL',
            'CRWV','SMSN','HYUNDAI','SKHX'}
_FOREX   = {'EUR','JPY'}

def _atr_multipliers(symbol: str):
    s = symbol.upper()
    if s in SYMBOLS_24H:
        return 3.0, 1.5
    elif s in _STOCKS:
        return 8.0, 4.0
    elif s in _FOREX:
        return 5.0, 2.5
    else:
        return 5.0, 2.5


def is_market_open(symbol: str) -> bool:
    """
    Restituisce True se il simbolo può essere tradato adesso.
    Crypto (in SYMBOLS_24H): sempre True.
    Tutto il resto (XYZ): False nel weekend (sabato e domenica).
    """
    if symbol.upper() in SYMBOLS_24H:
        return True
    return datetime.now().weekday() < 5   # 0-4 = lun-ven, 5-6 = weekend

_last_signals: dict = {}
_live_prices:  dict = {}   # cache prezzi live Hyperliquid {symbol: float}

# ---------------------------------------------------------------------------
# 1. LETTURA CANDELE
# ---------------------------------------------------------------------------

def find_csv(symbol: str):
    sym = symbol.upper()
    for p in [
        CANDLES_DIR / sym / f"{sym}_15m.csv",
        CANDLES_DIR / f"{sym}_15m.csv",
        CANDLES_DIR / sym / f"{sym}.csv",
        CANDLES_DIR / f"{sym}.csv",
    ]:
        if p.exists():
            return p
    return None


def load_last_candles(symbol: str, n: int = LOOKBACK):
    csv_path = find_csv(symbol)
    if not csv_path:
        return None
    rows = []
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                rows.append(row)
    except Exception:
        return None
    if len(rows) < 60:
        return None
    rows = rows[-n:]
    try:
        return {
            'symbol':     symbol,
            'opens':      [float(r['open'])   for r in rows],
            'highs':      [float(r['high'])   for r in rows],
            'lows':       [float(r['low'])    for r in rows],
            'closes':     [float(r['close'])  for r in rows],
            'volumes':    [float(r['volume']) for r in rows],
            'timestamps': [r['timestamp']     for r in rows],
        }
    except (ValueError, KeyError):
        return None


def discover_symbols():
    symbols = set()
    if not CANDLES_DIR.exists():
        return []
    for item in CANDLES_DIR.iterdir():
        if item.is_dir():
            for f in item.glob('*.csv'):
                symbols.add(item.name.upper())
        elif item.suffix == '.csv':
            sym = item.stem.replace('_15m', '').replace('_15M', '').upper()
            symbols.add(sym)
    return sorted(symbols)


# ---------------------------------------------------------------------------
# 1b. PREZZI LIVE HYPERLIQUID
# ---------------------------------------------------------------------------
# Lo scanner usa il prezzo live per:
#   - Mostrare il prezzo ATTUALE nell'alert (non quello dell'ultima candela chiusa)
#   - Calcolare target/stop dal prezzo corrente invece che dal close della candela
#
# Se la fetch fallisce (offline, standalone senza rete) usa il close dell'ultima candela.
# Il funzionamento VSA/score è sempre basato sulle candele — il prezzo live è solo display.

async def fetch_live_prices() -> dict:
    """
    Recupera tutti i prezzi live da Hyperliquid in 2 chiamate HTTP (perp + xyz).
    Identico a fetch_all_prices() nel bot principale.
    Ritorna {symbol: float} o {} se la rete non è disponibile.
    """
    prices = {}
    try:
        import aiohttp
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as session:
            # Perp standard + spot
            async with session.post(HYPERLIQUID_API,
                                    json={'type': 'allMids', 'dex': ''}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for key, val in data.items():
                        try:
                            price = float(val)
                        except (ValueError, TypeError):
                            continue
                        if '/' in key:
                            prices[key.split('/')[0]] = price
                        elif ':' not in key:
                            prices[key] = price

            # Perp XYZ e altri DEX
            for dex in SUPPORTED_DEXS:
                async with session.post(HYPERLIQUID_API,
                                        json={'type': 'allMids', 'dex': dex}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for key, val in data.items():
                            if ':' in key:
                                try:
                                    prices[key.split(':', 1)[1]] = float(val)
                                except (ValueError, TypeError):
                                    pass
    except Exception:
        pass   # standalone senza rete: usa close candela
    return prices


# ---------------------------------------------------------------------------
# 2. INDICATORI
# ---------------------------------------------------------------------------

def _ema(values, period):
    out = [None] * len(values)
    k = 2 / (period + 1)
    for i, v in enumerate(values):
        if i < period - 1:
            continue
        if out[i-1] is None:
            out[i] = sum(values[i-period+1:i+1]) / period
        else:
            out[i] = v * k + out[i-1] * (1 - k)
    return out


def _sma(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = sum(values[i-period+1:i+1]) / period
    return out


def _atr(highs, lows, closes, period=14):
    out = [None] * len(closes)
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i]  - closes[i-1]))
        trs.append(tr)
    for i in range(period - 1, len(trs)):
        out[i+1] = sum(trs[i-period+1:i+1]) / period
    return out


def _bollinger(closes, period=20, dev=2.0):
    mid = [None] * len(closes)
    up  = [None] * len(closes)
    lo  = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        w = closes[i-period+1:i+1]
        m = sum(w) / period
        s = math.sqrt(sum((x-m)**2 for x in w) / period)
        mid[i] = m
        up[i]  = m + dev * s
        lo[i]  = m - dev * s
    return mid, up, lo


# ---------------------------------------------------------------------------
# 3. ENGINE VSA
# ---------------------------------------------------------------------------

def _vol_percentile(volumes, i, window=20):
    hist = volumes[max(0, i-window):i]
    if not hist:
        return 0.5
    return sum(1 for v in hist if v <= volumes[i]) / len(hist)


def _range_percentile(highs, lows, i, window=20):
    ranges = [highs[j] - lows[j] for j in range(max(0, i-window), i)]
    if not ranges:
        return 0.5
    curr = highs[i] - lows[i]
    return sum(1 for r in ranges if r <= curr) / len(ranges)


def detect_vsa_patterns(data, i):
    """
    Rileva i pattern VSA sulla candela i.
    Tutte le soglie sono percentili relativi al simbolo — non valori assoluti.
    Restituisce lista di dict: {name, direction, strength, desc}
    """
    opens  = data['opens']
    highs  = data['highs']
    lows   = data['lows']
    closes = data['closes']
    vols   = data['volumes']

    c   = closes[i]
    o   = opens[i]
    h   = highs[i]
    lo  = lows[i]
    rng = max(h - lo, 1e-10)

    body_ratio = abs(c - o) / rng
    close_pos  = (c - lo) / rng       # 0=close in fondo, 1=in cima
    is_bull    = c >= o

    vol_pct   = _vol_percentile(vols, i, 20)
    range_pct = _range_percentile(highs, lows, i, 20)

    hist_vols = vols[max(0, i-20):i]
    vol_mean  = sum(hist_vols) / len(hist_vols) if hist_vols else vols[i]
    vol_ratio = vols[i] / vol_mean if vol_mean > 0 else 1.0

    patterns = []

    # ── 1. CLIMAX ─────────────────────────────────────────────────────────
    if vol_pct >= 0.90 and range_pct >= 0.75:
        if is_bull and close_pos >= 0.60:
            s = 3 if vol_pct >= 0.97 else (2 if vol_pct >= 0.93 else 1)
            patterns.append({
                'name': 'Buying Climax', 'direction': 'SHORT', 'strength': s,
                'desc': f'Buying climax — vol {vol_ratio:.1f}× media, range ampio, close in cima → esaurimento compratori',
            })
        elif not is_bull and close_pos <= 0.40:
            s = 3 if vol_pct >= 0.97 else (2 if vol_pct >= 0.93 else 1)
            patterns.append({
                'name': 'Selling Climax', 'direction': 'LONG', 'strength': s,
                'desc': f'Selling climax — vol {vol_ratio:.1f}× media, range ampio, close in fondo → esaurimento venditori',
            })

    # ── 2. EFFORT vs RESULT ───────────────────────────────────────────────
    if vol_pct >= 0.75 and range_pct <= 0.35 and 0.30 <= close_pos <= 0.70:
        s = 2 if vol_pct >= 0.88 else 1
        # Sforzo in una direzione senza movimento = pressione opposta
        # La direzione del segnale è quella che ha "vinto" il braccio di ferro
        direction = 'LONG' if not is_bull else 'SHORT'
        label     = 'Effort↑ No Result' if is_bull else 'Effort↓ No Result'
        patterns.append({
            'name': label, 'direction': direction, 'strength': s,
            'desc': f'Effort senza risultato — vol {vol_ratio:.1f}× ma range stretto → sforzo assorbito',
        })

    # ── 3. STOPPING VOLUME ────────────────────────────────────────────────
    if vol_pct >= 0.80:
        if not is_bull and close_pos >= 0.55:
            s = 3 if (vol_pct >= 0.92 and close_pos >= 0.70) else 2
            patterns.append({
                'name': 'Stopping Volume', 'direction': 'LONG', 'strength': s,
                'desc': f'Stopping volume — candela bearish ma close risale ({close_pos*100:.0f}% range) → domanda assorbe offerta',
            })
        elif is_bull and close_pos <= 0.45:
            s = 3 if (vol_pct >= 0.92 and close_pos <= 0.30) else 2
            patterns.append({
                'name': 'Stopping Volume', 'direction': 'SHORT', 'strength': s,
                'desc': f'Stopping volume — candela bullish ma close ricade ({close_pos*100:.0f}% range) → offerta assorbe domanda',
            })

    # ── 4. NO SUPPLY / NO DEMAND ─────────────────────────────────────────
    if vol_pct <= 0.20 and range_pct <= 0.30 and i >= 5:
        trend = (closes[i] - closes[i-5]) / closes[i-5]
        if trend < -0.005:
            s = 2 if vol_pct <= 0.10 else 1
            patterns.append({
                'name': 'No Supply', 'direction': 'LONG', 'strength': s,
                'desc': f'No Supply — vol {vol_ratio:.2f}× in downtrend → assenza offerta, rimbalzo imminente',
            })
        elif trend > 0.005:
            s = 2 if vol_pct <= 0.10 else 1
            patterns.append({
                'name': 'No Demand', 'direction': 'SHORT', 'strength': s,
                'desc': f'No Demand — vol {vol_ratio:.2f}× in uptrend → assenza domanda, debolezza',
            })

    return patterns


# ---------------------------------------------------------------------------
# 4. CONTESTO BB + EMA
# ---------------------------------------------------------------------------

def get_context(data, i):
    closes = data['closes']
    highs  = data['highs']
    lows   = data['lows']

    ema9  = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    ema50 = _ema(closes, 50)
    _, bb_up, bb_lo = _bollinger(closes, 20)

    c = closes[i]

    # Posizione BB
    bb_pos  = 0.5
    bb_zone = 'mid'
    bb_squeeze = False

    if bb_up[i] and bb_lo[i] and bb_up[i] > bb_lo[i]:
        bw     = bb_up[i] - bb_lo[i]
        bb_pos = (c - bb_lo[i]) / bw

        if   bb_pos <= 0.05:  bb_zone = 'lower_band'
        elif bb_pos <= 0.25:  bb_zone = 'lower_mid'
        elif bb_pos <= 0.75:  bb_zone = 'mid'
        elif bb_pos <= 0.95:  bb_zone = 'upper_mid'
        else:                 bb_zone = 'upper_band'

        recent_bw = [bb_up[j] - bb_lo[j]
                     for j in range(max(0, i-20), i+1)
                     if bb_up[j] and bb_lo[j]]
        if recent_bw:
            avg_bw = sum(recent_bw) / len(recent_bw)
            if bw < avg_bw * 0.70:
                bb_squeeze = True

    # Trend EMA
    ema_trend = 'flat'
    ema_cross = None

    if ema9[i] and ema21[i] and ema50[i]:
        if   ema9[i] > ema21[i] > ema50[i]: ema_trend = 'strong_up'
        elif ema9[i] > ema21[i]:             ema_trend = 'up'
        elif ema9[i] < ema21[i] < ema50[i]: ema_trend = 'strong_down'
        elif ema9[i] < ema21[i]:             ema_trend = 'down'

        if i >= 1 and ema9[i-1] and ema21[i-1]:
            if ema9[i] > ema21[i] and ema9[i-1] <= ema21[i-1]:
                ema_cross = 'bull_cross'
            elif ema9[i] < ema21[i] and ema9[i-1] >= ema21[i-1]:
                ema_cross = 'bear_cross'

    # Context score e descrizioni
    ctx_score = 0
    ctx_desc  = []

    if   bb_zone == 'lower_band': ctx_score += 20; ctx_desc.append("📍 Prezzo sulla banda BB inferiore")
    elif bb_zone == 'lower_mid':  ctx_score += 10; ctx_desc.append("📍 Prezzo metà inferiore BB")
    elif bb_zone == 'upper_band': ctx_score += 20; ctx_desc.append("📍 Prezzo sulla banda BB superiore")
    elif bb_zone == 'upper_mid':  ctx_score += 10; ctx_desc.append("📍 Prezzo metà superiore BB")

    if bb_squeeze:
        ctx_score += 8
        ctx_desc.append("🗜️ BB Squeeze — volatilità compressa")

    if   ema_trend == 'strong_up':   ctx_score += 8;  ctx_desc.append("📈 Trend forte rialzista (EMA9>21>50)")
    elif ema_trend == 'strong_down': ctx_score += 8;  ctx_desc.append("📉 Trend forte ribassista (EMA9<21<50)")
    elif ema_trend in ('up', 'down'): ctx_score += 4

    if ema_cross == 'bull_cross':
        ctx_score += 12; ctx_desc.append("🔀 Cross rialzista EMA9/EMA21")
    elif ema_cross == 'bear_cross':
        ctx_score += 12; ctx_desc.append("🔀 Cross ribassista EMA9/EMA21")

    return {
        'bb_zone':       bb_zone,
        'bb_squeeze':    bb_squeeze,
        'ema_trend':     ema_trend,
        'ema_cross':     ema_cross,
        'context_score': min(ctx_score, 30),
        'context_desc':  ctx_desc,
    }


# ---------------------------------------------------------------------------
# 5. SCORE FINALE
# ---------------------------------------------------------------------------

def compute_opportunity(data, live_price: float = None):
    i = len(data['closes']) - 1

    atr_vals = _atr(data['highs'], data['lows'], data['closes'])
    atr = next((atr_vals[j] for j in range(i, max(i-5, 0), -1)
                if atr_vals[j] is not None), None)

    patterns = detect_vsa_patterns(data, i)
    if not patterns:
        return None

    ctx = get_context(data, i)

    long_w  = sum(p['strength'] for p in patterns if p['direction'] == 'LONG')
    short_w = sum(p['strength'] for p in patterns if p['direction'] == 'SHORT')

    if long_w == short_w:
        return None

    direction     = 'LONG' if long_w > short_w else 'SHORT'
    main_patterns = [p for p in patterns if p['direction'] == direction]
    opp_patterns  = [p for p in patterns if p['direction'] != direction]
    max_strength  = max(p['strength'] for p in main_patterns)

    # Base score VSA
    vsa_base = {1: 30, 2: 50, 3: 65}.get(max_strength, 30)
    if len(main_patterns) >= 2:
        vsa_base = min(vsa_base + 10, 70)

    # Penalità segnali contrari
    if opp_patterns:
        vsa_base -= sum(p['strength'] * 5 for p in opp_patterns)

    # Moltiplicatore contesto
    ctx_mult = 1.0
    if direction == 'LONG':
        if ctx['bb_zone'] in ('lower_band', 'lower_mid'):
            ctx_mult = 1.0
        elif ctx['bb_zone'] in ('upper_band', 'upper_mid'):
            ctx_mult = -0.5
        if ctx['ema_trend'] in ('strong_down', 'down'):
            ctx_mult -= 0.3
    else:
        if ctx['bb_zone'] in ('upper_band', 'upper_mid'):
            ctx_mult = 1.0
        elif ctx['bb_zone'] in ('lower_band', 'lower_mid'):
            ctx_mult = -0.5
        if ctx['ema_trend'] in ('strong_up', 'up'):
            ctx_mult -= 0.3

    context_contribution = int(ctx['context_score'] * max(ctx_mult, -1.0))
    final_score = max(0, min(100, vsa_base + context_contribution))

    # Prezzo: usa live se disponibile, altrimenti close ultima candela
    candle_close = data['closes'][i]
    c = live_price if live_price else candle_close
    price_source = 'live' if live_price else 'candela'

    # Moltiplicatori ATR adattivi per categoria simbolo
    atr_target_mult, atr_stop_mult = _atr_multipliers(data['symbol'])

    if atr:
        if direction == 'LONG':
            target = c + atr * atr_target_mult
            stop   = c - atr * atr_stop_mult
        else:
            target = c - atr * atr_target_mult
            stop   = c + atr * atr_stop_mult
        target_pct = abs(target - c) / c * 100
        stop_pct   = abs(stop   - c) / c * 100
        rr_ratio   = target_pct / stop_pct if stop_pct > 0 else 0
    else:
        target = stop = None
        target_pct = stop_pct = rr_ratio = 0

    return {
        'symbol':       data['symbol'],
        'direction':    direction,
        'score':        final_score,
        'price':        c,
        'price_source': price_source,
        'target':       target,
        'stop':         stop,
        'target_pct':   target_pct,
        'stop_pct':     stop_pct,
        'rr_ratio':     rr_ratio,
        'patterns':     main_patterns,
        'context':      ctx,
        'timestamp':    data['timestamps'][i] if data['timestamps'] else '',
    }


# ---------------------------------------------------------------------------
# 6. FORMATO ALERT
# ---------------------------------------------------------------------------

def _fmt_price(p):
    if p is None: return '—'
    if p >= 1000: return f"${p:,.2f}"
    if p >= 1:    return f"${p:.3f}"
    return f"${p:.6f}"


def format_alert(r):
    dirn  = r['direction']
    score = r['score']
    bar   = '🟩' * (score // 20) + '⬜' * (5 - score // 20)
    emoji = '🟢' if dirn == 'LONG' else '🔴'
    price_tag = '🔴 live' if r.get('price_source') == 'live' else '📊 candela'

    lines = [
        f"⚡ <b>{r['symbol']}</b>  [{dirn}] {emoji}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Score:  <b>{score}/100</b>  {bar}",
        f"Prezzo: <b>{_fmt_price(r['price'])}</b>  ({price_tag})",
        "",
    ]

    if r['target'] and r['stop']:
        t_sign = '+' if dirn == 'LONG' else '-'
        s_sign = '-' if dirn == 'LONG' else '+'
        lines += [
            f"🎯 Target: {_fmt_price(r['target'])}  ({t_sign}{r['target_pct']:.1f}%)",
            f"🛑 Stop:   {_fmt_price(r['stop'])}  ({s_sign}{r['stop_pct']:.1f}%)",
            f"📊 R/R:    1 : {r['rr_ratio']:.1f}",
            "",
        ]

    lines.append("📋 Pattern VSA:")
    for p in r['patterns']:
        stars = '★' * p['strength'] + '☆' * (3 - p['strength'])
        lines.append(f"  [{stars}] {p['name']}")
        lines.append(f"  {p['desc']}")

    if r['context']['context_desc']:
        lines += ["", "📐 Contesto:"]
        for d in r['context']['context_desc']:
            lines.append(f"  {d}")

    lines += ["", f"⏰ {r['timestamp']}  |  15m"]
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 7. TELEGRAM
# ---------------------------------------------------------------------------

async def send_telegram(message, chat_ids):
    if not TELEGRAM_TOKEN or not chat_ids:
        return
    try:
        import aiohttp
    except ImportError:
        print("  ⚠️  aiohttp non disponibile")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as session:
        for chat_id in chat_ids:
            try:
                await session.post(url, json={
                    'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'
                })
            except Exception as e:
                print(f"  ⚠️  Telegram error: {e}")


# ---------------------------------------------------------------------------
# 8. SCAN
# ---------------------------------------------------------------------------

async def run_scan(symbols, min_score, dry_run, chat_ids, verbose=True):
    if verbose:
        t = datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M UTC')
        print(f"\n{'═'*55}")
        print(f"  ml_scanner — VSA + Bollinger + EMA  |  {t}")
        print(f"  Simboli: {len(symbols)}  |  Min score: {min_score}")
        print(f"{'═'*55}\n")

    # Fetch prezzi live (fallback silenzioso se offline)
    live_prices = await fetch_live_prices()
    if verbose:
        src = f"prezzi live: {len(live_prices)} simboli" if live_prices else "prezzi live: non disponibili (uso candele)"
        print(f"  📡 {src}\n")

    opportunities = []
    skipped = 0

    for sym in symbols:
        if not is_market_open(sym):
            continue
        data = load_last_candles(sym, LOOKBACK)
        if data is None:
            skipped += 1
            continue
        live_price = live_prices.get(sym)
        result = compute_opportunity(data, live_price=live_price)
        if result and result['score'] >= min_score:
            opportunities.append(result)

    opportunities.sort(key=lambda r: r['score'], reverse=True)

    if verbose:
        if opportunities:
            print(f"  {'SIMBOLO':<12} {'DIR':<6} {'SCORE':>5}  PATTERN")
            print(f"  {'─'*52}")
            for r in opportunities:
                bar    = '█' * (r['score'] // 10)
                pnames = ' + '.join(p['name'] for p in r['patterns'])
                print(f"  {r['symbol']:<12} {r['direction']:<6} {r['score']:>3}/100  {bar}")
                print(f"    └ {pnames}")
        else:
            print(f"  Nessuna opportunità (score < {min_score})")
        if skipped:
            print(f"\n  ⚠️  {skipped} simboli saltati")
        print(f"\n  Totale: {len(opportunities)} opportunità\n{'═'*55}")

    sent = 0
    for r in opportunities:
        sym, score, dirn = r['symbol'], r['score'], r['direction']
        last = _last_signals.get(sym)
        if last and last['direction'] == dirn and score <= last['score']:
            continue
        _last_signals[sym] = {'score': score, 'direction': dirn, 'time': datetime.now(timezone.utc).replace(tzinfo=None)}
        msg = format_alert(r)
        if verbose:
            print(f"\n{'─'*55}")
            for line in msg.replace('<b>', '').replace('</b>', '').split('\n'):
                print(f"  {line}")
        if not dry_run:
            await send_telegram(msg, chat_ids)
            sent += 1

    if verbose and sent > 0:
        print(f"\n  📬 {sent} alert inviati su Telegram")

    return opportunities


# ---------------------------------------------------------------------------
# 9. DAEMON
# ---------------------------------------------------------------------------

async def daemon_loop(symbols, min_score, chat_ids, interval=15):
    print(f"\n  🤖 Daemon avviato — scan ogni {interval} min  |  {len(symbols)} simboli")
    print(f"  Ctrl+C per fermare\n")
    last_cleanup = datetime.now(timezone.utc).replace(tzinfo=None)

    while True:
        now    = datetime.now(timezone.utc).replace(tzinfo=None)
        next_q = ((now.minute // interval) + 1) * interval
        if next_q >= 60:
            next_q -= 60
            wait = (60 - now.minute - 1) * 60 + (60 - now.second) + next_q * 60
        else:
            wait = (next_q - now.minute) * 60 - now.second
        wait += 30

        next_t = (now + timedelta(seconds=wait)).strftime('%H:%M')
        print(f"  ⏳ Prossimo scan alle {next_t} UTC")
        await asyncio.sleep(wait)

        if (datetime.now(timezone.utc).replace(tzinfo=None) - last_cleanup).total_seconds() > 86400:
            _last_signals.clear()
            last_cleanup = datetime.now(timezone.utc).replace(tzinfo=None)

        try:
            await run_scan(symbols, min_score, dry_run=False,
                           chat_ids=chat_ids, verbose=True)
        except Exception as e:
            print(f"  ❌ Errore: {e}")


# ---------------------------------------------------------------------------
# 10. MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='ml_scanner — VSA + BB + EMA')
    parser.add_argument('--symbol',    default=None)
    parser.add_argument('--min-score', type=int, default=DEFAULT_MIN_SCORE)
    parser.add_argument('--dry-run',   action='store_true')
    parser.add_argument('--daemon',    action='store_true')
    args = parser.parse_args()

    chat_ids = TELEGRAM_CHAT_IDS
    if not chat_ids and not args.dry_run:
        print("  ⚠️  SCANNER_CHAT_IDS non configurato — uso dry-run")
        args.dry_run = True

    symbols = [args.symbol.upper()] if args.symbol else discover_symbols()
    if not symbols:
        print(f"  ❌ Nessun simbolo trovato in {CANDLES_DIR}")
        sys.exit(1)

    if args.daemon:
        asyncio.run(daemon_loop(symbols, args.min_score, chat_ids))
    else:
        asyncio.run(run_scan(symbols, args.min_score, args.dry_run,
                             chat_ids, verbose=True))


if __name__ == '__main__':
    main()
