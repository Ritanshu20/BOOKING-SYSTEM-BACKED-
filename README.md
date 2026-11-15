# Booking Backend

A seat and ticket booking system (a small BookMyShow-style platform) built with Django,
Django REST Framework and PostgreSQL. Customers hold seats for ten minutes, pay through a
test-mode payment provider and cancel for a tiered refund; organizers publish events,
cancel them with automatic refunds and follow sales.

The point of the project is **correctness under concurrency**: two people can never get
the same seat, a payment webhook delivered ten times confirms a booking once, and a
refund is never paid twice. Every guarantee is enforced by PostgreSQL and proven by tests
that run real concurrent transactions.

**347 automated tests** (including 13 multi-threaded concurrency test classes) run on
PostgreSQL in CI on every push.

## Features

**Customers**
- Browse published events with search, date filters and pagination.
- Pick seats on an accessible seat map (available / held / booked, live refresh).
- Hold up to N seats for `HOLD_TTL_MINUTES` (10), with a countdown.
- Pay in test mode (HMAC-signed webhook), with optional promo codes.
- Cancel with a refund preview: 100% at 48 h or more, 50% from 24 h, 0% under 24 h.
- "My bookings" with status badges and totals.

**Organizers**
- Create and edit events, generate seat grids with price tiers, publish and unpublish.
- Cancel an event: every booking is cancelled and every paid booking refunded 100%, in one
  transaction, after a confirmation page that shows exactly what will happen.
- Sales reports: sold / held / available, occupancy, gross / refunded / net revenue,
  bookings per day, top events.

**Platform**
- REST API with JWT (access + rotating refresh tokens), Swagger at `/api/docs/`, one error
  format for everything: `{"error": {"code", "message", "details"}}`.
- `Idempotency-Key` support for hold and pay; throttling on auth, hold and pay.
- Server-rendered pages (Django templates, Bootstrap 5, dark mode, keyboard and
  screen-reader friendly), sharing every service with the API.

## Tech stack

| Area | Choice |
|---|---|
| Language | Python 3.13 |
| Framework | Django 5.2 LTS, Django REST Framework 3.18, `djangorestframework-simplejwt`, `drf-spectacular` |
| Database | PostgreSQL 18 (development, tests and production; never SQLite) |
| Frontend | Django templates, Bootstrap 5.3.8 and Bootstrap Icons (CDN with SRI), four small vanilla JS files |
| Quality | Ruff (lint and format), `pip-audit`, `manage.py check --deploy`, GitHub Actions CI with a PostgreSQL service |

Why Django 5.2 and not 6.1: `djangorestframework-simplejwt` does not yet declare Django 6
support ([docs/DECISIONS.md](docs/DECISIONS.md), D1).

## Architecture

```mermaid
flowchart LR
    Browser["Browser pages<br/>(views.py, templates)"] --> Services
    Client["API clients<br/>(api_views.py, serializers)"] --> Services
    Provider["Payment provider<br/>(signed webhook)"] --> Services
    Services["services.py / selectors.py<br/>business rules, one place"] --> DB[("PostgreSQL<br/>row locks + constraints")]
```

| App | Responsibility |
|---|---|
| `accounts` | custom user (email login, role), JWT auth, permissions, ownership filter |
| `events` | events, seats, derived seat status, organizer pages, reports |
| `bookings` | holds, the booking state machine, expiry, cancellations, refund policy, promo codes |
| `payments` | test-mode provider, payments, idempotent webhooks, refunds |
| `core` | clock, errors, pagination, throttling, idempotency keys, test helpers |

Views stay thin: the API (`<app>/api_views.py`) and the pages (`<app>/views.py`) both call
`<app>/services.py` and `<app>/selectors.py`. Full map: [docs/CODE_MAP.md](docs/CODE_MAP.md).

### Booking state machine

```mermaid
stateDiagram-v2
    [*] --> pending: hold seats
    pending --> confirmed: payment succeeded
    pending --> expired: hold time over
    pending --> cancelled: customer cancels
    confirmed --> cancelled: customer or organizer cancels
    expired --> confirmed: late payment, seats still free (the one documented exception)
    expired --> [*]
    cancelled --> [*]
```

