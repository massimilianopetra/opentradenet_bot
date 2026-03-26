"""
ml_analyst.py — Analisi tecnica AI tramite Claude Vision API
Genera chart daily + 15m di un simbolo e li invia a Claude per analisi.
Integrato nel bot tramite comando /analyze SIMBOLO.
"""

import base64
import logging
import tempfile
import csv as _csv
from pathlib import Path
from collections import defaultdict
import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL   = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """Sei un analista tecnico esperto di crypto trading.
Ricevi due chart dello stesso simbolo: il primo è il timeframe DAILY, il secondo è il 15 MINUTI.
Analizza entrambi e rispondi SEMPRE in italiano con questo formato esatto:

📊 {SIMBOLO} — Analisi Tecnica

🔵 DAILY: [2-3 righe: trend, EMA, RSI, MACD, livelli chiave]
🟢 15m: [2-3 righe: struttura, momentum, segnali intraday]

🤖 Bias: LONG / SHORT / NEUTRO

📐 Setup suggerito:
• Entry: [prezzo o range]
• Stop: [prezzo] ([%])
• Target 1: [prezzo] ([%]) → R:R [valore]
• Target 2: [prezzo] ([%]) → R:R [valore]

⚠️ [1 riga: avvertenze principali, es. contro-trend, resistenza, ecc.]

Sii conciso e diretto. Non aggiungere testo extra fuori da questo formato."""

# ---------------------------------------------------------------------------
# Funzione principale
# ---------------------------------------------------------------------------

