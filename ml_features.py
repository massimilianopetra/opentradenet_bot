#!/usr/bin/env python3
"""
ml_features.py — Feature engineering per OpenTradeNet ML
=========================================================

Legge i CSV candele 15m prodotti dal bot (formato: timestamp,open,high,low,close,volume)
e produce un dataset con feature tecniche + label per il training ML.

Tutto in Python puro + matplotlib/numpy (già installati nel progetto).
Nessuna dipendenza nuova richiesta.

USO STANDALONE
--------------
  # Processa tutti i simboli e genera report + grafici
  python3 ml_features.py

  # Solo un simbolo specifico
  python3 ml_features.py --symbol BTC

  # Cartella candele custom
  python3 ml_features.py --data-dir /altro/percorso/candles

  # Cambia soglia label e orizzonte futuro
  python3 ml_features.py --horizon 4 --threshold 0.003

  # Salva il dataset CSV invece di stamparlo
  python3 ml_features.py --save-csv

  # Non generare grafici (solo testo/CSV)
  python3 ml_features.py --no-charts

OUTPUT
------
  - Stampa a schermo: statistiche per simbolo (n° candele, % LONG/SHORT/NEUTRO,
    feature importance approssimativa via correlazione con label)
  - Grafici PNG in: ml_reports/SIMBOLO_features.png
  - Dataset CSV (opzionale) in: ml_reports/SIMBOLO_dataset.csv
  - Report riassuntivo: ml_reports/summary.txt

STRUTTURA DIRECTORY ATTESA
--------------------------
  data/candles/BTC/BTC_15m.csv
  data/candles/ETH/ETH_15m.csv
  ...

FORMATO CSV ATTESO
------------------
  timestamp,open,high,low,close,volume
  2026-03-01 14:00:00,83000.0,83200.0,82900.0,83150.0,12.45
"""

import os
import sys
import csv
import math
import argparse
import warnings
from pathlib import Path
from datetime import datetime

# matplotlib in modalità headless (no GUI) — uguale a candle_chart.py
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np

warnings.filterwarnings('ignore')

# ---------------------------------------------------------------------------
# Configurazione default
# ---------------------------------------------------------------------------

DEFAULT_DATA_DIR  = 'data/candles'
DEFAULT_HORIZON   = 4          # candele future da guardare per il label (4 × 15min = 1 ora)
DEFAULT_THRESHOLD = 0.003      # 0.3% — soglia per definire LONG/SHORT vs NEUTRO
# Target NEUTRO alto: vogliamo pochi segnali ma precisi.
# 75% NEUTRO → ~12.5% LONG, ~12.5% SHORT — il modello segnala solo i movimenti forti.
AUTO_THRESHOLD_NEUTRO_TARGET = 0.75
OUTPUT_DIR        = Path('ml_reports')

# ---------------------------------------------------------------------------
# 1. LETTURA CSV
# ---------------------------------------------------------------------------

