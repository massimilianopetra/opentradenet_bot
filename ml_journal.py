"""
ml_journal.py — Trade journal automatico per OpenTradeNet
==========================================================
Registra ogni trade aperto via bot con snapshot tecnico (RSI, EMA trend,
Bollinger, ATR, score ML scanner) e lo completa alla chiusura con P&L.

API pubblica
------------
    log_open(symbol, direction, entry_price, size_usd,
             ml_score=None, ml_signal=None, candles_dir=None, chat_id=None)
             → trade_id: str

    log_close(symbol, exit_price, pnl_usd, chat_id=None)
             → bool (True se trovato e aggiornato)

    query_similar(symbol, direction, min_trades=3)
             → dict | None   (win_rate, n_trades, avg_pnl, avg_rr)

    get_open_trades(chat_id=None)
             → list[dict]

    get_journal_stats()
             → dict   (totali, per simbolo, per direzione)

Persistenza
-----------
    data/journal/trades.json   ← unico file JSON, array di record

Formato record
--------------
{
  "id":           "SOL_20260318_143000_abc1",
  "symbol":       "SOL",
  "direction":    "long",          # "long" | "short"
  "entry_price":  134.5,
  "entry_time":   "2026-03-18T14:30:00",
  "size_usd":     500.0,
  "ml_score":     74,              # None se scanner non disponibile
  "ml_signal":    "LONG",         # None se scanner non disponibile
  "features":     {                # snapshot tecnico al momento entry
      "rsi":          38.2,
      "ema_trend":   -1,           # +1 rialzo, -1 ribasso, 0 neutro
      "bb_position":  0.21,        # 0=banda inf, 1=banda sup
      "atr_pct":      1.4,         # ATR% sul prezzo
      "volume_ratio": 1.3          # volume candela / media 20
  },
  "exit_price":   null,
  "exit_time":    null,
  "pnl_usd":      null,
  "pnl_pct":      null,
  "status":       "open",          # "open" | "closed"
  "chat_id":      123456789        # opzionale, per multi-utente
}
"""

from __future__ import annotations

import csv
import json
import logging
import random
import string
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

_JOURNAL_DIR  = Path(__file__).parent / "data" / "journal"
_JOURNAL_FILE = _JOURNAL_DIR / "trades.json"

# Numero di candele 15m da leggere per lo snapshot tecnico (~6 ore)
_FEATURE_LOOKBACK = 24

# ---------------------------------------------------------------------------
# Persistenza
# ---------------------------------------------------------------------------

def _load_trades() -> list[dict]:
    """Carica il journal da disco. Ritorna lista vuota se non esiste."""
    if not _JOURNAL_FILE.exists():
        return []
    try:
        with open(_JOURNAL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"ml_journal: errore lettura {_JOURNAL_FILE}: {e}")
        return []


def _save_trades(trades: list[dict]) -> None:
    """Salva il journal su disco (scrittura atomica)."""
    _JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _JOURNAL_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(trades, f, indent=2, ensure_ascii=False)
        tmp.replace(_JOURNAL_FILE)
    except Exception as e:
        logger.error(f"ml_journal: errore scrittura {_JOURNAL_FILE}: {e}")


def _make_id(symbol: str) -> str:
    """Genera un ID univoco per il trade."""
    ts  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{symbol}_{ts}_{rnd}"


# ---------------------------------------------------------------------------
# Snapshot tecnico
# ---------------------------------------------------------------------------

