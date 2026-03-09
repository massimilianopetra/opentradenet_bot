"""
ml_analyst.py — Analisi tecnica AI tramite Claude Vision API
Genera chart daily + 15m di un simbolo e li invia a Claude per analisi.
Integrato nel bot tramite comando /analyze SIMBOLO.
"""

import os
import base64
import logging
import tempfile
import csv as _csv
from pathlib import Path
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

async def analyze_symbol(
    symbol: str,
    ml_score,
    ml_signal,
    candles_dir: Path,
    anthropic_api_key: str
):
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
                ANTHROPIC_API_URL,
                headers=headers,
                json=payload,
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
# Generazione chart — usa le API reali di candle_chart.py:
#   cc.load_csv(path, bars, timeframe)  → dict
#   cc.resample_rows(rows, tf)          → rows ricampionate
#   cc.plot_chart(data, symbol, timeframe) → Figure
# ---------------------------------------------------------------------------

def _generate_charts(symbol: str, candles_dir: Path):
    import candle_chart as cc
    import matplotlib.pyplot as plt

    sym      = symbol.upper()
    csv_path = candles_dir / sym / f"{sym}_15m.csv"

    if not csv_path.exists():
        logger.warning(f"CSV non trovato: {csv_path}")
        return []

    chart_paths = []

    # --- Chart 1: Daily ricostruito (ultimi 56 giorni) ---
    try:
        # Carica abbastanza barre 15m per coprire 56 giorni (56*96 = 5376)
        data_raw = cc.load_csv(csv_path, bars=5376, timeframe='15m')
        if len(data_raw.get('closes', [])) >= 20:
            # Ricostruisci le righe dal dict per passarle a resample_rows
            rows_15m   = _data_dict_to_rows(data_raw)
            rows_daily = cc.resample_rows(rows_15m, '1D')
            rows_daily = rows_daily[-56:]

            # Scrivi CSV temp daily
            tmp_csv = tempfile.NamedTemporaryFile(
                suffix='.csv', delete=False, mode='w', newline='', encoding='utf-8'
            )
            writer = _csv.writer(tmp_csv)
            writer.writerow(['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            writer.writerows(rows_daily)
            tmp_csv.close()
            tmp_csv_path = Path(tmp_csv.name)

            data_daily = cc.load_csv(tmp_csv_path, bars=56, timeframe='1D')
            tmp_csv_path.unlink(missing_ok=True)

            tmp_png = tempfile.NamedTemporaryFile(suffix='.png', delete=False, prefix=f'{sym}_daily_')
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
            tmp_png = tempfile.NamedTemporaryFile(suffix='.png', delete=False, prefix=f'{sym}_15m_')
            tmp_png.close()
            fig = cc.plot_chart(data_15m, symbol=sym, timeframe='15m')
            fig.savefig(tmp_png.name, dpi=130, bbox_inches='tight', facecolor='#0d1117')
            plt.close(fig)
            chart_paths.append(tmp_png.name)
            logger.debug(f"Chart 15m generato: {tmp_png.name}")

    except Exception as e:
        logger.error(f"Errore generazione chart 15m {sym}: {e}")

    return chart_paths


def _data_dict_to_rows(data: dict) -> list:
    """
    Converte il dict di cc.load_csv in lista di righe
    [timestamp, open, high, low, close, volume] per cc.resample_rows.
    """
    timestamps = data.get('timestamps', [])
    opens      = data.get('opens',      [])
    highs      = data.get('highs',      [])
    lows       = data.get('lows',       [])
    closes     = data.get('closes',     [])
    volumes    = data.get('volumes',    [])
    n = len(timestamps)
    rows = []
    for i in range(n):
        rows.append([
            timestamps[i],
            opens[i]   if i < len(opens)   else 0,
            highs[i]   if i < len(highs)   else 0,
            lows[i]    if i < len(lows)    else 0,
            closes[i]  if i < len(closes)  else 0,
            volumes[i] if i < len(volumes) else 0,
        ])
    return rows


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
