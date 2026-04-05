"""
sync.py — Stateless transaction fetching, normalization, and webhook firing.

Two modes:
  • run_sync()     — fetches today's transactions only (used by auto-sync)
  • run_backfill() — fetches from a given date to today (manual trigger)

No database, no deduplication. Every fetched transaction fires a webhook.
Consumers are responsible for dedup using the `tx_hash` field in the payload.
"""
import hashlib
import json
import logging
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
    default_currency = config.get("sync", {}).get("default_currency", "EUR")

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
                salary_names, default_currency,
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
    default_currency: str = "EUR",
) -> tuple[int, int]:
    """
    Fetch, normalize, and fire webhooks for all transactions in the date range.
    Returns (webhooks_fired_count, total_fetched_count).
    """
    raw = client.get_transactions(access_token, account_uid, date_from=date_from, date_to=date_to)

    all_txs = raw.get("transactions", [])
    booked_txs = [t for t in all_txs if t.get("status") == "BOOK"]
    pending_txs = [t for t in all_txs if t.get("status") == "PDNG"]
    total_fetched = len(booked_txs) + len(pending_txs)

    fired_count = 0

    for raw_tx in booked_txs + pending_txs:
        status = "booked" if raw_tx.get("status") == "BOOK" else "pending"
        tx = _normalize_tx(raw_tx, account_uid, status=status, default_currency=default_currency)
        tx["is_salary"] = _is_salary(tx, salary_names)
        webhooks.fire_new_transaction(tx)
        fired_count += 1
        if tx["is_salary"]:
            webhooks.fire_salary_detected(tx)

    return fired_count, total_fetched


def _normalize_tx(raw: dict, account_uid: str, status: str, default_currency: str = "EUR") -> dict:
    """
    Map the Enable Banking transaction JSON to our internal schema.
    Enable Banking follows the Berlin Group / NextGenPSD2 naming conventions.
    """
    amount_data = raw.get("transaction_amount", raw.get("transactionAmount", {}))
    amount = float(amount_data.get("amount", 0))
    currency = amount_data.get("currency", default_currency)

    booking_date = (
        raw.get("booking_date")
        or raw.get("bookingDate")
        or raw.get("value_date")
        or raw.get("valueDate")
    )
    value_date = raw.get("value_date") or raw.get("valueDate")

    debtor = raw.get("debtor") or {}
    creditor = raw.get("creditor") or {}

    debtor_name = debtor.get("name") or ""
    creditor_name = creditor.get("name") or ""

    reference = (
        raw.get("remittance_information", [None])[0]
        or raw.get("entry_reference")
        or ""
    )

    description = reference

    bank_id = raw.get("transaction_id") or raw.get("transactionId") or raw.get("entry_reference")

    tx_hash = _make_tx_hash(amount, booking_date, debtor_name, creditor_name, reference)

    return {
        "tx_hash": tx_hash,
        "bank_id": bank_id,
        "account_uid": account_uid,
        "amount": amount,
        "currency": currency,
        "status": status,
        "booking_date": booking_date,
        "value_date": value_date,
        "debtor_name": debtor_name or None,
        "creditor_name": creditor_name or None,
        "reference": reference or None,
        "description": description or None,
        "raw_json": json.dumps(raw),
    }


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
    debtor = (tx.get("debtor_name") or "").lower()
    return any(name in debtor for name in salary_names)


def _make_tx_hash(amount, booking_date, debtor_name, creditor_name, reference) -> str:
    """Deterministic hash for consumer-side deduplication."""
    composite = f"{amount}|{booking_date or ''}|{debtor_name or ''}|{creditor_name or ''}|{reference or ''}"
    return hashlib.sha256(composite.encode()).hexdigest()