def _compute_features(symbol: str, candles_dir: str | Path | None) -> dict:
    """
    Calcola un subset leggero di feature tecniche dalle candele 15m locali.
    Non usa ml_features.py per evitare dipendenze circolari — logica autonoma
    basata su numpy puro (già presente nel progetto).

    Ritorna dict con chiavi: rsi, ema_trend, bb_position, atr_pct, volume_ratio.
    Ritorna {} se le candele non sono disponibili.
    """
    if candles_dir is None:
        return {}

    candles_dir = Path(candles_dir)

    # Trova il CSV (stessa logica di candle_chart.find_csv)
    candidates = [
        candles_dir / symbol / f"{symbol}_15m.csv",
        candles_dir / f"{symbol}_15m.csv",
        candles_dir / symbol / f"{symbol}.csv",
        candles_dir / f"{symbol}.csv",
    ]
    csv_path = next((p for p in candidates if p.exists()), None)
    if csv_path is None:
        return {}

    try:
        closes  = []
        highs   = []
        lows    = []
        volumes = []

        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows   = list(reader)

        # Ultime N candele ordinate cronologicamente
        rows = sorted(rows, key=lambda r: r["timestamp"])[-(_FEATURE_LOOKBACK + 1):]

        if len(rows) < 10:
            return {}

        for r in rows:
            closes.append(float(r["close"]))
            highs.append(float(r["high"]))
            lows.append(float(r["low"]))
            volumes.append(float(r["volume"]))

        import numpy as np
        closes  = np.array(closes)
        highs   = np.array(highs)
        lows    = np.array(lows)
        volumes = np.array(volumes)

        # ── RSI(14) ──────────────────────────────────────────────────────
        def _rsi(c: np.ndarray, period: int = 14) -> float:
            if len(c) < period + 1:
                return 50.0
            deltas = np.diff(c)
            gains  = np.where(deltas > 0, deltas, 0.0)
            losses = np.where(deltas < 0, -deltas, 0.0)
            avg_g  = gains[-period:].mean()
            avg_l  = losses[-period:].mean()
            if avg_l == 0:
                return 100.0
            rs = avg_g / avg_l
            return round(100 - 100 / (1 + rs), 1)

        rsi = _rsi(closes)

        # ── EMA trend (9/21) ─────────────────────────────────────────────
        def _ema(c: np.ndarray, n: int) -> float:
            k = 2 / (n + 1)
            e = c[0]
            for v in c[1:]:
                e = v * k + e * (1 - k)
            return e

        ema9  = _ema(closes, 9)
        ema21 = _ema(closes, 21)
        last  = closes[-1]
        if last > ema9 > ema21:
            ema_trend = 1
        elif last < ema9 < ema21:
            ema_trend = -1
        else:
            ema_trend = 0

        # ── Bollinger position (20,2) ─────────────────────────────────────
        period = min(20, len(closes))
        ma     = closes[-period:].mean()
        std    = closes[-period:].std()
        if std > 0:
            bb_position = round(float((last - (ma - 2 * std)) / (4 * std)), 3)
            bb_position = max(0.0, min(1.0, bb_position))
        else:
            bb_position = 0.5

        # ── ATR% ────────────────────────────────────────────────────────
        atr_period = min(14, len(closes) - 1)
        trs = []
        for i in range(1, atr_period + 1):
            tr = max(
                highs[-i] - lows[-i],
                abs(highs[-i] - closes[-i - 1]),
                abs(lows[-i]  - closes[-i - 1]),
            )
            trs.append(tr)
        atr     = float(np.mean(trs)) if trs else 0.0
        atr_pct = round(atr / last * 100, 3) if last > 0 else 0.0

        # ── Volume ratio (ultima candela / media 20) ─────────────────────
        vol_avg   = volumes[-20:].mean() if len(volumes) >= 20 else volumes.mean()
        vol_ratio = round(float(volumes[-1] / vol_avg), 2) if vol_avg > 0 else 1.0

        return {
            "rsi":          rsi,
            "ema_trend":    ema_trend,
            "bb_position":  bb_position,
            "atr_pct":      atr_pct,
            "volume_ratio": vol_ratio,
        }

    except Exception as e:
        logger.warning(f"ml_journal: snapshot feature {symbol} fallito: {e}")
        return {}


# ---------------------------------------------------------------------------
# API pubblica
# ---------------------------------------------------------------------------

