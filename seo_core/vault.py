"""Encryption for the dashboard data kept in the public cloud-refresh repo.

Format: b"IXE1" + salt(16) + nonce(12) + AES-256-GCM(gzip(data)).
The key is derived from the dashboard key (a passphrase) with scrypt.
Used by the Mac plugin, the GitHub Actions refresh and the Claude publish task.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import secrets

MAGIC = b"IXE1"


def new_key() -> str:
    return secrets.token_urlsafe(32)


def _derive(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=2 ** 26, dklen=32)


def encrypt(data: bytes, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC + salt + nonce + AESGCM(_derive(passphrase, salt)).encrypt(nonce, gzip.compress(data, 9), MAGIC)


def decrypt(blob: bytes, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:4] != MAGIC:
        raise ValueError("not an encrypted dashboard file")
    salt, nonce, ct = blob[4:20], blob[20:32], blob[32:]
    return gzip.decompress(AESGCM(_derive(passphrase, salt)).decrypt(nonce, ct, MAGIC))


if __name__ == "__main__":  # python vault.py decrypt IN OUT   (key in $DASHBOARD_KEY)
    import sys
    op, src, dst = sys.argv[1:4]
    key = os.environ["DASHBOARD_KEY"]
    data = open(src, "rb").read()
    out = decrypt(data, key) if op == "decrypt" else encrypt(data, key)
    open(dst, "wb").write(out)