async def analyze_symbol(symbol, ml_score, ml_signal, candles_dir, anthropic_api_key):
    """
    Genera i chart e chiama Claude Vision per l'analisi.
    Returns: (testo_analisi, lista_path_chart_temporanei)
    """
    chart_paths = _generate_charts(symbol, candles_dir)
    if not chart_paths:
        return f"❌ Nessun dato disponibile per {symbol}", []

    images_b64 = []
    for path in chart_paths:
        try:
            with open(path, "rb") as f:
                images_b64.append(base64.standard_b64encode(f.read()).decode("utf-8"))
        except Exception as e:
            logger.error(f"Errore lettura chart {path}: {e}")

    if not images_b64:
        return "❌ Errore nella generazione dei chart", chart_paths

    content = []
    labels = ["DAILY (ricostruito da 15m, ultimi 56 giorni)", "15 MINUTI (ultime 120 candele)"]
    for i, img_b64 in enumerate(images_b64):
        content.append({"type": "text", "text": f"Chart {i+1}: {labels[i] if i < len(labels) else ''}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}})

    ml_context = ""
    if ml_score is not None and ml_signal is not None:
        ml_context = f"\n\nContesto aggiuntivo — ML Scanner: segnale={ml_signal}, score={ml_score:.2f}"
    content.append({"type": "text", "text": f"Analizza il simbolo {symbol.upper()}.{ml_context}"})

    headers = {
        "x-api-key": anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 600,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content}]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ANTHROPIC_API_URL, headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.error(f"Anthropic API error {resp.status}: {err}")
                    return f"❌ Errore API Claude ({resp.status})", chart_paths
                data = await resp.json()
                text = data.get("content", [{}])[0].get("text", "").strip()
                if not text:
                    return "❌ Risposta vuota da Claude", chart_paths
                return text, chart_paths
    except Exception as e:
        logger.error(f"Errore chiamata Anthropic API: {e}")
        return f"❌ Errore connessione API: {e}", chart_paths


# ---------------------------------------------------------------------------
# Lettura CSV e ricampionamento daily (autonomo, senza dipendenze da cc)
# ---------------------------------------------------------------------------

def _load_candles_csv(csv_path: Path) -> list:
    """
    Legge CSV candele formato: timestamp,open,high,low,close,volume
    Ritorna lista di dict con valori già convertiti in float.
    """
    candles = []
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
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
    except Exception as e:
        logger.error(f"Errore lettura CSV {csv_path}: {e}")
    return candles


def _resample_to_daily(candles_15m: list) -> list:
    """
    Ricampiona candele 15m in daily. Ritorna lista di dict.
    Il timestamp è stringa 'YYYY-MM-DD 00:00:00' (primo quarto d'ora del giorno).
    """
    buckets = defaultdict(list)
    for c in candles_15m:
        # Prende solo la parte data come chiave bucket
        day_key = c['timestamp'][:10]   # 'YYYY-MM-DD'
        buckets[day_key].append(c)

    daily = []
    for day_key in sorted(buckets.keys()):
        day_c = buckets[day_key]
        daily.append({
            'timestamp': f"{day_key} 00:00:00",
            'open':   day_c[0]['open'],
            'high':   max(c['high']   for c in day_c),
            'low':    min(c['low']    for c in day_c),
            'close':  day_c[-1]['close'],
            'volume': sum(c['volume'] for c in day_c),
        })
    return daily


def _write_temp_csv(candles: list) -> Path:
    """Scrive lista di dict candele in un CSV temporaneo, ritorna il Path."""
    tmp = tempfile.NamedTemporaryFile(
        suffix='.csv', delete=False, mode='w', newline='', encoding='utf-8'
    )
    writer = _csv.writer(tmp)
    writer.writerow(['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    for c in candles:
        writer.writerow([c['timestamp'], c['open'], c['high'],
                         c['low'], c['close'], c['volume']])
    tmp.close()
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# Generazione chart — usa cc.load_csv + cc.plot_chart con CSV temp per il daily
# ---------------------------------------------------------------------------

def _generate_charts(symbol: str, candles_dir: Path) -> list:
    import candle_chart as cc
    import matplotlib.pyplot as plt

    sym      = symbol.upper()
    csv_path = candles_dir / sym / f"{sym}_15m.csv"

    if not csv_path.exists():
        logger.warning(f"CSV non trovato: {csv_path}")
        return []

    # Carica tutte le candele 15m una volta sola
    all_15m = _load_candles_csv(csv_path)
    if len(all_15m) < 20:
        logger.warning(f"Dati insufficienti per {sym}: {len(all_15m)} candele")
        return []

    chart_paths = []

    # --- Chart 1: Daily ricostruito (ultimi 56 giorni) ---
    try:
        daily_candles = _resample_to_daily(all_15m)
        daily_slice   = daily_candles[-56:]

        tmp_csv = _write_temp_csv(daily_slice)
        try:
            data_daily = cc.load_csv(tmp_csv, bars=56, timeframe='1D')
        finally:
            tmp_csv.unlink(missing_ok=True)

        if len(data_daily.get('closes', [])) >= 5:
            tmp_png = tempfile.NamedTemporaryFile(
                suffix='.png', delete=False, prefix=f'{sym}_daily_'
            )
            tmp_png.close()
            fig = cc.plot_chart(data_daily, symbol=sym, timeframe='1D')
            fig.savefig(tmp_png.name, dpi=130, bbox_inches='tight', facecolor='#0d1117')
            plt.close(fig)
            chart_paths.append(tmp_png.name)
            logger.debug(f"Chart daily generato: {tmp_png.name}")

    except Exception as e:
        logger.error(f"Errore generazione chart daily {sym}: {e}")

    # --- Chart 2: 15m (ultime 120 candele) ---
    try:
        data_15m = cc.load_csv(csv_path, bars=120, timeframe='15m')

        if len(data_15m.get('closes', [])) >= 20:
            tmp_png = tempfile.NamedTemporaryFile(
                suffix='.png', delete=False, prefix=f'{sym}_15m_'
            )
            tmp_png.close()
            fig = cc.plot_chart(data_15m, symbol=sym, timeframe='15m')
            fig.savefig(tmp_png.name, dpi=130, bbox_inches='tight', facecolor='#0d1117')
            plt.close(fig)
            chart_paths.append(tmp_png.name)
            logger.debug(f"Chart 15m generato: {tmp_png.name}")

    except Exception as e:
        logger.error(f"Errore generazione chart 15m {sym}: {e}")

    return chart_paths


# ---------------------------------------------------------------------------
# Dryrun: chart + mega_summary locale, senza chiamata Claude API
# ---------------------------------------------------------------------------

async def analyze_symbol_dryrun(symbol: str, candles_dir) -> tuple:
    """
    Genera i due chart PNG e il mega_summary testuale locale.
    NON chiama la Claude Vision API.

    Returns:
        (summary_text: str, chart_paths: list[str])
    """
    import mega_summary as _ms

    chart_paths = _generate_charts(symbol, Path(candles_dir))
    if not chart_paths:
        return f"❌ Nessun dato disponibile per {symbol}", []

    sym = symbol.upper()
    csv_15m_path = Path(candles_dir) / sym / f"{sym}_15m.csv"

    # Ricostruisce il CSV daily temporaneo (stessa logica di _generate_charts)
    all_15m = _load_candles_csv(csv_15m_path) if csv_15m_path.exists() else []
    current_price = all_15m[-1]['close'] if all_15m else 0.0

    # Scrivi CSV daily temporaneo per mega_summary
    daily_candles = _resample_to_daily(all_15m)
    tmp_1d = _write_temp_csv(daily_candles)
    try:
        summary_text = _ms.compute_mega_summary(
            symbol       = sym,
            csv_15m_path = str(csv_15m_path),
            csv_1d_path  = str(tmp_1d),
            current_price= current_price,
        )
    except Exception as e:
        logger.error(f"analyze_symbol_dryrun mega_summary {sym}: {e}")
        summary_text = f"❌ Errore generazione summary: {e}"
    finally:
        tmp_1d.unlink(missing_ok=True)

    return summary_text, chart_paths


# ---------------------------------------------------------------------------
# Cleanup helper
# ---------------------------------------------------------------------------

def cleanup_charts(paths) -> None:
    """Elimina i file temporanei dei chart."""
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass
