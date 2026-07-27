# Self-Hosting LAZEIMS

This guide deploys a LAZEIMS **Central** zone deployment (FastAPI API + Next.js web)
on a single small VPS behind Nginx with TLS. The offline **Station** is not
hosted — it runs on ordinary PCs at marking centres (see `lazeims-station`).

> **Baseline:** 1 VPS, 2 vCPU / 4 GB RAM, current Ubuntu LTS. This stack uses
> long-running processes (Uvicorn + Node), so it is **not** classic shared/cPanel
> hosting. Cost is typically $5–20/month.

The four public repos:

| Repo | Role |
|---|---|
| `lazeims-common` | Shared validation rules + contracts (installed by API + Station) |
| `lazeims-central-api` | FastAPI backend (this repo) |
| `lazaims` | Next.js web app |
| `lazeims-station` | Offline station kit (packaged per exam, not hosted) |

---

## 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv postgresql nginx
# Node.js LTS (for the web app):
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
```

## 2. PostgreSQL

```bash
sudo -u postgres psql -c "CREATE ROLE lazeims LOGIN PASSWORD '<STRONG_PASSWORD>';"
sudo -u postgres psql -c "CREATE DATABASE lazeims OWNER lazeims;"
```

Automated encrypted backups (cron, nightly):

```bash
# /etc/cron.d/lazeims-backup
0 2 * * * lazeims pg_dump -Fc lazeims | gpg -c --batch --passphrase-file /etc/lazeims/backup.key > /var/backups/lazeims/lazeims-$(date +\%F).dump.gpg
```

## 3. Backend (lazeims-central-api)

```bash
cd /opt/lazeims/lazeims-central-api
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ../lazeims-common
pip install -e .
cp .env.example .env      # then fill in real values (see §6)
alembic upgrade head
python -m app.seed --admin <admin_username> '<admin_password>'   # standing roles + first admin
```

Run under systemd (`/etc/systemd/system/lazeims-api.service`):

```ini
[Unit]
Description=LAZEIMS Central API
After=network.target postgresql.service

[Service]
User=lazeims
WorkingDirectory=/opt/lazeims/lazeims-central-api
EnvironmentFile=/opt/lazeims/lazeims-central-api/.env
ExecStart=/opt/lazeims/lazeims-central-api/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## 4. Web app (lazaims)

```bash
cd /opt/lazeims/lazaims
npm ci
echo "NEXT_PUBLIC_API_BASE_URL=https://exams.example.org/api/v1" > .env.local
npm run build
```

systemd (`/etc/systemd/system/lazeims-web.service`):

```ini
[Unit]
Description=LAZEIMS Web
After=network.target

[Service]
User=lazeims
WorkingDirectory=/opt/lazeims/lazaims
ExecStart=/usr/bin/npx next start -p 3000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now lazeims-api lazeims-web
```

## 5. Nginx reverse proxy (same-origin) + TLS

`/etc/nginx/sites-available/lazeims`:

```nginx
server {
    listen 80;
    server_name exams.example.org;
    location /api/ { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme; }
    location /     { proxy_pass http://127.0.0.1:3000; proxy_set_header Host $host; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/lazeims /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d exams.example.org   # TLS; enforce HSTS after verifying
```

Because API and web are same-origin behind Nginx, the session cookie and CSRF
work without cross-origin CORS. If you split origins, set
`ALLOWED_CORS_ORIGINS` and serve both over HTTPS.

## 6. Environment (`.env`) — all keys required

See `.env.example`. Generate real secrets:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # SESSION_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(48))"   # STATION_PACKAGE_INTEGRITY_KEY
```

Set `APP_ENV=production` — the app then refuses to start with placeholder secrets
or the default `postgres:postgres` credentials.

## 7. Go-live verification

- `GET /health` and `/readiness` return ok
- Log in as the seeded admin; create an exam, attach schools/subjects, register students
- Set a `ScopeWriteAssignment`, open entry, enter marks online, finalize a scope
- Register a station, download a scope-only package, run it offline, sync, reconcile to MATCHED
- Seal a collection snapshot and download the export

## 8. Backup / restore rehearsal

```bash
pg_dump -Fc lazeims -f /tmp/lazeims.dump                 # backup
createdb lazeims_restore && pg_restore -d lazeims_restore /tmp/lazeims.dump   # restore
# verify row counts match, then drop the restore db
```

Station backups use SQLite's online backup API (`station/backup.py`) — rolling
snapshots, and a pre-upgrade snapshot is taken automatically before any schema
migration so marks/outbox are never at risk.

## 9. Upgrades

- **Central:** `git pull` → `pip install -e .` → `alembic upgrade head` (migrations
  are additive and reversible) → restart the systemd units.
- **Station:** ship a new build; the launcher runs `apply_migrations`, which backs
  up first and applies additive migrations without discarding marks/outbox.

## Excluded

This deployment contains **no** results-processing integration (`backend-sis`),
no Processing API key, and no processing/API-key UI. Collection, sync, closeout,
and export are fully self-contained.