def find_csv(symbol: str, data_dir: Path) -> Path:
    """
    Cerca il CSV del simbolo nelle posizioni standard del progetto.
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
        f"Percorsi cercati:\n" + "\n".join(f"  {p}" for p in candidates)
    )


def load_csv(csv_path: Path) -> dict:
    """
    Carica il CSV e restituisce dizionario con liste parallele OHLCV + timestamp.
    Skippa righe malformate o con valori non numerici.
    """
    timestamps, opens, highs, lows, closes, volumes = [], [], [], [], [], []

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            raise ValueError(f"File vuoto: {csv_path}")

        for i, row in enumerate(reader):
            if len(row) < 6:
                continue
            try:
                timestamps.append(row[0].strip())
                opens.append(float(row[1]))
                highs.append(float(row[2]))
                lows.append(float(row[3]))
                closes.append(float(row[4]))
                volumes.append(float(row[5]))
            except (ValueError, IndexError):
                continue  # riga corrotta, skippa silenziosamente

    if len(closes) < 60:
        raise ValueError(
            f"Dati insufficienti per {csv_path.parent.name}: "
            f"{len(closes)} candele (minimo 60 richiesto)"
        )

    return {
        'timestamps': timestamps,
        'opens':      opens,
        'highs':      highs,
        'lows':       lows,
        'closes':     closes,
        'volumes':    volumes,
    }


# ---------------------------------------------------------------------------
# 2. INDICATORI TECNICI (Python puro, stessa filosofia di candle_chart.py)
# ---------------------------------------------------------------------------

def ema(values: list, period: int) -> list:
    """
    Exponential Moving Average.
    Restituisce lista della stessa lunghezza, None per i primi (period-1) valori.
    """
    result = [None] * len(values)
    if len(values) < period:
        return result
    k = 2.0 / (period + 1)
    # seed: prima EMA = SMA del primo periodo
    sma = sum(values[:period]) / period
    result[period - 1] = sma
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def sma(values: list, period: int) -> list:
    """Simple Moving Average. None per i primi (period-1) valori."""
    result = [None] * len(values)
    for i in range(period - 1, len(values)):
        result[i] = sum(values[i - period + 1:i + 1]) / period
    return result


def bollinger_bands(closes: list, period: int = 20, k: float = 2.0):
    """
    Bollinger Bands.
    Restituisce (mid, upper, lower) — tutti lista della stessa lunghezza.
    """
    mid   = sma(closes, period)
    upper = [None] * len(closes)
    lower = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        mean   = mid[i]
        std    = math.sqrt(sum((x - mean) ** 2 for x in window) / period)
        upper[i] = mean + k * std
        lower[i] = mean - k * std
    return mid, upper, lower


def rsi(closes: list, period: int = 14) -> list:
    """
    RSI (Relative Strength Index).
    Usa la media esponenziale di Wilder (RMA).
    """
    result = [None] * len(closes)
    if len(closes) < period + 1:
        return result

    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    # Seed: prima media semplice
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(closes)):
        idx = i - period  # indice in gains/losses
        avg_gain = (avg_gain * (period - 1) + gains[idx]) / period
        avg_loss = (avg_loss * (period - 1) + losses[idx]) / period
        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100.0 - (100.0 / (1 + rs))

    return result


def macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9):
    """
    MACD line, signal line, histogram.
    Restituisce (macd_line, signal_line, histogram) — liste della stessa lunghezza.
    """
    ema_fast   = ema(closes, fast)
    ema_slow   = ema(closes, slow)
    macd_line  = [None] * len(closes)
    for i in range(len(closes)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]

    # Signal = EMA(macd_line, signal_period) — ignora i None iniziali
    sig_line = [None] * len(closes)
    valid_start = next((i for i, v in enumerate(macd_line) if v is not None), None)
    if valid_start is not None:
        valid_values = [v for v in macd_line if v is not None]
        valid_ema    = ema(valid_values, signal)
        j = 0
        for i in range(len(closes)):
            if macd_line[i] is not None:
                sig_line[i] = valid_ema[j]
                j += 1

    histogram = [None] * len(closes)
    for i in range(len(closes)):
        if macd_line[i] is not None and sig_line[i] is not None:
            histogram[i] = macd_line[i] - sig_line[i]

    return macd_line, sig_line, histogram


def atr(highs: list, lows: list, closes: list, period: int = 14) -> list:
    """
    Average True Range.
    Misura la volatilità media delle candele.
    """
    result = [None] * len(closes)
    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return result

    # Prima ATR = media semplice
    atr_val = sum(true_ranges[:period]) / period
    result[period] = atr_val
    for i in range(period + 1, len(closes)):
        atr_val = (atr_val * (period - 1) + true_ranges[i - 1]) / period
        result[i] = atr_val

    return result


# ---------------------------------------------------------------------------
# 3. FEATURE ENGINEERING
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 3a. PATTERN CANDLESTICK
# ---------------------------------------------------------------------------

def candlestick_patterns(opens: list, highs: list, lows: list, closes: list, i: int) -> dict:
    """
    Riconosce i pattern candlestick classici all'indice i.
    Tutti i pattern sono binari (0/1).
    Richiede almeno i >= 2 per i pattern multi-candela.

    PATTERN SINGOLA CANDELA (indice i):
      doji              — open ≈ close (body < 10% del range) → indecisione
      hammer            — wick inferiore > 2× body, corpo nel terzo superiore → reversal rialzista
      inverted_hammer   — wick superiore > 2× body, corpo nel terzo inferiore → possibile reversal
      marubozu_bull     — candela tutta corpo rialzista (wick < 5% range per lato) → forza pura
      marubozu_bear     — stessa cosa ribassista
      spinning_top      — body piccolo (<30% range) con wick simmetrici → indecisione

    PATTERN DUE CANDELE (indici i-1, i):
      engulfing_bull    — i-1 ribassista, i rialzista che ingloba corpo precedente → forte long
      engulfing_bear    — i-1 rialzista, i ribassista che ingloba corpo precedente → forte short
      harami_bull       — i-1 grande ribassista, i piccolo rialzista dentro il corpo → possibile long
      harami_bear       — i-1 grande rialzista, i piccolo ribassista dentro il corpo → possibile short
      tweezer_bottom    — due minimi quasi uguali → supporto forte → long
      tweezer_top       — due massimi quasi uguali → resistenza forte → short
      piercing_line     — i-1 ribassista, i apre sotto e chiude oltre metà corpo prec. → long
      dark_cloud_cover  — opposto di piercing_line → short

    PATTERN TRE CANDELE (indici i-2, i-1, i):
      morning_star      — rosso, doji/piccola, verde → reversal rialzista classico
      evening_star      — verde, doji/piccola, rosso → reversal ribassista classico
      three_white_soldiers — tre candele verdi forti consecutive → forte trend rialzista
      three_black_crows    — tre candele rosse forti consecutive → forte trend ribassista
    """
    c0, o0, h0, l0 = closes[i],   opens[i],   highs[i],   lows[i]
    c1, o1, h1, l1 = closes[i-1], opens[i-1], highs[i-1], lows[i-1]
    c2, o2, h2, l2 = closes[i-2], opens[i-2], highs[i-2], lows[i-2]

    # Range e body (evita div/0)
    def rng(h, l):   return max(h - l, 1e-10)
    def body(c, o):  return abs(c - o)

    r0, r1, r2 = rng(h0, l0), rng(h1, l1), rng(h2, l2)
    b0, b1, b2 = body(c0, o0), body(c1, o1), body(c2, o2)

    bull0 = c0 > o0
    bull1 = c1 > o1
    bull2 = c2 > o2

    upper_wick0 = h0 - max(c0, o0)
    lower_wick0 = min(c0, o0) - l0
    upper_wick1 = h1 - max(c1, o1)
    lower_wick1 = min(c1, o1) - l1

    # ── Singola candela ───────────────────────────────────────────────────
    doji            = 1 if b0 / r0 < 0.10 else 0
    # hammer: wick inferiore lungo, corpo piccolo nel terzo superiore (bull o bear)
    hammer          = 1 if (lower_wick0 > 2 * b0
                            and upper_wick0 < b0 * 0.5
                            and (min(c0,o0) - l0) / r0 > 0.55) else 0
    inverted_hammer = 1 if (upper_wick0 > 2 * b0
                            and lower_wick0 < b0 * 0.5
                            and (h0 - max(c0,o0)) / r0 > 0.55) else 0
    marubozu_bull   = 1 if (bull0
                            and b0 / r0 > 0.90
                            and upper_wick0 / r0 < 0.05
                            and lower_wick0 / r0 < 0.05) else 0
    marubozu_bear   = 1 if (not bull0
                            and b0 / r0 > 0.90
                            and upper_wick0 / r0 < 0.05
                            and lower_wick0 / r0 < 0.05) else 0
    spinning_top    = 1 if (b0 / r0 < 0.30
                            and upper_wick0 / r0 > 0.25
                            and lower_wick0 / r0 > 0.25) else 0

    # ── Due candele ───────────────────────────────────────────────────────
    engulfing_bull  = 1 if (bull0 and not bull1
                            and o0 <= c1              # apre sotto close prec
                            and c0 >= o1) else 0      # chiude sopra open prec

    engulfing_bear  = 1 if (not bull0 and bull1
                            and o0 >= c1
                            and c0 <= o1) else 0

    harami_bull     = 1 if (bull0 and not bull1
                            and b1 > 0
                            and o0 > min(o1,c1)       # corpo corrente dentro
                            and c0 < max(o1,c1)) else 0

    harami_bear     = 1 if (not bull0 and bull1
                            and b1 > 0
                            and o0 < max(o1,c1)
                            and c0 > min(o1,c1)) else 0

    # Tweezer: minimi/massimi a ≤0.1% di distanza
    tol = 0.001
    tweezer_bottom  = 1 if (abs(l0 - l1) / max(l0, 1e-10) < tol
                            and not bull1 and bull0) else 0
    tweezer_top     = 1 if (abs(h0 - h1) / max(h0, 1e-10) < tol
                            and bull1 and not bull0) else 0

    # Piercing line: i-1 ribassista, i apre sotto low prec e chiude oltre metà corpo prec
    mid_body1 = (o1 + c1) / 2
    piercing_line   = 1 if (not bull1 and bull0
                            and o0 < l1
                            and c0 > mid_body1
                            and c0 < o1) else 0

    dark_cloud_cover= 1 if (bull1 and not bull0
                            and o0 > h1
                            and c0 < mid_body1
                            and c0 > c1) else 0

    # ── Tre candele ───────────────────────────────────────────────────────
    # Morning star: grande rosso, piccola (qualsiasi), grande verde
    morning_star    = 1 if (not bull2
                            and b2 / r2 > 0.5          # prima candela grande
                            and b1 / r1 < 0.3          # seconda piccola
                            and bull0
                            and b0 / r0 > 0.5          # terza grande
                            and c0 > (o2 + c2) / 2) else 0  # chiude oltre metà prima

    evening_star    = 1 if (bull2
                            and b2 / r2 > 0.5
                            and b1 / r1 < 0.3
                            and not bull0
                            and b0 / r0 > 0.5
                            and c0 < (o2 + c2) / 2) else 0

    three_white_soldiers = 1 if (bull0 and bull1 and bull2
                                  and b0/r0 > 0.6 and b1/r1 > 0.6 and b2/r2 > 0.6
                                  and c0 > c1 > c2
                                  and o0 > o1 > o2) else 0

    three_black_crows    = 1 if (not bull0 and not bull1 and not bull2
                                  and b0/r0 > 0.6 and b1/r1 > 0.6 and b2/r2 > 0.6
                                  and c0 < c1 < c2
                                  and o0 < o1 < o2) else 0

    return {
        # Singola candela
        'pat_doji':               doji,
        'pat_hammer':             hammer,
        'pat_inverted_hammer':    inverted_hammer,
        'pat_marubozu_bull':      marubozu_bull,
        'pat_marubozu_bear':      marubozu_bear,
        'pat_spinning_top':       spinning_top,
        # Due candele
        'pat_engulfing_bull':     engulfing_bull,
        'pat_engulfing_bear':     engulfing_bear,
        'pat_harami_bull':        harami_bull,
        'pat_harami_bear':        harami_bear,
        'pat_tweezer_bottom':     tweezer_bottom,
        'pat_tweezer_top':        tweezer_top,
        'pat_piercing_line':      piercing_line,
        'pat_dark_cloud_cover':   dark_cloud_cover,
        # Tre candele
        'pat_morning_star':       morning_star,
        'pat_evening_star':       evening_star,
        'pat_three_white_soldiers': three_white_soldiers,
        'pat_three_black_crows':    three_black_crows,
    }


def build_features(data: dict, horizon: int = 4, threshold: float = 0.003) -> list:
    """
    Costruisce il dataset: una riga per ogni candela con tutte le feature + label.

    Parametri:
        data       : dizionario OHLCV da load_csv()
        horizon    : quante candele future guardare per il label
        threshold  : soglia % per etichettare LONG (+1) / SHORT (-1) / NEUTRO (0)

    Restituisce:
        Lista di dict, una per candela valida (esclude le prime N candele
        necessarie per calcolare gli indicatori e le ultime 'horizon' candele
        per cui non esiste ancora il futuro).

    FEATURE CALCOLATE:
    ------------------
    Struttura candela corrente:
      - body_ratio      : |close-open| / (high-low)  →  forza del movimento
      - upper_wick_ratio: (high - max(open,close)) / (high-low)  →  pressione vendita
      - lower_wick_ratio: (min(open,close) - low) / (high-low)   →  pressione acquisto
      - close_position  : (close-low) / (high-low)  →  0=fondo, 1=top del range
      - is_bullish      : 1 se close > open, 0 altrimenti
      - range_pct       : (high-low) / close  →  volatilità della singola candela

    Ritorni recenti (momentum):
      - ret_1, ret_3, ret_5  : return % su 1, 3, 5 candele passate

    Indicatori tecnici (distanza normalizzata):
      - dist_ema9_pct   : (close - EMA9) / close  →  quanto siamo sopra/sotto EMA veloce
      - dist_ema21_pct  : (close - EMA21) / close
      - dist_ema50_pct  : (close - EMA50) / close
      - ema9_vs_ema21   : 1 se EMA9 > EMA21 (trend rialzista), -1 altrimenti
      - bb_position     : (close - BB_lower) / (BB_upper - BB_lower)  →  0=basso, 1=alto
      - bb_width_pct    : (BB_upper - BB_lower) / BB_mid  →  volatilità BB
      - rsi              : RSI 14 (normalizzato 0-100)
      - macd_hist        : istogramma MACD (positivo=momentum rialzista)
      - macd_hist_change : variazione istogramma vs candela precedente
      - atr_pct          : ATR / close  →  volatilità relativa

    Volume:
      - vol_ratio        : volume / media volume 20 candele  →  spike se > 1.5
      - vol_ratio_5       : volume / media volume 5 candele

    Sequenza pattern (ultime 3 candele):
      - prev1_bullish, prev2_bullish, prev3_bullish  : direzione candele precedenti
      - consec_bullish  : candele rialziste consecutive (max 5)
      - consec_bearish  : candele ribassiste consecutive (max 5)

    Pattern candlestick (tutti binari 0/1):
      Singola: doji, hammer, inverted_hammer, marubozu_bull, marubozu_bear, spinning_top
      Due:     engulfing_bull/bear, harami_bull/bear, tweezer_bottom/top,
               piercing_line, dark_cloud_cover
      Tre:     morning_star, evening_star, three_white_soldiers, three_black_crows

    Contesto temporale:
      - hour_sin, hour_cos   : ora UTC codificata ciclicamente (0-23)
      - weekday_sin, weekday_cos : giorno settimana codificato ciclicamente (0-6)

    LABEL (Favorable Excursion):
      - label  : +1 (LONG)   se il prezzo sale oltre +threshold nelle prossime
                              'horizon' candele SENZA prima scendere oltre -threshold/2
                  -1 (SHORT)  se il prezzo scende oltre -threshold SENZA prima salire
                              oltre +threshold/2
                   0 (NEUTRO) movimento ambiguo o laterale
      - future_return_pct : return % semplice close→close (per analisi, non usato nel label)
    """
    closes    = data['closes']
    opens     = data['opens']
    highs     = data['highs']
    lows      = data['lows']
    volumes   = data['volumes']
    timestamps= data['timestamps']
    n         = len(closes)

    # Calcola indicatori
    ema9_vals         = ema(closes, 9)
    ema21_vals        = ema(closes, 21)
    ema50_vals        = ema(closes, 50)
    bb_mid, bb_up, bb_lo = bollinger_bands(closes, 20)
    rsi_vals          = rsi(closes, 14)
    macd_line, sig_line, macd_hist = macd(closes)
    atr_vals          = atr(highs, lows, closes, 14)
    vol_sma20         = sma(volumes, 20)
    vol_sma5          = sma(volumes, 5)

    rows = []

    # Indice minimo da cui tutte le feature sono disponibili
    min_idx = 51   # EMA50 ha bisogno di almeno 50 periodi

    for i in range(min_idx, n - horizon):
        c   = closes[i]
        o   = opens[i]
        h   = highs[i]
        lo  = lows[i]
        v   = volumes[i]
        rng = h - lo if (h - lo) > 0 else 1e-10  # evita divisione per zero

        # ── Feature candela corrente ───────────────────────────────────────
        body_ratio       = abs(c - o) / rng
        upper_wick_ratio = (h - max(o, c)) / rng
        lower_wick_ratio = (min(o, c) - lo) / rng
        close_position   = (c - lo) / rng
        is_bullish       = 1 if c > o else 0
        range_pct        = rng / c

        # ── Ritorni recenti ────────────────────────────────────────────────
        ret_1 = (closes[i] - closes[i-1]) / closes[i-1] if closes[i-1] != 0 else 0
        ret_3 = (closes[i] - closes[i-3]) / closes[i-3] if closes[i-3] != 0 else 0
        ret_5 = (closes[i] - closes[i-5]) / closes[i-5] if closes[i-5] != 0 else 0

        # ── Indicatori tecnici (skip se None) ─────────────────────────────
        def safe_dist(indicator_val):
            """Distanza % da close all'indicatore. 0 se non disponibile."""
            if indicator_val is None:
                return 0.0
            return (c - indicator_val) / c

        dist_ema9_pct  = safe_dist(ema9_vals[i])
        dist_ema21_pct = safe_dist(ema21_vals[i])
        dist_ema50_pct = safe_dist(ema50_vals[i])

        ema9_vs_ema21  = 0
        if ema9_vals[i] is not None and ema21_vals[i] is not None:
            ema9_vs_ema21 = 1 if ema9_vals[i] > ema21_vals[i] else -1

        bb_position   = 0.5
        bb_width_pct  = 0.0
        if bb_up[i] is not None and bb_lo[i] is not None and bb_mid[i] is not None:
            band_width = bb_up[i] - bb_lo[i]
            if band_width > 0:
                bb_position  = (c - bb_lo[i]) / band_width
            bb_width_pct = band_width / bb_mid[i]

        rsi_val    = rsi_vals[i] if rsi_vals[i] is not None else 50.0
        macd_h     = macd_hist[i] if macd_hist[i] is not None else 0.0
        macd_h_chg = 0.0
        if macd_hist[i] is not None and macd_hist[i-1] is not None:
            macd_h_chg = macd_hist[i] - macd_hist[i-1]

        atr_pct    = (atr_vals[i] / c) if atr_vals[i] is not None else 0.0

        # ── Volume ────────────────────────────────────────────────────────
        vol_ratio   = (v / vol_sma20[i]) if vol_sma20[i] and vol_sma20[i] > 0 else 1.0
        vol_ratio_5 = (v / vol_sma5[i])  if vol_sma5[i]  and vol_sma5[i]  > 0 else 1.0

        # ── Pattern sequenza ──────────────────────────────────────────────
        prev1_bullish = 1 if closes[i-1] > opens[i-1] else 0
        prev2_bullish = 1 if closes[i-2] > opens[i-2] else 0
        prev3_bullish = 1 if closes[i-3] > opens[i-3] else 0

        # Candele consecutive nella stessa direzione
        consec_bull, consec_bear = 0, 0
        for j in range(1, 6):
            if i - j < 0:
                break
            if closes[i-j] > opens[i-j]:
                if consec_bear == 0:
                    consec_bull += 1
            else:
                if consec_bull == 0:
                    consec_bear += 1

        # ── Pattern candlestick ───────────────────────────────────────────
        patterns = candlestick_patterns(opens, highs, lows, closes, i)

        # ── Contesto temporale ────────────────────────────────────────────        hour, weekday = 12, 1  # default se parsing fallisce
        try:
            dt       = datetime.strptime(timestamps[i], '%Y-%m-%d %H:%M:%S')
            hour     = dt.hour
            weekday  = dt.weekday()
        except Exception:
            pass
        hour_sin     = math.sin(2 * math.pi * hour / 24)
        hour_cos     = math.cos(2 * math.pi * hour / 24)
        weekday_sin  = math.sin(2 * math.pi * weekday / 7)
        weekday_cos  = math.cos(2 * math.pi * weekday / 7)

        # ── Label (Favorable Excursion) ───────────────────────────────────
        # Invece di guardare solo close[T+horizon], usiamo il massimo e
        # minimo realmente toccati nelle prossime 'horizon' candele.
        #
        # LONG  : il prezzo sale oltre +threshold SENZA prima scendere
        #         oltre -threshold/2  (movimento pulito verso l'alto)
        # SHORT : il prezzo scende oltre -threshold SENZA prima salire
        #         oltre +threshold/2  (movimento pulito verso il basso)
        # NEUTRO: tutto il resto — oscillazioni laterali o movimenti
        #         ambigui che toccherebbero sia stop che target
        #
        # Questo label è più realistico del semplice return finale:
        # cattura se il trade avrebbe funzionato davvero, non solo
        # dove si trova il prezzo alla candela N.
        future_highs  = highs[i+1 : i+1+horizon]
        future_lows   = lows[i+1  : i+1+horizon]
        max_excursion = (max(future_highs) - c) / c   # max rialzo raggiunto
        min_excursion = (min(future_lows)  - c) / c   # max ribasso raggiunto

        half = threshold * 0.8   # tolleranza contro-movimento (80% della soglia)

        if max_excursion > threshold and min_excursion > -half:
            label = 1    # LONG pulito
        elif min_excursion < -threshold and max_excursion < half:
            label = -1   # SHORT pulito
        else:
            label = 0    # NEUTRO / ambiguo

        # future_return_pct rimane il return semplice (per analisi)
        future_close  = closes[i + horizon]
        future_return = (future_close - c) / c

        rows.append({
            # Metadati
            'timestamp':         timestamps[i],
            'close':             c,
            # Candela corrente
            'body_ratio':        round(body_ratio, 5),
            'upper_wick_ratio':  round(upper_wick_ratio, 5),
            'lower_wick_ratio':  round(lower_wick_ratio, 5),
            'close_position':    round(close_position, 5),
            'is_bullish':        is_bullish,
            'range_pct':         round(range_pct, 6),
            # Momentum
            'ret_1':             round(ret_1, 6),
            'ret_3':             round(ret_3, 6),
            'ret_5':             round(ret_5, 6),
            # Indicatori
            'dist_ema9_pct':     round(dist_ema9_pct, 6),
            'dist_ema21_pct':    round(dist_ema21_pct, 6),
            'dist_ema50_pct':    round(dist_ema50_pct, 6),
            'ema9_vs_ema21':     ema9_vs_ema21,
            'bb_position':       round(bb_position, 5),
            'bb_width_pct':      round(bb_width_pct, 6),
            'rsi':               round(rsi_val, 3),
            'macd_hist':         round(macd_h, 8),
            'macd_hist_change':  round(macd_h_chg, 8),
            'atr_pct':           round(atr_pct, 6),
            # Volume
            'vol_ratio':         round(vol_ratio, 4),
            'vol_ratio_5':       round(vol_ratio_5, 4),
            # Sequenza
            'prev1_bullish':     prev1_bullish,
            'prev2_bullish':     prev2_bullish,
            'prev3_bullish':     prev3_bullish,
            'consec_bullish':    consec_bull,
            'consec_bearish':    consec_bear,
            # Pattern candlestick
            **patterns,
            # Temporale
            'hour_sin':          round(hour_sin, 5),
            'hour_cos':          round(hour_cos, 5),
            'weekday_sin':       round(weekday_sin, 5),
            'weekday_cos':       round(weekday_cos, 5),
            # Target
            'future_return_pct': round(future_return * 100, 4),
            'label':             label,
        })

    return rows


