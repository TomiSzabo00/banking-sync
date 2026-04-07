"""
sync.py — Stateless transaction fetching and webhook firing.

Two modes:
  • run_sync()     — fetches today's transactions only (used by auto-sync)
  • run_backfill() — fetches from a given date to today (manual trigger)

No database, no deduplication. Every fetched transaction fires a webhook.
Consumers are responsible for dedup using the `tx_hash` field in the payload.

Transaction payloads preserve the original Enable Banking / Berlin Group
schema with keys normalised to snake_case. Extra fields (tx_hash, account_uid,
is_salary) are added on top — nothing is renamed, flattened, or dropped.
"""
import hashlib
import logging
import re
from datetime import date

import session_store
import webhooks
from enablebanking_client import EnableBankingClient, EnableBankingError

logger = logging.getLogger(__name__)


def run_sync(config: dict) -> dict:
    """
    Fetch today's transactions for all known accounts and fire webhooks.
    Returns a summary dict.
    """
    return _run(config, date_from=date.today(), date_to=date.today())


def run_backfill(config: dict, date_from: date | None = None) -> dict:
    """
    Fetch transactions from `date_from` (default: config backfill_from) to today.
    Fires webhooks for every transaction found.
    """
    if date_from is None:
        date_from = date.fromisoformat(
            config.get("sync", {}).get("backfill_from", "2025-01-01")
        )
    return _run(config, date_from=date_from, date_to=date.today())


def _run(config: dict, date_from: date, date_to: date) -> dict:
    session = session_store.get_session()
    if not session:
        logger.warning("No active session — re-authentication required")
        webhooks.fire_auth_required()
        return {"error": "no_session", "message": "Re-authentication required. Visit /auth/start"}

    client = EnableBankingClient(
        application_id=config["enable_banking"]["application_id"],
        private_key_path=config["enable_banking"]["private_key_path"],
    )
    access_token = session["access_token"]
    salary_names = [n.lower() for n in config.get("salary_detection", {}).get("debtor_names", [])]

    accounts = session_store.get_accounts()
    if not accounts:
        logger.info("No accounts stored — fetching from Enable Banking...")
        try:
            raw_accounts = client.get_accounts(access_token)
            accounts = [_normalize_account(raw) for raw in raw_accounts]
            session_store.save_accounts(accounts)
            logger.info("Discovered %d account(s)", len(accounts))
        except EnableBankingError as exc:
            if exc.status_code == 401:
                session_store.clear_session()
                webhooks.fire_auth_required()
                return {"error": "session_expired"}
            return {"error": str(exc)}

    summary = {"accounts_synced": 0, "transactions_fired": 0, "errors": []}

    for account in accounts:
        uid = account["uid"]
        try:
            count, fetched = _sync_account(
                client, access_token, uid, date_from, date_to,
                salary_names,
            )
            summary["accounts_synced"] += 1
            summary["transactions_fired"] += count
            webhooks.fire_sync_completed(uid, count, fetched)
            logger.info("Account %s — %d transactions fired (%d fetched)", uid, count, fetched)
        except EnableBankingError as exc:
            logger.error("Sync error for account %s: %s", uid, exc)
            if exc.status_code == 401:
                session_store.clear_session()
                webhooks.fire_auth_required()
                return {"error": "session_expired"}
            summary["errors"].append({"account_uid": uid, "error": str(exc)})

    return summary


def _sync_account(
    client: EnableBankingClient,
    access_token: str,
    account_uid: str,
    date_from: date,
    date_to: date,
    salary_names: list[str],
) -> tuple[int, int]:
    """
    Fetch, enrich, and fire webhooks for all transactions in the date range.
    Returns (webhooks_fired_count, total_fetched_count).
    """
    raw = client.get_transactions(access_token, account_uid, date_from=date_from, date_to=date_to)

    all_txs = raw.get("transactions", [])
    booked_txs = [t for t in all_txs if t.get("status") == "BOOK"]
    pending_txs = [t for t in all_txs if t.get("status") == "PDNG"]
    total_fetched = len(booked_txs) + len(pending_txs)

    fired_count = 0

    for raw_tx in booked_txs + pending_txs:
        tx = _enrich_tx(raw_tx, account_uid)
        tx["is_salary"] = _is_salary(tx, salary_names)
        webhooks.fire_new_transaction(tx)
        fired_count += 1
        if tx["is_salary"]:
            webhooks.fire_salary_detected(tx)

    return fired_count, total_fetched


def _camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def _normalize_keys(obj):
    """Recursively normalise dict keys from camelCase to snake_case."""
    if isinstance(obj, dict):
        return {_camel_to_snake(k): _normalize_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_keys(item) for item in obj]
    return obj


def _enrich_tx(raw: dict, account_uid: str) -> dict:
    """
    Normalise keys to snake_case and add enrichment fields (tx_hash, account_uid).
    The original Enable Banking / Berlin Group structure is preserved as-is.
    """
    tx = _normalize_keys(raw)
    tx["account_uid"] = account_uid
    tx["tx_hash"] = _make_tx_hash(tx)
    return tx


def _normalize_account(raw: dict) -> dict:
    uid = raw.get("uid") or raw.get("resource_id") or raw.get("resourceId") or raw.get("id")
    iban_data = raw.get("account_id", raw.get("accountId", {})) or {}
    return {
        "uid": uid,
        "iban": iban_data.get("iban") or raw.get("iban"),
        "currency": raw.get("currency"),
        "name": raw.get("name") or raw.get("product"),
    }


def _is_salary(tx: dict, salary_names: list[str]) -> bool:
    if not salary_names:
        return False
    debtor = ((tx.get("debtor") or {}).get("name") or "").lower()
    return any(name in debtor for name in salary_names)


def _make_tx_hash(tx: dict) -> str:
    """Deterministic hash for consumer-side deduplication."""
    amount = (tx.get("transaction_amount") or {}).get("amount", 0)
    booking_date = tx.get("booking_date") or tx.get("value_date") or ""
    debtor_name = (tx.get("debtor") or {}).get("name") or ""
    creditor_name = (tx.get("creditor") or {}).get("name") or ""
    remittance = tx.get("remittance_information") or []
    reference = remittance[0] if remittance else ""
    composite = f"{amount}|{booking_date}|{debtor_name}|{creditor_name}|{reference}"
    return hashlib.sha256(composite.encode()).hexdigest()
