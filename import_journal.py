"""
import_journal.py — Importa trade history CSV Hyperliquid in ml_journal
=======================================================================
Uso:
    python3 import_journal.py trade_history.csv [--chat-id 123456789] [--dry-run]

Logica di matching Open → Close
--------------------------------
- Mantiene uno stack per simbolo+direzione
- Due Open prima di una Close: la Close viene splittata proporzionalmente
  e genera un record per ogni Open, con P&L ripartito in proporzione al ntl
- Skippati: Buy/Sell spot (/USDC), Auto-Deleveraging, Liquidation
- Simboli normalizzati: "SILVER (xyz)" → "SILVER", dex="xyz"

Deduplicazione
--------------
Controlla il journal esistente: skippato qualsiasi trade con stesso
symbol + direction + entry_time già presente.

Feature tecniche
----------------
Per ogni trade importato, prova a caricare le candele 15m locali e
calcola le feature al momento dell'entry (stesso algoritmo di ml_journal).
Se le candele non coprono quel periodo → features = {}.
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configurazione paths (allineati con il bot)
# ---------------------------------------------------------------------------

BASE_DIR     = Path(__file__).parent
CANDLES_DIR  = BASE_DIR / "data" / "candles"
JOURNAL_FILE = BASE_DIR / "data" / "journal" / "trades.json"

# ---------------------------------------------------------------------------
# Normalizzazione simboli
# ---------------------------------------------------------------------------

def _parse_symbol(raw: str):
    """
    "SILVER (xyz)" → ("SILVER", "xyz")
    "GOLD"         → ("GOLD",   "")
    "FARTCOIN/USDC"→ None  (spot, skip)
    """
    raw = raw.strip()
    if '/USDC' in raw or '/USD' in raw:
        return None, None   # spot → skip
    if raw.endswith(')') and '(' in raw:
        sym = raw[:raw.index('(')].strip()
        dex = raw[raw.index('(')+1:raw.index(')')].strip().lower()
        return sym, dex
    return raw, ""


def _parse_dir(raw: str):
    """
    "Open Long"  → ("open",  "long")
    "Close Long" → ("close", "long")
    "Open Short" → ("open",  "short")
    "Close Short"→ ("close", "short")
    Liquidation  → ("close", direzione dedotta dal testo)
    None se riga da skippare.
    """
    raw = raw.strip()
    rl  = raw.lower()

    # Skip espliciti
    skip_keywords = ['Buy', 'Sell', 'Auto-Deleveraging']
    for kw in skip_keywords:
        if kw.lower() in rl:
            return None, None

    # Liquidation: trattata come close forzata
    if 'liquidation' in rl:
        if 'long' in rl:
            return 'close', 'long'
        if 'short' in rl:
            return 'close', 'short'
        # direzione non specificata nel testo → close generica, gestiamo sotto
        return 'close', None

    if raw.startswith('Open Long'):
        return 'open', 'long'
    if raw.startswith('Open Short'):
        return 'open', 'short'
    if raw.startswith('Close Long'):
        return 'close', 'long'
    if raw.startswith('Close Short'):
        return 'close', 'short'
    return None, None


def _parse_time(raw: str) -> Optional[datetime]:
    """'04/09/2025 - 19:16:23' → datetime"""
    try:
        return datetime.strptime(raw.strip(), "%d/%m/%Y - %H:%M:%S")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Feature tecniche (replica leggera di ml_journal._compute_features)
# ---------------------------------------------------------------------------

def _compute_features(symbol: str, entry_time: datetime, candles_dir: Path) -> dict:
    """
    Carica le candele 15m locali e calcola le feature alla candela
    più vicina a entry_time (e le 24 precedenti).
    """
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
        rows = []
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for r in reader:
                try:
                    ts = datetime.strptime(r['timestamp'], '%Y-%m-%d %H:%M:%S')
                    rows.append((ts, r))
                except Exception:
                    continue

        if not rows:
            return {}

        rows.sort(key=lambda x: x[0])

        # Trova l'indice della candela più vicina a entry_time (senza superarla)
        idx = None
        for i, (ts, _) in enumerate(rows):
            if ts <= entry_time:
                idx = i
            else:
                break

        if idx is None:
            return {}

        # Prendi le ultime 25 candele fino a idx
        window = rows[max(0, idx - 24): idx + 1]
        if len(window) < 10:
            return {}

        import numpy as np

        closes  = np.array([float(r['close'])  for _, r in window])
        highs   = np.array([float(r['high'])   for _, r in window])
        lows    = np.array([float(r['low'])    for _, r in window])
        volumes = np.array([float(r['volume']) for _, r in window])

        # RSI(14)
        def _rsi(c, period=14):
            if len(c) < period + 1: return 50.0
            deltas = np.diff(c)
            gains  = np.where(deltas > 0, deltas, 0.0)
            losses = np.where(deltas < 0, -deltas, 0.0)
            ag = gains[-period:].mean()
            al = losses[-period:].mean()
            if al == 0: return 100.0
            return round(100 - 100 / (1 + ag / al), 1)

        rsi = _rsi(closes)

        # EMA trend
        def _ema(c, n):
            k = 2 / (n + 1)
            e = c[0]
            for v in c[1:]: e = v * k + e * (1 - k)
            return e

        ema9  = _ema(closes, 9)
        ema21 = _ema(closes, 21)
        last  = closes[-1]
        if last > ema9 > ema21:   ema_trend = 1
        elif last < ema9 < ema21: ema_trend = -1
        else:                     ema_trend = 0

        # Bollinger position
        p   = min(20, len(closes))
        ma  = closes[-p:].mean()
        std = closes[-p:].std()
        bb_position = round(float((last - (ma - 2*std)) / (4*std)), 3) if std > 0 else 0.5
        bb_position = max(0.0, min(1.0, bb_position))

        # ATR%
        ap  = min(14, len(closes) - 1)
        trs = [max(highs[-i] - lows[-i],
                   abs(highs[-i] - closes[-i-1]),
                   abs(lows[-i]  - closes[-i-1])) for i in range(1, ap+1)]
        atr_pct = round(float(np.mean(trs)) / last * 100, 3) if last > 0 else 0.0

        # Volume ratio
        va  = volumes[-20:].mean() if len(volumes) >= 20 else volumes.mean()
        vol_ratio = round(float(volumes[-1] / va), 2) if va > 0 else 1.0

        return {
            'rsi':          rsi,
            'ema_trend':    ema_trend,
            'bb_position':  bb_position,
            'atr_pct':      atr_pct,
            'volume_ratio': vol_ratio,
        }

    except Exception as e:
        print(f"  ⚠️  Feature {symbol} @ {entry_time}: {e}")
        return {}


# ---------------------------------------------------------------------------
# Journal I/O
# ---------------------------------------------------------------------------

def _load_journal() -> list:
    if not JOURNAL_FILE.exists():
        return []
    try:
        with open(JOURNAL_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_journal(trades: list):
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOURNAL_FILE.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(trades, f, indent=2, ensure_ascii=False)
    tmp.replace(JOURNAL_FILE)


def _make_id(symbol: str, entry_time: datetime) -> str:
    import random, string
    ts  = entry_time.strftime("%Y%m%d_%H%M%S")
    rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{symbol}_{ts}_{rnd}"


def _existing_keys(trades: list) -> set:
    """
    Ritorna un set di (symbol, direction, entry_time_str) già nel journal.
    Usato per deduplicazione.
    """
    keys = set()
    for t in trades:
        keys.add((t['symbol'], t['direction'], t['entry_time']))
    return keys


# ---------------------------------------------------------------------------
# Parsing CSV → lista trade completi
# ---------------------------------------------------------------------------

def parse_csv(csv_path: Path, chat_id: Optional[int]) -> list:
    """
    Legge il CSV e restituisce lista di record pronti per il journal.
    Gestisce: doppio open + singola close (P&L ripartito proporzionalmente).
    """
    rows = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    # Stack per simbolo+direzione: lista di open non ancora matchati
    # key: (symbol, direction)  value: lista di dict open
    stacks: dict = {}

    completed = []   # trade completati pronti per il journal
    skipped   = 0

    for r in rows:
        raw_sym = r['coin']
        raw_dir = r['dir']

        symbol, dex = _parse_symbol(raw_sym)
        if symbol is None:
            skipped += 1
            continue

        action, direction = _parse_dir(raw_dir)
        if action is None:
            skipped += 1
            continue

        ts = _parse_time(r['time'])
        if ts is None:
            skipped += 1
            continue

        try:
            px  = float(r['px'])
            sz  = float(r['sz'])
            ntl = float(r['ntl'])
            pnl = float(r['closedPnl'])
        except Exception:
            skipped += 1
            continue

        key = (symbol, direction)

        if action == 'open':
            stacks.setdefault(key, []).append({
                'symbol':     symbol,
                'dex':        dex,
                'direction':  direction,
                'entry_time': ts,
                'entry_price': px,
                'size_usd':   ntl,
                'sz':         sz,
            })

        elif action == 'close':
            # Se direction è None (liquidation senza long/short nel testo),
            # cerca quale stack ha open per questo simbolo
            if direction is None:
                found_dir = None
                for d in ('long', 'short'):
                    if stacks.get((symbol, d)):
                        found_dir = d
                        break
                if found_dir is None:
                    print(f"  ⚠️  Close senza open: {symbol} (dir sconosciuta) @ {ts} — skip")
                    skipped += 1
                    continue
                direction = found_dir

            key   = (symbol, direction)
            stack = stacks.get(key, [])

            if not stack:
                print(f"  ⚠️  Close senza open: {symbol} {direction} @ {ts} — skip")
                skipped += 1
                continue

            close_pnl = pnl

            # Calcola peso proporzionale di ogni open rispetto al totale ntl in stack
            total_open_ntl = sum(o['size_usd'] for o in stack)

            for o in stack:
                weight   = o['size_usd'] / total_open_ntl if total_open_ntl else 1.0
                pnl_part = round(close_pnl * weight, 6)
                pnl_pct  = round(pnl_part / o['size_usd'] * 100, 2) if o['size_usd'] else None

                completed.append({
                    'symbol':      symbol,
                    'dex':         dex,
                    'direction':   direction,
                    'entry_time':  o['entry_time'],
                    'entry_price': o['entry_price'],
                    'size_usd':    o['size_usd'],
                    'exit_time':   ts,
                    'exit_price':  px,
                    'pnl_usd':     pnl_part,
                    'pnl_pct':     pnl_pct,
                    'chat_id':     chat_id,
                })

            # Svuota lo stack per questo simbolo+direzione
            stacks[key] = []

    # Open rimasti senza close (posizioni ancora aperte)
    open_count = sum(len(v) for v in stacks.values() if v)
    if open_count:
        print(f"\n  ℹ️  {open_count} open senza close (posizioni ancora aperte) — non importate")

    print(f"  ℹ️  {skipped} righe skippate (spot, liquidation senza match, auto-delev, parse error)")

    # Sort per entry_time cronologico
    completed.sort(key=lambda t: t['entry_time'])
    return completed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Importa trade history CSV in ml_journal')
    parser.add_argument('csv_file', help='Path al CSV esportato da Hyperliquid')
    parser.add_argument('--chat-id', type=int, default=None, help='Il tuo Telegram chat_id')
    parser.add_argument('--candles-dir', type=str, default=str(CANDLES_DIR),
                        help=f'Directory candele 15m (default: {CANDLES_DIR})')
    parser.add_argument('--dry-run', action='store_true',
                        help='Mostra cosa verrebbe importato senza scrivere')
    args = parser.parse_args()

    csv_path    = Path(args.csv_file)
    candles_dir = Path(args.candles_dir)

    if not csv_path.exists():
        print(f"❌ File non trovato: {csv_path}")
        sys.exit(1)

    print(f"\n📂 CSV:      {csv_path}")
    print(f"📂 Candele:  {candles_dir}")
    print(f"📓 Journal:  {JOURNAL_FILE}")
    print(f"👤 chat_id:  {args.chat_id or '(non impostato)'}")
    print(f"🔍 Dry-run:  {'SI' if args.dry_run else 'NO'}")
    print()

    # Carica journal esistente
    existing_trades = _load_journal()
    existing_keys   = _existing_keys(existing_trades)
    print(f"📖 Trade già nel journal: {len(existing_trades)}")

    # Parsa CSV
    print(f"\n🔄 Parsing CSV...")
    parsed = parse_csv(csv_path, args.chat_id)
    print(f"✅ Trade completi parsati: {len(parsed)}")

    # Costruisci record journal + deduplicazione + feature
    new_records = []
    duplicates  = 0
    feat_found  = 0
    feat_miss   = 0

    print(f"\n🔬 Elaborazione trade...")
    for t in parsed:
        entry_time_str = t['entry_time'].strftime("%Y-%m-%dT%H:%M:%S")
        key = (t['symbol'], t['direction'], entry_time_str)

        if key in existing_keys:
            duplicates += 1
            continue

        # Feature tecniche
        features = _compute_features(t['symbol'], t['entry_time'], candles_dir)
        if features:
            feat_found += 1
        else:
            feat_miss += 1

        record = {
            'id':          _make_id(t['symbol'], t['entry_time']),
            'symbol':      t['symbol'],
            'direction':   t['direction'],
            'entry_price': round(t['entry_price'], 8),
            'entry_time':  entry_time_str,
            'size_usd':    round(t['size_usd'], 2),
            'ml_score':    None,
            'ml_signal':   None,
            'features':    features,
            'exit_price':  round(t['exit_price'], 8),
            'exit_time':   t['exit_time'].strftime("%Y-%m-%dT%H:%M:%S"),
            'pnl_usd':     t['pnl_usd'],
            'pnl_pct':     t['pnl_pct'],
            'status':      'closed',
            'chat_id':     t['chat_id'],
        }
        new_records.append(record)

    # Report
    print(f"\n{'='*50}")
    print(f"📊 RIEPILOGO IMPORT")
    print(f"{'='*50}")
    print(f"  Trade parsati dal CSV:    {len(parsed)}")
    print(f"  Duplicati (già presenti): {duplicates}")
    print(f"  Nuovi da importare:       {len(new_records)}")
    print(f"  Con feature tecniche:     {feat_found}")
    print(f"  Senza feature (candele):  {feat_miss}")

    if new_records:
        print(f"\n📋 Anteprima primi 5:")
        for r in new_records[:5]:
            pnl_s = f"{r['pnl_usd']:+.2f}$" if r['pnl_usd'] is not None else "n/d"
            feat_s = "✅" if r['features'] else "⬜"
            print(f"  {feat_s} {r['symbol']:12} {r['direction']:5}  "
                  f"entry={r['entry_price']:.4g}  "
                  f"pnl={pnl_s}  "
                  f"{'WIN' if r['pnl_usd'] and r['pnl_usd'] >= 0 else 'LOSS'}")

    if args.dry_run:
        print(f"\n⚠️  DRY-RUN: nessuna scrittura effettuata.")
        return

    if not new_records:
        print(f"\n✅ Nessun nuovo trade da importare.")
        return

    # Scrivi — ordinato per entry_time
    all_trades = existing_trades + new_records
    all_trades.sort(key=lambda t: t['entry_time'])
    _save_journal(all_trades)

    wins  = [r for r in new_records if r['pnl_usd'] is not None and r['pnl_usd'] >= 0]
    total_pnl = sum(r['pnl_usd'] for r in new_records if r['pnl_usd'] is not None)

    print(f"\n✅ Importati {len(new_records)} trade nel journal.")
    print(f"   Win rate:  {len(wins)/len(new_records)*100:.1f}%  ({len(wins)}/{len(new_records)})")
    print(f"   P&L tot:   {total_pnl:+.2f}$")
    print(f"   Journal:   {len(all_trades)} trade totali")


if __name__ == '__main__':
    main()
