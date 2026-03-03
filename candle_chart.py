#!/usr/bin/env python3
"""
candle_chart.py — Grafico candlestick interattivo da CSV
Struttura attesa: data/candles/SIMBOLO/SIMBOLO_15m.csv
                  oppure: data/candles/SIMBOLO_15m.csv

Dipendenze: matplotlib, numpy  (no pandas)

Uso:
    python candle_chart.py GOLD
    python candle_chart.py SILVER --bars 96
    python candle_chart.py GOLD --bars 200 --data-dir /altro/percorso
    python candle_chart.py GOLD --save chart.png
"""

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


# ---------------------------------------------------------------------------
# Ricerca file CSV
# ---------------------------------------------------------------------------

def find_csv(symbol: str, data_dir: Path) -> Path:
    symbol = symbol.upper()
    candidates = [
        data_dir / symbol / f"{symbol}_15m.csv",
        data_dir / f"{symbol}_15m.csv",
        data_dir / symbol / f"{symbol}.csv",
        data_dir / f"{symbol}.csv",
    ]
    for p in candidates:
        if p.exists():
            return p
    for p in data_dir.rglob(f"*{symbol}*.csv"):
        return p
    raise FileNotFoundError(
        f"Nessun CSV trovato per '{symbol}' in {data_dir}\n"
        "Percorsi cercati:\n" + "\n".join(f"  {c}" for c in candidates)
    )


# ---------------------------------------------------------------------------
# Lettura CSV senza pandas — split puro
# ---------------------------------------------------------------------------

def load_csv(path: Path, bars: int) -> dict:
    """
    Legge il CSV riga per riga con split(',').
    Formato atteso: timestamp,open,high,low,close,volume
    Ritorna dict di liste Python sliciate alle ultime `bars` righe.
    """
    dates, opens, highs, lows, closes, volumes = [], [], [], [], [], []

    with open(path, encoding='utf-8') as fh:
        header = True
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if header:
                header = False
                continue
            parts = line.split(',')
            if len(parts) < 6:
                continue
            try:
                dates.append(parts[0].strip())
                opens.append(float(parts[1]))
                highs.append(float(parts[2]))
                lows.append(float(parts[3]))
                closes.append(float(parts[4]))
                volumes.append(float(parts[5]))
            except ValueError:
                continue

    n = min(bars, len(dates))
    return {
        'dates':   dates[-n:],
        'opens':   opens[-n:],
        'highs':   highs[-n:],
        'lows':    lows[-n:],
        'closes':  closes[-n:],
        'volumes': volumes[-n:],
        'total':   len(dates),
    }


# ---------------------------------------------------------------------------
# Indicatori tecnici — liste Python puro + numpy solo per il plot
# ---------------------------------------------------------------------------

def calc_ema(values: list, period: int) -> list:
    result = [None] * len(values)
    if len(values) < period:
        return result
    k = 2.0 / (period + 1)
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def calc_rsi(closes: list, period: int = 14) -> list:
    result = [None] * len(closes)
    if len(closes) <= period:
        return result
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, period + 1)]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, period + 1)]
    avg_g  = sum(gains)  / period
    avg_l  = sum(losses) / period
    result[period] = 100 - (100 / (1 + avg_g / avg_l)) if avg_l else 100.0
    for i in range(period + 1, len(closes)):
        d      = closes[i] - closes[i - 1]
        avg_g  = (avg_g * (period - 1) + max(d,  0)) / period
        avg_l  = (avg_l * (period - 1) + max(-d, 0)) / period
        result[i] = 100 - (100 / (1 + avg_g / avg_l)) if avg_l else 100.0
    return result


def calc_macd(closes: list, fast=12, slow=26, signal=9):
    ef = calc_ema(closes, fast)
    es = calc_ema(closes, slow)
    ml = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ef, es)
    ]
    # EMA del macd_line sui soli valori non-None
    valid = [(i, v) for i, v in enumerate(ml) if v is not None]
    sl    = [None] * len(ml)
    if valid:
        sub_vals = [v for _, v in valid]
        sub_ema  = calc_ema(sub_vals, signal)
        for j, (i, _) in enumerate(valid):
            sl[i] = sub_ema[j]
    hist = [
        (m - s) if m is not None and s is not None else None
        for m, s in zip(ml, sl)
    ]
    return ml, sl, hist


