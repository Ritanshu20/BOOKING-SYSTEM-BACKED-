# Local Setup (Windows 11)

These are the exact steps used to set up this project on Windows 11 with Python 3.13.
All commands are for **PowerShell**, run from the project root unless stated otherwise.

## 1. Prerequisites

| Tool | Used for | Check |
|---|---|---|
| Python 3.13 (3.12 minimum) | Django | `python --version` |
| Git | version control | `git --version` |
| PostgreSQL 18 | database (dev, tests, production) | see step 2 |

Docker is not required.

## 2. PostgreSQL (portable binaries, no admin rights)

This machine uses the official PostgreSQL Windows binaries zip from EnterpriseDB
(the "binaries" download linked from postgresql.org). It installs per user and needs
no administrator prompt. The server runs as your Windows user and is not a Windows
service, so you start it yourself after a reboot (step 2.4).

Paths used below:

| What | Path |
|---|---|
| Binaries | `%LOCALAPPDATA%\Programs\PostgreSQL18\pgsql\bin` |
| Data directory | `%LOCALAPPDATA%\PostgreSQL18\data` |
| Server log | `%LOCALAPPDATA%\PostgreSQL18\server.log` |
| Superuser password file | `%LOCALAPPDATA%\PostgreSQL18\superuser-password.txt` |

### 2.1 Download and extract

1. Download "Windows x86-64" for PostgreSQL 18.x from
   <https://www.enterprisedb.com/download-postgresql-binaries>.
2. Extract it so that `%LOCALAPPDATA%\Programs\PostgreSQL18\pgsql\bin\postgres.exe` exists.

```powershell
$bin = "$env:LOCALAPPDATA\Programs\PostgreSQL18\pgsql\bin"
& "$bin\postgres.exe" --version
```

### 2.2 Create the database cluster (once)

Write a strong superuser password into the password file first (any editor, one line,
no trailing spaces). `initdb` reads it from there, so it never appears on screen.

```powershell
$pg = "$env:LOCALAPPDATA\PostgreSQL18"
& "$bin\initdb.exe" -D "$pg\data" -U postgres --pwfile="$pg\superuser-password.txt" `
    -A scram-sha-256 -E UTF8 --locale-provider=builtin --builtin-locale=C.UTF-8 --locale=C
```

### 2.3 Start and stop the server

```powershell
& "$bin\pg_ctl.exe" -D "$pg\data" -l "$pg\server.log" -o "-p 5432" -w start
& "$bin\pg_ctl.exe" -D "$pg\data" status
& "$bin\pg_ctl.exe" -D "$pg\data" stop
```

The server listens on `localhost:5432` only.

### 2.4 After a reboot

Run only the `start` command from step 2.3.

### 2.5 Create the app role and database (once)

The application uses a dedicated role that is **not** a superuser. It has `CREATEDB`
so Django's test runner can create and drop `test_booking`.

```powershell
& "$bin\psql.exe" -h localhost -U postgres -d postgres
```

Type the superuser password at the prompt, then run (choose your own password):

```sql
CREATE ROLE booking_app LOGIN CREATEDB NOSUPERUSER NOCREATEROLE PASSWORD 'choose-a-password';
CREATE DATABASE booking OWNER booking_app ENCODING 'UTF8' TEMPLATE template0;
\q
```

Verify the app role can connect:

```powershell
& "$bin\psql.exe" "postgres://booking_app:<password>@localhost:5432/booking" -c "select current_user;"
```

### 2.6 Reset the database (if needed)

```powershell
& "$bin\psql.exe" -h localhost -U postgres -d postgres -c "DROP DATABASE IF EXISTS booking;"
& "$bin\psql.exe" -h localhost -U postgres -d postgres -c "CREATE DATABASE booking OWNER booking_app ENCODING 'UTF8' TEMPLATE template0;"
.\.venv\Scripts\python manage.py migrate
```

## 3. Python virtual environment

Every Python package is installed **only** inside `.venv` (never globally).

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # optional; or call .\.venv\Scripts\python directly
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
```

If PowerShell blocks `Activate.ps1`, skip activation and call `.\.venv\Scripts\python`
directly, as every command in this file does.

## 4. Environment variables

```powershell
Copy-Item .env.example .env
```

Edit `.env` and replace every `<placeholder>`:

| Variable | Meaning |
|---|---|
| `SECRET_KEY` | Django secret key (long random string) |
| `DEBUG` | `True` for local development |
| `ALLOWED_HOSTS` | Comma-separated host names |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated origins for template-page forms |
| `DATABASE_URL` | `postgres://booking_app:<password>@localhost:5432/booking` |
| `HOLD_TTL_MINUTES` | Minutes a seat hold stays valid (default 10) |
| `PAYMENT_WEBHOOK_SECRET` | Secret used to sign payment webhooks |
| `NUM_PROXIES` | Reverse proxies in front of the app (default 0); throttling ignores `X-Forwarded-For` beyond this |
| `SECURE_HSTS_PRELOAD` | Production only, default `False`; opt in to HSTS preloading |
| `LOG_LEVEL` | Optional, default `INFO` |

Production settings (`config.settings.production`) refuse to start if `SECRET_KEY` is
shorter than 50 characters, `PAYMENT_WEBHOOK_SECRET` is shorter than 32, or
`ALLOWED_HOSTS` contains `*`.

Generate random secrets with:

```powershell
.\.venv\Scripts\python -c "import secrets; print(secrets.token_hex(32))"
```

## 5. Migrate, check and test

```powershell
.\.venv\Scripts\python manage.py check
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py test
.\.venv\Scripts\ruff check .
.\.venv\Scripts\ruff format --check .
```

A successful test run prints `Creating test database for alias 'default' ('test_booking')`
and `Destroying test database ...`, which proves the app role can create the test database.

## 6. Run the development server

```powershell
.\.venv\Scripts\python manage.py createsuperuser
.\.venv\Scripts\python manage.py runserver
```

Health check: <http://127.0.0.1:8000/api/health/>
