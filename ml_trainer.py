#!/usr/bin/env python3
"""
ml_trainer.py — Training XGBoost per OpenTradeNet ML
=====================================================

Legge i dataset CSV prodotti da ml_features.py (ml_reports/SIMBOLO_dataset.csv)
e allena un modello XGBoost per ogni simbolo. Salva i modelli in models/.

USO
---
  # Allena tutti i simboli disponibili
  python3 ml_trainer.py

  # Solo un simbolo
  python3 ml_trainer.py --symbol BTC

  # Cartelle personalizzate
  python3 ml_trainer.py --dataset-dir ml_reports --model-dir models

  # Output minimale (solo tabella finale)
  python3 ml_trainer.py --quiet

  # Non salvare i modelli (dry-run per vedere le metriche)
  python3 ml_trainer.py --dry-run

REQUISITI
---------
  pip install xgboost --break-system-packages

OUTPUT
------
  - models/SIMBOLO.json        — modello XGBoost serializzato
  - models/SIMBOLO_meta.json   — metadati: metriche, feature importance, soglia usata
  - models/training_report.txt — report riepilogativo di tutti i simboli

STRUTTURA TRAINING
------------------
  Dataset ordinato cronologicamente:
  |←────── training (80%) ──────→|←── test (20%) ──→|
  Le ultime 20% candele sono sempre il test set —
  mai mescolate col training (walk-forward split).

  Il modello classifica 3 classi: +1 (LONG), -1 (SHORT), 0 (NEUTRO)
  
  I NEUTRO vengono inclusi nel training (il modello impara anche a
  "stare fermo") ma le metriche principali sono precision/recall su
  LONG e SHORT — le classi che generano i segnali di trading.
"""

import os
import sys
import csv
import json
import math
import argparse
import time
from pathlib import Path
from datetime import datetime

try:
    import xgboost as xgb
except ImportError:
    print("❌ XGBoost non installato. Esegui:")
    print("   pip install xgboost --break-system-packages")
    sys.exit(1)

# ---------------------------------------------------------------------------
# JSON encoder — gestisce float32/int32 di numpy/xgboost
# ---------------------------------------------------------------------------

class SafeJSONEncoder(json.JSONEncoder):
    """Converte tipi numpy in tipi Python nativi per json.dump."""
    def default(self, obj):
        if hasattr(obj, 'item'):    # numpy scalar (float32, int32, ...)
            return obj.item()
        if hasattr(obj, 'tolist'): # numpy array
            return obj.tolist()
        return super().default(obj)

# ---------------------------------------------------------------------------
# Configurazione default
# ---------------------------------------------------------------------------

DEFAULT_DATASET_DIR = 'ml_reports'
DEFAULT_MODEL_DIR   = 'models'
MIN_SAMPLES         = 100    # campioni minimi per tentare il training
WARN_ACCURACY       = 0.55   # soglia sotto cui segnalare il modello come debole
TEST_SPLIT          = 0.20   # percentuale del dataset per il test set

# Parametri XGBoost — ottimizzati per ALTA PRECISIONE, pochi segnali.
# Filosofia: meglio sbagliare per difetto (non segnalare) che per eccesso
# (segnalare operazioni sbagliate). Regolarizzazione forte, alberi profondi
# ma con split difficili da fare.
XGBOOST_PARAMS = {
    'max_depth':        3,       # alberi meno profondi → meno overfitting
    'n_estimators':     400,     # più alberi con learning rate basso
    'learning_rate':    0.03,    # apprendimento lento → generalizza meglio
    'subsample':        0.7,     # campiona 70% dei dati per albero
    'colsample_bytree': 0.6,     # campiona 60% delle feature per albero
    'min_child_weight': 20,      # nodo richiede molti campioni → no overfitting
    'gamma':            0.5,     # split solo se guadagno significativo
    'reg_alpha':        0.5,     # L1 forte — azzera le feature irrilevanti
    'reg_lambda':       3.0,     # L2 forte
    'use_label_encoder': False,
    'eval_metric':      'mlogloss',
    'verbosity':        0,
    'random_state':     42,
}

