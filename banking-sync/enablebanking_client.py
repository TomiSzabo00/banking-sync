"""
enablebanking_client.py — HTTP client for the Enable Banking REST API.

Authentication:
  Enable Banking uses application-level JWT tokens signed with your RSA private
  key (RS256). Each request gets a short-lived token generated on the fly.

  JWT claims:
    iss  — your application_id (UUID from Enable Banking dashboard)
    iat  — issued-at (UTC epoch)
    exp  — expiry (iat + TOKEN_TTL_SECONDS)

  All requests:  Authorization: Bearer <jwt>
"""
import logging
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import jwt          # PyJWT
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.enablebanking.com"
TOKEN_TTL_SECONDS = 3600


class EnableBankingError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class EnableBankingClient:
    def __init__(self, application_id: str, private_key_path: str):
        """
        application_id          — your application UUID from the Enable Banking dashboard
        private_key_path — path to the downloaded PEM file
        """
        self.application_id = application_id
        self._private_key = Path(private_key_path).read_text()
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ── JWT ───────────────────────────────────────────────────────────────────

    def _make_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": "enablebanking.com",
                "aud": "api.enablebanking.com",
                "iat": now,
                "exp": now + TOKEN_TTL_SECONDS,
            },
            self._private_key,
            algorithm="RS256",
            headers={"kid": self.application_id, "typ": "JWT"},
        )

    def _app_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._make_jwt()}"}

    def _session_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._make_jwt()}",
        }

    # ── Auth flow ─────────────────────────────────────────────────────────────

    def initiate_auth(self, aspsp_name: str, country: str, redirect_url: str,
                      credentials: dict | None = None) -> dict:
        payload = {
            "access": {
                "valid_until": (datetime.utcnow() + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            },
            "aspsp": {"name": aspsp_name, "country": country},
            "state": str(uuid.uuid4()),
            "redirect_url": redirect_url,
            "psu_type": "personal",
        }
        if credentials:
            payload["credentials"] = credentials
        return self._post("/auth", payload, headers=self._app_headers())

    def exchange_code_for_session(self, authorization_code: str) -> dict:
        return self._post("/sessions", {"code": authorization_code}, headers=self._app_headers())

    # ── Accounts ──────────────────────────────────────────────────────────────

    def get_accounts(self, access_token: str) -> list[dict]:
        resp = self._get("/accounts", headers=self._session_headers())
        if isinstance(resp, dict):
            return resp.get("accounts", resp.get("data", []))
        return resp

    # ── Transactions ──────────────────────────────────────────────────────────

    def get_transactions(
        self,
        access_token: str,
        account_uid: str,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> dict:
        """
        Fetch all transactions for the given date range, following
        continuation_key pagination until exhausted.
        """
        params: dict = {}
        if date_from:
            params["date_from"] = date_from.isoformat()
        if date_to:
            params["date_to"] = date_to.isoformat()

        all_transactions: list[dict] = []
        page = 0

        while True:
            resp = self._get(
                f"/accounts/{account_uid}/transactions",
                headers=self._session_headers(),
                params=params,
            )

            txs = resp.get("transactions", []) if isinstance(resp, dict) else []
            all_transactions.extend(txs)
            page += 1

            continuation_key = resp.get("continuation_key") if isinstance(resp, dict) else None
            if not continuation_key:
                break

            logger.debug("Continuation key received (page %d, %d txs so far)", page, len(all_transactions))
            params["continuation_key"] = continuation_key

        logger.info("Fetched %d transaction(s) across %d page(s) for account %s", len(all_transactions), page, account_uid)
        return {"transactions": all_transactions}

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict, headers: dict) -> dict:
        resp = self._session.post(BASE_URL + path, json=payload, headers=headers, timeout=30)
        return self._handle_response(resp)

    def _get(self, path: str, headers: dict, params: dict | None = None) -> dict | list:
        resp = self._session.get(BASE_URL + path, headers=headers, params=params, timeout=30)
        return self._handle_response(resp)

    @staticmethod
    def _handle_response(resp: requests.Response) -> dict | list:
        if resp.status_code == 429:
            raise EnableBankingError(429, "Rate limit exceeded")
        if resp.status_code == 401:
            raise EnableBankingError(401, "Unauthorized — check application_id and private key")
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise EnableBankingError(resp.status_code, f"API error {resp.status_code}: {detail}")
        return resp.json()
