#!/usr/bin/env python3
"""
fill_candles.py — Riempie i buchi nelle candele a 15 minuti.

Legge la configurazione da opentradenet.env (o file specificato come argomento).
Per ogni simbolo in data/candles/, controlla il CSV e recupera le candele mancanti
da Hyperliquid fino a MAX_DAYS giorni fa.

Uso:
    python3 fill_candles.py                        # usa opentradenet.env
    python3 fill_candles.py altro.env              # usa file env specificato
    python3 fill_candles.py --days 30              # recupera fino a 30 giorni fa
    python3 fill_candles.py --symbol BTC           # solo un simbolo
    python3 fill_candles.py --dry-run              # mostra buchi senza scrivere

Non modifica mai il bot né i file esistenti: aggiunge solo righe mancanti.
"""

import argparse
import csv
import os
import sys
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

def load_config(env_file: str) -> dict:
    env_path = Path(env_file) if Path(env_file).is_absolute() else Path('.') / env_file
    if not env_path.exists():
        print(f"❌ File di configurazione non trovato: {env_file}")
        sys.exit(1)
    load_dotenv(dotenv_path=env_path)

    candles_dir  = Path(os.getenv('CANDLES_DIR',  'data/candles'))
    spike_extra  = [s.strip().upper() for s in os.getenv('SPIKE_EXTRA_SYMBOLS', 'BTC,SOL,ETH,XRP,SUI,HYPE').split(',') if s.strip()]
    supported_dexs = [d.strip() for d in os.getenv('SUPPORTED_DEXS', 'xyz').split(',') if d.strip()]
    api_url      = 'https://api.hyperliquid.xyz/info'

    return {
        'candles_dir':    candles_dir,
        'spike_extra':    spike_extra,
        'supported_dexs': supported_dexs,
        'api_url':        api_url,
    }


# ---------------------------------------------------------------------------
# Fetch simboli dal dex (stesso meccanismo del bot)
# ---------------------------------------------------------------------------

def get_xyz_symbols(dex: str, api_url: str) -> set:
    """Recupera tutti i simboli disponibili su un dex xyz."""
    try:
        resp = requests.post(api_url, json={'type': 'allMids', 'dex': dex}, timeout=10)
        data = resp.json()
        return {k.split(':', 1)[1] for k in data if ':' in k}
    except Exception as e:
        print(f"  ⚠️  Errore fetch simboli {dex}: {e}")
        return set()


def get_all_symbols(cfg: dict) -> dict:
    """
    Ritorna {simbolo: dex} per tutti i simboli da monitorare.
    Combina i simboli xyz dall'API + SPIKE_EXTRA_SYMBOLS dai perp standard.
    """
    symbols = {}

    # Perp standard (SPIKE_EXTRA_SYMBOLS)
    for sym in cfg['spike_extra']:
        symbols[sym] = ''  # dex vuoto = perp standard

    # Simboli xyz da ogni dex supportato
    for dex in cfg['supported_dexs']:
        xyz = get_xyz_symbols(dex, cfg['api_url'])
        for sym in xyz:
            symbols[sym] = dex  # il dex xyz sovrascrive se già presente

    return symbols


# ---------------------------------------------------------------------------
# Analisi buchi
# ---------------------------------------------------------------------------

INTERVAL_MINUTES = 15


def read_existing_timestamps(csv_path: Path) -> set:
    """Legge tutti i timestamp già presenti nel CSV."""
    existing = set()
    if not csv_path.exists():
        return existing
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            next(reader, None)  # header
            for row in reader:
                if row:
                    existing.add(row[0])
    except Exception as e:
        print(f"  ⚠️  Errore lettura {csv_path}: {e}")
    return existing