# ---------------------------------------------------------------------------
# 1. LETTURA DATASET CSV
# ---------------------------------------------------------------------------

# Colonne da escludere dalle feature (metadati e target)
NON_FEATURE_COLS = {'timestamp', 'close', 'future_return_pct', 'label'}

def load_dataset(csv_path: Path) -> tuple:
    """
    Legge il dataset CSV prodotto da ml_features.py.

    Restituisce:
        (X, y, feature_names, timestamps)
        X              : lista di liste di float (campioni × feature)
        y              : lista di int (-1, 0, +1)
        feature_names  : lista dei nomi delle colonne feature
        timestamps     : lista di stringhe timestamp (per riferimento)
    """
    rows = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    if not rows:
        raise ValueError(f"Dataset vuoto: {csv_path}")

    all_cols      = list(rows[0].keys())
    feature_names = [c for c in all_cols if c not in NON_FEATURE_COLS]

    X, y, timestamps = [], [], []
    skipped = 0

    for row in rows:
        try:
            features = [float(row[c]) for c in feature_names]
            label    = int(float(row['label']))
            ts       = row.get('timestamp', '')
            X.append(features)
            y.append(label)
            timestamps.append(ts)
        except (ValueError, KeyError):
            skipped += 1
            continue

    if skipped > 0:
        print(f"    ⚠️  {skipped} righe malformate skippate")

    return X, y, feature_names, timestamps


# ---------------------------------------------------------------------------
# 2. WALK-FORWARD SPLIT
# ---------------------------------------------------------------------------

def train_test_split_temporal(X, y, test_ratio=TEST_SPLIT):
    """
    Split cronologico — NON randomico.
    Le ultime test_ratio% osservazioni vanno sempre nel test set.
    
    In finanza è fondamentale: non si può "sbirciare il futuro" durante
    il training. Un campione di gennaio non può essere nel test se
    stiamo allenando su febbraio.
    """
    n          = len(X)
    split_idx  = int(n * (1 - test_ratio))
    split_idx  = max(split_idx, MIN_SAMPLES)  # garantisce almeno MIN_SAMPLES in train

    X_train = X[:split_idx]
    y_train = y[:split_idx]
    X_test  = X[split_idx:]
    y_test  = y[split_idx:]

    return X_train, X_test, y_train, y_test, split_idx


# ---------------------------------------------------------------------------
# 3. TRAINING
# ---------------------------------------------------------------------------

def train_model(X_train, y_train):
    """
    Allena il modello XGBoost.
    
    Le label sono -1, 0, +1 ma XGBoost richiede classi 0-based.
    Facciamo la mappatura: -1→0, 0→1, +1→2
    e la invertiamo alla predizione.
    
    Restituisce il modello e il mapping delle classi.
    """
    # Mappa -1→0, 0→1, +1→2
    label_map     = {-1: 0, 0: 1, 1: 2}
    label_map_inv = {0: -1, 1: 0, 2: 1}
    
    y_mapped = [label_map[lbl] for lbl in y_train]

    model = xgb.XGBClassifier(
        objective='multi:softprob',
        num_class=3,
        **XGBOOST_PARAMS
    )
    model.fit(X_train, y_mapped)

    return model, label_map_inv


# ---------------------------------------------------------------------------
# 4. VALUTAZIONE
# ---------------------------------------------------------------------------

