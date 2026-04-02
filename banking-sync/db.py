"""
db.py — SQLite persistence layer for Banking-Sync.
All schema creation and CRUD lives here so other modules stay clean.
"""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

_DB_PATH: str | None = None


def init_db(path: str) -> None:
    global _DB_PATH
    _DB_PATH = path
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    with get_conn() as conn:
        conn.executescript("""
            -- Active Enable Banking session token
            CREATE TABLE IF NOT EXISTS sessions (
                id              INTEGER PRIMARY KEY,
                access_token    TEXT    NOT NULL,
                authorization_id TEXT,
                expires_at      TEXT,
                created_at      TEXT    DEFAULT (datetime('now'))
            );

            -- Discovered bank accounts
            CREATE TABLE IF NOT EXISTS accounts (
                uid         TEXT PRIMARY KEY,
                iban        TEXT,
                currency    TEXT,
                name        TEXT,
                raw_json    TEXT,
                synced_at   TEXT DEFAULT (datetime('now'))
            );

            -- Transactions (booked + pending, deduplicated by hash)
            CREATE TABLE IF NOT EXISTS transactions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                hash            TEXT    UNIQUE NOT NULL,
                bank_id         TEXT,
                account_uid     TEXT    NOT NULL,
                amount          REAL    NOT NULL,
                currency        TEXT    NOT NULL,
                status          TEXT    NOT NULL,   -- 'booked' | 'pending'
                booking_date    TEXT,
                value_date      TEXT,
                debtor_name     TEXT,
                creditor_name   TEXT,
                reference       TEXT,
                description     TEXT,
                is_salary       INTEGER DEFAULT 0,
                raw_json        TEXT,
                created_at      TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY (account_uid) REFERENCES accounts(uid)
            );

            CREATE INDEX IF NOT EXISTS idx_tx_booking_date  ON transactions(booking_date DESC);
            CREATE INDEX IF NOT EXISTS idx_tx_account       ON transactions(account_uid);
            CREATE INDEX IF NOT EXISTS idx_tx_status        ON transactions(status);
            CREATE INDEX IF NOT EXISTS idx_tx_salary        ON transactions(is_salary);

            -- Registered webhook endpoints
            CREATE TABLE IF NOT EXISTS webhooks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url         TEXT    NOT NULL,
                events      TEXT    NOT NULL,   -- JSON array of event names
                secret      TEXT,
                active      INTEGER DEFAULT 1,
                created_at  TEXT    DEFAULT (datetime('now'))
            );

            -- Delivery log for debugging
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                webhook_id      INTEGER NOT NULL,
                event           TEXT    NOT NULL,
                payload         TEXT,
                response_code   INTEGER,
                error           TEXT,
                delivered_at    TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY (webhook_id) REFERENCES webhooks(id)
            );

            -- Per-account sync cursor
            CREATE TABLE IF NOT EXISTS sync_state (
                account_uid         TEXT    PRIMARY KEY,
                last_sync_at        TEXT,
                last_booked_date    TEXT
            );
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(_DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # safe concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Transaction helpers ────────────────────────────────────────────────────────

def make_tx_hash(amount, booking_date, debtor_name, creditor_name, reference) -> str:
    """
    Deterministic hash that survives a pending→booked state change where
    the bank may alter its own transaction ID.
    """
    composite = f"{amount}|{booking_date or ''}|{debtor_name or ''}|{creditor_name or ''}|{reference or ''}"
    return hashlib.sha256(composite.encode()).hexdigest()


def upsert_transaction(tx: dict) -> bool:
    """
    Insert a transaction if new; update status if it transitioned from pending→booked.
    Returns True if this is a brand-new transaction (for webhook firing).
    """
    h = tx["hash"]
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, status FROM transactions WHERE hash = ?", (h,)
        ).fetchone()

        if existing:
            if existing["status"] != tx["status"]:
                conn.execute(
                    "UPDATE transactions SET status=?, raw_json=? WHERE hash=?",
                    (tx["status"], tx.get("raw_json"), h),
                )
            return False

        conn.execute(
            """
            INSERT INTO transactions
              (hash, bank_id, account_uid, amount, currency, status,
               booking_date, value_date, debtor_name, creditor_name,
               reference, description, is_salary, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                tx["hash"], tx.get("bank_id"), tx["account_uid"],
                tx["amount"], tx["currency"], tx["status"],
                tx.get("booking_date"), tx.get("value_date"),
                tx.get("debtor_name"), tx.get("creditor_name"),
                tx.get("reference"), tx.get("description"),
                int(tx.get("is_salary", False)),
                tx.get("raw_json"),
            ),
        )
        return True


def get_transactions(
    account_uid: str | None = None,
    status: str | None = None,
    is_salary: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    with get_conn() as conn:
        q = "SELECT * FROM transactions WHERE 1=1"
        params: list = []
        if account_uid:
            q += " AND account_uid=?"; params.append(account_uid)
        if status:
            q += " AND status=?"; params.append(status)
        if is_salary is not None:
            q += " AND is_salary=?"; params.append(int(is_salary))
        q += " ORDER BY booking_date DESC, created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [dict(r) for r in conn.execute(q, params).fetchall()]


# ── Session helpers ────────────────────────────────────────────────────────────

def save_session(access_token: str, expires_at: str | None = None, authorization_id: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions")
        conn.execute(
            "INSERT INTO sessions (access_token, expires_at, authorization_id) VALUES (?,?,?)",
            (access_token, expires_at, authorization_id),
        )


def get_session() -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def clear_session() -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions")


# ── Account helpers ────────────────────────────────────────────────────────────

def save_account(acc: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO accounts (uid, iban, currency, name, raw_json) VALUES (?,?,?,?,?)",
            (acc["uid"], acc.get("iban"), acc.get("currency"), acc.get("name"), acc.get("raw_json")),
        )


def get_accounts() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM accounts").fetchall()]


# ── Sync state helpers ─────────────────────────────────────────────────────────

def update_sync_state(account_uid: str, last_booked_date: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO sync_state (account_uid, last_sync_at, last_booked_date)
            VALUES (?, datetime('now'), ?)
            """,
            (account_uid, last_booked_date),
        )


def get_sync_state(account_uid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sync_state WHERE account_uid=?", (account_uid,)
        ).fetchone()
        return dict(row) if row else None


# ── Webhook helpers ────────────────────────────────────────────────────────────

def get_webhooks(event: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if event:
            # JSON array contains the event name
            rows = conn.execute(
                "SELECT * FROM webhooks WHERE active=1 AND events LIKE ?",
                (f'%"{event}"%',),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM webhooks WHERE active=1").fetchall()
        return [dict(r) for r in rows]


def add_webhook(url: str, events: list[str], secret: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO webhooks (url, events, secret) VALUES (?,?,?)",
            (url, json.dumps(events), secret),
        )
        return cur.lastrowid


def delete_webhook(webhook_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE webhooks SET active=0 WHERE id=?", (webhook_id,))


def log_delivery(webhook_id: int, event: str, payload: str, response_code: int | None, error: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO webhook_deliveries (webhook_id, event, payload, response_code, error) VALUES (?,?,?,?,?)",
            (webhook_id, event, payload, response_code, error),
        )