def find_gaps(existing_ts: set, since: datetime, until: datetime) -> list:
    """
    Trova i timestamp mancanti tra 'since' e 'until' a intervalli di 15 minuti.
    Ritorna lista di datetime.
    """
    # Allinea 'since' al quarto d'ora precedente
    since_aligned = since.replace(
        minute=(since.minute // 15) * 15,
        second=0, microsecond=0
    )
    missing = []
    current = since_aligned
    while current < until:
        ts_str = current.strftime('%Y-%m-%d %H:%M:%S')
        if ts_str not in existing_ts:
            missing.append(current)
        current += timedelta(minutes=INTERVAL_MINUTES)
    return missing


def gaps_to_ranges(missing: list, merge_minutes: int = 60) -> list:
    """
    Raggruppa i timestamp mancanti in range (start, end) per minimizzare
    le chiamate API. Range contigui distanti meno di merge_minutes vengono uniti.
    """
    if not missing:
        return []

    ranges = []
    start  = missing[0]
    end    = missing[0]

    for ts in missing[1:]:
        if (ts - end).total_seconds() / 60 <= merge_minutes:
            end = ts
        else:
            ranges.append((start, end + timedelta(minutes=INTERVAL_MINUTES)))
            start = ts
            end   = ts

    ranges.append((start, end + timedelta(minutes=INTERVAL_MINUTES)))
    return ranges


# ---------------------------------------------------------------------------
# Fetch candele da Hyperliquid
# ---------------------------------------------------------------------------

def fetch_candles(symbol: str, dex: str, start_dt: datetime, end_dt: datetime, api_url: str) -> list:
    """
    Fetcha candele a 15m per un simbolo tra start_dt e end_dt.
    Ritorna lista di dict: {timestamp, open, high, low, close, volume}
    """
    internal_name = f"{dex}:{symbol}" if dex else symbol
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin":      internal_name,
            "interval":  "15m",
            "startTime": start_ms,
            "endTime":   end_ms,
        }
    }
    if dex:
        payload["dex"] = dex

    resp = requests.post(api_url, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, list):
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


# ---------------------------------------------------------------------------
# Scrittura CSV
# ---------------------------------------------------------------------------