### Data model

```mermaid
erDiagram
    USER ||--o{ EVENT : organizes
    EVENT ||--o{ SEAT : has
    USER ||--o{ BOOKING : makes
    EVENT ||--o{ BOOKING : for
    BOOKING ||--|{ BOOKING_ITEM : contains
    SEAT ||--o{ BOOKING_ITEM : reserved_by
    BOOKING ||--o{ PAYMENT : paid_by
    PAYMENT ||--o| REFUND : refunded_by
    PROMO_CODE ||--o{ BOOKING : discounts
```

Every table, constraint and index: [docs/DATABASE.md](docs/DATABASE.md).

## How double booking is prevented

1. **Row locks in a fixed order.** A hold runs `SELECT ... FOR NO KEY UPDATE` on the
   requested seats, ordered by id, inside one transaction. A competing request waits,
   then sees the seats taken and gets **409 `seats_unavailable`**. The fixed order makes
   deadlocks impossible between overlapping multi-seat holds.
2. **A database guarantee.** `BookingItem` has a partial unique index
   `UNIQUE (seat_id) WHERE is_active`. Even if application code forgot to lock, PostgreSQL
   would refuse a second active booking for a seat.
3. **Proof.** 20 threads holding one seat at the same moment: exactly one succeeds, every
   run (`bookings/tests/test_concurrency.py`). Removing the lock makes the test fail.

The same approach (lock, re-check, constraint, concurrent test) protects payment webhooks,
cancellations, hold expiry, promo-code uses and idempotency keys. The full story,
including a real deadlock the tests found, is in [docs/CONCURRENCY.md](docs/CONCURRENCY.md).

## Setup

Full Windows instructions, including installing PostgreSQL without admin rights, are in
[docs/SETUP.md](docs/SETUP.md). In short:

```powershell
# 1. PostgreSQL 18 running on localhost:5432 with a database "booking" and an app role
#    that has CREATEDB (docs/SETUP.md, step 2)

# 2. Python environment (everything installs inside .venv)
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt

# 3. Configuration
Copy-Item .env.example .env      # fill in SECRET_KEY, DATABASE_URL, PAYMENT_WEBHOOK_SECRET

# 4. Database, admin user and demo data
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py createsuperuser
.\.venv\Scripts\python manage.py seed_demo

# 5. Run
.\.venv\Scripts\python manage.py runserver
```

| Environment variable | Meaning |
|---|---|
| `SECRET_KEY` | Django secret key (production requires 50+ characters) |
| `DEBUG` | `True` for local development |
| `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` | comma-separated hosts and origins |
| `DATABASE_URL` | `postgres://user:password@host:5432/booking` (PostgreSQL only) |
| `HOLD_TTL_MINUTES` | hold length, default 10 |
| `PAYMENT_WEBHOOK_SECRET` | HMAC secret for payment webhooks (production requires 32+ characters) |
| `NUM_PROXIES`, `SECURE_HSTS_PRELOAD`, `LOG_LEVEL` | optional, see [docs/SETUP.md](docs/SETUP.md) |

Periodic cleanup (for example from cron): `manage.py expire_holds` and
`manage.py purge_idempotency_keys`. Correctness does not depend on them: expired holds
are also released lazily.

## Running the tests

```powershell
# Everything (unit, API, pages, concurrency), failing on deprecated Django APIs
.\.venv\Scripts\python -W error::DeprecationWarning manage.py test

# Only the concurrency tests (real threads, real PostgreSQL transactions)
.\.venv\Scripts\python manage.py test bookings.tests.test_concurrency bookings.tests.test_expiry bookings.tests.test_cancellation_concurrency payments.tests.test_payment_concurrency bookings.tests.test_promotions.LastPromoUseRaceTests core.tests.test_idempotency.ConcurrentIdempotentRetryTests bookings.tests.test_pages_concurrency

# Quality gates (the same as CI)
.\.venv\Scripts\ruff check .
.\.venv\Scripts\ruff format --check .
.\.venv\Scripts\python manage.py makemigrations --check --dry-run
.\.venv\Scripts\pip-audit -r requirements-dev.txt
```

