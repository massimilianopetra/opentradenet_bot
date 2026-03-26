"""
mega_summary.py — Summary tecnico locale per /analyze --dryrun
Dipendenze: solo stdlib (math, datetime, csv)
"""
import csv as _csv
import math
from datetime import datetime, timedelta


# ──────────────────────────────────────────────────────────────────────────────
# Lettura CSV
# ──────────────────────────────────────────────────────────────────────────────

def _load_csv(path: str, max_rows: int = None) -> list:
    """Legge CSV OHLCV, ordina ASC per timestamp. Se max_rows, prende gli ultimi N."""
    candles = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            reader = _csv.DictReader(f)
            for row in reader:
                try:
                    candles.append({
                        'timestamp': row['timestamp'],
                        'open':   float(row['open']),
                        'high':   float(row['high']),
                        'low':    float(row['low']),
                        'close':  float(row['close']),
                        'volume': float(row['volume']),
                    })
                except Exception:
                    continue
    except Exception:
        return []
    candles.sort(key=lambda c: c['timestamp'])
    if max_rows and len(candles) > max_rows:
        candles = candles[-max_rows:]
    return candles


# ──────────────────────────────────────────────────────────────────────────────
# Indicatori tecnici
# ──────────────────────────────────────────────────────────────────────────────

def _ema(closes: list, period: int) -> list:
    """EMA standard. Inizializza con SMA dei primi `period` valori."""
    n = len(closes)
    result = [None] * n
    if n < period:
        return result
    alpha = 2.0 / (period + 1)
    sma = sum(closes[:period]) / period
    result[period - 1] = sma
    prev = sma
    for i in range(period, n):
        val = closes[i] * alpha + prev * (1 - alpha)
        result[i] = val
        prev = val
    return result


