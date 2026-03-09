"""
ml_analyst.py — Analisi tecnica AI tramite Claude Vision API
Genera chart daily + 15m di un simbolo e li invia a Claude per analisi.
Integrato nel bot tramite comando /analyze SIMBOLO.
"""

import os
import sys
import base64
import logging
import tempfile
import json
from pathlib import Path
from datetime import datetime, timedelta
import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL   = "claude-haiku-4-5-20251001"   # economico, supporta vision

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
    ml_score: float | None,
    ml_signal: str | None,
    candles_dir: Path,
    anthropic_api_key: str
) -> tuple[str, list[str]]:
    """
    Genera i chart e chiama Claude Vision per l'analisi.

    Returns:
        (testo_analisi, lista_path_chart_temporanei)
        I file temporanei vanno eliminati dal chiamante dopo l'invio.
    """

    # --- Genera i chart ---
    chart_paths = _generate_charts(symbol, candles_dir)
    if not chart_paths:
        return f"❌ Nessun dato disponibile per {symbol}", []

    # --- Codifica immagini in base64 ---
    images_b64 = []
    for path in chart_paths:
        try:
            with open(path, "rb") as f:
                images_b64.append(base64.standard_b64encode(f.read()).decode("utf-8"))
        except Exception as e:
            logger.error(f"Errore lettura chart {path}: {e}")

    if not images_b64:
        return "❌ Errore nella generazione dei chart", chart_paths

    # --- Costruisci messaggio per Claude ---
    content = []
    labels = ["DAILY (ricostruito da 15m, ultimi 56 giorni)", "15 MINUTI (ultime 120 candele)"]
    for i, img_b64 in enumerate(images_b64):
        content.append({
            "type": "text",
            "text": f"Chart {i+1}: {labels[i] if i < len(labels) else ''}"
        })
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": img_b64
            }
        })

    # Aggiungi contesto ML scanner se disponibile
    ml_context = ""
    if ml_score is not None and ml_signal is not None:
        ml_context = f"\n\nContesto aggiuntivo — ML Scanner: segnale={ml_signal}, score={ml_score:.2f}"
    content.append({
        "type": "text",
        "text": f"Analizza il simbolo {symbol.upper()}.{ml_context}"
    })

    # --- Chiamata API ---
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
# Generazione chart
# ---------------------------------------------------------------------------

def _generate_charts(symbol: str, candles_dir: Path) -> list[str]:
    """
    Genera due chart PNG (daily ricostruito + 15m) in file temporanei.
    Ritorna lista di path. Ritorna [] se dati insufficienti.
    """
    try:
        import candle_chart as cc
    except ImportError:
        logger.error("candle_chart.py non trovato")
        return []

    sym = symbol.upper()
    csv_path = candles_dir / sym / f"{sym}_15m.csv"

    if not csv_path.exists():
        logger.warning(f"CSV non trovato: {csv_path}")
        return []

    # Leggi tutte le candele 15m
    candles_15m = cc.load_candles_from_csv(str(csv_path))
    if len(candles_15m) < 20:
        logger.warning(f"Dati insufficienti per {sym}: {len(candles_15m)} candele")
        return []

    chart_paths = []

    # --- Chart 1: Daily ricostruito (ultime 56 candele daily = ~56 giorni) ---
    try:
        daily_candles = _resample_to_daily(candles_15m)
        daily_to_use  = daily_candles[-56:] if len(daily_candles) > 56 else daily_candles

        tmp_daily = tempfile.NamedTemporaryFile(
            suffix=".png", delete=False, prefix=f"{sym}_daily_"
        )
        tmp_daily.close()

        fig_daily = cc.build_chart(
            candles=daily_to_use,
            symbol=sym,
            interval="1D",
            tl_r_period=3,
            tl_s_period=5
        )
        fig_daily.savefig(tmp_daily.name, dpi=130, bbox_inches="tight",
                          facecolor="#0d1117")
        import matplotlib.pyplot as plt
        plt.close(fig_daily)
        chart_paths.append(tmp_daily.name)
        logger.debug(f"Chart daily generato: {tmp_daily.name}")

    except Exception as e:
        logger.error(f"Errore generazione chart daily {sym}: {e}")

    # --- Chart 2: 15m (ultime 120 candele) ---
    try:
        candles_15m_slice = candles_15m[-120:]

        tmp_15m = tempfile.NamedTemporaryFile(
            suffix=".png", delete=False, prefix=f"{sym}_15m_"
        )
        tmp_15m.close()

        fig_15m = cc.build_chart(
            candles=candles_15m_slice,
            symbol=sym,
            interval="15m",
            tl_r_period=28,
            tl_s_period=43
        )
        fig_15m.savefig(tmp_15m.name, dpi=130, bbox_inches="tight",
                        facecolor="#0d1117")
        import matplotlib.pyplot as plt
        plt.close(fig_15m)
        chart_paths.append(tmp_15m.name)
        logger.debug(f"Chart 15m generato: {tmp_15m.name}")

    except Exception as e:
        logger.error(f"Errore generazione chart 15m {sym}: {e}")

    return chart_paths


def _resample_to_daily(candles_15m: list) -> list:
    """
    Ricampiona candele 15m in candele daily.
    Ogni candela è un dict con keys: timestamp, open, high, low, close, volume
    timestamp è in millisecondi (unix ms).
    """
    from collections import defaultdict

    buckets = defaultdict(list)
    for c in candles_15m:
        ts_ms = int(c["timestamp"])
        # Calcola il bucket giornaliero (ms da epoch per inizio giornata UTC)
        day_key = (ts_ms // 86_400_000) * 86_400_000
        buckets[day_key].append(c)

    daily = []
    for day_ts in sorted(buckets.keys()):
        day_candles = buckets[day_ts]
        daily.append({
            "timestamp": day_ts,
            "open":   float(day_candles[0]["open"]),
            "high":   max(float(c["high"])   for c in day_candles),
            "low":    min(float(c["low"])    for c in day_candles),
            "close":  float(day_candles[-1]["close"]),
            "volume": sum(float(c["volume"]) for c in day_candles),
        })

    return daily


# ---------------------------------------------------------------------------
# Cleanup helper
# ---------------------------------------------------------------------------

def cleanup_charts(paths: list[str]) -> None:
    """Elimina i file temporanei dei chart."""
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass
