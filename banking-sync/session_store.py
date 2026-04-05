"""
session_store.py — Minimal JSON-file persistence for session token and accounts.

Replaces the full SQLite layer. Only stores what's needed to authenticate
with Enable Banking between restarts:
  - access_token
  - authorization_id
  - expires_at
  - discovered accounts (uid, iban, currency, name)
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SESSION_PATH: Path | None = None


def init(path: str = "data/session.json") -> None:
    global _SESSION_PATH
    _SESSION_PATH = Path(path)
    _SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)


def save_session(
    access_token: str,
    expires_at: str | None = None,
    authorization_id: str | None = None,
    accounts: list[dict] | None = None,
) -> None:
    existing = _read() or {}
    data = {
        "access_token": access_token,
        "authorization_id": authorization_id,
        "expires_at": expires_at,
        "accounts": accounts if accounts is not None else existing.get("accounts", []),
    }
    _write(data)
    logger.info("Session saved (expires_at=%s, accounts=%d)", expires_at, len(data["accounts"]))


def save_accounts(accounts: list[dict]) -> None:
    data = _read() or {}
    data["accounts"] = accounts
    _write(data)


def get_session() -> dict | None:
    data = _read()
    if not data or not data.get("access_token"):
        return None
    return data


def get_accounts() -> list[dict]:
    data = _read()
    if not data:
        return []
    return data.get("accounts", [])


def clear_session() -> None:
    if _SESSION_PATH and _SESSION_PATH.exists():
        _SESSION_PATH.unlink()
        logger.info("Session file deleted")


# ── Internal ──────────────────────────────────────────────────────────────────

def _read() -> dict | None:
    if not _SESSION_PATH or not _SESSION_PATH.exists():
        return None
    try:
        return json.loads(_SESSION_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read session file: %s", exc)
        return None


def _write(data: dict) -> None:
    _SESSION_PATH.write_text(json.dumps(data, indent=2, default=str))