def evaluate_model(model, X_test, y_test, label_map_inv):
    """
    Calcola le metriche di classificazione sul test set.
    
    Metriche calcolate:
      - accuracy globale
      - per ogni classe (LONG, SHORT, NEUTRO):
          precision = veri positivi / (veri + falsi positivi)
          recall    = veri positivi / (veri positivi + falsi negativi)
          f1        = media armonica precision e recall
      - confidence media delle predizioni corrette
    
    Restituisce dict con tutte le metriche.
    """
    # Predici classi (0,1,2) e rimappa a (-1,0,+1)
    y_pred_mapped = model.predict(X_test)
    y_pred        = [label_map_inv[p] for p in y_pred_mapped]

    n = len(y_test)
    if n == 0:
        return {}

    # Accuracy globale
    correct  = sum(1 for a, b in zip(y_test, y_pred) if a == b)
    accuracy = correct / n

    # Precision, Recall, F1 per classe
    classes = [-1, 0, 1]
    metrics = {}
    for cls in classes:
        tp = sum(1 for a, b in zip(y_test, y_pred) if a == cls and b == cls)
        fp = sum(1 for a, b in zip(y_test, y_pred) if a != cls and b == cls)
        fn = sum(1 for a, b in zip(y_test, y_pred) if a == cls and b != cls)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1        = (2 * precision * recall / (precision + recall)
                     if (precision + recall) > 0 else 0.0)
        support   = sum(1 for a in y_test if a == cls)

        metrics[cls] = {
            'precision': round(precision, 4),
            'recall':    round(recall,    4),
            'f1':        round(f1,        4),
            'support':   support,
        }

    # Confidence media (probabilità della classe predetta)
    y_proba       = model.predict_proba(X_test)
    pred_classes  = model.predict(X_test)
    confidences   = [y_proba[i][pred_classes[i]] for i in range(len(y_test))]
    mean_conf     = sum(confidences) / len(confidences) if confidences else 0.0

    return {
        'accuracy':       round(accuracy, 4),
        'mean_confidence': round(mean_conf, 4),
        'n_test':         n,
        'per_class':      metrics,
    }


# ---------------------------------------------------------------------------
# 5. FEATURE IMPORTANCE
# ---------------------------------------------------------------------------

def get_feature_importance(model, feature_names: list) -> list:
    """
    Estrae la feature importance dal modello XGBoost.
    Usa 'gain' — la riduzione media dell'errore quando quella feature
    viene usata per uno split. È più informativa di 'weight' (numero di split).
    
    Restituisce lista di (feature_name, importance) ordinata per importanza.
    """
    scores = model.get_booster().get_score(importance_type='gain')
    # XGBoost usa nomi f0, f1, ... se non specificati — mappiamo agli indici
    result = []
    for fname_xgb, score in scores.items():
        # fname_xgb è tipo 'f23' → indice 23
        try:
            idx = int(fname_xgb[1:])
            if idx < len(feature_names):
                result.append((feature_names[idx], round(score, 4)))
        except ValueError:
            pass

    result.sort(key=lambda x: x[1], reverse=True)
    return result


# ---------------------------------------------------------------------------
# 6. SALVATAGGIO MODELLO
# ---------------------------------------------------------------------------

def save_model(model, feature_names: list, metrics: dict,
               feature_importance: list, symbol: str,
               model_dir: Path, horizon: int = None, threshold: float = None):
    """
    Salva:
      - models/SIMBOLO.json         : modello XGBoost serializzato
      - models/SIMBOLO_meta.json    : metadati completi
    
    I metadati contengono tutto il necessario per usare il modello
    in ml_scanner.py senza riaprire il dataset:
      - lista feature nell'ordine corretto
      - metriche di performance
      - feature importance top-20
      - timestamp training
      - warning se accuracy bassa
    """
    model_dir.mkdir(parents=True, exist_ok=True)

    # Salva modello
    model_path = model_dir / f"{symbol}.json"
    model.save_model(str(model_path))

    # Metadati
    is_weak = metrics.get('accuracy', 0) < WARN_ACCURACY
    meta = {
        'symbol':            symbol,
        'trained_at':        datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'),
        'feature_names':     feature_names,
        'n_features':        len(feature_names),
        'horizon':           horizon,
        'threshold':         threshold,
        'metrics':           metrics,
        'feature_importance': feature_importance[:20],  # top 20
        'weak_model':        is_weak,
        'xgboost_params':    {k: v for k, v in XGBOOST_PARAMS.items()
                              if k not in ('use_label_encoder', 'eval_metric', 'verbosity')},
    }
    meta_path = model_dir / f"{symbol}_meta.json"
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2, cls=SafeJSONEncoder)

    return model_path, meta_path


