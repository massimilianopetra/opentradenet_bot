#!/usr/bin/env python3
"""
candle_chart.py — Grafico candlestick multi-timeframe da CSV 15m

Aggrega automaticamente le candele a 15m in 1H o 1D prima di plottare.
Non richiede pandas — usa solo matplotlib e numpy.

Uso standalone:
    python3 candle_chart.py GOLD                          # 15m, ultime 120 candele (~30h)
    python3 candle_chart.py GOLD --bars 200               # 15m, 200 candele
    python3 candle_chart.py GOLD --timeframe 1H           # 1H, ultime 120 ore
    python3 candle_chart.py GOLD --timeframe 1H --bars 72 # 1H, ultime 72 ore (3 giorni)
    python3 candle_chart.py GOLD --timeframe 1D           # 1D, ultimi 120 giorni
    python3 candle_chart.py GOLD --timeframe 1D --bars 60 # 1D, ultimi 60 giorni
    python3 candle_chart.py GOLD --save gold_chart.png    # salva PNG
    python3 candle_chart.py GOLD --data-dir /altro/path   # directory custom

Uso come modulo (da hyperliquid_bot.py):
    import candle_chart as cc
    csv_path = cc.find_csv(symbol, Path(CANDLES_DIR))
    data     = cc.load_csv(csv_path, bars, timeframe='1H')   # ← aggiunto timeframe
    n        = len(data['closes'])
    fig      = cc.plot_chart(data, symbol, timeframe='1H')
    fig.savefig(tmp_path, dpi=110, bbox_inches='tight', facecolor='#0d1117')
"""

import argparse
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------

TIMEFRAMES = {
    '15m': {'minutes': 15,  'label': '15m', 'group_fmt': '%Y-%m-%d %H:%M'},
    '1H':  {'minutes': 60,  'label': '1H',  'group_fmt': '%Y-%m-%d %H:00'},
    '1D':  {'minutes': 1440,'label': '1D',  'group_fmt': '%Y-%m-%d'},
}

# Numero di candele 15m per aggregare ciascun timeframe
TF_CANDLES = {
    '15m': 1,
    '1H':  4,
    '1D':  96,
}

# Default bar counts per timeframe
DEFAULT_BARS = {
    '15m': 120,   # ~30 ore
    '1H':  120,   # 5 giorni
    '1D':  120,   # ~4 mesi
}

# ---------------------------------------------------------------------------
# Ricerca CSV
# ---------------------------------------------------------------------------

def find_csv(symbol: str, data_dir: Path) -> Path:
    """
    Cerca il CSV delle candele 15m del simbolo in varie posizioni standard.
    Lancia FileNotFoundError se non trovato.
    """
    candidates = [
        data_dir / symbol / f"{symbol}_15m.csv",
        data_dir / f"{symbol}_15m.csv",
        data_dir / symbol / f"{symbol}.csv",
        data_dir / f"{symbol}.csv",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        f"Nessun CSV trovato per {symbol} in {data_dir}. "
        f"Cercato: {[str(c) for c in candidates]}"
    )

# ---------------------------------------------------------------------------
# Aggregazione timeframe
# ---------------------------------------------------------------------------

def _group_key(ts_str: str, tf: str) -> str:
    """Calcola la chiave di raggruppamento per una riga CSV dato il timeframe."""
    fmt = TIMEFRAMES[tf]['group_fmt']
    dt  = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
    if tf == '1H':
        return dt.strftime(fmt)
    if tf == '1D':
        return dt.strftime(fmt)
    return ts_str  # 15m: chiave = timestamp stesso


def resample_rows(rows: list, tf: str) -> list:
    """
    Aggrega una lista di righe CSV raw (dict con keys: timestamp,open,high,low,close,volume)
    nel timeframe richiesto.
    Ritorna lista di dict con le stesse chiavi.
    """
    if tf == '15m':
        return rows  # nessuna aggregazione

    groups: dict = {}
    order:  list = []

    for row in rows:
        key = _group_key(row['timestamp'], tf)
        if key not in groups:
            groups[key] = {
                'timestamp': row['timestamp'],
                'open':      row['open'],
                'high':      row['high'],
                'low':       row['low'],
                'close':     row['close'],
                'volume':    row['volume'],
            }
            order.append(key)
        else:
            g = groups[key]
            g['high']   = max(g['high'],   row['high'])
            g['low']    = min(g['low'],    row['low'])
            g['close']  = row['close']
            g['volume'] += row['volume']

    return [groups[k] for k in order]

