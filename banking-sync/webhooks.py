"""
webhooks.py — Dispatch events to registered HTTP endpoints.

Endpoints are loaded from config.yaml at init time. No database, no runtime
registration — config.yaml is the single source of truth.

Payload structure for every event:
{
  "event":     "new_transaction" | "salary_detected" | "sync_completed" | "auth_required",
  "timestamp": "2025-01-15T14:30:00Z",
  "data":      { ... event-specific payload ... }
}

If an endpoint has a `secret` configured, the request also carries:
  X-Bank-Signature: <HMAC-SHA256 hex of the raw JSON body using the secret>
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10

_endpoints: list[dict] = []


def init(config: dict) -> None:
    """Load webhook endpoints from config.yaml."""
    global _endpoints
    _endpoints = config.get("webhooks", {}).get("endpoints", []) or []
    logger.info("Loaded %d webhook endpoint(s) from config", len(_endpoints))


def fire(event: str, data: dict) -> None:
    """
    Deliver payload to all endpoints subscribed to `event`.
    Failures are logged but never raise — sync must not be interrupted.
    """
    matching = [ep for ep in _endpoints if event in ep.get("events", [])]
    if not matching:
        return

    payload = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    body = json.dumps(payload, default=str)

    for endpoint in matching:
        _deliver(endpoint, event, body)


def _deliver(endpoint: dict, event: str, body: str) -> None:
    url = endpoint["url"]
    secret = endpoint.get("secret")

    headers = {"Content-Type": "application/json", "X-Bank-Event": event}
    if secret:
        sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        headers["X-Bank-Signature"] = sig

    try:
        resp = requests.post(url, data=body, headers=headers, timeout=TIMEOUT_SECONDS)
        logger.info("Webhook %s → %s: HTTP %s", event, url, resp.status_code)
    except requests.RequestException as exc:
        logger.warning("Webhook %s → %s failed: %s", event, url, exc)


# ── Convenience fire functions ────────────────────────────────────────────────

def fire_new_transaction(tx: dict) -> None:
    fire("new_transaction", tx)


def fire_salary_detected(tx: dict) -> None:
    fire("salary_detected", {"transaction": tx})


def fire_sync_completed(account_uid: str, new_count: int, total_fetched: int) -> None:
    fire("sync_completed", {
        "account_uid": account_uid,
        "new_transactions": new_count,
        "total_fetched": total_fetched,
    })


def fire_auth_required() -> None:
    fire("auth_required", {
        "message": "Enable Banking session has expired. Visit /auth/start to re-authenticate.",
    })
