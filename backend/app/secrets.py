"""Encryption helpers for gateway credentials.

API keys are encrypted at rest with a machine-local Fernet key
(`.gateway_keys.key`, gitignored). Only a masked reference is ever returned
by the API — the plaintext never appears in responses, prompts or logs.
"""
import os

from cryptography.fernet import Fernet

KEY_PATH = os.environ.get(
    "AGENT_OFFICE_KEYFILE",
    os.path.join(os.path.dirname(__file__), "..", "..", ".gateway_keys.key"))


def _fernet() -> Fernet:
    if not os.path.exists(KEY_PATH):
        os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
        with open(KEY_PATH, "wb") as f:
            f.write(Fernet.generate_key())
    with open(KEY_PATH, "rb") as f:
        return Fernet(f.read())


def encrypt(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    if not value:
        return ""
    return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