def write_candles(csv_path: Path, candles: list, existing_ts: set) -> int:
    """
    Scrive le candele nel CSV.
    Le righe il cui timestamp NON è in existing_ts vengono sovrascritte/aggiunte
    (consente il re-fetch forzato tramite --since rimuovendo ts da existing_ts).
    Mantiene l'ordine cronologico. Ritorna il numero di righe scritte/aggiornate.
    """
    if not candles:
        return 0

    csv_path.parent.mkdir(parents=True, exist_ok=True)

    # Legge le righe esistenti tenendo solo quelle ancora in existing_ts
    # (quelle escluse da --since vengono scartate e riscritte con i dati API)
    updated_rows: dict = {}
    if csv_path.exists():
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                next(reader, None)  # skip header
                for row in reader:
                    if row and row[0] in existing_ts:
                        updated_rows[row[0]] = row
        except Exception:
            pass

    written = 0
    for c in candles:
        updated_rows[c['timestamp']] = [
            c['timestamp'], c['open'], c['high'], c['low'], c['close'], c['volume']
        ]
        if c['timestamp'] not in existing_ts:
            existing_ts.add(c['timestamp'])
        written += 1

    if not written:
        return 0

    all_rows = sorted(updated_rows.values(), key=lambda r: r[0])
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        writer.writerows(all_rows)

    return written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Riempie i buchi nelle candele 15m di Hyperliquid'
    )
    parser.add_argument('env_file',        nargs='?', default='opentradenet.env',
                        help='File .env di configurazione (default: opentradenet.env)')
    parser.add_argument('--days',          type=int,  default=14,
                        help='Quanti giorni di storia recuperare (default: 14)')
    parser.add_argument('--symbol',        type=str,  default=None,
                        help='Elabora solo questo simbolo (es: BTC)')
    parser.add_argument('--dry-run',       action='store_true',
                        help='Mostra solo i buchi senza scrivere nulla')
    parser.add_argument('--pause',         type=float, default=0.5,
                        help='Pausa in secondi tra chiamate API (default: 0.5)')
    parser.add_argument('--since',         type=str,   default=None,
                        help='Riscrivi tutte le candele da questa data (YYYY-MM-DD). '
                             'Forza il re-fetch sovrascrivendo i dati già presenti.')
    args = parser.parse_args()

    print(f"{'='*60}")
    print(f"  fill_candles.py — Hyperliquid candle gap filler")
    print(f"{'='*60}")
    print(f"  Config:  {args.env_file}")
    print(f"  Storia:  {args.days} giorni")
    print(f"  Since:   {args.since or '(non impostato)'}")
    print(f"  Dry-run: {'sì' if args.dry_run else 'no'}")
    print()

    cfg   = load_config(args.env_file)
    until = datetime.utcnow()

    # --since sovrascrive --days per il punto di inizio
    if args.since:
        try:
            since = datetime.strptime(args.since, '%Y-%m-%d')
        except ValueError:
            print(f"❌ Formato --since non valido: '{args.since}' (atteso YYYY-MM-DD)")
            sys.exit(1)
    else:
        since = datetime.utcnow() - timedelta(days=args.days)

    # Ricava simboli
    print("📡 Recupero lista simboli...")
    all_symbols = get_all_symbols(cfg)

    if args.symbol:
        sym_upper = args.symbol.upper()
        if sym_upper not in all_symbols:
            # Prova a indovinare il dex
            print(f"  ⚠️  {sym_upper} non trovato nell'universo, provo con dex vuoto")
            all_symbols = {sym_upper: ''}
        else:
            all_symbols = {sym_upper: all_symbols[sym_upper]}

    print(f"  Trovati {len(all_symbols)} simboli da controllare\n")

    total_gaps    = 0
    total_written = 0
    total_errors  = 0

    for sym, dex in sorted(all_symbols.items()):
        market_s = f"[{dex.upper()}]" if dex else "[PERP]"
        csv_path = cfg['candles_dir'] / sym / f"{sym}_15m.csv"

        existing_ts = read_existing_timestamps(csv_path)

        # --since: rimuovi da existing_ts i timestamp >= since
        # così find_gaps li considera buchi e write_candles li sovrascrive
        if args.since:
            since_str   = since.strftime('%Y-%m-%d %H:%M:%S')
            existing_ts = {ts for ts in existing_ts if ts < since_str}

        missing     = find_gaps(existing_ts, since, until)

        if not missing:
            print(f"  ✅ {sym:12s} {market_s:8s} — nessun buco")
            continue

        ranges = gaps_to_ranges(missing)
        n_missing = len(missing)
        total_gaps += n_missing

        print(f"  🔍 {sym:12s} {market_s:8s} — {n_missing} candele mancanti "
              f"in {len(ranges)} range")

        if args.dry_run:
            for start, end in ranges:
                print(f"     {start.strftime('%Y-%m-%d %H:%M')} → "
                      f"{end.strftime('%Y-%m-%d %H:%M')}")
            continue

        # Fetch e scrittura per ogni range
        sym_written = 0
        for start, end in ranges:
            try:
                candles = fetch_candles(sym, dex, start, end, cfg['api_url'])
                n = write_candles(csv_path, candles, existing_ts)
                sym_written += n
                time.sleep(args.pause)
            except Exception as e:
                print(f"     ❌ Errore range {start} → {end}: {e}")
                total_errors += 1
                time.sleep(args.pause)

        total_written += sym_written
        print(f"     ✍️  Scritte {sym_written} candele → {csv_path}")

    print()
    print(f"{'='*60}")
    if args.dry_run:
        print(f"  [DRY RUN] Buchi trovati: {total_gaps} candele")
    else:
        print(f"  Candele mancanti trovate: {total_gaps}")
        print(f"  Candele scritte:          {total_written}")
        if total_errors:
            print(f"  Errori:                   {total_errors}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