# ---------------------------------------------------------------------------
# 3b. AUTO-THRESHOLD
# ---------------------------------------------------------------------------

def compute_auto_threshold(closes: list, horizon: int,
                            highs: list = None, lows: list = None,
                            target_neutro: float = AUTO_THRESHOLD_NEUTRO_TARGET) -> float:
    """
    Calcola automaticamente la soglia ottimale per un simbolo in modo che
    la percentuale di campioni NEUTRO sia vicina a target_neutro (default 62%).

    Con il favorable excursion label, usa il massimo movimento direzionale
    pulito: max(high[i+1..i+horizon]) - close[i] per i rialzi,
    close[i] - min(low[i+1..i+horizon]) per i ribassi.
    Prende il minore dei due come escursione favorevole per ogni candela.

    Se highs/lows non sono disponibili, ricade sul semplice |close-to-close|.

    Restituisce la soglia come float (es. 0.018 = 1.8%).
    """
    n = len(closes)
    excursions = []

    if highs and lows and len(highs) == n and len(lows) == n:
        # Favorable excursion: usa high/low futuri
        for i in range(n - horizon):
            if closes[i] <= 0:
                continue
            c = closes[i]
            max_up   = (max(highs[i+1 : i+1+horizon]) - c) / c
            max_down = (c - min(lows[i+1  : i+1+horizon])) / c
            # Usa il minore: rappresenta il "segnale più difficile da raggiungere"
            excursions.append(min(max_up, max_down))
    else:
        # Fallback: close-to-close
        for i in range(n - horizon):
            if closes[i] > 0:
                excursions.append(abs(closes[i + horizon] - closes[i]) / closes[i])

    if not excursions:
        return DEFAULT_THRESHOLD

    excursions.sort()
    # percentile corrispondente a target_neutro
    idx = int(len(excursions) * target_neutro)
    idx = max(0, min(idx, len(excursions) - 1))
    threshold = excursions[idx]

    # Clamp ragionevole: mai sotto 0.1% né sopra 10%
    threshold = max(0.001, min(0.10, threshold))
    return threshold


# ---------------------------------------------------------------------------
# 4. STATISTICHE E REPORT TESTUALE
# ---------------------------------------------------------------------------

def print_stats(symbol: str, rows: list, horizon: int, threshold: float):
    """Stampa statistiche descrittive del dataset a schermo."""
    n       = len(rows)
    n_long  = sum(1 for r in rows if r['label'] ==  1)
    n_short = sum(1 for r in rows if r['label'] == -1)
    n_neut  = sum(1 for r in rows if r['label'] ==  0)

    print(f"\n{'═'*60}")
    print(f"  {symbol}  —  {n} campioni  |  horizon={horizon}  |  soglia={threshold*100:.2f}%")
    print(f"{'═'*60}")
    print(f"  LONG   (label= 1):  {n_long:5d}  ({n_long/n*100:5.1f}%)")
    print(f"  SHORT  (label=-1):  {n_short:5d}  ({n_short/n*100:5.1f}%)")
    print(f"  NEUTRO (label= 0):  {n_neut:5d}  ({n_neut/n*100:5.1f}%)")

    # Correlazione feature → label (approssimazione interpretativa)
    feature_cols = [k for k in rows[0].keys()
                    if k not in ('timestamp', 'close', 'future_return_pct', 'label')]
    labels = [r['label'] for r in rows]

    correlations = {}
    for col in feature_cols:
        vals = [r[col] for r in rows]
        # Pearson semplificato
        mean_v = sum(vals) / n
        mean_l = sum(labels) / n
        num    = sum((v - mean_v) * (l - mean_l) for v, l in zip(vals, labels))
        den_v  = math.sqrt(sum((v - mean_v) ** 2 for v in vals) + 1e-10)
        den_l  = math.sqrt(sum((l - mean_l) ** 2 for l in labels) + 1e-10)
        correlations[col] = num / (den_v * den_l)

    sorted_corr = sorted(correlations.items(), key=lambda x: abs(x[1]), reverse=True)

    print(f"\n  Top 10 feature per correlazione con label:")
    print(f"  {'Feature':<22}  {'Correlazione':>12}")
    print(f"  {'-'*38}")
    for col, corr in sorted_corr[:10]:
        bar = '█' * int(abs(corr) * 20)
        direction = '▲' if corr > 0 else '▼'
        print(f"  {col:<22}  {corr:+.4f}  {direction} {bar}")

    return correlations