The concurrency suite has been run 20 times in a row without a failure. A test also checks
that every file path and function name mentioned in these docs exists.

## API

Interactive docs: **<http://127.0.0.1:8000/api/docs/>** (schema at `/api/schema/`).

| Method | Path | Who |
|---|---|---|
| POST | `/api/auth/register/`, `/api/auth/login/`, `/api/auth/refresh/` | anyone (throttled) |
| POST | `/api/auth/logout/` | authenticated |
| GET | `/api/auth/me/` | authenticated |
| GET | `/api/events/`, `/api/events/{id}/`, `/api/events/{id}/seats/` | anyone |
| GET, POST | `/api/organizer/events/` | organizer (list also admin) |
| GET, PATCH, DELETE | `/api/organizer/events/{id}/` | owning organizer, admin |
| POST | `/api/organizer/events/{id}/generate-seats/`, `/publish/`, `/unpublish/`, `/cancel/` | owning organizer, admin |
| GET | `/api/organizer/reports/events/`, `/api/organizer/reports/events/{id}/`, `/api/organizer/reports/top-events/` | organizer, admin |
| POST | `/api/bookings/hold/` | customer (throttled, `Idempotency-Key`) |
| GET | `/api/bookings/`, `/api/bookings/{id}/`, `/api/bookings/summary/` | authenticated (scoped) |
| POST | `/api/bookings/{id}/pay/` | owning customer (throttled, `Idempotency-Key`) |
| POST | `/api/bookings/{id}/cancel/` | booking owner, admin |
| POST | `/api/payments/webhook/` | payment provider (HMAC signature) |
| GET | `/api/health/` | anyone |

Error codes, throttling and pagination: [docs/LOGIC.md](docs/LOGIC.md), "API conventions".

## Screenshots

| Event list | Seat grid (two seats selected) |
|---|---|
| ![Event list](docs/screenshots/event-list.png) | ![Seat grid](docs/screenshots/seat-grid.png) |

| Booking with hold countdown | My bookings |
|---|---|
| ![Booking with countdown](docs/screenshots/booking-timer.png) | ![My bookings](docs/screenshots/my-bookings.png) |

| Organizer sales report | Dark mode | Phone |
|---|---|---|
| ![Organizer report](docs/screenshots/organizer-report.png) | ![Dark mode](docs/screenshots/seat-grid-dark.png) | ![Phone width](docs/screenshots/seat-grid-phone.png) |

## Demo data

`python manage.py seed_demo` (local development only; it refuses to run when `DEBUG` is
off) creates, without duplicates when run again:

| Item | Value |
|---|---|
| Customer | `demo.customer@example.com` |
| Organizer | `demo.organizer@example.com` |
| Password | the `DEMO_PASSWORD` environment variable (the command prints the local default) |
| Event | a published event with a 5 x 10 seat grid in two price tiers |
| Bookings | one confirmed booking (paid through the signed webhook) and one pending hold |
| Promo code | `DEMO10`, 10% off the demo event |

## Documentation

| File | Contents |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Local setup on Windows, PostgreSQL, environment variables |
| [docs/CODE_MAP.md](docs/CODE_MAP.md) | Where everything lives, every endpoint and page |
| [docs/LOGIC.md](docs/LOGIC.md) | Business rules with worked examples |
| [docs/CONCURRENCY.md](docs/CONCURRENCY.md) | How double booking (and every other race) is prevented |
| [docs/DATABASE.md](docs/DATABASE.md) | Tables, constraints, indexes, ER diagram |
| [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) | Hold, pay, expiry and cancel traced from URL to database |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Important decisions and why |
| [docs/GLOSSARY.md](docs/GLOSSARY.md) | Every technical term in one line |

## Not included

- Deployment (production settings exist; hosting and scheduling are a later phase).
- Tickets and organizer check-in (optional feature F3, skipped).
- Real payment gateways: the provider is test mode only.
