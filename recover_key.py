"""
recover_key.py — Utility di recovery per le private key degli utenti
=====================================================================
Decifra il file key.enc usando WALLET_ENCRYPTION_KEY dal file .env.

Uso:
    python3 recover_key.py                              # lista tutti gli utenti
    python3 recover_key.py --chat-id 123456789          # decifra uno specifico
    python3 recover_key.py --all                        # decifra tutti
    python3 recover_key.py --env altro.env              # usa env file diverso

⚠️  USO ESCLUSIVO PER RECOVERY — non diffondere l'output.
"""

import argparse
import os
import sys
from pathlib import Path
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_env(env_file: str) -> str:
    """Carica il .env e ritorna WALLET_ENCRYPTION_KEY."""
    env_path = Path(env_file)
    if not env_path.exists():
        print(f"❌ File .env non trovato: {env_file}")
        sys.exit(1)
    load_dotenv(dotenv_path=env_path)
    key = os.getenv('WALLET_ENCRYPTION_KEY', '')
    if not key:
        print("❌ WALLET_ENCRYPTION_KEY non trovata nel file .env")
        sys.exit(1)
    return key


def _decrypt_key(key_enc_path: Path, encryption_key: str) -> str:
    """Decifra un file key.enc e ritorna la private key in chiaro."""
    from cryptography.fernet import Fernet, InvalidToken
    f = Fernet(encryption_key.encode())
    try:
        decrypted = f.decrypt(key_enc_path.read_bytes())
        return decrypted.decode()
    except InvalidToken:
        raise ValueError("Chiave di cifratura errata o file corrotto")


def _get_address(chat_dir: Path) -> str:
    addr_file = chat_dir / 'address.txt'
    if addr_file.exists():
        return addr_file.read_text(encoding='utf-8').strip()
    return '(nessun address)'


def _short(s: str) -> str:
    if len(s) > 10:
        return f"{s[:6]}...{s[-4:]}"
    return s


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Recovery private key cifrate degli utenti OpenTradeNet'
    )
    parser.add_argument('--chat-id', type=int, default=None,
                        help='Telegram chat_id specifico da decifrare')
    parser.add_argument('--all', action='store_true',
                        help='Decifra tutti gli utenti')
    parser.add_argument('--env', type=str, default='opentradenet.env',
                        help='Path al file .env (default: opentradenet.env)')
    parser.add_argument('--data-dir', type=str, default='data',
                        help='Directory dati (default: data)')
    args = parser.parse_args()

    encryption_key = _load_env(args.env)
    wallet_dir     = Path(args.data_dir) / 'wallet'

    if not wallet_dir.exists():
        print(f"❌ Directory wallet non trovata: {wallet_dir}")
        sys.exit(1)

    # Trova tutti gli utenti con key.enc
    user_dirs = sorted(
        [d for d in wallet_dir.iterdir() if d.is_dir() and (d / 'key.enc').exists()],
        key=lambda d: d.name
    )

    if not user_dirs:
        print("ℹ️  Nessun utente con key.enc trovato.")
        sys.exit(0)

    # --- Lista utenti (senza decifrare) ---
    if not args.chat_id and not args.all:
        print(f"\n👥 Utenti con key.enc in {wallet_dir}:\n")
        for d in user_dirs:
            addr = _get_address(d)
            print(f"  chat_id: {d.name:>15}   address: {_short(addr)}")
        print(f"\nTotale: {len(user_dirs)} utente/i")
        print("\nUsa --chat-id ID o --all per decifrare.")
        return

    # --- Decifra utente specifico ---
    if args.chat_id:
        target_dir = wallet_dir / str(args.chat_id)
        key_file   = target_dir / 'key.enc'

        if not target_dir.exists():
            print(f"❌ Nessun dato trovato per chat_id {args.chat_id}")
            sys.exit(1)
        if not key_file.exists():
            print(f"❌ key.enc non trovato per chat_id {args.chat_id}")
            sys.exit(1)

        addr = _get_address(target_dir)
        try:
            private_key = _decrypt_key(key_file, encryption_key)
            print(f"\n✅ Recovery chat_id {args.chat_id}")
            print(f"   Address:     {addr}")
            print(f"   Private key: {private_key}")
        except ValueError as e:
            print(f"❌ Errore decifratura chat_id {args.chat_id}: {e}")
            sys.exit(1)
        return

    # --- Decifra tutti ---
    if args.all:
        print(f"\n🔓 Recovery di {len(user_dirs)} utente/i\n")
        ok    = 0
        errors = 0
        for d in user_dirs:
            addr = _get_address(d)
            try:
                private_key = _decrypt_key(d / 'key.enc', encryption_key)
                print(f"  ✅ chat_id {d.name:>15}   address: {_short(addr)}")
                print(f"              private_key: {private_key}\n")
                ok += 1
            except ValueError as e:
                print(f"  ❌ chat_id {d.name:>15}   ERRORE: {e}\n")
                errors += 1

        print(f"Completato: {ok} OK, {errors} errori.")


if __name__ == '__main__':
    main()
