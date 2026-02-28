"""
hl_wallet.py — Gestione credenziali e accesso API Hyperliquid per-utente

Struttura dati su disco:
  data/wallet/{chat_id}/address.txt   — account address (pubblico, in chiaro)
  data/wallet/{chat_id}/key.enc       — private key cifrata con Fernet (AES-128)

La chiave di cifratura WALLET_ENCRYPTION_KEY viene generata al primo avvio
e salvata nell'env file. Non viene mai scritta nella stessa directory dei dati.

Flusso comandi:
  /setaddress 0x...   — salva l'account address pubblico
  /setkey 0x...       — salva la private key cifrata
  /walletinfo         — mostra address e stato chiave
  /positions          — mostra le posizioni aperte (richiede solo address)
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cifratura — Fernet (AES-128-CBC + HMAC-SHA256), dalla stdlib cryptography
# ---------------------------------------------------------------------------

def _get_fernet(encryption_key: str):
    """Restituisce un'istanza Fernet dalla chiave in base64."""
    from cryptography.fernet import Fernet
    key_bytes = encryption_key.encode()
    # Fernet richiede una chiave base64url da 32 byte → 44 caratteri
    if len(key_bytes) != 44:
        raise ValueError("WALLET_ENCRYPTION_KEY deve essere una chiave Fernet valida (44 caratteri base64)")
    return Fernet(key_bytes)


def generate_encryption_key() -> str:
    """Genera una nuova chiave Fernet da mettere nel .env."""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


# ---------------------------------------------------------------------------
# WalletStore — gestione file per chat_id
# ---------------------------------------------------------------------------

class WalletStore:
    def __init__(self, data_dir: Path, encryption_key: str):
        self.base_dir       = data_dir / 'wallet'
        self.encryption_key = encryption_key
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _user_dir(self, chat_id: int) -> Path:
        d = self.base_dir / str(chat_id)
        d.mkdir(exist_ok=True)
        return d

    # --- Address pubblico (in chiaro) ---

    def save_address(self, chat_id: int, address: str) -> None:
        path = self._user_dir(chat_id) / 'address.txt'
        path.write_text(address.strip().lower(), encoding='utf-8')
        logger.info(f"Chat {chat_id} address salvato")

    def get_address(self, chat_id: int) -> Optional[str]:
        path = self._user_dir(chat_id) / 'address.txt'
        if path.exists():
            return path.read_text(encoding='utf-8').strip()
        return None

    # --- Private key (cifrata) ---

    def save_key(self, chat_id: int, private_key: str) -> None:
        f    = _get_fernet(self.encryption_key)
        enc  = f.encrypt(private_key.strip().encode())
        path = self._user_dir(chat_id) / 'key.enc'
        path.write_bytes(enc)
        logger.info(f"Chat {chat_id} private key salvata (cifrata)")

    def get_key(self, chat_id: int) -> Optional[str]:
        path = self._user_dir(chat_id) / 'key.enc'
        if not path.exists():
            return None
        try:
            f   = _get_fernet(self.encryption_key)
            dec = f.decrypt(path.read_bytes())
            return dec.decode()
        except Exception as e:
            logger.error(f"Chat {chat_id} errore decifratura key: {e}")
            return None

    def has_key(self, chat_id: int) -> bool:
        return (self._user_dir(chat_id) / 'key.enc').exists()

    def delete(self, chat_id: int) -> None:
        """Cancella tutte le credenziali di un utente."""
        import shutil
        d = self.base_dir / str(chat_id)
        if d.exists():
            shutil.rmtree(d)
        logger.info(f"Chat {chat_id} credenziali cancellate")


# ---------------------------------------------------------------------------
# HyperliquidClient — accesso API (read-only non richiede key)
# ---------------------------------------------------------------------------