# ---------------------------------------------------------------------------
# 7. STAMPA RISULTATI
# ---------------------------------------------------------------------------

def print_results(symbol: str, n_train: int, n_test: int,
                  metrics: dict, feature_importance: list, quiet: bool):
    """Stampa i risultati del training per un simbolo."""
    if quiet:
        return

    acc   = metrics.get('accuracy', 0)
    conf  = metrics.get('mean_confidence', 0)
    pc    = metrics.get('per_class', {})
    weak  = acc < WARN_ACCURACY

    warn_str = '  ⚠️  MODELLO DEBOLE' if weak else ''

    print(f"\n  {'═'*56}")
    print(f"  {symbol}  —  train: {n_train}  |  test: {n_test}{warn_str}")
    print(f"  {'═'*56}")
    print(f"  Accuracy globale:   {acc*100:.1f}%   "
          f"Confidence media: {conf*100:.1f}%")
    print()
    print(f"  {'Classe':<10} {'Precision':>10} {'Recall':>8} {'F1':>8} {'Campioni':>10}")
    print(f"  {'-'*50}")
    for cls, lbl in [(-1, 'SHORT'), (0, 'NEUTRO'), (1, 'LONG')]:
        m = pc.get(cls, {})
        print(f"  {lbl:<10} {m.get('precision',0)*100:>9.1f}% "
              f"{m.get('recall',0)*100:>7.1f}% "
              f"{m.get('f1',0)*100:>7.1f}% "
              f"{m.get('support',0):>10}")

    print(f"\n  Top 10 feature importance (gain):")
    print(f"  {'Feature':<25} {'Score':>8}  {'Bar'}")
    print(f"  {'-'*55}")
    max_score = feature_importance[0][1] if feature_importance else 1
    for fname, score in feature_importance[:10]:
        bar_len = int(score / max_score * 20)
        bar     = '█' * bar_len
        print(f"  {fname:<25} {score:>8.1f}  {bar}")


# ---------------------------------------------------------------------------
# 8. REPORT RIEPILOGATIVO
# ---------------------------------------------------------------------------

def write_training_report(results: list, output_path: Path):
    """
    Scrive un file di testo con il riepilogo di tutti i simboli allenati.
    Ordinato per accuracy decrescente — i modelli migliori in cima.
    """
    results_sorted = sorted(results, key=lambda r: r['accuracy'], reverse=True)

    lines = [
        '=' * 75,
        '  OpenTradeNet — ML Training Report',
        f'  Generato: {datetime.now().strftime("%Y-%m-%d %H:%M")}',
        '=' * 75,
        '',
        f"  {'SIMBOLO':<12} {'TRAIN':>6} {'TEST':>6} {'ACC%':>6} "
        f"{'PREC_L':>7} {'REC_L':>7} {'PREC_S':>7} {'REC_S':>7} {'NOTE'}",
        '  ' + '-' * 70,
    ]

    n_weak = 0
    for r in results_sorted:
        pc    = r.get('per_class', {})
        long  = pc.get(1,  {})
        short = pc.get(-1, {})
        weak  = r['accuracy'] < WARN_ACCURACY
        note  = '⚠ debole' if weak else ''
        if weak:
            n_weak += 1
        lines.append(
            f"  {r['symbol']:<12} {r['n_train']:>6} {r['n_test']:>6} "
            f"{r['accuracy']*100:>5.1f}% "
            f"{long.get('precision',0)*100:>6.1f}% "
            f"{long.get('recall',0)*100:>6.1f}% "
            f"{short.get('precision',0)*100:>6.1f}% "
            f"{short.get('recall',0)*100:>6.1f}% "
            f"  {note}"
        )

    lines += [
        '',
        '=' * 75,
        f"  Totale simboli:    {len(results)}",
        f"  Modelli affidabili: {len(results) - n_weak}  "
        f"({(len(results)-n_weak)/max(len(results),1)*100:.0f}%)",
        f"  Modelli deboli:    {n_weak}",
        '=' * 75,
    ]

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f"\n  📝 Report salvato: {output_path}")


# ---------------------------------------------------------------------------
# 9. MAIN
# ---------------------------------------------------------------------------

def process_symbol(sym, dataset_dir, model_dir, dry_run, quiet,
                   min_samples=MIN_SAMPLES):
    """
    Carica, allena, valuta e salva il modello per un singolo simbolo.
    """
    csv_path = dataset_dir / f"{sym}_dataset.csv"
    if not csv_path.exists():
        return None, f"{sym}: dataset non trovato ({csv_path})"

    try:
        X, y, feature_names, timestamps = load_dataset(csv_path)
        n_total = len(X)

        if n_total < min_samples:
            return None, f"{sym}: solo {n_total} campioni (minimo {min_samples})"

        # Split
        X_train, X_test, y_train, y_test, split_idx = train_test_split_temporal(X, y)
        n_train = len(X_train)
        n_test  = len(X_test)

        if n_test == 0:
            return None, f"{sym}: test set vuoto dopo lo split"

        # Training
        model, label_map_inv = train_model(X_train, y_train)

        # Valutazione
        metrics = evaluate_model(model, X_test, y_test, label_map_inv)

        # Feature importance
        fi = get_feature_importance(model, feature_names)

        # Stampa
        print_results(sym, n_train, n_test, metrics, fi, quiet)

        # Salvataggio
        if not dry_run:
            save_model(model, feature_names, metrics, fi, sym, model_dir)
            if not quiet:
                acc = metrics.get('accuracy', 0)
                weak_str = '  ⚠️  (debole)' if acc < WARN_ACCURACY else ''
                print(f"  💾 Modello salvato: models/{sym}.json{weak_str}")

        return {
            'symbol':    sym,
            'n_train':   n_train,
            'n_test':    n_test,
            'accuracy':  metrics.get('accuracy', 0),
            'per_class': metrics.get('per_class', {}),
            'top_feature': fi[0][0] if fi else '—',
        }, None
    except Exception as e:
        return None, f"{sym}: errore — {e}"