def log_open(
    symbol:      str,
    direction:   str,          # "long" | "short"
    entry_price: float,
    size_usd:    float,
    ml_score:    Optional[int]   = None,
    ml_signal:   Optional[str]   = None,
    candles_dir: Optional[str | Path] = None,
    chat_id:     Optional[int]   = None,
) -> str:
    """
    Registra l'apertura di un trade.
    Ritorna il trade_id generato.
    """
    trade_id = _make_id(symbol)
    features = _compute_features(symbol, candles_dir)

    record = {
        "id":          trade_id,
        "symbol":      symbol.upper(),
        "direction":   direction.lower(),
        "entry_price": round(float(entry_price), 8),
        "entry_time":  datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        "size_usd":    round(float(size_usd), 2),
        "ml_score":    ml_score,
        "ml_signal":   ml_signal,
        "features":    features,
        "exit_price":  None,
        "exit_time":   None,
        "pnl_usd":     None,
        "pnl_pct":     None,
        "status":      "open",
        "chat_id":     chat_id,
    }

    trades = _load_trades()
    trades.append(record)
    _save_trades(trades)

    logger.info(
        f"ml_journal: OPEN {symbol} {direction} @ {entry_price} "
        f"size={size_usd} score={ml_score} id={trade_id}"
    )
    return trade_id


def log_close(
    symbol:     str,
    exit_price: float,
    pnl_usd:    float,
    chat_id:    Optional[int] = None,
) -> bool:
    """
    Completa il trade aperto più recente per il simbolo (e chat_id se fornito).
    Ritorna True se trovato e aggiornato, False altrimenti.
    """
    trades = _load_trades()
    symbol = symbol.upper()

    # Cerca l'open trade più recente per symbol (+chat_id opzionale)
    candidates = [
        t for t in trades
        if t["symbol"] == symbol
        and t["status"] == "open"
        and (chat_id is None or t.get("chat_id") == chat_id)
    ]

    if not candidates:
        logger.warning(f"ml_journal: nessun trade open trovato per {symbol} chat={chat_id}")
        return False

    # Prende il più recente per entry_time
    target = max(candidates, key=lambda t: t["entry_time"])

    # Calcola pnl%
    ep = target["entry_price"]
    pnl_pct = None
    if ep and ep > 0 and target["size_usd"]:
        pnl_pct = round(pnl_usd / target["size_usd"] * 100, 2)

    target["exit_price"] = round(float(exit_price), 8)
    target["exit_time"]  = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    target["pnl_usd"]    = round(float(pnl_usd), 4)
    target["pnl_pct"]    = pnl_pct
    target["status"]     = "closed"

    _save_trades(trades)

    outcome = "✅ WIN" if pnl_usd >= 0 else "❌ LOSS"
    logger.info(
        f"ml_journal: CLOSE {symbol} @ {exit_price} "
        f"pnl={pnl_usd:+.2f}$ ({pnl_pct:+.1f}%) {outcome} id={target['id']}"
    )
    return True


def query_similar(
    symbol:     str,
    direction:  str,
    min_trades: int = 3,
) -> Optional[dict]:
    """
    Cerca nel journal i trade chiusi con stesso simbolo e direzione.
    Ritorna un dict con statistiche aggregate, o None se non ci sono
    abbastanza trade (< min_trades).

    Returned dict:
        {
          "n_trades":   int,
          "win_rate":   float,   # 0-100
          "avg_pnl":    float,   # USD medio
          "avg_pnl_pct": float,  # % medio
          "best_pnl":   float,
          "worst_pnl":  float,
        }
    """
    trades = _load_trades()
    symbol    = symbol.upper()
    direction = direction.lower()

    similar = [
        t for t in trades
        if t["symbol"]    == symbol
        and t["direction"] == direction
        and t["status"]    == "closed"
        and t["pnl_usd"]  is not None
    ]

    if len(similar) < min_trades:
        return None

    pnls     = [t["pnl_usd"]  for t in similar]
    pnl_pcts = [t["pnl_pct"]  for t in similar if t["pnl_pct"] is not None]
    wins     = [p for p in pnls if p >= 0]

    return {
        "n_trades":    len(similar),
        "win_rate":    round(len(wins) / len(similar) * 100, 1),
        "avg_pnl":     round(sum(pnls) / len(pnls), 2),
        "avg_pnl_pct": round(sum(pnl_pcts) / len(pnl_pcts), 2) if pnl_pcts else None,
        "best_pnl":    round(max(pnls), 2),
        "worst_pnl":   round(min(pnls), 2),
    }