def _rsi(closes: list, period: int = 14) -> list:
    """RSI con Wilder smoothing."""
    n = len(closes)
    result = [None] * n
    if n < period + 1:
        return result
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    result[period] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
        result[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return result


def _macd(closes: list) -> tuple:
    """MACD (12,26,9). Ritorna (macd_line, signal_line, histogram)."""
    n = len(closes)
    e12 = _ema(closes, 12)
    e26 = _ema(closes, 26)
    macd_line = [
        e12[i] - e26[i] if e12[i] is not None and e26[i] is not None else None
        for i in range(n)
    ]
    m_pts = [(i, v) for i, v in enumerate(macd_line) if v is not None]
    signal_line = [None] * n
    histogram   = [None] * n
    if len(m_pts) >= 9:
        alpha = 2.0 / 10
        sma   = sum(v for _, v in m_pts[:9]) / 9
        fi    = m_pts[8][0]
        signal_line[fi] = sma
        histogram[fi]   = macd_line[fi] - sma
        prev = sma
        for j in range(9, len(m_pts)):
            i, v = m_pts[j]
            sig        = v * alpha + prev * (1 - alpha)
            signal_line[i] = sig
            histogram[i]   = v - sig
            prev = sig
    return macd_line, signal_line, histogram


def _bollinger(closes: list, period: int = 20) -> tuple:
    """Bollinger Bands (period, 2σ population). Ritorna (upper, mid, lower)."""
    n = len(closes)
    upper, mid, lower = [None] * n, [None] * n, [None] * n
    for i in range(period - 1, n):
        w   = closes[i - period + 1: i + 1]
        m   = sum(w) / period
        dev = math.sqrt(sum((x - m) ** 2 for x in w) / period)
        mid[i]   = m
        upper[i] = m + 2 * dev
        lower[i] = m - 2 * dev
    return upper, mid, lower


def _find_sr_zones(candles: list, tolerance: float = 0.30) -> dict:
    """
    Raggruppa low/high in zone S/R con tolleranza ±0.30 assoluta (consecutive sort).
    Filtra zone con >=2 tocchi o volume >50k.
    Ritorna {'supports': [...], 'resistances': [...]}
    ciascuna lista di dict: {price, touches, volume, strength}
    """
    def _cluster(pts):
        s = sorted(pts, key=lambda x: x[0])
        if not s:
            return []
        zones, cur = [], [s[0]]
        for i in range(1, len(s)):
            if s[i][0] - s[i - 1][0] < tolerance:
                cur.append(s[i])
            else:
                zones.append(cur)
                cur = [s[i]]
        zones.append(cur)
        out = []
        for z in zones:
            prices = [p[0] for p in z]
            vols   = [p[1] for p in z]
            n_z    = len(z)
            vol_z  = sum(vols)
            if n_z < 2 and vol_z <= 50_000:
                continue
            if n_z >= 7 or vol_z > 500_000:
                strength = '⭐⭐⭐'
            elif n_z >= 4:
                strength = '⭐⭐'
            else:
                strength = '⭐'
            out.append({
                'price':    sum(prices) / n_z,
                'touches':  n_z,
                'volume':   vol_z,
                'strength': strength,
            })
        return out

    low_pts  = [(c['low'],  c['volume']) for c in candles]
    high_pts = [(c['high'], c['volume']) for c in candles]
    return {
        'supports':    _cluster(low_pts),
        'resistances': _cluster(high_pts),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ──────────────────────────────────────────────────────────────────────────────

def _last_valid(lst):
    for v in reversed(lst):
        if v is not None:
            return v
    return None


def _last_n_valid(lst, n):
    out = []
    for v in reversed(lst):
        if v is not None:
            out.append(v)
        if len(out) == n:
            break
    return list(reversed(out))


def _fmt_px(v):
    if v is None:
        return 'N/A'
    return f"{v:.3f}"


def _fmt_pct(v):
    if v is None:
        return 'N/A'
    sign = '+' if v >= 0 else ''
    return f"{sign}{v:.2f}%"


def _local_minima(values, window=2):
    """Indici che sono minimi locali con finestra ±window."""
    n = len(values)
    out = []
    for i in range(window, n - window):
        if (all(values[i] <= values[i - j] for j in range(1, window + 1)) and
                all(values[i] <= values[i + j] for j in range(1, window + 1))):
            out.append(i)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Builder sezioni
# ──────────────────────────────────────────────────────────────────────────────

def _build_regime(candles_1d: list, m: dict) -> str:
    """SEZIONE 1 — REGIME DI MERCATO (1D)."""
    closes = [c['close'] for c in candles_1d]
    n = len(closes)
    lines = ['📈 SEZIONE 1 — REGIME DI MERCATO (1D)', '─' * 44]

    e9  = _ema(closes, 9)
    e21 = _ema(closes, 21)
    e50 = _ema(closes, 50)

    v9  = _last_valid(e9)
    v21 = _last_valid(e21)
    v50 = _last_valid(e50)
    px  = closes[-1] if closes else None

    if v9  is not None: lines.append(f"EMA9:  {_fmt_px(v9)}")
    if v21 is not None: lines.append(f"EMA21: {_fmt_px(v21)}")
    if v50 is not None:
        lines.append(f"EMA50: {_fmt_px(v50)}")
    else:
        lines.append(f"EMA50: dati insufficienti ({n}/50)")

    regime = 'LATERALE'
    if v9 and v21 and v50 and px:
        if v9 > v21 > v50 and px > v9:
            regime = 'TREND RIALZISTA'
        elif v9 < v21 < v50 and px < v9:
            regime = 'TREND RIBASSISTA'
        else:
            # Check VOLATILE
            bb_u, bb_m, bb_l = _bollinger(closes, 20)
            bu, bm, bl = _last_valid(bb_u), _last_valid(bb_m), _last_valid(bb_l)
            vol_vals = [c['volume'] for c in candles_1d]
            vol_last = vol_vals[-1] if vol_vals else 0
            vol_ma20 = sum(vol_vals[-20:]) / 20 if len(vol_vals) >= 20 else (sum(vol_vals) / len(vol_vals) if vol_vals else 1)
            if bu and bm and bl and bm != 0:
                if (bu - bl) / bm * 100 > 8 and vol_last > 2 * vol_ma20:
                    regime = 'VOLATILE'
    elif v9 and v21 and px:
        if v9 > v21 and px > v9:
            regime = 'TREND RIALZISTA'
        elif v9 < v21 and px < v9:
            regime = 'TREND RIBASSISTA'

    lines.append(f"Regime: {regime}")
    if px and v9 and v21 and v50:
        if px > v9 > v21 > v50:
            lines.append("Prezzo: SOPRA tutte le EMA (forza)")
        elif px < v9 < v21 < v50:
            lines.append("Prezzo: SOTTO tutte le EMA (debolezza)")
        else:
            lines.append("Prezzo: tra le EMA (zona di transizione)")
    elif px:
        lines.append("Prezzo: dati EMA parziali")

    m['regime']   = regime
    m['ema9_1d']  = v9
    m['ema21_1d'] = v21
    m['ema50_1d'] = v50
    return '\n'.join(lines)


def _build_indicators(candles: list, label: str, m: dict) -> str:
    """SEZIONE 2 (1D) o SEZIONE 3 (15M) — Indicatori."""
    closes = [c['close'] for c in candles]
    n = len(closes)
    sec_num = '2' if label == '1D' else '3'
    lines = [f'📊 SEZIONE {sec_num} — INDICATORI {label}', '─' * 44]

    # ── RSI ──────────────────────────────────────────
    rsi_vals = _rsi(closes)
    rsi_cur  = _last_valid(rsi_vals)

    if rsi_cur is not None:
        rsi_zone = ('IPERVENDUTO' if rsi_cur < 30 else
                    'IPERCOMPRATO' if rsi_cur > 70 else 'NEUTRO')
        valid_rsi = [(i, v) for i, v in enumerate(rsi_vals) if v is not None]
        if len(valid_rsi) >= 4:
            diff = rsi_cur - valid_rsi[-4][1]
            rsi_dir = 'SALENDO' if diff > 2 else ('SCENDENDO' if diff < -2 else 'STABILE')
        else:
            rsi_dir = 'N/D'
        lines.append(f"RSI14: {rsi_cur:.2f}  Zona: {rsi_zone}  Dir: {rsi_dir}")

        # Divergenza: ultimi 2 minimi locali di prezzo
        lm_idx  = _local_minima(closes)
        div_txt = 'NESSUNA'
        if (len(lm_idx) >= 2
                and rsi_vals[lm_idx[-1]] is not None
                and rsi_vals[lm_idx[-2]] is not None):
            p1, p2   = closes[lm_idx[-2]], closes[lm_idx[-1]]
            r1, r2   = rsi_vals[lm_idx[-2]], rsi_vals[lm_idx[-1]]
            if p2 < p1 and r2 > r1:
                div_txt = 'RIALZISTA (bullish divergence)'
            elif p2 > p1 and r2 < r1:
                div_txt = 'RIBASSISTA (bearish divergence)'
        lines.append(f"  Divergenza: {div_txt}")
    else:
        rsi_dir = 'N/D'
        lines.append("RSI14: dati insufficienti")

    # ── MACD ─────────────────────────────────────────
    if n < 35:
        lines.append("MACD: dati insufficienti")
        m[f'macd_cross_{label}'] = False
        m[f'hist_{label}']       = None
    else:
        ml, sl, hl = _macd(closes)
        mv = _last_valid(ml)
        sv = _last_valid(sl)
        hv = _last_valid(hl)
        if mv is None:
            lines.append("MACD: dati insufficienti")
            m[f'macd_cross_{label}'] = False
            m[f'hist_{label}']       = None
        else:
            pos_zero = 'SOPRA' if mv > 0 else 'SOTTO'
            last3h   = _last_n_valid(hl, 3)
            crossover = (len(last3h) >= 2 and
                         any(last3h[i] * last3h[i + 1] < 0 for i in range(len(last3h) - 1)))
            cross_s = 'SI' if crossover else 'NO'
            if len(last3h) >= 3:
                if last3h[0] < last3h[1] < last3h[2]:
                    momentum = 'AUMENTANDO'
                elif last3h[0] > last3h[1] > last3h[2]:
                    momentum = 'DIMINUENDO'
                else:
                    momentum = 'MISTO'
            else:
                momentum = 'N/D'
            lines.append(f"MACD: {mv:.4f}  Signal: {sv:.4f}  Hist: {hv:.4f}")
            lines.append(f"  vs zero: {pos_zero}  Crossover: {cross_s}  Momentum: {momentum}")
            m[f'macd_cross_{label}'] = crossover
            m[f'hist_{label}']       = hv

    # ── Bollinger Bands ───────────────────────────────
    bb_u, bb_m, bb_l = _bollinger(closes)
    bu, bm, bl = _last_valid(bb_u), _last_valid(bb_m), _last_valid(bb_l)
    px = closes[-1] if closes else None
    if bu is None or bm is None or bl is None:
        lines.append("BB: dati insufficienti")
    else:
        if px is not None:
            if px > bm + 0.6 * (bu - bm):
                bb_pos = 'VICINO_SUPERIORE'
            elif px < bm - 0.6 * (bm - bl):
                bb_pos = 'VICINO_INFERIORE'
            else:
                bb_pos = 'CENTRALE'
        else:
            bb_pos = 'N/D'
        bb_w = (bu - bl) / bm * 100 if bm else 0
        bb_cls = 'STRETTE' if bb_w < 3 else ('NORMALI' if bb_w <= 8 else 'ALLARGATE')
        lines.append(f"BB: Sup {_fmt_px(bu)}  Mid {_fmt_px(bm)}  Inf {_fmt_px(bl)}")
        lines.append(f"  Posizione: {bb_pos}  Ampiezza: {bb_w:.2f}% ({bb_cls})")
        m[f'bb_width_{label}'] = bb_w

    # Salva metriche per sezione 6
    m[f'rsi_{label}'] = rsi_cur
    if label == '15M':
        e9v  = _last_valid(_ema(closes, 9))
        e21v = _last_valid(_ema(closes, 21))
        e50v = _last_valid(_ema(closes, 50))
        m['ema9_15m']  = e9v
        m['ema21_15m'] = e21v
        m['ema50_15m'] = e50v

    return '\n'.join(lines)


def _build_sr(candles_15m_all: list, current_price: float, m: dict) -> str:
    """SEZIONE 4 — SUPPORTI E RESISTENZE (dati 15M ultimi 5 giorni)."""
    lines = ['🎯 SEZIONE 4 — SUPPORTI E RESISTENZE', '─' * 44]

    # Filtro ultimi 5 giorni
    if candles_15m_all:
        last_ts = candles_15m_all[-1]['timestamp']
        try:
            last_dt = datetime.strptime(last_ts[:19], '%Y-%m-%d %H:%M:%S')
        except Exception:
            last_dt = datetime.now()
        cutoff_str = (last_dt - timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S')
        candles_5d = [c for c in candles_15m_all if c['timestamp'] >= cutoff_str]
    else:
        candles_5d = []

    if not candles_5d:
        lines.append("Dati insufficienti per S/R")
        m['sr_near_strong'] = False
        return '\n'.join(lines)

    zones = _find_sr_zones(candles_5d)

    def add_dist(lst):
        out = []
        for z in lst:
            d = abs(z['price'] - current_price) / current_price * 100 if current_price else 0
            out.append({**z, 'distance_pct': d})
        return sorted(out, key=lambda x: x['distance_pct'])

    supports    = add_dist(zones['supports'])[:3]
    resistances = add_dist(zones['resistances'])[:3]

    lines.append("SUPPORTI:")
    if supports:
        for z in supports:
            lines.append(
                f"  {_fmt_px(z['price'])}  tocchi:{z['touches']}  "
                f"vol:{z['volume']:.0f}  {z['strength']}  dist:{z['distance_pct']:.2f}%"
            )
    else:
        lines.append("  Nessun supporto rilevante")

    lines.append("RESISTENZE:")
    if resistances:
        for z in resistances:
            lines.append(
                f"  {_fmt_px(z['price'])}  tocchi:{z['touches']}  "
                f"vol:{z['volume']:.0f}  {z['strength']}  dist:{z['distance_pct']:.2f}%"
            )
    else:
        lines.append("  Nessuna resistenza rilevante")

    # Min/Max assoluto del periodo
    all_lows  = [c['low']  for c in candles_5d]
    all_highs = [c['high'] for c in candles_5d]
    ts_list   = [c['timestamp'] for c in candles_5d]
    if all_lows:
        min_idx = all_lows.index(min(all_lows))
        max_idx = all_highs.index(max(all_highs))
        lines.append(
            f"Min periodo: {_fmt_px(all_lows[min_idx])}  ({ts_list[min_idx][:16]})"
        )
        lines.append(
            f"Max periodo: {_fmt_px(all_highs[max_idx])}  ({ts_list[max_idx][:16]})"
        )

    # Alert: prezzo vicino a zona ⭐⭐⭐ entro 0.5%
    near_strong = any(
        z['distance_pct'] < 0.5 and z['strength'] == '⭐⭐⭐'
        for z in supports + resistances
    )
    m['sr_near_strong'] = near_strong
    return '\n'.join(lines)


def _build_volume(candles_15m: list, m: dict) -> str:
    """SEZIONE 5 — VOLUME (15M)."""
    lines = ['📦 SEZIONE 5 — VOLUME (15M)', '─' * 44]

    vols = [c['volume'] for c in candles_15m]
    if len(vols) < 2:
        lines.append("Dati insufficienti")
        m['vol_ratio'] = 1.0
        return '\n'.join(lines)

    # MA20
    if len(vols) >= 20:
        vol_ma20 = sum(vols[-20:]) / 20
    else:
        vol_ma20 = sum(vols) / len(vols)

    vol_last = vols[-2] if len(vols) >= 2 else vols[-1]  # ultima candela chiusa
    ratio    = vol_last / vol_ma20 if vol_ma20 > 0 else 1.0

    if ratio > 2.0:
        vol_class = 'SPIKE ANOMALO'
    elif ratio >= 1.5:
        vol_class = 'ALTO'
    elif ratio >= 0.5:
        vol_class = 'NORMALE'
    else:
        vol_class = 'BASSO'

    # Trend ultimi 5 periodi
    last5 = vols[-5:] if len(vols) >= 5 else vols
    if len(last5) >= 2:
        if all(last5[i] < last5[i + 1] for i in range(len(last5) - 1)):
            vol_trend = 'CRESCENTE'
        elif all(last5[i] > last5[i + 1] for i in range(len(last5) - 1)):
            vol_trend = 'DECRESCENTE'
        else:
            vol_trend = 'MISTO'
    else:
        vol_trend = 'N/D'

    lines.append(f"MA20 volume:    {vol_ma20:.2f}")
    lines.append(f"Ultima candela: {vol_last:.2f}  ({ratio:.2f}x MA20)")
    lines.append(f"Classificazione: {vol_class}")
    lines.append(f"Trend 5 periodi: {vol_trend}")

    m['vol_ratio'] = ratio
    return '\n'.join(lines)


def _build_summary(symbol: str, current_price: float,
                   candles_15m: list, candles_15m_all: list, m: dict) -> str:
    """SEZIONE 6 — SINTESI OPERATIVA."""
    lines = ['⚡ SEZIONE 6 — SINTESI OPERATIVA', '─' * 44]

    closes_15m = [c['close'] for c in candles_15m]
    last_close = closes_15m[-1] if closes_15m else current_price
    now_str    = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Variazione 24H (~96 candele 15m fa)
    closes_all = [c['close'] for c in candles_15m_all]
    if len(closes_all) >= 97:
        ref_24h   = closes_all[-97]
        chg_24h   = (closes_all[-1] - ref_24h) / ref_24h * 100 if ref_24h else 0
    elif len(closes_all) >= 2:
        ref_24h   = closes_all[0]
        chg_24h   = (closes_all[-1] - ref_24h) / ref_24h * 100 if ref_24h else 0
    else:
        chg_24h = 0.0

    lines.append(f"Simbolo:   {symbol.upper()}")
    lines.append(f"Timestamp: {now_str}")
    lines.append(f"Prezzo:    {_fmt_px(last_close)}")
    lines.append(f"Var. 24H:  {_fmt_pct(chg_24h)}")

    # BIAS 1D
    regime = m.get('regime', 'LATERALE')
    if regime == 'TREND RIALZISTA':
        bias_1d = 'RIALZISTA'
    elif regime == 'TREND RIBASSISTA':
        bias_1d = 'RIBASSISTA'
    else:
        bias_1d = 'NEUTRO'

    # BIAS 15M
    e9  = m.get('ema9_15m')
    e21 = m.get('ema21_15m')
    e50 = m.get('ema50_15m')
    if e9 and e21 and e50:
        if e9 > e21 > e50:
            bias_15m = 'RIALZISTA'
        elif e9 < e21 < e50:
            bias_15m = 'RIBASSISTA'
        else:
            bias_15m = 'NEUTRO'
    elif e9 and e21:
        bias_15m = 'RIALZISTA' if e9 > e21 else ('RIBASSISTA' if e9 < e21 else 'NEUTRO')
    else:
        bias_15m = 'NEUTRO'

    alignment = 'ALLINEATI' if (
        (bias_1d == 'RIALZISTA' and bias_15m == 'RIALZISTA') or
        (bias_1d == 'RIBASSISTA' and bias_15m == 'RIBASSISTA')
    ) else 'CONFLITTO'

    lines.append(f"BIAS 1D:  {bias_1d}")
    lines.append(f"BIAS 15M: {bias_15m}")
    lines.append(f"Allineamento: {alignment}")

    # Testo contestuale (4 casi)
    if bias_1d == 'RIALZISTA' and bias_15m == 'RIALZISTA':
        ctx = "Trend rialzista confermato su 1D e 15M. Contesto favorevole per setup long."
    elif bias_1d == 'RIBASSISTA' and bias_15m == 'RIBASSISTA':
        ctx = "Trend ribassista confermato su 1D e 15M. Contesto favorevole per setup short."
    elif bias_1d == 'RIALZISTA' and bias_15m == 'RIBASSISTA':
        ctx = "Trend 1D rialzista, pressione ribassista su 15M. Attendere segnale di ripresa per long."
    else:
        ctx = "Trend 1D ribassista, rimbalzo su 15M. Attenzione: possibile bull trap."
    lines.append(f"Contesto: {ctx}")

    # Alert attivi
    alerts = []
    rsi_1d  = m.get('rsi_1D')
    rsi_15m = m.get('rsi_15M')
    if rsi_1d  is not None and (rsi_1d  < 30 or rsi_1d  > 70):
        alerts.append(f"RSI 1D zona estrema ({rsi_1d:.1f})")
    if rsi_15m is not None and (rsi_15m < 30 or rsi_15m > 70):
        alerts.append(f"RSI 15M zona estrema ({rsi_15m:.1f})")
    if m.get('macd_cross_15M'):
        alerts.append("MACD crossover recente su 15M")
    if m.get('vol_ratio', 0) > 2.0:
        alerts.append(f"Volume spike 15M ({m['vol_ratio']:.1f}x MA20)")
    if m.get('sr_near_strong'):
        alerts.append("Prezzo vicino a zona S/R ⭐⭐⭐ (<0.5%)")

    if alerts:
        lines.append("ALERT ATTIVI:")
        for a in alerts:
            lines.append(f"  ⚠️  {a}")
    else:
        lines.append("ALERT ATTIVI: nessuno")

    return '\n'.join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point pubblico
# ──────────────────────────────────────────────────────────────────────────────

def compute_mega_summary(symbol: str, csv_15m_path: str, csv_1d_path: str,
                         current_price: float) -> str:
    """
    Legge i due CSV, calcola indicatori tecnici e restituisce un testo
    formattato pronto per Telegram (ASCII + emoji, no HTML).

    Args:
        symbol        : es. 'SOL'
        csv_15m_path  : percorso CSV candele 15m
        csv_1d_path   : percorso CSV candele giornaliere (ricostruito da ml_analyst)
        current_price : prezzo corrente (usato per distanze S/R)
    """
    m: dict = {}  # metriche condivise tra sezioni

    candles_1d      = _load_csv(csv_1d_path)
    candles_15m_all = _load_csv(csv_15m_path)
    candles_15m     = candles_15m_all[-100:] if len(candles_15m_all) > 100 else candles_15m_all

    if not candles_1d and not candles_15m_all:
        return f"❌ Nessun dato disponibile per {symbol.upper()}"

    # Fallback 1D vuoto: usa dati 15M come proxy
    if not candles_1d:
        candles_1d = candles_15m_all[-60:] if len(candles_15m_all) > 60 else candles_15m_all

    sections = [
        f"📋 MEGA SUMMARY — {symbol.upper()}\n{'═' * 44}",
        _build_regime(candles_1d, m),
        _build_indicators(candles_1d,  '1D',  m),
        _build_indicators(candles_15m, '15M', m),
        _build_sr(candles_15m_all, current_price, m),
        _build_volume(candles_15m, m),
        _build_summary(symbol, current_price, candles_15m, candles_15m_all, m),
    ]
    return '\n\n'.join(sections)