# ---------------------------------------------------------------------------
# 5. GRAFICI
# ---------------------------------------------------------------------------

def plot_features(symbol: str, rows: list, output_path: Path):
    """
    Genera un grafico a 6 pannelli che mostra la qualità del dataset:

    Pannello 1: Distribuzione label (torta)
    Pannello 2: Distribuzione future_return_pct (istogramma con linee soglia)
    Pannello 3: RSI medio per label (boxplot semplificato a barre)
    Pannello 4: bb_position medio per label
    Pannello 5: vol_ratio medio per label
    Pannello 6: Top 15 feature — correlazione con label (barchart orizzontale)
    """
    n       = len(rows)
    labels  = [r['label'] for r in rows]
    returns = [r['future_return_pct'] for r in rows]

    n_long  = labels.count(1)
    n_short = labels.count(-1)
    n_neut  = labels.count(0)

    # Colori coerenti con il tema scuro del progetto
    COL_LONG   = '#26a69a'
    COL_SHORT  = '#ef5350'
    COL_NEUT   = '#78909c'
    COL_BG     = '#0d1117'
    COL_PANEL  = '#161b22'
    COL_TEXT   = '#c9d1d9'
    COL_GRID   = '#21262d'

    fig = plt.figure(figsize=(18, 14), facecolor=COL_BG)
    fig.suptitle(
        f'{symbol} — Feature Dataset  |  {n} campioni',
        color=COL_TEXT, fontsize=16, fontweight='bold', y=0.98
    )

    gs = gridspec.GridSpec(3, 3, figure=fig, hspace=0.45, wspace=0.35,
                           left=0.07, right=0.97, top=0.93, bottom=0.07)

    def style_ax(ax, title):
        ax.set_facecolor(COL_PANEL)
        ax.set_title(title, color=COL_TEXT, fontsize=10, pad=6)
        ax.tick_params(colors=COL_TEXT, labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor(COL_GRID)
        ax.grid(color=COL_GRID, linewidth=0.5, alpha=0.7)

    # ── Pannello 1: Distribuzione label (pie) ─────────────────────────────
    ax1 = fig.add_subplot(gs[0, 0])
    ax1.set_facecolor(COL_PANEL)
    ax1.set_title('Distribuzione label', color=COL_TEXT, fontsize=10, pad=6)
    sizes  = [n_long, n_short, n_neut]
    colors = [COL_LONG, COL_SHORT, COL_NEUT]
    labels_pie = [
        f'LONG\n{n_long} ({n_long/n*100:.1f}%)',
        f'SHORT\n{n_short} ({n_short/n*100:.1f}%)',
        f'NEUTRO\n{n_neut} ({n_neut/n*100:.1f}%)',
    ]
    wedges, _ = ax1.pie(sizes, colors=colors, startangle=90,
                        wedgeprops={'edgecolor': COL_BG, 'linewidth': 1.5})
    ax1.legend(wedges, labels_pie, loc='lower center', fontsize=7,
               labelcolor=COL_TEXT, facecolor=COL_PANEL, edgecolor=COL_GRID,
               bbox_to_anchor=(0.5, -0.18), ncol=3)

    # ── Pannello 2: Istogramma future_return_pct ──────────────────────────
    ax2 = fig.add_subplot(gs[0, 1])
    style_ax(ax2, 'Distribuzione return futuro (%)')

    # Colora le barre per zona
    threshold_pct = rows[0] if not rows else None
    # Recupera la soglia dai dati (future_return_pct mediano del confine)
    all_ret_sorted = sorted(returns)
    bins = np.linspace(min(returns), max(returns), 50)
    counts, edges = np.histogram(returns, bins=bins)
    for k in range(len(counts)):
        mid_bin = (edges[k] + edges[k+1]) / 2
        if mid_bin > 0:
            color = COL_LONG
        elif mid_bin < 0:
            color = COL_SHORT
        else:
            color = COL_NEUT
        ax2.bar(edges[k], counts[k], width=(edges[1]-edges[0]),
                color=color, alpha=0.75, align='edge')

    ax2.axvline(0, color=COL_TEXT, linewidth=1, linestyle='--', alpha=0.5)
    ax2.set_xlabel('Return %', color=COL_TEXT, fontsize=8)
    ax2.set_ylabel('Frequenza', color=COL_TEXT, fontsize=8)
    ax2.yaxis.label.set_color(COL_TEXT)
    ax2.xaxis.label.set_color(COL_TEXT)

    # ── Pannello 3: RSI medio per label ───────────────────────────────────
    ax3 = fig.add_subplot(gs[0, 2])
    style_ax(ax3, 'RSI medio per label')

    for label_val, col, lbl in [(1, COL_LONG, 'LONG'), (-1, COL_SHORT, 'SHORT'), (0, COL_NEUT, 'NEUTRO')]:
        subset = [r['rsi'] for r in rows if r['label'] == label_val]
        if not subset:
            continue
        mean_rsi = sum(subset) / len(subset)
        std_rsi  = math.sqrt(sum((x - mean_rsi)**2 for x in subset) / len(subset))
        ax3.bar(lbl, mean_rsi, color=col, alpha=0.8, width=0.5)
        ax3.errorbar(lbl, mean_rsi, yerr=std_rsi, color=COL_TEXT,
                     fmt='none', capsize=5, linewidth=1.5)
        ax3.text(lbl, mean_rsi + std_rsi + 1, f'{mean_rsi:.1f}',
                 ha='center', color=COL_TEXT, fontsize=8)

    ax3.axhline(70, color=COL_SHORT, linewidth=0.8, linestyle='--', alpha=0.6)
    ax3.axhline(30, color=COL_LONG,  linewidth=0.8, linestyle='--', alpha=0.6)
    ax3.set_ylim(0, 110)
    ax3.set_ylabel('RSI', color=COL_TEXT, fontsize=8)
    ax3.yaxis.label.set_color(COL_TEXT)

    # ── Pannello 4: bb_position medio per label ───────────────────────────
    ax4 = fig.add_subplot(gs[1, 0])
    style_ax(ax4, 'Posizione BB media per label')

    for label_val, col, lbl in [(1, COL_LONG, 'LONG'), (-1, COL_SHORT, 'SHORT'), (0, COL_NEUT, 'NEUTRO')]:
        subset = [r['bb_position'] for r in rows if r['label'] == label_val]
        if not subset:
            continue
        mean_bb = sum(subset) / len(subset)
        ax4.bar(lbl, mean_bb, color=col, alpha=0.8, width=0.5)
        ax4.text(lbl, mean_bb + 0.01, f'{mean_bb:.3f}',
                 ha='center', color=COL_TEXT, fontsize=8)

    ax4.axhline(1.0, color=COL_SHORT, linewidth=0.8, linestyle='--', alpha=0.5, label='BB upper')
    ax4.axhline(0.0, color=COL_LONG,  linewidth=0.8, linestyle='--', alpha=0.5, label='BB lower')
    ax4.axhline(0.5, color=COL_NEUT,  linewidth=0.8, linestyle=':', alpha=0.5, label='BB mid')
    ax4.set_ylim(-0.1, 1.2)
    ax4.set_ylabel('Posizione (0=low, 1=high)', color=COL_TEXT, fontsize=8)
    ax4.yaxis.label.set_color(COL_TEXT)

    # ── Pannello 5: vol_ratio medio per label ─────────────────────────────
    ax5 = fig.add_subplot(gs[1, 1])
    style_ax(ax5, 'Volume ratio medio per label')

    for label_val, col, lbl in [(1, COL_LONG, 'LONG'), (-1, COL_SHORT, 'SHORT'), (0, COL_NEUT, 'NEUTRO')]:
        subset = [r['vol_ratio'] for r in rows if r['label'] == label_val]
        if not subset:
            continue
        mean_vr = sum(subset) / len(subset)
        ax5.bar(lbl, mean_vr, color=col, alpha=0.8, width=0.5)
        ax5.text(lbl, mean_vr + 0.02, f'{mean_vr:.2f}x',
                 ha='center', color=COL_TEXT, fontsize=8)

    ax5.axhline(1.0, color=COL_TEXT, linewidth=0.8, linestyle='--', alpha=0.5)
    ax5.set_ylabel('Volume / SMA20', color=COL_TEXT, fontsize=8)
    ax5.yaxis.label.set_color(COL_TEXT)

    # ── Pannello 6: close_position e body_ratio per label ─────────────────
    ax6 = fig.add_subplot(gs[1, 2])
    style_ax(ax6, 'Struttura candela per label')

    x    = np.arange(3)
    lbl_names = ['LONG', 'SHORT', 'NEUTRO']
    label_vals = [1, -1, 0]
    cols_list  = [COL_LONG, COL_SHORT, COL_NEUT]

    body_means = []
    cp_means   = []
    for lv in label_vals:
        subset_b  = [r['body_ratio']    for r in rows if r['label'] == lv]
        subset_cp = [r['close_position'] for r in rows if r['label'] == lv]
        body_means.append(sum(subset_b)/len(subset_b) if subset_b else 0)
        cp_means.append(sum(subset_cp)/len(subset_cp) if subset_cp else 0)

    width = 0.35
    bars1 = ax6.bar(x - width/2, body_means, width, label='Body ratio',
                    color=[COL_LONG, COL_SHORT, COL_NEUT], alpha=0.8)
    bars2 = ax6.bar(x + width/2, cp_means, width, label='Close position',
                    color=[COL_LONG, COL_SHORT, COL_NEUT], alpha=0.4, edgecolor='white', linewidth=0.5)

    ax6.set_xticks(x)
    ax6.set_xticklabels(lbl_names, color=COL_TEXT, fontsize=8)
    ax6.set_ylim(0, 1.1)
    ax6.set_ylabel('Valore medio (0-1)', color=COL_TEXT, fontsize=8)
    ax6.yaxis.label.set_color(COL_TEXT)
    ax6.legend(fontsize=7, labelcolor=COL_TEXT, facecolor=COL_PANEL, edgecolor=COL_GRID)

    # ── Pannello 7: Top correlazioni feature (barchart orizzontale) ────────
    ax7 = fig.add_subplot(gs[2, :])
    style_ax(ax7, 'Correlazione feature → label  (proxy importanza per ML)')

    feature_cols = [k for k in rows[0].keys()
                    if k not in ('timestamp', 'close', 'future_return_pct', 'label')]
    n_rows = len(rows)
    lbl_arr = [r['label'] for r in rows]
    mean_l  = sum(lbl_arr) / n_rows

    correlations = {}
    for col in feature_cols:
        vals   = [r[col] for r in rows]
        mean_v = sum(vals) / n_rows
        num    = sum((v - mean_v) * (l - mean_l) for v, l in zip(vals, lbl_arr))
        den_v  = math.sqrt(sum((v - mean_v) ** 2 for v in vals) + 1e-10)
        den_l  = math.sqrt(sum((l - mean_l) ** 2 for l in lbl_arr) + 1e-10)
        correlations[col] = num / (den_v * den_l)

    sorted_corr = sorted(correlations.items(), key=lambda x: abs(x[1]), reverse=True)[:20]
    feat_names  = [s[0] for s in sorted_corr]
    feat_corrs  = [s[1] for s in sorted_corr]

    bar_colors = [COL_LONG if c > 0 else COL_SHORT for c in feat_corrs]
    y_pos = range(len(feat_names))
    ax7.barh(list(y_pos), feat_corrs, color=bar_colors, alpha=0.8, height=0.6)
    ax7.set_yticks(list(y_pos))
    ax7.set_yticklabels(feat_names, color=COL_TEXT, fontsize=8)
    ax7.axvline(0, color=COL_TEXT, linewidth=0.8)
    ax7.set_xlabel('Correlazione di Pearson con label', color=COL_TEXT, fontsize=9)
    ax7.xaxis.label.set_color(COL_TEXT)
    ax7.invert_yaxis()

    # Valori numerici sulle barre
    for i, (name, val) in enumerate(sorted_corr):
        ax7.text(val + (0.001 if val >= 0 else -0.001),
                 i, f'{val:+.4f}',
                 va='center', ha='left' if val >= 0 else 'right',
                 color=COL_TEXT, fontsize=7)

    plt.savefig(output_path, dpi=120, bbox_inches='tight', facecolor=COL_BG)
    plt.close(fig)
    print(f"  📊 Grafico salvato: {output_path}")


# ---------------------------------------------------------------------------
# 6. SALVATAGGIO CSV DATASET
# ---------------------------------------------------------------------------

def save_dataset_csv(symbol: str, rows: list, output_path: Path):
    """Salva il dataset in CSV pronto per il training."""
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  💾 Dataset CSV salvato: {output_path}  ({len(rows)} righe)")


# ---------------------------------------------------------------------------
# 7. REPORT RIASSUNTIVO TESTUALE
# ---------------------------------------------------------------------------

def write_summary(results: list, output_path: Path, horizon: int, threshold):
    """
    Scrive un file di testo con il riassunto di tutti i simboli processati.
    threshold può essere float (manuale) o None (auto-threshold per simbolo).
    """
    soglia_str = f"{threshold*100:.2f}%" if threshold else "AUTO (per simbolo)"
    lines = [
        "=" * 70,
        "  OpenTradeNet — ML Feature Engineering Report",
        f"  Horizon: {horizon} candele ({horizon*15} min)  |  Soglia: {soglia_str}",
        "=" * 70,
        "",
        f"  {'SIMBOLO':<12} {'CAMP':>6} {'LONG%':>7} {'SHORT%':>7} {'NEUTRO%':>8} {'SOGLIA':>7}  TOP FEATURE",
        "  " + "-" * 66,
    ]

    for r in results:
        n    = r['n_rows']
        thr  = r.get('threshold', threshold or 0)
        top3 = ', '.join(r['top_features'][:3])
        lines.append(
            f"  {r['symbol']:<12} {n:>6} "
            f"{r['n_long']/n*100:>6.1f}% "
            f"{r['n_short']/n*100:>6.1f}% "
            f"{r['n_neut']/n*100:>7.1f}% "
            f"{thr*100:>6.2f}%  {top3}"
        )

    lines += ["", "=" * 70]

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f"\n  📝 Summary salvato: {output_path}")