def get_open_trades(chat_id: Optional[int] = None) -> list[dict]:
    """Ritorna la lista dei trade attualmente aperti."""
    trades = _load_trades()
    return [
        t for t in trades
        if t["status"] == "open"
        and (chat_id is None or t.get("chat_id") == chat_id)
    ]


def get_journal_stats() -> dict:
    """
    Statistiche aggregate del journal.
    Ritorna dict con chiavi: total, closed, open, win_rate, avg_pnl,
    total_pnl, by_symbol, by_direction.
    """
    trades  = _load_trades()
    closed  = [t for t in trades if t["status"] == "closed" and t["pnl_usd"] is not None]
    open_tr = [t for t in trades if t["status"] == "open"]

    if not closed:
        return {
            "total":        len(trades),
            "closed":       0,
            "open":         len(open_tr),
            "win_rate":     None,
            "avg_pnl":      None,
            "total_pnl":    None,
            "by_symbol":    {},
            "by_direction": {},
        }

    pnls  = [t["pnl_usd"] for t in closed]
    wins  = [p for p in pnls if p >= 0]

    # Per simbolo
    by_symbol: dict = {}
    for t in closed:
        s = t["symbol"]
        if s not in by_symbol:
            by_symbol[s] = {"n": 0, "wins": 0, "pnl": 0.0}
        by_symbol[s]["n"]    += 1
        by_symbol[s]["pnl"]  += t["pnl_usd"]
        if t["pnl_usd"] >= 0:
            by_symbol[s]["wins"] += 1
    for s in by_symbol:
        d = by_symbol[s]
        d["win_rate"] = round(d["wins"] / d["n"] * 100, 1)
        d["pnl"]      = round(d["pnl"], 2)

    # Per direzione
    by_direction: dict = {}
    for t in closed:
        d = t["direction"]
        if d not in by_direction:
            by_direction[d] = {"n": 0, "wins": 0, "pnl": 0.0}
        by_direction[d]["n"]    += 1
        by_direction[d]["pnl"]  += t["pnl_usd"]
        if t["pnl_usd"] >= 0:
            by_direction[d]["wins"] += 1
    for d in by_direction:
        v = by_direction[d]
        v["win_rate"] = round(v["wins"] / v["n"] * 100, 1)
        v["pnl"]      = round(v["pnl"], 2)

    return {
        "total":        len(trades),
        "closed":       len(closed),
        "open":         len(open_tr),
        "win_rate":     round(len(wins) / len(closed) * 100, 1),
        "avg_pnl":      round(sum(pnls) / len(pnls), 2),
        "total_pnl":    round(sum(pnls), 2),
        "by_symbol":    by_symbol,
        "by_direction": by_direction,
    }


def format_similar_stats(stats: Optional[dict], direction: str) -> str:
    """
    Formatta le statistiche query_similar per un alert Telegram.
    Ritorna stringa vuota se stats è None.
    """
    if not stats:
        return ""
    wr    = stats["win_rate"]
    n     = stats["n_trades"]
    avg   = stats["avg_pnl"]
    emoji = "🟢" if wr >= 55 else ("🟡" if wr >= 45 else "🔴")
    dir_str = direction.upper()
    avg_str = f"{avg:+.2f}$" if avg is not None else "n/d"
    return (
        f"\n{emoji} <b>Storico {dir_str}:</b> {n} trade, "
        f"win {wr}%, avg {avg_str}"
    )