# ---------------------------------------------------------------------------
# Lettura CSV + aggregazione
# ---------------------------------------------------------------------------

def load_csv(csv_path: Path, bars: int = 120, timeframe: str = '15m') -> dict:
    """
    Legge il CSV delle candele 15m, aggrega nel timeframe richiesto,
    ritorna un dict con arrays numpy pronti per il plot.

    Args:
        csv_path:  percorso al CSV 15m
        bars:      numero di candele del timeframe richiesto da restituire
        timeframe: '15m' | '1H' | '1D'

    Returns:
        dict con keys: timestamps, opens, highs, lows, closes, volumes
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Timeframe non valido: {timeframe}. Usa: {list(TIMEFRAMES.keys())}")

    # Per avere 'bars' candele nel TF finale, dobbiamo leggere abbastanza 15m
    # Leggiamo sempre tutto (efficiente: file raramente > 100k righe) poi tagliamo
    raw_rows = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                raw_rows.append({
                    'timestamp': row['timestamp'],
                    'open':      float(row['open']),
                    'high':      float(row['high']),
                    'low':       float(row['low']),
                    'close':     float(row['close']),
                    'volume':    float(row['volume']),
                })
            except (ValueError, KeyError):
                continue

    if not raw_rows:
        raise ValueError(f"CSV vuoto o malformato: {csv_path}")

    # Aggrega nel timeframe
    resampled = resample_rows(raw_rows, timeframe)

    # Taglia alle ultime 'bars' candele
    resampled = resampled[-bars:] if len(resampled) > bars else resampled

    if not resampled:
        raise ValueError(f"Nessuna candela dopo il resampling a {timeframe}")

    timestamps = [r['timestamp'] for r in resampled]
    opens      = np.array([r['open']   for r in resampled], dtype=float)
    highs      = np.array([r['high']   for r in resampled], dtype=float)
    lows       = np.array([r['low']    for r in resampled], dtype=float)
    closes     = np.array([r['close']  for r in resampled], dtype=float)
    volumes    = np.array([r['volume'] for r in resampled], dtype=float)

    return {
        'timestamps': timestamps,
        'opens':      opens,
        'highs':      highs,
        'lows':       lows,
        'closes':     closes,
        'volumes':    volumes,
    }

# ---------------------------------------------------------------------------
# Indicatori tecnici (pure Python / numpy)
# ---------------------------------------------------------------------------

def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    out = np.full_like(arr, np.nan)
    k   = 2.0 / (period + 1)
    # Primo valore = media semplice
    start = period - 1
    if start >= len(arr):
        return out
    out[start] = np.mean(arr[:period])
    for i in range(start + 1, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def _rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    out    = np.full_like(closes, np.nan)
    deltas = np.diff(closes)
    if len(deltas) < period:
        return out
    gains  = np.where(deltas > 0, deltas,  0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = np.mean(gains[:period])
    avg_l  = np.mean(losses[:period])
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i])  / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs    = avg_g / avg_l if avg_l != 0 else 1e9
        out[i + 1] = 100 - 100 / (1 + rs)
    return out


def _macd(closes: np.ndarray, fast=12, slow=26, signal=9):
    ema_fast   = _ema(closes, fast)
    ema_slow   = _ema(closes, slow)
    macd_line  = ema_fast - ema_slow
    valid      = ~np.isnan(macd_line)
    signal_line = np.full_like(macd_line, np.nan)
    idx        = np.where(valid)[0]
    if len(idx) >= signal:
        tmp    = _ema(macd_line[idx], signal)
        signal_line[idx] = tmp
    histogram  = macd_line - signal_line
    return macd_line, signal_line, histogram


def _bollinger(closes: np.ndarray, period: int = 20, std_dev: float = 2.0):
    upper = np.full_like(closes, np.nan)
    lower = np.full_like(closes, np.nan)
    mid   = np.full_like(closes, np.nan)
    for i in range(period - 1, len(closes)):
        window  = closes[i - period + 1 : i + 1]
        m       = np.mean(window)
        s       = np.std(window)
        mid[i]  = m
        upper[i] = m + std_dev * s
        lower[i] = m - std_dev * s
    return upper, mid, lower


def _linear_regression_channel(closes: np.ndarray, n: int, bars: int = 120,
                               k: float = 2.0) -> dict:
    """
    Canale di regressione lineare (least-squares) sulle ultime `bars` candele.

    Ritorna dict con array (lunghezza n, NaN fuori dalla finestra) per
    mid/upper/lower e l'indice di inizio finestra, così plot_chart può
    disegnare il canale solo sul segmento coperto dal fit, come in TradingView.
    """
    mid   = np.full(n, np.nan)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)

    start = max(0, n - bars)
    xs      = np.arange(start, n)
    ys      = closes[start:n]
    if len(xs) < 2:
        return {'mid': mid, 'upper': upper, 'lower': lower, 'start': start}

    slope, intercept = np.polyfit(xs, ys, 1)
    fit  = slope * xs + intercept
    std  = float(np.std(ys - fit))

    mid[start:n]   = fit
    upper[start:n] = fit + k * std
    lower[start:n] = fit - k * std
    return {'mid': mid, 'upper': upper, 'lower': lower, 'start': start}

# ---------------------------------------------------------------------------
# Formattazione etichette asse X per ogni timeframe
# ---------------------------------------------------------------------------

def _x_labels(timestamps: list, tf: str, max_labels: int = 8) -> tuple:
    """
    Ritorna (positions, labels) per l'asse X del grafico principale.
    Adatta la granularità in base al timeframe.
    """
    n     = len(timestamps)
    step  = max(1, n // max_labels)
    pos   = list(range(0, n, step))

    if tf == '15m':
        fmt = '%d/%m %H:%M'
    elif tf == '1H':
        fmt = '%d/%m %H:00'
    else:  # 1D
        fmt = '%d/%m/%y'

    labels = []
    for i in pos:
        try:
            dt = datetime.strptime(timestamps[i], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            try:
                dt = datetime.strptime(timestamps[i], '%Y-%m-%d')
            except ValueError:
                labels.append(timestamps[i][:10])
                continue
        labels.append(dt.strftime(fmt))

    return pos, labels

# ---------------------------------------------------------------------------
# Candela live parziale
# ---------------------------------------------------------------------------

def append_live_candle(data: dict, live_candle) -> dict:
    """
    Aggiunge una candela parziale in coda al dizionario data senza modificare
    i dati originali (non-distruttiva).

    Args:
        data:        output di load_csv()
        live_candle: float → candela sintetica fallback:
                         open = close[-1], high/low da live_price, volume = 0
                     dict  → candela OHLC reale da API, con chiavi:
                         open, high, low, close, volume, timestamp
                         (timestamp in ms int oppure stringa ISO)

    Returns:
        Nuovo dict con le stesse chiavi di load_csv() più live_candle=True.
    """
    # Calcola il timestamp di default (ultima candela + 15 min) per il fallback
    last_ts = data['timestamps'][-1]
    try:
        _dt = datetime.strptime(last_ts, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        try:
            _dt = datetime.strptime(last_ts, '%Y-%m-%d')
        except ValueError:
            _dt = datetime.now()
    default_ts = (_dt + timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')

    if isinstance(live_candle, dict):
        # Dati OHLC reali dall'API
        live_open   = float(live_candle['open'])
        live_high   = float(live_candle['high'])
        live_low    = float(live_candle['low'])
        live_close  = float(live_candle['close'])
        live_volume = float(live_candle.get('volume', 0.0))
        ts_raw = live_candle.get('timestamp')
        if isinstance(ts_raw, (int, float)):
            # Timestamp in millisecondi → stringa ISO
            live_ts = datetime.utcfromtimestamp(ts_raw / 1000).strftime('%Y-%m-%d %H:%M:%S')
        elif isinstance(ts_raw, str):
            live_ts = ts_raw
        else:
            live_ts = default_ts
    else:
        # Fallback sintetico: solo il prezzo last è noto
        live_price  = float(live_candle)
        last_close  = float(data['closes'][-1])
        live_open   = last_close
        live_high   = max(live_open, live_price)
        live_low    = min(live_open, live_price)
        live_close  = live_price
        live_volume = 0.0
        live_ts     = default_ts

    return {
        'timestamps':  list(data['timestamps']) + [live_ts],
        'opens':       np.append(data['opens'],   live_open),
        'highs':       np.append(data['highs'],   live_high),
        'lows':        np.append(data['lows'],    live_low),
        'closes':      np.append(data['closes'],  live_close),
        'volumes':     np.append(data['volumes'], live_volume),
        'live_candle': True,
    }


# ---------------------------------------------------------------------------
# Plot principale
# ---------------------------------------------------------------------------

def _draw_pattern_panel(ax, opens, highs, lows, closes, n, pattern_info):
    """
    Disegna il pannello delle ultime 5 candele con etichette pattern.
    Usato internamente da plot_chart quando pattern_info non è None.
    """
    _DIR_COLORS = {'LONG': '#26a641', 'SHORT': '#f85149', 'NEUTRAL': '#ffd700'}

    num_candles = min(5, n)
    start_idx   = n - num_candles

    # Mappa abs_idx → (name, direction, strength): per ogni candela prende il
    # pattern con strength maggiore tra quelli che la includono.
    candle_pattern_map = {}
    for pat in pattern_info:
        for ci in pat.get('candle_indices', []):
            if ci < 0 or ci >= n:
                continue
            existing = candle_pattern_map.get(ci)
            if existing is None or pat['strength'] > existing[2]:
                candle_pattern_map[ci] = (pat['name'], pat['direction'], pat['strength'])

    ax.set_facecolor('#0d1117')
    ax.tick_params(colors='#8b949e', labelsize=8)
    for spine in ax.spines.values():
        spine.set_color('#30363d')
    ax.yaxis.label.set_color('#8b949e')

    w_body = 0.35
    seg_highs = highs[start_idx : start_idx + num_candles]
    seg_lows  = lows[start_idx  : start_idx + num_candles]

    for local_idx in range(num_candles):
        abs_idx = start_idx + local_idx
        o = opens[abs_idx];  h = highs[abs_idx]
        l = lows[abs_idx];   c = closes[abs_idx]
        bull   = c >= o
        col    = '#26a641' if bull else '#f85149'
        lo_b   = min(o, c);  hi_b = max(o, c)
        body_h = max(hi_b - lo_b, (h - l) * 0.002)
        ax.bar(local_idx, body_h,  bottom=lo_b, width=w_body,        color=col, zorder=3)
        ax.bar(local_idx, h - l,   bottom=l,    width=w_body * 0.15, color=col, zorder=2)

    y_min   = float(np.min(seg_lows))
    y_max   = float(np.max(seg_highs))
    y_range = max(y_max - y_min, abs(y_max + y_min) * 0.001, 1e-8)
    y_label = y_min - y_range * 0.14

    for local_idx in range(num_candles):
        abs_idx = start_idx + local_idx
        rel_pos = local_idx - (num_candles - 1)   # -4 … 0

        pat_entry = candle_pattern_map.get(abs_idx)
        if pat_entry:
            label      = pat_entry[0]
            color      = _DIR_COLORS.get(pat_entry[1], '#ffd700')
            fontweight = 'bold'
            ax.text(local_idx, y_label, label,
                    ha='right', va='top', fontsize=8, rotation=45,
                    color=color, fontweight=fontweight, clip_on=False)
        else:
            label = str(rel_pos)
            ax.text(local_idx, y_label, label,
                    ha='center', va='top', fontsize=8, rotation=0,
                    color='#c9d1d9', fontweight='normal', clip_on=False)

    ax.set_xlim(-0.7, num_candles - 0.3)
    ax.set_ylim(y_label - y_range * 0.04, y_max + y_range * 0.12)
    ax.set_xticks([])
    ax.set_yticks([])

    pat_names = ', '.join(dict.fromkeys(p['name'] for p in pattern_info))
    ax.set_title(f'\U0001f56f Pattern rilevato \u2014 {pat_names}',
                 color='white', fontsize=9, loc='left', pad=4)


def plot_chart(data: dict, symbol: str, timeframe: str = '15m',
               pattern_info: list | None = None,
               show_regression: bool = False,
               regression_bars: int = 120) -> plt.Figure:
    """
    Genera il grafico a 4 pannelli: candele+BB+EMA, volume, RSI, MACD.
    Se pattern_info non è None aggiunge un 5° pannello con le ultime 5 candele
    e le etichette dei pattern rilevati.

    Args:
        data:            output di load_csv()
        symbol:          nome simbolo (per il titolo)
        timeframe:       '15m' | '1H' | '1D'
        pattern_info:    lista di dict con campi name, direction, candle_indices.
                         Se None il pannello pattern non viene aggiunto e il
                         grafico è identico alla versione precedente.
        show_regression: se True disegna il canale di regressione lineare
                         (mediana + bande a ±2 deviazioni standard) sulle
                         ultime `regression_bars` candele (default False).
        regression_bars: numero di candele su cui calcolare il fit lineare
                         (default 120).

    Returns:
        matplotlib Figure
    """
    timestamps = data['timestamps']
    opens      = data['opens']
    highs      = data['highs']
    lows       = data['lows']
    closes     = data['closes']
    volumes    = data['volumes']
    n          = len(closes)
    x          = np.arange(n)

    # ── Indicatori ──────────────────────────────────────────────────────
    ema9   = _ema(closes, 9)
    ema21  = _ema(closes, 21)
    ema50  = _ema(closes, 50)
    bb_up, bb_mid, bb_lo = _bollinger(closes, 20, 2.0)
    rsi    = _rsi(closes, 14)
    macd_l, macd_s, macd_h = _macd(closes, 12, 26, 9)
    vol_ma = np.full(n, np.nan)
    for i in range(19, n):
        vol_ma[i] = np.mean(volumes[i - 19 : i + 1])
    if show_regression:
        regression = _linear_regression_channel(closes, n, bars=regression_bars)
    else:
        regression = None

    # ── Layout ──────────────────────────────────────────────────────────
    if pattern_info is not None:
        fig = plt.figure(figsize=(16, 16), facecolor='#0d1117')
        gs  = fig.add_gridspec(5, 1, height_ratios=[5, 1.5, 1.5, 1.5, 2.5],
                               hspace=0.08, left=0.07, right=0.97,
                               top=0.94, bottom=0.06)
    else:
        fig = plt.figure(figsize=(16, 12), facecolor='#0d1117')
        gs  = fig.add_gridspec(4, 1, height_ratios=[5, 1.5, 1.5, 1.5],
                               hspace=0.05, left=0.07, right=0.97,
                               top=0.94, bottom=0.06)

    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax3 = fig.add_subplot(gs[2], sharex=ax1)
    ax4 = fig.add_subplot(gs[3], sharex=ax1)

    for ax in (ax1, ax2, ax3, ax4):
        ax.set_facecolor('#0d1117')
        ax.tick_params(colors='#8b949e', labelsize=8)
        ax.spines['bottom'].set_color('#30363d')
        ax.spines['top'].set_color('#30363d')
        ax.spines['left'].set_color('#30363d')
        ax.spines['right'].set_color('#30363d')
        ax.yaxis.label.set_color('#8b949e')

    # ── Pannello 1: Candlestick + BB + EMA + S&R ────────────────────────
    w_body   = 0.6
    w_wick   = 0.8
    is_live  = data.get('live_candle', False)   # ultima candela è sintetica?

    for i in x:
        bull     = closes[i] >= opens[i]
        col      = '#26a641' if bull else '#f85149'
        lo_b     = min(opens[i], closes[i])
        hi_b     = max(opens[i], closes[i])
        body_h   = max(hi_b - lo_b, (highs[i] - lows[i]) * 0.002)
        live_col = ('#74c99a' if bull else '#f4a59a') if (is_live and i == n - 1) else col

        # Wick
        ax1.bar(i, highs[i] - lows[i], bottom=lows[i], width=w_wick * 0.12,
                color=live_col, zorder=2)
        # Corpo
        ax1.bar(i, body_h, bottom=lo_b, width=w_body,
                color=live_col, alpha=0.85 if (is_live and i == n - 1) else 1.0, zorder=3)

    # Bollinger Bands
    valid_bb = ~np.isnan(bb_up)
    if valid_bb.any():
        ax1.plot(x[valid_bb], bb_up[valid_bb],  color='#58a6ff', lw=0.8, ls='--', alpha=0.6, label='BB up')
        ax1.plot(x[valid_bb], bb_lo[valid_bb],  color='#58a6ff', lw=0.8, ls='--', alpha=0.6, label='BB lo')
        ax1.fill_between(x[valid_bb], bb_up[valid_bb], bb_lo[valid_bb],
                         alpha=0.05, color='#58a6ff')

    # EMA
    for ema_arr, col, lbl in [(ema9, '#ffd700', 'EMA9'), (ema21, '#ff6b6b', 'EMA21'), (ema50, '#4ecdc4', 'EMA50')]:
        v = ~np.isnan(ema_arr)
        if v.any():
            ax1.plot(x[v], ema_arr[v], color=col, lw=1.0, alpha=0.85, label=lbl)

    # Canale di regressione lineare (mediana + bande ±2 dev.std)
    if regression is not None:
        r_valid = ~np.isnan(regression['mid'])
        if r_valid.any():
            rx  = x[r_valid]
            mid = regression['mid'][r_valid]
            up  = regression['upper'][r_valid]
            lo  = regression['lower'][r_valid]
            ax1.plot(rx, mid, color='#58a6ff', lw=1.0, ls='--', alpha=0.8,
                     label=f"LR ({regression_bars})", zorder=4)
            ax1.plot(rx, up, color='#58a6ff', lw=0.9, alpha=0.7, zorder=4)
            ax1.plot(rx, lo, color='#58a6ff', lw=0.9, alpha=0.7, zorder=4)
            ax1.fill_between(rx, up, mid, color='#58a6ff', alpha=0.12, zorder=1)
            ax1.fill_between(rx, mid, lo, color='#f85149', alpha=0.12, zorder=1)

    ax1.set_xlim(-1, n)
    ax1.legend(loc='upper left', fontsize=7, facecolor='#161b22',
               labelcolor='#8b949e', framealpha=0.8)

    # Titolo
    last_close = closes[-1]
    pct_chg    = ((closes[-1] - closes[0]) / closes[0] * 100) if closes[0] != 0 else 0
    sign       = '+' if pct_chg >= 0 else ''
    tf_label   = TIMEFRAMES[timeframe]['label']
    live_note  = '  · live' if is_live else ''
    ax1.set_title(
        f"{symbol}  ·  {tf_label}  ·  {n} candele  ·  "
        f"Ultimo: {last_close:.4g}  ({sign}{pct_chg:.2f}%){live_note}",
        color='#e6edf3', fontsize=11, pad=8, loc='left'
    )

    # ── Pannello 2: Volume ───────────────────────────────────────────────
    bar_colors = ['#26a641' if closes[i] >= opens[i] else '#f85149' for i in x]
    ax2.bar(x, volumes, color=bar_colors, alpha=0.7, zorder=2)
    v_ma_valid = ~np.isnan(vol_ma)
    if v_ma_valid.any():
        ax2.plot(x[v_ma_valid], vol_ma[v_ma_valid],
                 color='#ffd700', lw=1.0, alpha=0.8, label='Vol MA20')
    ax2.set_ylabel('Volume', fontsize=8)
    ax2.legend(loc='upper left', fontsize=7, facecolor='#161b22',
               labelcolor='#8b949e', framealpha=0.8)

    # ── Pannello 3: RSI ──────────────────────────────────────────────────
    rsi_valid = ~np.isnan(rsi)
    if rsi_valid.any():
        ax3.plot(x[rsi_valid], rsi[rsi_valid], color='#c9d1d9', lw=1.0)
    ax3.axhline(70, color='#f85149', lw=0.7, ls='--', alpha=0.7)
    ax3.axhline(30, color='#26a641', lw=0.7, ls='--', alpha=0.7)
    ax3.fill_between(x[rsi_valid], rsi[rsi_valid], 70,
                     where=rsi[rsi_valid] >= 70, alpha=0.15, color='#f85149')
    ax3.fill_between(x[rsi_valid], rsi[rsi_valid], 30,
                     where=rsi[rsi_valid] <= 30, alpha=0.15, color='#26a641')
    ax3.set_ylim(0, 100)
    ax3.set_ylabel('RSI 14', fontsize=8)
    ax3.set_yticks([30, 50, 70])

    # ── Pannello 4: MACD ─────────────────────────────────────────────────
    macd_valid = ~np.isnan(macd_h)
    if macd_valid.any():
        mv = macd_h[macd_valid]
        hist_colors = []
        for i, v in enumerate(mv):
            prev = mv[i - 1] if i > 0 else v
            if v >= 0:
                # verde pieno = cresce, verde chiaro = indebolisce
                hist_colors.append('#26a641' if v >= prev else '#74c99a')
            else:
                # rosso pieno = cala ulteriormente, rosso chiaro = si riduce
                hist_colors.append('#f85149' if v <= prev else '#f4a59a')
        ax4.bar(x[macd_valid], mv, color=hist_colors, alpha=0.85, zorder=2)
    ml_valid = ~np.isnan(macd_l)
    ms_valid = ~np.isnan(macd_s)
    if ml_valid.any():
        ax4.plot(x[ml_valid], macd_l[ml_valid], color='#58a6ff', lw=1.0, label='MACD')
    if ms_valid.any():
        ax4.plot(x[ms_valid], macd_s[ms_valid], color='#ffd700', lw=1.0, label='Signal')
    ax4.axhline(0, color='#30363d', lw=0.8)
    ax4.set_ylabel('MACD', fontsize=8)
    ax4.legend(loc='upper left', fontsize=7, facecolor='#161b22',
               labelcolor='#8b949e', framealpha=0.8)

    # ── Asse X ───────────────────────────────────────────────────────────
    x_pos, x_lbl = _x_labels(timestamps, timeframe)
    plt.setp(ax1.get_xticklabels(), visible=False)
    plt.setp(ax2.get_xticklabels(), visible=False)
    plt.setp(ax3.get_xticklabels(), visible=False)

    if pattern_info is not None:
        plt.setp(ax4.get_xticklabels(), visible=False)
        ax5 = fig.add_subplot(gs[4])
        _draw_pattern_panel(ax5, opens, highs, lows, closes, n, pattern_info)
    else:
        ax4.set_xticks(x_pos)
        ax4.set_xticklabels(x_lbl, rotation=30, ha='right', fontsize=7)

    return fig

# ---------------------------------------------------------------------------
# Main standalone
# ---------------------------------------------------------------------------

def main():
    # Risolve il path relativo ai dati rispetto alla posizione dello script
    script_dir  = Path(__file__).parent
    default_dir = str(script_dir / 'data' / 'candles')

    parser = argparse.ArgumentParser(
        description='Grafico candlestick multi-timeframe da CSV 15m'
    )
    parser.add_argument('symbol',
                        help='Simbolo da graficare (es. GOLD, BTC)')
    parser.add_argument('--bars', type=int, default=None,
                        help='Numero di candele da visualizzare (default: 120 per tutti i TF)')
    parser.add_argument('--timeframe', choices=list(TIMEFRAMES.keys()), default='15m',
                        help='Timeframe: 15m (default), 1H, 1D')
    parser.add_argument('--save',
                        help='Salva il grafico in questo file PNG invece di aprirlo')
    parser.add_argument('--data-dir', default=default_dir,
                        help=f'Directory base dei CSV (default: {default_dir})')
    parser.add_argument('--regression', action='store_true',
                        help='Disegna il canale di regressione lineare')
    parser.add_argument('--regression-bars', type=int, default=120,
                        help='Numero di candele su cui calcolare la regressione (default: 120)')
    args = parser.parse_args()

    symbol    = args.symbol.upper()
    timeframe = args.timeframe
    bars      = args.bars if args.bars is not None else DEFAULT_BARS[timeframe]
    data_dir  = Path(args.data_dir)

    print(f"📊 {symbol}  TF={timeframe}  bars={bars}  dir={data_dir}")

    try:
        csv_path = find_csv(symbol, data_dir)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)

    print(f"📂 CSV: {csv_path}")

    try:
        data = load_csv(csv_path, bars, timeframe)
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    n = len(data['closes'])
    print(f"✅ Caricate {n} candele {timeframe}")

    fig = plot_chart(data, symbol, timeframe,
                     show_regression=args.regression,
                     regression_bars=args.regression_bars)

    if args.save:
        fig.savefig(args.save, dpi=110, bbox_inches='tight', facecolor='#0d1117')
        print(f"💾 Salvato: {args.save}")
    else:
        plt.show()

    plt.close(fig)


if __name__ == '__main__':
    main()
