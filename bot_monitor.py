"""
bot_monitor.py — Classe HyperliquidPriceMonitor e configurazione

Estratto da hyperliquid_bot.py. Contiene:
  - MonitorConfig  : parametri di configurazione iniettati dal bot
  - HyperliquidPriceMonitor : stato condiviso, fetch prezzi, persistenza ordini

Utilizzo in hyperliquid_bot.py:
    from bot_monitor import HyperliquidPriceMonitor, MonitorConfig

    monitor = HyperliquidPriceMonitor(MonitorConfig(
        api_url                = HYPERLIQUID_API,
        supported_dexs         = SUPPORTED_DEXS,
        price_change_threshold = PRICE_CHANGE_THRESHOLD,
        spike_threshold        = SPIKE_THRESHOLD,
        cond_dir               = COND_DIR,
        data_dir               = DATA_DIR,
    ))
"""

import asyncio
import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Set, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configurazione iniettata
# ---------------------------------------------------------------------------

class MonitorConfig:
    """
    Parametri di configurazione passati al costruttore di HyperliquidPriceMonitor.
    Evita che il modulo importi direttamente le costanti da hyperliquid_bot.
    """
    def __init__(
        self,
        api_url:                str,
        supported_dexs:         list,
        price_change_threshold: float,
        spike_threshold:        float,
        cond_dir:               Path,
        data_dir:               Path,
    ):
        self.api_url                = api_url
        self.supported_dexs         = supported_dexs
        self.price_change_threshold = price_change_threshold
        self.spike_threshold        = spike_threshold
        self.cond_dir               = cond_dir
        self.data_dir               = data_dir


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class HyperliquidPriceMonitor:

    def __init__(self, config: MonitorConfig):
        self._cfg = config

        # Sottoscrizioni prezzi: {chat_id: {simboli}}
        self.subscribers:            Dict[int, Set[str]] = {}
        # Cache prezzi ultimo poll
        self.last_prices:            Dict[str, float]    = {}
        # Candela 15m in formazione: {symbol: {'open', 'high', 'low', 'close', 'slot'}}
        # 'slot' = timestamp del quarto d'ora corrente (es. "2026-03-26 14:15")
        # Resettato automaticamente al cambio di slot dal price_polling_task
        self.live_candle_ohlc: dict = {}
        # Prezzo di riferimento per alert subscribe (aggiornato dopo ogni alert)
        self.alert_base_prices:      Dict[str, float]    = {}
        # Prezzo al primo subscribe (mai modificato)
        self.subscribe_base_prices:  Dict[str, float]    = {}
        # Soglia alert per-utente (fallback a config.price_change_threshold)
        self.user_thresholds:        Dict[int, float]    = {}
        # Soglia spike per-utente (fallback a config.spike_threshold)
        self.spike_thresholds:       Dict[int, float]    = {}
        # Chat iscritti a pricespike
        self.spike_subscribers:      Set[int]            = set()
        # Prezzi poll precedente per calcolo spike
        self.spike_prev_prices:      Dict[str, float]    = {}
        # Data ultimo snapshot giornaliero scritto
        self.last_snapshot_date:     Optional[date]      = None
        # Chat già passati per il primo ciclo di tracking
        self._first_track_done:      set                 = set()

        # Position tracking: {chat_id: {coin: {entry_px, is_long, size, ...}}}
        self.position_tracks:        Dict[int, Dict[str, dict]] = {}
        # Ordini in attesa di conferma: {chat_id: {symbol, dex, is_buy, usd, expires_at}}
        self.pending_orders:         Dict[int, dict]            = {}
        # Ordini condizionali: {chat_id: {oid: {coin, dex, type, trigger_px, ...}}}
        self.conditional_orders:     Dict[int, Dict[str, dict]] = {}
        self._cond_id_counter:       int                        = 0
        # Chat con tracking posizioni attivo
        self.tracking_enabled:       Set[int]                   = set()

        # Sessione HTTP aiohttp riusabile
        self.session: Optional[aiohttp.ClientSession] = None

        # ML Scanner
        self.scanner_enabled:      bool = False
        self.scanner_min_score:    int  = 60
        self.scanner_chat_ids:     set  = set()   # deprecato, mantenuto per compatibilità
        self.scanner_mode:         str  = 'vsa'   # 'vsa' | 'volume'
        # daemon per-utente: {chat_id: {'mode': 'vsa'|'volume'|'pattern', 'min_score': int}}
        self.scanner_subscribers:  dict = {}

        # Stop Loss nativi: {chat_id: {symbol: {oid, dex, trigger_px, is_long}}}
        self.native_sl_orders: dict = {}

    # ---------------------------------------------------------------------------
    # Sessione HTTP
    # ---------------------------------------------------------------------------

    async def init_session(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
            logger.debug("Sessione HTTP inizializzata")

    async def close_session(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None
            logger.debug("Sessione HTTP chiusa")

    # ---------------------------------------------------------------------------
    # Fetch prezzi
    # ---------------------------------------------------------------------------

    async def _allMids(self, dex: str) -> dict:
        """
        POST /info {"type": "allMids", "dex": dex}
          dex=""    -> {"BTC": "price", "PURR/USDC": "price", ...}
          dex="xyz" -> {"xyz:MSTR": "price", ...}
        """
        await self.init_session()
        try:
            async with self.session.post(
                self._cfg.api_url,
                json={"type": "allMids", "dex": dex}
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error(f"allMids dex='{dex}' HTTP {resp.status}")
                return {}
        except asyncio.TimeoutError:
            logger.error(f"allMids dex='{dex}' timeout")
            return {}
        except Exception as e:
            logger.error(f"allMids dex='{dex}' errore: {e}")
            return {}

    async def fetch_all_prices(self) -> Dict[str, Tuple[float, str, Optional[str]]]:
        """
        Recupera tutti i prezzi in batch (2 chiamate HTTP totali).
        Ritorna: { symbol -> (prezzo, tipo, dex_label) }
        """
        prices: Dict[str, Tuple[float, str, Optional[str]]] = {}

        # Perp standard + spot
        data = await self._allMids("")
        for key, val in data.items():
            try:
                price = float(val)
            except (ValueError, TypeError):
                continue
            if '/' in key:
                sym = key.split('/')[0]
                prices[sym] = (price, 'SPOT', None)
            elif ':' not in key:
                prices[key] = (price, 'PERP', None)

        # Perp DEX aggiuntivi
        for dex in self._cfg.supported_dexs:
            dex_data = await self._allMids(dex)
            for key, val in dex_data.items():
                if ':' not in key:
                    continue
                try:
                    price = float(val)
                except (ValueError, TypeError):
                    continue
                sym = key.split(':', 1)[1]
                prices[sym] = (price, 'PERP', dex.upper())

        return prices

    async def get_price(self, symbol: str) -> Optional[Tuple[float, str, Optional[str]]]:
        """Prezzo singolo (usato da /price e /subscribe)."""
        sym = symbol.upper()

        data = await self._allMids("")
        if sym in data:
            return (float(data[sym]), 'PERP', None)
        if f"{sym}/USDC" in data:
            return (float(data[f"{sym}/USDC"]), 'SPOT', None)

        for dex in self._cfg.supported_dexs:
            dex_data = await self._allMids(dex)
            if f"{dex}:{sym}" in dex_data:
                return (float(dex_data[f"{dex}:{sym}"]), 'PERP', dex.upper())

        logger.debug(f"Simbolo {sym} non trovato")
        return None

    async def get_all_symbols(self) -> dict:
        """Restituisce tutti i simboli disponibili raggruppati per tipo."""
        result: dict = {'perps': [], 'spot': [], 'dex_perps': {}}
        data = await self._allMids("")
        result['perps'] = sorted(
            k for k in data
            if '/' not in k and ':' not in k and not k.startswith('@')
        )
        result['spot'] = sorted(k for k in data if '/' in k)
        for dex in self._cfg.supported_dexs:
            dex_data = await self._allMids(dex)
            if dex_data:
                result['dex_perps'][dex.upper()] = sorted(
                    k.split(':', 1)[1] for k in dex_data if ':' in k
                )
        return result

    # ---------------------------------------------------------------------------
    # Gestione sottoscrizioni e soglie
    # ---------------------------------------------------------------------------

    def get_threshold(self, chat_id: int) -> float:
        """Soglia subscribe per-utente, fallback a config globale."""
        return self.user_thresholds.get(chat_id, self._cfg.price_change_threshold)

    def set_threshold(self, chat_id: int, threshold: float) -> None:
        self.user_thresholds[chat_id] = threshold
        logger.info(f"Chat {chat_id} soglia subscribe impostata a {threshold}%")

    def get_spike_threshold(self, chat_id: int) -> float:
        """Soglia spike per-utente, fallback a config globale."""
        return self.spike_thresholds.get(chat_id, self._cfg.spike_threshold)

    def set_spike_threshold(self, chat_id: int, threshold: float) -> None:
        self.spike_thresholds[chat_id] = threshold
        logger.info(f"Chat {chat_id} soglia spike impostata a {threshold}%")

    def add_subscriber(self, chat_id: int, symbol: str) -> None:
        self.subscribers.setdefault(chat_id, set()).add(symbol.upper())
        logger.info(f"Chat {chat_id} sottoscritta a {symbol.upper()}")

    def remove_subscriber(self, chat_id: int, symbol: str) -> None:
        if chat_id in self.subscribers:
            self.subscribers[chat_id].discard(symbol.upper())
            if not self.subscribers[chat_id]:
                del self.subscribers[chat_id]

    def get_subscriptions(self, chat_id: int) -> Set[str]:
        return self.subscribers.get(chat_id, set())

    def get_all_monitored_symbols(self) -> Set[str]:
        """Tutti i simboli attualmente monitorati (subscribe + ordini condizionali)."""
        result: Set[str] = set()
        for syms in self.subscribers.values():
            result.update(syms)
        for conds in self.conditional_orders.values():
            for o in conds.values():
                result.add(o['coin'])
        return result

    # ---------------------------------------------------------------------------
    # Persistenza ordini condizionali
    # ---------------------------------------------------------------------------

    def _cond_path(self, chat_id: int) -> Path:
        p = self._cfg.cond_dir / str(chat_id)
        p.mkdir(parents=True, exist_ok=True)
        return p / 'conditional_orders.json'

    def save_conditional_orders(self, chat_id: int) -> None:
        """Salva gli ordini condizionali di un chat_id su file JSON."""
        conds = self.conditional_orders.get(chat_id, {})
        data  = {}
        for oid, o in conds.items():
            row = dict(o)
            if isinstance(row.get('created_at'), datetime):
                row['created_at'] = row['created_at'].isoformat()
            data[oid] = row
        try:
            self._cond_path(chat_id).write_text(
                json.dumps(data, indent=2), encoding='utf-8'
            )
        except Exception as e:
            logger.error(f"Errore salvataggio ordini condizionali {chat_id}: {e}")

    def load_conditional_orders(self, chat_id: int) -> None:
        """Carica gli ordini condizionali da file per un chat_id."""
        path = self._cond_path(chat_id)
        if not path.exists():
            return
        try:
            data   = json.loads(path.read_text(encoding='utf-8'))
            orders = {}
            for oid, o in data.items():
                if isinstance(o.get('created_at'), str):
                    try:
                        o['created_at'] = datetime.fromisoformat(o['created_at'])
                    except Exception:
                        o['created_at'] = datetime.now()
                orders[oid] = o
                try:
                    num = int(oid[1:])
                    if num > self._cond_id_counter:
                        self._cond_id_counter = num
                except Exception:
                    pass
            self.conditional_orders[chat_id] = orders
            logger.info(f"Caricati {len(orders)} ordini condizionali per chat {chat_id}")
        except Exception as e:
            logger.error(f"Errore caricamento ordini condizionali {chat_id}: {e}")

    def new_order_id(self) -> str:
        self._cond_id_counter += 1
        return f"C{self._cond_id_counter:04d}"

    def load_all_conditional_orders(self) -> None:
        """Carica tutti gli ordini condizionali + native SL al boot."""
        self._cfg.cond_dir.mkdir(parents=True, exist_ok=True)
        for chat_dir in self._cfg.cond_dir.iterdir():
            if chat_dir.is_dir():
                try:
                    cid = int(chat_dir.name)
                    self.load_conditional_orders(cid)
                    self.load_native_sl_orders(cid)
                except Exception:
                    pass
        self._fix_missing_is_long()

    # ---------------------------------------------------------------------------
    # Persistenza native SL orders
    # ---------------------------------------------------------------------------

    def _sl_path(self, chat_id: int) -> Path:
        p = self._cfg.cond_dir / str(chat_id)
        p.mkdir(parents=True, exist_ok=True)
        return p / 'native_sl_orders.json'

    def save_native_sl_orders(self, chat_id: int) -> None:
        data = self.native_sl_orders.get(chat_id, {})
        try:
            self._sl_path(chat_id).write_text(
                json.dumps(data, indent=2), encoding='utf-8'
            )
        except Exception as e:
            logger.error(f"Errore salvataggio native_sl_orders {chat_id}: {e}")

    def load_native_sl_orders(self, chat_id: int) -> None:
        path = self._sl_path(chat_id)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            self.native_sl_orders[chat_id] = data
            logger.info(f"Chat {chat_id}: caricati {len(data)} native SL orders")
        except Exception as e:
            logger.error(f"Errore caricamento native_sl_orders {chat_id}: {e}")

    # ---------------------------------------------------------------------------
    # Fix is_long al boot
    # ---------------------------------------------------------------------------

    def _fix_missing_is_long(self) -> None:
        """
        Per ogni ordine caricato senza campo is_long, recupera la direzione
        dalla posizione aperta su Hyperliquid. Chiamato una volta al boot.
        Usa clearinghouseState per perp standard e per dex xyz.
        """
        import requests as _req
        API = 'https://api.hyperliquid.xyz/info'

        for chat_id, conds in self.conditional_orders.items():
            needs_fix = [o for o in conds.values() if 'is_long' not in o]
            if not needs_fix:
                continue

            # Legge address dal disco
            addr = None
            try:
                addr_file = self._cfg.data_dir / 'wallet' / str(chat_id) / 'address.txt'
                if addr_file.exists():
                    addr = addr_file.read_text(encoding='utf-8').strip()
            except Exception:
                pass

            if not addr:
                for o in needs_fix:
                    o['is_long'] = True
                continue

            pos_by_coin: Dict[str, bool] = {}

            # 1) Perp standard
            try:
                resp  = _req.post(
                    API,
                    json={'type': 'clearinghouseState', 'user': addr},
                    timeout=8
                )
                state = resp.json()
                for ap in state.get('assetPositions', []):
                    pos = ap.get('position', {})
                    szi = float(pos.get('szi', 0))
                    if szi == 0:
                        continue
                    coin = pos.get('coin', '').split(':')[-1]
                    pos_by_coin[coin] = szi > 0
                    logger.info(f"_fix_is_long PERP {coin}: szi={szi} is_long={szi > 0}")
            except Exception as e:
                logger.warning(f"_fix_missing_is_long perp: {e}")

            # 2) DEX aggiuntivi
            for dex in self._cfg.supported_dexs:
                try:
                    resp  = _req.post(
                        API,
                        json={'type': 'clearinghouseState', 'user': addr, 'dex': dex},
                        timeout=8
                    )
                    state = resp.json()
                    for ap in state.get('assetPositions', []):
                        pos = ap.get('position', {})
                        szi = float(pos.get('szi', 0))
                        if szi == 0:
                            continue
                        coin = pos.get('coin', '').split(':')[-1]
                        pos_by_coin[coin] = szi > 0
                        logger.info(f"_fix_is_long {dex} {coin}: szi={szi} is_long={szi > 0}")
                except Exception as e:
                    logger.warning(f"_fix_missing_is_long dex={dex}: {e}")

            # Applica e salva
            changed = False
            for o in needs_fix:
                coin = o.get('coin', '')
                if coin in pos_by_coin:
                    o['is_long'] = pos_by_coin[coin]
                    logger.info(f"Ordine {coin} ({o['type']}) is_long={o['is_long']}")
                else:
                    o['is_long'] = True
                    logger.warning(
                        f"Posizione {coin} non trovata nell'API, default is_long=True"
                    )
                changed = True
            if changed:
                self.save_conditional_orders(chat_id)
