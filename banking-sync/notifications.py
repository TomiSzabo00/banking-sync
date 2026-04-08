"""
notifications.py — Human-facing notifications via Home Assistant webhooks.

Unlike webhooks.py (which is for machine-to-machine integrations), this module
sends notifications intended for a person — e.g. push notifications via HA.

Two event types:
  - sync_completed  — only fires when notify_sync: true in config
  - auth_required   — always fires so you never miss an expiry

Config (config.yaml):
  notifications:
    homeassistant:
      webhook_url: "http://homeassistant.local:8123/api/webhook/banking-sync"
      notify_sync: false
"""
import json
import logging

import requests

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10

_webhook_url: str | None = None
_notify_sync: bool = False


def init(config: dict) -> None:
    global _webhook_url, _notify_sync
    ha = config.get("notifications", {}).get("homeassistant", {}) or {}
    _webhook_url = ha.get("webhook_url") or None
    _notify_sync = bool(ha.get("notify_sync", False))
    if _webhook_url:
        logger.info("Notifications: HA webhook configured (notify_sync=%s)", _notify_sync)
    else:
        logger.info("Notifications: no HA webhook configured — skipping")


def notify_sync_completed(account_uid: str, new_count: int, total_fetched: int) -> None:
    if not _notify_sync:
        return
    _post({
        "event": "sync_completed",
        "account_uid": account_uid,
        "new_transactions": new_count,
        "total_fetched": total_fetched,
        "success": True,
    })


def notify_sync_failed(account_uid: str, error: str) -> None:
    if not _notify_sync:
        return
    _post({
        "event": "sync_completed",
        "account_uid": account_uid,
        "new_transactions": 0,
        "total_fetched": 0,
        "success": False,
        "error": error,
    })


def notify_auth_required() -> None:
    _post({
        "event": "auth_required",
        "message": "Enable Banking session expired. Visit /auth/start to re-authenticate.",
    })


def _post(data: dict) -> None:
    if not _webhook_url:
        return
    try:
        body = json.dumps(data)
        requests.post(
            _webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )
        logger.info("Notification sent: %s", data.get("event"))
    except requests.RequestException as exc:
        logger.warning("Notification failed: %s", exc)