class HyperliquidClient:
    """
    Wrapper leggero attorno all'SDK hyperliquid-python-sdk.
    Per operazioni read-only basta l'address pubblico.
    Per operazioni di trading serve anche la private key.
    """

    API_URL = 'https://api.hyperliquid.xyz'

    def __init__(self, account_address: str, private_key: Optional[str] = None):
        self.account_address = account_address.lower()
        self.private_key     = private_key
        self._info           = None
        self._exchange       = None

    def _get_info(self):
        if self._info is None:
            from hyperliquid.info import Info
            self._info = Info(self.API_URL, skip_ws=True)
        return self._info

    def _get_exchange(self):
        if self._exchange is None:
            if not self.private_key:
                raise ValueError("Private key non impostata — usa /setkey prima")
            import eth_account
            from hyperliquid.exchange import Exchange
            wallet           = eth_account.Account.from_key(self.private_key)
            self._exchange   = Exchange(wallet, self.API_URL,
                                        account_address=self.account_address)
        return self._exchange

    def get_positions(self, extra_dexs: list = None) -> list:
        """
        Restituisce le posizioni aperte come lista di dict.
        Legge il dex principale (perp standard) + tutti i dex extra (es. ['xyz']).
        Non richiede private key.
        """
        info      = self._get_info()
        positions = []

        # Legge tutti i dex: "" = perp standard, + quelli extra (xyz ecc.)
        dexs_to_query = [''] + (extra_dexs or [])

        for dex in dexs_to_query:
            try:
                if dex == '':
                    state = info.user_state(self.account_address)
                else:
                    # clearinghouseState con dex specifico
                    import requests
                    resp  = requests.post(
                        f'{self.API_URL}/info',
                        json={'type': 'clearinghouseState', 'user': self.account_address, 'dex': dex},
                        timeout=10
                    )
                    state = resp.json()

                logger.debug(f"dex='{dex}' assetPositions: {len(state.get('assetPositions', []))}")

                for ap in state.get('assetPositions', []):
                    pos = ap.get('position', {})
                    szi = float(pos.get('szi', 0))
                    if szi == 0:
                        continue
                    entry_px   = float(pos.get('entryPx',       0) or 0)
                    unrealized = float(pos.get('unrealizedPnl',  0) or 0)
                    liq_px     = float(pos.get('liquidationPx',  0) or 0)
                    margin     = float(pos.get('marginUsed',    '0') or 0)
                    leverage   = pos.get('leverage', {})
                    lev_val    = leverage.get('value', '?') if isinstance(leverage, dict) else '?'
                    coin       = pos.get('coin', '?')
                    # Per xyz il coin arriva come "xyz:NFLX" — rimuoviamo il prefisso per display
                    coin_display = coin.split(':')[-1] if ':' in coin else coin
                    positions.append({
                        'coin':         coin_display,
                        'coin_raw':     coin,
                        'dex':          dex or 'PERP',
                        'size':         szi,
                        'entry_px':     entry_px,
                        'unrealized':   unrealized,
                        'liq_px':       liq_px,
                        'margin':       margin,
                        'leverage':     lev_val,
                        'is_long':      szi > 0,
                    })
            except Exception as e:
                logger.error(f"Errore get_positions dex='{dex}': {e}")

        return positions

    def set_leverage(self, coin: str, leverage: int, is_cross: bool = False, dex: str = '') -> dict:
        """
        Imposta la leva su un simbolo.
        dex='' per perp standard, dex='xyz' per xyz.
        is_cross=False → isolated (default Hyperliquid)
        Richiede private key.
        """
        if dex:
            import eth_account
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info

            wallet = eth_account.Account.from_key(self.private_key)

            # Info con perp_dexs=[dex] carica i simboli xyz con offset 110000
            info = Info(self.API_URL, skip_ws=True, perp_dexs=[dex])

            # Il nome interno è 'xyz:GOLD' (come restituito dall'API meta)
            internal_name = f"{dex}:{coin.upper()}"

            exchange = Exchange(
                wallet,
                self.API_URL,
                account_address=self.account_address,
                vault_address=None,
            )
            exchange.info = info

            return exchange.update_leverage(leverage, internal_name, is_cross)
        else:
            # Perp standard — usa Exchange SDK
            exchange = self._get_exchange()
            return exchange.update_leverage(leverage, coin, is_cross)

    def _get_exchange_xyz(self, dex: str):
        """Exchange configurato per un dex xyz."""
        import eth_account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        wallet   = eth_account.Account.from_key(self.private_key)
        info     = Info(self.API_URL, skip_ws=True, perp_dexs=[dex])
        exchange = Exchange(wallet, self.API_URL,
                            account_address=self.account_address,
                            vault_address=None)
        exchange.info = info
        return exchange

    def get_current_price(self, coin: str, dex: str = '') -> float:
        """Prezzo corrente mid del simbolo."""
        import requests
        resp = requests.post(f'{self.API_URL}/info',
                             json={'type': 'allMids', 'dex': dex}, timeout=10)
        mids = resp.json()
        internal = f"{dex}:{coin.upper()}" if dex else coin.upper()
        val = mids.get(internal) or mids.get(coin.upper())
        if val is None:
            raise ValueError(f"Prezzo non trovato per '{coin}' (dex='{dex}')")
        return float(val)

    def market_open(self, coin: str, is_buy: bool, usd_amount: float,
                    dex: str = '', slippage: float = 0.01) -> dict:
        """
        Apre una posizione long (is_buy=True) o short (is_buy=False).
        usd_amount = valore nozionale in dollari (es. 500 → $500 di posizione).
        size calcolata automaticamente: size = usd_amount / prezzo_corrente.
        """
        price    = self.get_current_price(coin, dex)
        sz_dec   = self._get_sz_decimals(coin, dex)
        size     = round(usd_amount / price, sz_dec)

        if dex:
            exchange = self._get_exchange_xyz(dex)
            name     = f"{dex}:{coin.upper()}"
        else:
            exchange = self._get_exchange()
            name     = coin.upper()

        result = exchange.market_open(name, is_buy, size, slippage=slippage)
        result['_price']  = price
        result['_size']   = size
        result['_usd']    = usd_amount
        return result

    def market_close(self, coin: str, dex: str = '',
                     usd_amount: float = None, pct: float = None) -> dict:
        """
        Chiude una posizione aperta.
        usd_amount: chiudi $N di posizione (None = tutto)
        pct: chiudi X% della posizione (es. 0.5 = 50%)
        Se entrambi None chiude tutto.
        """
        # Legge size attuale
        pos_list = self.get_positions(extra_dexs=[dex] if dex else [])
        coin_up  = coin.upper()
        pos      = next((p for p in pos_list if p['coin'].upper() == coin_up), None)
        if pos is None:
            raise ValueError(f"Nessuna posizione aperta su {coin}")

        total_size = abs(pos['size'])

        if pct is not None:
            close_size = round(total_size * pct, 6)
        elif usd_amount is not None:
            price      = self.get_current_price(coin, dex)
            sz_dec     = self._get_sz_decimals(coin, dex)
            close_size = round(usd_amount / price, sz_dec)
            close_size = min(close_size, total_size)
        else:
            close_size = total_size

        if dex:
            exchange = self._get_exchange_xyz(dex)
            name     = f"{dex}:{coin.upper()}"
        else:
            exchange = self._get_exchange()
            name     = coin.upper()

        result = exchange.market_close(name, close_size)
        result['_size']  = close_size
        result['_total'] = total_size
        return result

    def _get_sz_decimals(self, coin: str, dex: str = '') -> int:
        """Numero di decimali per la size del simbolo."""
        try:
            import requests
            resp = requests.post(f'{self.API_URL}/info',
                                 json={'type': 'meta', 'dex': dex}, timeout=10)
            universe = resp.json().get('universe', [])
            prefix   = f"{dex}:" if dex else ""
            for asset in universe:
                name = asset.get('name', '').replace(prefix, '').replace('XYZ:', '')
                if name.upper() == coin.upper():
                    return asset.get('szDecimals', 4)
        except Exception:
            pass
        return 4  # default safe

    def get_account_summary(self) -> dict:
        """
        Restituisce il riepilogo del conto: equity, margin, ecc.
        Non richiede private key.
        """
        info  = self._get_info()
        state = info.user_state(self.account_address)
        ms    = state.get('marginSummary', {})
        return {
            'account_value':    float(ms.get('accountValue',    0) or 0),
            'total_margin':     float(ms.get('totalMarginUsed', 0) or 0),
            'total_ntl_pos':    float(ms.get('totalNtlPos',     0) or 0),
            'total_raw_usd':    float(ms.get('totalRawUsd',     0) or 0),
        }