# ---------------------------------------------------------------------------
# 8. MAIN
# ---------------------------------------------------------------------------

def process_symbol(args_tuple):
    """
    Processa un singolo simbolo. Funzione standalone per compatibilità
    con multiprocessing (deve essere importabile a livello modulo).

    args_tuple = (sym, data_dir, horizon, threshold, auto_threshold,
                  save_csv, no_charts, quiet)
    Restituisce (result_dict | None, error_str | None)
    """
    sym, data_dir, horizon, threshold, auto_threshold, save_csv, no_charts, quiet = args_tuple

    try:
        csv_path  = find_csv(sym, data_dir)
        raw_data  = load_csv(csv_path)
        n_candles = len(raw_data['closes'])

        # Auto-threshold: calcola soglia adattiva per questo simbolo
        if auto_threshold:
            threshold = compute_auto_threshold(
                raw_data['closes'], horizon,
                highs=raw_data['highs'], lows=raw_data['lows']
            )

        if not quiet:
            print(f"  🔄 {sym:<12} ({n_candles} candele)  soglia={threshold*100:.2f}%")

        rows = build_features(raw_data, horizon, threshold)
        if not rows:
            return None, f"{sym}: nessun campione (dati insufficienti)"

        # Statistiche
        correlations = print_stats(sym, rows, horizon, threshold) if not quiet else _compute_correlations(rows)
        top_features = [k for k, _ in sorted(correlations.items(),
                                              key=lambda x: abs(x[1]), reverse=True)]

        # Grafico PNG
        if not no_charts:
            chart_path = OUTPUT_DIR / f"{sym}_features.png"
            plot_features(sym, rows, chart_path)

        # Dataset CSV
        if save_csv:
            csv_out = OUTPUT_DIR / f"{sym}_dataset.csv"
            save_dataset_csv(sym, rows, csv_out)

        n_long  = sum(1 for r in rows if r['label'] ==  1)
        n_short = sum(1 for r in rows if r['label'] == -1)
        n_neut  = sum(1 for r in rows if r['label'] ==  0)

        return {
            'symbol':       sym,
            'n_rows':       len(rows),
            'n_long':       n_long,
            'n_short':      n_short,
            'n_neut':       n_neut,
            'threshold':    threshold,
            'top_features': top_features,
        }, None

    except FileNotFoundError as e:
        return None, f"{sym}: {e}"
    except ValueError as e:
        return None, f"{sym}: {e}"
    except Exception as e:
        return None, f"{sym}: errore inatteso — {e}"


