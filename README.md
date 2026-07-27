# lazeims-central-api

LAZEIMS Central API — FastAPI system of record (auth, registry, exams, collection,
station package/sync intake, controlled Excel, closeout/export, audit, notifications).

**Stack:** FastAPI · SQLAlchemy 2.0 (async) · asyncpg · Alembic · PostgreSQL 15+ ·
Pydantic v2. Shares all validation rules with the Station via the `lazeims-common`
package (never re-implemented here).

## Local setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ../lazeims-common      # shared rules (path dependency)
pip install -e ".[dev]"
cp .env.example .env                  # then edit as needed
```

Local PostgreSQL (rootless dev instance) is managed by `../pgctl.sh`:

```bash
../pgctl.sh start        # start server on 127.0.0.1:5432
../pgctl.sh psql lazeims # open a shell
```

Default dev credentials: `postgres` / `postgres`; databases `lazeims` (dev) and
`lazeims_test` (tests).

## Migrations

```bash
alembic upgrade head                                  # apply
alembic revision --autogenerate -m "describe change"  # new migration (review before applying!)
```

## Seed & run

```bash
python -m app.seed --admin superadmin adminpass123    # standing roles + a super admin
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- Liveness:  `GET /health`
- Readiness: `GET /readiness` (checks DB connectivity)
- OpenAPI:   `GET /api/v1/openapi.json` · docs at `/api/v1/docs`

## Tests

```bash
pytest        # runs against lazeims_test
```

## Auth model

Two entirely separate schemes (see `app/deps.py`):
- **Human** — signed, HttpOnly session cookie → server-side revocable session row;
  CSRF token required on state-changing requests.
- **Machine (station)** — `X-Station-Key` header, resolved in a separate dependency
  that can never reach human routes.

Authorization is one reusable layer (`require_role`, `require_geography_scope`);
every route declares its guard explicitly.
