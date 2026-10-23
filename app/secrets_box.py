"""Secrets for integrations (MCP servers R38, imported API tools R39), encrypted
at rest with INTEGRATIONS_KEY (a Fernet key). Without the key, nothing that
needs a secret can be stored, and nothing stored can be read."""

import json
import os


class SecretError(Exception):
    pass


def _fernet():
    key = os.environ.get("INTEGRATIONS_KEY", "").strip()
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def available() -> bool:
    return bool(os.environ.get("INTEGRATIONS_KEY", "").strip())


def seal(value: dict) -> str | None:
    if not value:
        return None
    box = _fernet()
    if box is None:
        raise SecretError("set INTEGRATIONS_KEY (a Fernet key) on the server to store a secret")
    return box.encrypt(json.dumps(value).encode()).decode()


def open_(sealed: str | None) -> dict:
    if not sealed:
        return {}
    box = _fernet()
    if box is None:
        raise SecretError("INTEGRATIONS_KEY is not set, so a stored secret can't be read")
    return json.loads(box.decrypt(sealed.encode()))