def _compute_correlations(rows: list) -> dict:
    """Calcola correlazioni senza stamparle (usato in modalità --quiet)."""
    if not rows:
        return {}
    feature_cols = [k for k in rows[0].keys()
                    if k not in ('timestamp', 'close', 'future_return_pct', 'label')]
    n      = len(rows)
    labels = [r['label'] for r in rows]
    mean_l = sum(labels) / n
    result = {}
    for col in feature_cols:
        vals   = [r[col] for r in rows]
        mean_v = sum(vals) / n
        num    = sum((v - mean_v) * (l - mean_l) for v, l in zip(vals, labels))
        den_v  = math.sqrt(sum((v - mean_v) ** 2 for v in vals) + 1e-10)
        den_l  = math.sqrt(sum((l - mean_l) ** 2 for l in labels) + 1e-10)
        result[col] = num / (den_v * den_l)
    return result


def main():
    parser = argparse.ArgumentParser(
        description='ml_features.py — Feature engineering per OpenTradeNet ML',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  # Tutti i simboli, soglia automatica, solo testo (VELOCE per 300 simboli)
  python3 ml_features.py --horizon 16 --auto-threshold --no-charts --quiet

  # Tutti i simboli + salva dataset CSV per il training
  python3 ml_features.py --horizon 16 --auto-threshold --save-csv --no-charts --quiet

  # Solo un simbolo con grafico
  python3 ml_features.py --symbol BTC --horizon 16 --auto-threshold

  # Soglia manuale fissa per tutti
  python3 ml_features.py --horizon 16 --threshold 0.015
        """
    )
    parser.add_argument('--data-dir',      default=DEFAULT_DATA_DIR,
                        help=f'Directory base candles (default: {DEFAULT_DATA_DIR})')
    parser.add_argument('--symbol',        default=None,
                        help='Processa solo questo simbolo (es: BTC)')
    parser.add_argument('--horizon',       type=int,   default=DEFAULT_HORIZON,
                        help=f'Candele future per label (default: {DEFAULT_HORIZON} = {DEFAULT_HORIZON*15}min)')
    parser.add_argument('--threshold',     type=float, default=DEFAULT_THRESHOLD,
                        help=f'Soglia manuale (default: {DEFAULT_THRESHOLD*100:.1f}%%). Ignorata se --auto-threshold')
    parser.add_argument('--auto-threshold',action='store_true',
                        help='Calcola soglia adattiva per simbolo (target ~62%% NEUTRO)')
    parser.add_argument('--save-csv',      action='store_true',
                        help='Salva dataset CSV in ml_reports/SIMBOLO_dataset.csv')
    parser.add_argument('--no-charts',     action='store_true',
                        help='Non generare grafici PNG (molto più veloce su 300 simboli)')
    parser.add_argument('--quiet',         action='store_true',
                        help='Output minimale: solo una riga per simbolo + summary finale')
    args = parser.parse_args()

    # Risolve data_dir relativa alla posizione di questo script
    script_dir = Path(__file__).parent
    data_dir   = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = script_dir / data_dir

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n{'═'*60}")
    print(f"  ml_features.py — OpenTradeNet Feature Engineering")
    print(f"{'═'*60}")
    print(f"  Data dir  : {data_dir}")
    print(f"  Horizon   : {args.horizon} candele ({args.horizon*15} min)")
    if args.auto_threshold:
        print(f"  Soglia    : AUTO (target ~{AUTO_THRESHOLD_NEUTRO_TARGET*100:.0f}% NEUTRO per simbolo)")
    else:
        print(f"  Soglia    : {args.threshold*100:.2f}% (manuale)")
    print(f"  Grafici   : {'no' if args.no_charts else 'sì'}")
    print(f"  Output    : {OUTPUT_DIR}/")

    # Scopre simboli
    if args.symbol:
        symbols = [args.symbol.upper()]
    else:
        symbols = []
        if data_dir.exists():
            for entry in sorted(data_dir.iterdir()):
                if entry.is_dir():
                    symbols.append(entry.name)
                elif entry.suffix == '.csv':
                    name = entry.stem.replace('_15m', '').replace('_15M', '')
                    symbols.append(name)
        if not symbols:
            print(f"\n  ❌ Nessun simbolo trovato in {data_dir}")
            sys.exit(1)

    print(f"  Simboli   : {len(symbols)}  ({', '.join(symbols[:8])}{'...' if len(symbols) > 8 else ''})")
    print()

    # Prepara argomenti per ogni simbolo
    task_args = [
        (sym, data_dir, args.horizon, args.threshold,
         args.auto_threshold, args.save_csv, args.no_charts, args.quiet)
        for sym in symbols
    ]

    results = []
    errors  = []
    import time
    t0 = time.time()

    # Elaborazione sequenziale con progress counter
    for i, task in enumerate(task_args):
        sym = task[0]
        if args.quiet:
            # Mostra progresso ogni 10 simboli
            if i % 10 == 0:
                elapsed = time.time() - t0
                eta = (elapsed / (i + 1)) * (len(symbols) - i - 1) if i > 0 else 0
                print(f"  [{i+1:3d}/{len(symbols)}]  {sym:<12}  "
                      f"elapsed={elapsed:.0f}s  ETA={eta:.0f}s")
                sys.stdout.flush()

        result, error = process_symbol(task)
        if result:
            results.append(result)
        if error:
            errors.append(error)
            if not args.quiet:
                print(f"  ⚠️  {error}")

    elapsed_total = time.time() - t0

    # Summary a schermo in modalità quiet
    if args.quiet and results:
        print(f"\n{'─'*60}")
        print(f"  {'SIMBOLO':<12} {'CAMPIONI':>8} {'LONG%':>7} {'SHORT%':>7} {'NEUTRO%':>8} {'SOGLIA':>7}")
        print(f"{'─'*60}")
        for r in results:
            n = r['n_rows']
            print(f"  {r['symbol']:<12} {n:>8} "
                  f"{r['n_long']/n*100:>6.1f}% "
                  f"{r['n_short']/n*100:>6.1f}% "
                  f"{r['n_neut']/n*100:>7.1f}% "
                  f"{r['threshold']*100:>6.2f}%")

    # Summary file
    if results:
        summary_path = OUTPUT_DIR / 'summary.txt'
        write_summary(results, summary_path, args.horizon,
                      args.threshold if not args.auto_threshold else None)

    print(f"\n{'═'*60}")
    print(f"  ✅ Completato in {elapsed_total:.1f}s")
    print(f"  Simboli OK : {len(results)}")
    if errors:
        print(f"  Errori     : {len(errors)}")
        for e in errors[:5]:
            print(f"    ⚠️  {e}")
        if len(errors) > 5:
            print(f"    ... e altri {len(errors)-5}")
    print(f"{'═'*60}\n")


if __name__ == '__main__':
    main()
