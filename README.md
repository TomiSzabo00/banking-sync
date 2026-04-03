> [!IMPORTANT]
> This whole repo was vibecoded so read the code before you use it. I suggest starting with a sandbox or dev enviromnent where you can't cause any harm in your or your bank's systems. Use this code at your own risk.

# Banking-Sync

A self-hosted service that connects to your bank account through the [Enable Banking](https://enablebanking.com) API, automatically syncs transactions on a schedule, and notifies you via webhooks when events like salary payments are detected.

## Features

- **Automatic transaction sync** — polls your bank up to 4 times per day (the Enable Banking maximum) on a configurable schedule
- **Salary detection** — flags incoming transactions from configured sender names and fires a webhook
- **Webhook notifications** — receive HTTP POST callbacks for new transactions, salary detection, sync completion, and session expiry
- **HMAC-signed webhooks** — optionally sign webhook payloads with a shared secret for verification
- **REST API** — query transactions, accounts, sync status, and manage webhooks programmatically
- **SQLite storage** — zero-dependency persistence with WAL mode for safe concurrent reads
- **Deduplication** — content-based hashing survives pending-to-booked state transitions without creating duplicates
- **Systemd integration** — runs as a system service with automatic restart on failure

## Prerequisites

- Python 3.10+
- A Linux host with systemd (VM, LXC container, VPS, etc.)
- An [Enable Banking](https://enablebanking.com) application (free sandbox for testing, but also free personal usage available)

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/TomiSzabo00/banking-sync.git
cd banking-sync

# 2. Edit the config
nano banking-sync/config.yaml

# 3. Run the setup script (creates venv, installs deps, starts systemd service)
./start.sh

# 4. Authenticate with your bank (open in browser)
#    The URL is printed in the logs, or just visit:
http://<YOUR_HOST_IP>:8080/auth/start
```

After authenticating, the service begins syncing automatically. Sessions are valid for up to 90 days, after which you'll need to re-authenticate.

### Docker

```bash
# 1. Clone the repo
git clone https://github.com/TomiSzabo00/banking-sync.git
cd banking-sync

# 2. Edit the config — set private_key_path to "/app/private.pem"
nano banking-sync/config.yaml

# 3. Place your private key in the repo root
cp /path/to/your/private.pem ./private.pem

# 4. Start the container
docker compose up -d

# 5. Authenticate (open in browser)
http://<YOUR_HOST_IP>:8080/auth/start
```

The compose file mounts `config.yaml` and `private.pem` as read-only, and persists `data/` and `logs/` in named Docker volumes. The container restarts automatically on failure or host reboot.

### Proxmox LXC

If you run Proxmox, there's a script that creates a dedicated LXC container with everything pre-installed:

> [!TIP]
> You can copy the checked out repository from your computer to your Proxmox host with `scp -r banking-sync user@<proxmox-host ip>:/path/to/destination`

```bash
# 1. Edit the variables at the top of the script (CT_ID, CT_IP, CT_GW, etc.)
nano proxmox-create-lxc.sh

# 2. Run it on the Proxmox HOST
./proxmox-create-lxc.sh

# 3. Edit config and push your private key (commands printed by the script)
# 4. Start the service and authenticate
```

The script creates a minimal Debian 12 container, installs Python, sets up a dedicated service user, and enables the systemd service. It does **not** start the service — you need to edit the config and push your private key first.

## Configuration

All configuration lives in `banking-sync/config.yaml`. Edit it before running `start.sh`.

### Enable Banking (required)

```yaml
enable_banking:
  application_id: "YOUR_APPLICATION_ID"  # Application UUID from the Enable Banking dashboard
  private_key_path: "/path/to/private.pem" # RSA private key downloaded during app registration
  base_url: "https://api.enablebanking.com"
  aspsp_name: "YOUR_BANK_NAME"           # Bank name as listed in Enable Banking docs
  country: "YOUR_COUNTRY_CODE"           # ISO 3166-1 alpha-2 (e.g. DE, RO, FI)
  redirect_url: "http://YOUR_IP:8080/callback"
```

Some banks require additional credentials during the auth flow. If yours does, uncomment and fill in:

```yaml
  credentials:
    userId: "your-user-id"
    iban: "YOUR_IBAN"
    currencyCode: "YOUR_CURRENCY"
```

### Sync schedule

```yaml
sync:
  timezone: "Europe/Berlin"    # Your local timezone (IANA format)
  default_currency: "EUR"      # Fallback when the bank doesn't provide one
  initial_lookback_days: 30    # How far back to fetch on first run
```

The service syncs at **08:00, 13:30, 18:30, and 23:59** in your configured timezone. These times are hardcoded to stay within the 4-request daily limit imposed by Enable Banking.

### Salary detection

```yaml
salary_detection:
  debtor_names:
    - "ACME Corp"          # Case-insensitive substring match against the sender name
    - "My Employer GmbH"
```

### Webhooks

```yaml
webhooks:
  endpoints:
    - url: "https://your-server.com/webhook"
      events:
        - salary_detected
        - new_transaction
      secret: "optional-hmac-secret"  # Adds X-Bank-Signature header
```

Webhooks can also be managed at runtime via the API (see below).

### Other settings

| Key | Default | Description |
|-----|---------|-------------|
| `database.path` | `./data/transactions.db` | SQLite database location |
| `server.host` | `0.0.0.0` | Flask bind address |
| `server.port` | `8080` | Flask port |
| `server.secret_key` | `changeme` | Flask session secret |

## API Reference

### Authentication

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/auth/start` | Initiates the OAuth flow — open in a browser |
| `GET` | `/callback` | OAuth callback (handled automatically by the bank redirect) |
| `GET` | `/auth/status` | Check if a session is active and when it expires |

### Transactions

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/transactions` | List transactions (see query params below) |
| `GET` | `/api/accounts` | List discovered bank accounts |
| `GET` | `/api/sync/status` | Last sync timestamp per account |
| `POST` | `/api/sync/run` | Manually trigger a sync cycle |

**Transaction query parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `account` | string | Filter by account UID |
| `status` | string | `booked` or `pending` |
| `salary` | string | `true` or `false` |
| `limit` | int | Max results (default 100, max 500) |
| `offset` | int | Pagination offset |

### Webhooks

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/webhooks` | List registered webhooks |
| `POST` | `/api/webhooks` | Register a new webhook |
| `DELETE` | `/api/webhooks/<id>` | Remove a webhook |

**Create webhook body:**

```json
{
  "url": "https://example.com/hook",
  "events": ["salary_detected", "new_transaction"],
  "secret": "optional-hmac-key"
}
```

### Other

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Liveness probe (`{"status": "ok"}`) |
| `POST` | `/api/debug/inject-transaction` | Inject a fake transaction for testing |

## Webhook Events

Every webhook payload follows this structure:

```json
{
  "event": "event_name",
  "timestamp": "2025-01-15T14:30:00+00:00",
  "data": { }
}
```

| Event | Fires when | Data |
|-------|-----------|------|
| `new_transaction` | A new transaction is synced | Transaction object |
| `salary_detected` | A transaction matches salary rules | `{"transaction": {...}}` |
| `sync_completed` | A sync cycle finishes | `{"account_uid", "new_transactions", "total_fetched"}` |
| `auth_required` | The session has expired | `{"message": "..."}` |

If a `secret` is configured, each request includes an `X-Bank-Signature` header containing the HMAC-SHA256 hex digest of the raw JSON body.

## Architecture

```
banking-sync/
  app.py                  # Entry point — Flask server + APScheduler
  api.py                  # REST API routes
  sync.py                 # Transaction polling, normalization, deduplication
  enablebanking_client.py # Enable Banking HTTP client (JWT auth)
  db.py                   # SQLite persistence layer
  webhooks.py             # Webhook dispatch engine
  config.yaml             # Configuration (edit this)
  requirements.txt        # Python dependencies
```

**Data flow:** APScheduler triggers `sync.py` on schedule. It fetches transactions via `enablebanking_client.py`, normalizes and deduplicates them, persists to SQLite via `db.py`, and fires webhook events via `webhooks.py`. The Flask server in `api.py` handles the OAuth flow and exposes the REST API.

## Managing the Service

```bash
# Check status
sudo systemctl status banking-sync

# View logs (live)
sudo journalctl -u banking-sync -f

# Restart after config changes
sudo systemctl restart banking-sync

# Stop the service
sudo systemctl stop banking-sync
```

Application logs are also written to `banking-sync/logs/app.log`.

## Re-authentication

Enable Banking sessions expire after 90 days. When this happens:

1. The `auth_required` webhook fires (if configured)
2. The service logs a warning and skips sync cycles
3. Visit `http://<YOUR_HOST_IP>:8080/auth/start` in a browser to re-authenticate

## License

[MIT](LICENSE)