def main():
    parser = argparse.ArgumentParser(
        description='ml_trainer.py — Training XGBoost per OpenTradeNet',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  # Allena tutti i simboli (output minimale, veloce)
  python3 ml_trainer.py --quiet

  # Solo un simbolo con output dettagliato
  python3 ml_trainer.py --symbol BTC

  # Vedi le metriche senza salvare i modelli
  python3 ml_trainer.py --dry-run --quiet

  # Cambia minimo campioni richiesti
  python3 ml_trainer.py --quiet --min-samples 500
        """
    )
    parser.add_argument('--dataset-dir',  default=DEFAULT_DATASET_DIR,
                        help=f'Directory dei dataset CSV (default: {DEFAULT_DATASET_DIR})')
    parser.add_argument('--model-dir',    default=DEFAULT_MODEL_DIR,
                        help=f'Directory dove salvare i modelli (default: {DEFAULT_MODEL_DIR})')
    parser.add_argument('--symbol',       default=None,
                        help='Allena solo questo simbolo (es: BTC)')
    parser.add_argument('--dry-run',      action='store_true',
                        help='Non salvare i modelli — mostra solo le metriche')
    parser.add_argument('--quiet',        action='store_true',
                        help='Output minimale: una riga per simbolo + tabella finale')
    parser.add_argument('--min-samples',  type=int, default=MIN_SAMPLES,
                        help=f'Campioni minimi per allenare (default: {MIN_SAMPLES})')
    args = parser.parse_args()

    script_dir  = Path(__file__).parent
    dataset_dir = Path(args.dataset_dir)
    model_dir   = Path(args.model_dir)
    if not dataset_dir.is_absolute():
        dataset_dir = script_dir / dataset_dir
    if not model_dir.is_absolute():
        model_dir = script_dir / model_dir

    print(f"\n{'═'*60}")
    print(f"  ml_trainer.py — OpenTradeNet XGBoost Training")
    print(f"{'═'*60}")
    print(f"  Dataset dir  : {dataset_dir}")
    print(f"  Model dir    : {model_dir}")
    print(f"  Dry-run      : {'sì' if args.dry_run else 'no'}")
    print(f"  Min campioni : {args.min_samples}")
    print(f"  XGBoost      : {xgb.__version__}")

    if args.symbol:
        symbols = [args.symbol.upper()]
    else:
        symbols = []
        if dataset_dir.exists():
            for f in sorted(dataset_dir.iterdir()):
                if f.name.endswith('_dataset.csv'):
                    symbols.append(f.name.replace('_dataset.csv', ''))
        if not symbols:
            print(f"\n  ❌ Nessun dataset trovato in {dataset_dir}")
            print(f"     Lancia prima: python3 ml_features.py --save-csv")
            sys.exit(1)

    print(f"  Simboli      : {len(symbols)}"
          f"  ({', '.join(symbols[:6])}{'...' if len(symbols) > 6 else ''})")
    print()

    results, errors = [], []
    t0 = time.time()

    for i, sym in enumerate(symbols):
        if args.quiet and i % 10 == 0:
            elapsed = time.time() - t0
            eta     = (elapsed / (i + 1)) * (len(symbols) - i - 1) if i > 0 else 0
            print(f"  [{i+1:3d}/{len(symbols)}]  {sym:<12}  "
                  f"elapsed={elapsed:.0f}s  ETA={eta:.0f}s")
            sys.stdout.flush()
        elif not args.quiet:
            print(f"\n  🔄 {sym}  ({i+1}/{len(symbols)})")

        result, error = process_symbol(
            sym, dataset_dir, model_dir,
            args.dry_run, args.quiet,
            min_samples=args.min_samples,
        )
        if result:
            results.append(result)
        if error:
            errors.append(error)
            if not args.quiet:
                print(f"  ⚠️  {error}")

    elapsed_total = time.time() - t0

    if args.quiet and results:
        results_sorted = sorted(results, key=lambda r: r['accuracy'], reverse=True)
        print(f"\n{'─'*82}")
        print(f"  {'SIMBOLO':<12} {'TRAIN':>6} {'TEST':>6} {'ACC%':>6} "
              f"{'PREC_L':>7} {'REC_L':>6} {'PREC_S':>7} {'REC_S':>6} {'TOP FEATURE'}")
        print(f"{'─'*82}")
        for r in results_sorted:
            long  = r['per_class'].get(1,  {})
            short = r['per_class'].get(-1, {})
            weak  = '⚠' if r['accuracy'] < WARN_ACCURACY else ' '
            print(f"  {r['symbol']:<12} {r['n_train']:>6} {r['n_test']:>6} "
                  f"{r['accuracy']*100:>5.1f}%{weak} "
                  f"{long.get('precision',0)*100:>6.1f}% "
                  f"{long.get('recall',0)*100:>5.1f}% "
                  f"{short.get('precision',0)*100:>6.1f}% "
                  f"{short.get('recall',0)*100:>5.1f}% "
                  f"  {r['top_feature']}")

    if results and not args.dry_run:
        report_path = model_dir / 'training_report.txt'
        model_dir.mkdir(parents=True, exist_ok=True)
        write_training_report(results, report_path)

    n_ok   = len(results)
    n_weak = sum(1 for r in results if r['accuracy'] < WARN_ACCURACY)
    n_err  = len(errors)

    print(f"\n{'═'*60}")
    print(f"  ✅ Completato in {elapsed_total:.1f}s")
    print(f"  Modelli allenati : {n_ok}")
    if n_weak:
        print(f"  Modelli deboli   : {n_weak}  (accuracy < {WARN_ACCURACY*100:.0f}%)")
    if n_err:
        print(f"  Errori           : {n_err}")
        for e in errors[:5]:
            print(f"    ⚠️  {e}")
        if n_err > 5:
            print(f"    ... e altri {n_err - 5}")
    print(f"{'═'*60}\n")


if __name__ == '__main__':
    main()