def calc_bollinger(closes: list, period=20, mult=2):
    upper, mid, lower = [None]*len(closes), [None]*len(closes), [None]*len(closes)
    for i in range(period - 1, len(closes)):
        w  = closes[i - period + 1 : i + 1]
        m  = sum(w) / period
        sd = (sum((v - m) ** 2 for v in w) / period) ** 0.5
        mid[i]   = m
        upper[i] = m + mult * sd
        lower[i] = m - mult * sd
    return upper, mid, lower


def calc_vol_ma(volumes: list, period=20) -> list:
    result = [None] * len(volumes)
    for i in range(period - 1, len(volumes)):
        result[i] = sum(volumes[i - period + 1 : i + 1]) / period
    return result


# ---------------------------------------------------------------------------
# Supporti / Resistenze (pivot locali)
# ---------------------------------------------------------------------------

def find_pivots(highs: list, lows: list, window: int = 10):
    res, sup = [], []
    n = len(highs)
    for i in range(window, n - window):
        if highs[i] == max(highs[i - window : i + window + 1]):
            res.append(highs[i])
        if lows[i] == min(lows[i - window : i + window + 1]):
            sup.append(lows[i])

    def dedup(levels):
        out = []
        for lv in sorted(set(levels)):
            if not out or abs(lv - out[-1]) / out[-1] > 0.002:
                out.append(lv)
        return out

    return dedup(res)[-4:], dedup(sup)[:4]


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def to_arr(lst: list) -> np.ndarray:
    return np.array([v if v is not None else np.nan for v in lst], dtype=float)


def fmt_date(s: str) -> str:
    """'2026-03-01 14:30:00' → '01/03 14:30'"""
    s = s.strip()
    parts = s.split(' ')
    if len(parts) >= 2:
        d = parts[0].split('-')
        t = parts[1][:5]
        if len(d) == 3:
            return f"{d[2]}/{d[1]} {t}"
    return s[:16]


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

def plot_chart(data: dict, symbol: str):
    closes  = data['closes']
    opens   = data['opens']
    highs   = data['highs']
    lows    = data['lows']
    volumes = data['volumes']
    dates   = data['dates']
    n       = len(closes)

    ema9  = to_arr(calc_ema(closes, 9))
    ema21 = to_arr(calc_ema(closes, 21))
    ema50 = to_arr(calc_ema(closes, 50))
    bb_u, bb_m, bb_l = calc_bollinger(closes)
    bb_u, bb_m, bb_l = to_arr(bb_u), to_arr(bb_m), to_arr(bb_l)
    rsi_v = to_arr(calc_rsi(closes))
    ml, sl, hist = calc_macd(closes)
    ml, sl, hist = to_arr(ml), to_arr(sl), to_arr(hist)
    vol_ma = to_arr(calc_vol_ma(volumes))

    res_lvls, sup_lvls = find_pivots(highs, lows)

    x        = np.arange(n)
    col_up   = '#26a641'
    col_down = '#e05c5c'
    W        = 0.6

    # ── Layout ────────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(18, 14), facecolor='#0d1117')
    gs  = fig.add_gridspec(4, 1, height_ratios=[5, 1.5, 1.5, 1.5],
                           hspace=0.06, left=0.06, right=0.97,
                           top=0.94, bottom=0.06)
    ax_c = fig.add_subplot(gs[0])
    ax_v = fig.add_subplot(gs[1], sharex=ax_c)
    ax_r = fig.add_subplot(gs[2], sharex=ax_c)
    ax_m = fig.add_subplot(gs[3], sharex=ax_c)

    for ax in [ax_c, ax_v, ax_r, ax_m]:
        ax.set_facecolor('#0d1117')
        ax.tick_params(colors='#8b949e', labelsize=8)
        ax.yaxis.label.set_color('#8b949e')
        for sp in ax.spines.values():
            sp.set_edgecolor('#21262d')
    for ax in [ax_c, ax_v, ax_r]:
        plt.setp(ax.get_xticklabels(), visible=False)

    # ── Candele ───────────────────────────────────────────────────────────
    for i in range(n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        col = col_up if c >= o else col_down
        ax_c.plot([i, i], [l, h], color=col, linewidth=0.8, zorder=2)
        ax_c.bar(i, abs(c - o), bottom=min(o, c), width=W,
                 color=col, alpha=0.9, zorder=3)

    # Bollinger
    ax_c.fill_between(x, bb_u, bb_l, alpha=0.06, color='#58a6ff', label='BB')
    ax_c.plot(x, bb_u, color='#58a6ff', linewidth=0.6, alpha=0.5)
    ax_c.plot(x, bb_m, color='#58a6ff', linewidth=0.6, alpha=0.3, linestyle='--')
    ax_c.plot(x, bb_l, color='#58a6ff', linewidth=0.6, alpha=0.5)

    # EMA
    ax_c.plot(x, ema9,  color='#f0b429', linewidth=1.0, label='EMA 9',  zorder=4)
    ax_c.plot(x, ema21, color='#ff7b72', linewidth=1.0, label='EMA 21', zorder=4)
    ax_c.plot(x, ema50, color='#79c0ff', linewidth=1.2, label='EMA 50', zorder=4)

    # Supporti / Resistenze
    pr = max(highs) - min(lows)
    for lv in res_lvls:
        if min(lows) - pr * 0.1 < lv < max(highs) + pr * 0.1:
            ax_c.axhline(lv, color='#e05c5c', linewidth=0.8, linestyle='--', alpha=0.7)
            ax_c.text(n - 1, lv, f'R {lv:.2f}', color='#e05c5c',
                      fontsize=7, va='bottom', ha='right')
    for lv in sup_lvls:
        if min(lows) - pr * 0.1 < lv < max(highs) + pr * 0.1:
            ax_c.axhline(lv, color='#26a641', linewidth=0.8, linestyle='--', alpha=0.7)
            ax_c.text(n - 1, lv, f'S {lv:.2f}', color='#26a641',
                      fontsize=7, va='top', ha='right')

    # Ultimo prezzo
    ax_c.axhline(closes[-1], color='#ffffff', linewidth=0.5, linestyle=':', alpha=0.5)
    ax_c.text(0, closes[-1], f'{closes[-1]:.2f}', color='#ffffff', fontsize=8,
              va='center', bbox=dict(facecolor='#21262d', edgecolor='none', pad=2))

    ax_c.set_ylabel('Prezzo ($)', color='#8b949e', fontsize=9)
    ax_c.legend(loc='upper left', fontsize=8, facecolor='#161b22',
                edgecolor='#21262d', labelcolor='#c9d1d9')
    ax_c.grid(axis='y', color='#21262d', linewidth=0.5)

    # ── Volume ────────────────────────────────────────────────────────────
    vcols = [col_up if closes[i] >= opens[i] else col_down for i in range(n)]
    ax_v.bar(x, volumes, color=vcols, alpha=0.7, width=W)
    ax_v.plot(x, vol_ma, color='#f0b429', linewidth=0.8)
    ax_v.set_ylabel('Volume', color='#8b949e', fontsize=8)
    ax_v.grid(axis='y', color='#21262d', linewidth=0.5)

    # ── RSI ───────────────────────────────────────────────────────────────
    ax_r.plot(x, rsi_v, color='#c9d1d9', linewidth=0.9)
    ax_r.axhline(70, color='#e05c5c', linewidth=0.7, linestyle='--', alpha=0.7)
    ax_r.axhline(30, color='#26a641', linewidth=0.7, linestyle='--', alpha=0.7)
    ax_r.axhline(50, color='#8b949e', linewidth=0.5, linestyle=':', alpha=0.5)
    ax_r.fill_between(x, rsi_v, 70, where=(rsi_v >= 70), alpha=0.2, color='#e05c5c')
    ax_r.fill_between(x, rsi_v, 30, where=(rsi_v <= 30), alpha=0.2, color='#26a641')
    ax_r.set_ylim(0, 100)
    ax_r.set_ylabel('RSI 14', color='#8b949e', fontsize=8)
    last_rsi = next((v for v in reversed(calc_rsi(closes)) if v is not None), None)
    if last_rsi is not None:
        ax_r.text(n - 1, last_rsi, f'{last_rsi:.1f}', color='#c9d1d9',
                  fontsize=7, va='center', ha='right')
    ax_r.grid(axis='y', color='#21262d', linewidth=0.5)

    # ── MACD ──────────────────────────────────────────────────────────────
    ax_m.plot(x, ml, color='#79c0ff', linewidth=0.9, label='MACD')
    ax_m.plot(x, sl, color='#f0b429', linewidth=0.9, label='Signal')
    hcols = [col_up if (not np.isnan(v) and v >= 0) else col_down for v in hist]
    ax_m.bar(x, hist, color=hcols, alpha=0.7, width=W)
    ax_m.axhline(0, color='#8b949e', linewidth=0.5)
    ax_m.set_ylabel('MACD', color='#8b949e', fontsize=8)
    ax_m.legend(loc='upper left', fontsize=7, facecolor='#161b22',
                edgecolor='#21262d', labelcolor='#c9d1d9')
    ax_m.grid(axis='y', color='#21262d', linewidth=0.5)

    # ── Asse X ────────────────────────────────────────────────────────────
    n_ticks  = max(2, min(12, n // 8))
    tick_pos = [int(i) for i in np.linspace(0, n - 1, n_ticks)]
    ax_m.set_xticks(tick_pos)
    ax_m.set_xticklabels([fmt_date(dates[i]) for i in tick_pos],
                          rotation=30, ha='right', fontsize=7, color='#8b949e')

    # ── Titolo ────────────────────────────────────────────────────────────
    change     = closes[-1] - closes[-2] if n > 1 else 0
    change_pct = change / closes[-2] * 100 if n > 1 and closes[-2] else 0
    arrow      = '▲' if change >= 0 else '▼'
    fig.suptitle(
        f"{symbol} — 15m   |   "
        f"O: {opens[-1]:.2f}  H: {highs[-1]:.2f}  "
        f"L: {lows[-1]:.2f}  C: {closes[-1]:.2f}  "
        f"{arrow} {abs(change):.2f} ({abs(change_pct):.2f}%)",
        color='#c9d1d9', fontsize=11, fontweight='bold', y=0.97
    )
    fig.text(0.5, 0.005,
             f"{fmt_date(dates[0])}  →  {fmt_date(dates[-1])}  ({n} candele)",
             ha='center', color='#8b949e', fontsize=7)

    return fig


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Grafico candlestick 15m — no pandas, solo matplotlib+numpy'
    )
    parser.add_argument('symbol',
                        help='Simbolo da graficare (es: GOLD, SILVER, BTC)')
    parser.add_argument('--bars',     type=int, default=120,
                        help='Numero candele da visualizzare (default 120 ≈ 30h)')
    parser.add_argument('--data-dir', default='data/candles',
                        help='Directory base dei CSV (default: data/candles)')
    parser.add_argument('--save',     metavar='FILE',
                        help='Salva PNG invece di aprire la finestra interattiva')
    args = parser.parse_args()

    symbol   = args.symbol.upper()
    data_dir = Path(args.data_dir)

    try:
        csv_path = find_csv(symbol, data_dir)
    except FileNotFoundError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    print(f"📂 {csv_path}")
    data = load_csv(csv_path, args.bars)
    print(f"📊 Candele totali: {data['total']}  |  Visualizzate: {len(data['closes'])}")
    print(f"🕯 Da {data['dates'][0]}  a  {data['dates'][-1]}")
    print(f"💰 Ultimo prezzo: {data['closes'][-1]:.4f}")

    fig = plot_chart(data, symbol)

    if args.save:
        out = Path(args.save)
        fig.savefig(out, dpi=150, bbox_inches='tight', facecolor='#0d1117')
        print(f"✅ Salvato: {out}")
    else:
        plt.show()


if __name__ == '__main__':
    main()
