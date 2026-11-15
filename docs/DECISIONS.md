# Decisions

Each entry records what was decided and why, in the order the decisions were made.

## D1. Django 5.2 LTS instead of Django 6.1 (Phase 0)

- **Decision:** Django 5.2.19 (LTS, security fixes until April 2028) with Python 3.13.
- **Why:** the project rule is to use the newest Django only if *every* dependency
  officially supports it. On 10 Nov 2026 PyPI showed:
  - `djangorestframework` 3.18.3 supports Django 5.2, 6.0 and 6.1;
  - `djangorestframework-simplejwt` 5.5.1 (latest, July 2025) declares support up to Django 5.2 only;
  - `drf-spectacular` 0.30.0 declares support up to Django 6.0.
  Django 5.2 is the newest version every dependency officially supports.
- **Revisit when:** simplejwt and drf-spectacular declare Django 6.1 support.

## D2. PostgreSQL 18 from the official portable binaries (Phase 0)

- **Decision:** PostgreSQL 18.6, using the official EnterpriseDB Windows binaries zip,
  run as the current Windows user on `localhost:5432`.
- **Why:** PostgreSQL was not installed and Docker was not available. The portable
  binaries need no administrator prompt and no installer, and they are the official
  builds. SQLite and MySQL are never used, because row locks and partial unique
  constraints must be tested on the real database.
- **Trade-off:** the server is not a Windows service, so it must be started manually
  after a reboot (see `docs/SETUP.md`).

## D3. Configuration through environment variables (Phase 0)

- **Decision:** `python-dotenv` loads a git-ignored `.env` file for local work, and
  `dj-database-url` parses `DATABASE_URL`. Real environment variables (CI, production)
  take priority over `.env`.
- **Why:** one simple mechanism for every environment; secrets never live in the code.
  Startup fails fast if `SECRET_KEY`, `DATABASE_URL` or `PAYMENT_WEBHOOK_SECRET` is
  missing, or if `DATABASE_URL` is not PostgreSQL.

## D4. Settings split into base, local and production (Phase 0)

- **Decision:** `config/settings/base.py` holds everything shared; `config/settings/local.py` is used for
  development and CI; `config/settings/production.py` adds HTTPS-only hardening (secure cookies, HSTS,
  SSL redirect).
- **Why:** HTTPS-only settings would break local development over plain HTTP.

## D5. Email is the login identifier (Phase 0)

- **Decision:** the custom `accounts.User` uses `USERNAME_FIELD = "email"`. Emails are
  stored lowercase, and a functional unique constraint on `Lower("email")` makes
  uniqueness case-insensitive at the database level.
- **Why:** users remember their email, not a username, and two accounts that differ
  only by letter case would be a support and security problem.

## D6. Project test runner (Phase 0, extended in Phase 1)

- **Decision:** `core.test_runner.ProjectTestRunner` disables INFO-level logs and uses a
  fast password hasher while tests run.
- **Why:** services log every important action, and during tests those lines would
  hide the results (warnings and errors are still printed). The production PBKDF2
  hasher is slow on purpose. With it the Phase 1 suite took 43 seconds; with the
  test hasher it takes about 1 second. Production hashing is unchanged.

## D7. JWT settings and logout (Phase 1)

- **Decision:** access tokens live 15 minutes, refresh tokens 1 day. Refresh tokens
  rotate: each refresh returns a new refresh token and blacklists the old one. Logout
  blacklists the given refresh token and requires a valid access token for the same user.
- **Why:** short access tokens limit damage if one leaks. Rotation makes a stolen
  refresh token usable at most once. The ownership check stops one user from logging
  another user out.
- **Trade-off:** an access token stays valid until it expires (at most 15 minutes)
  even after logout. That is the normal stateless-JWT trade-off.

## D8. Domain errors are translated in one place (Phase 1)

- **Decision:** services raise `core.exceptions.DomainError` subclasses
  (`ValidationFailedError` 400, `PermissionDeniedError` 403, `NotFoundError` 404,
  `ConflictError` 409). `core.exceptions.api_exception_handler` renders them as
  `{"error": {"code", "message", "details"}}`.
- **Why:** services stay free of HTTP code, and template views can reuse the same
  services and show the same messages.

## D9. Self-registration roles (Phase 1)

- **Decision:** sign-up accepts `customer` (default) and `organizer`. Admins are
  created with `createsuperuser` or by another admin.
- **Why:** a self-service admin role would defeat every permission check.

## D10. Seats are generated once, while the event is a draft (Phase 2)

- **Decision:** `generate_seats()` only works for a draft event with no seats; it locks
  the event row so two concurrent requests cannot both create a grid.
- **Why:** holds and bookings point at seat rows. A seat map that changes after
  publishing could invalidate existing holds. Keeping it fixed is simpler and safer.

## D11. Availability is derived, not stored (Phase 2)

- **Decision:** `Seat` has no status column. `events.selectors.list_seats_with_status()`
  computes `available | held | booked` from booking data in one query.
- **Why:** a duplicated status column can drift out of sync with the bookings (for
  example when a hold expires). Deriving it keeps one source of truth.

## D12. Filters without extra dependencies (Phase 2)

- **Decision:** list filters (`search`, `venue`, `date_from`, `date_to`) are validated by
  a small DRF serializer (`EventFilterSerializer`) and applied in the selector.
- **Why:** four filters do not justify adding `django-filter`; the code stays easy to read.

## D13. Two layers against double booking (Phase 3)

- **Decision:** holds lock the seat rows with `SELECT ... FOR UPDATE` in ascending id
  order, and a partial unique constraint (`UNIQUE (seat_id) WHERE is_active`) backs it up.
- **Why:** row locks make concurrent requests wait their turn, so the application
  check sees the truth. The constraint makes double booking impossible even if a
  future code path forgets to lock. Ordered locking prevents deadlocks between
  overlapping multi-seat holds.
- **Alternatives rejected:** a seat status column with optimistic version numbers
  (duplicated state that can drift); `SERIALIZABLE` isolation (works, but needs retry
  loops on every request and is harder to explain); application-level locks or Redis
  (not allowed, and no stronger than the database).

## D14. Holds are customers-only; unpublished events behave as missing (Phase 3)

- **Decision:** `POST /api/bookings/hold/` requires the customer role. A draft or
  cancelled event returns 404; an event that started returns 409 `event_not_bookable`.
- **Why:** organizers and admins manage events; letting them hold seats would mix
  roles. Returning 404 for unpublished events does not reveal that drafts exist.

## D15. Stale holds count as expired immediately (Phase 3)

- **Decision:** a pending booking with `expires_at <= now` is reported as `expired`
  (`Booking.status_at`) and its seats show as available, even before any job updates
  the row. The next hold on those seats expires it for real (lazy expiry).
- **Why:** correctness must not depend on a background job running on time. The
  expiry command (next phases) is only cleanup.

## D16. How concurrency is tested (Phase 4)

- **Decision:** concurrency tests are `TransactionTestCase`s that start real threads
  together with a `threading.Barrier`; each thread opens its own PostgreSQL connection
  before the barrier and closes it afterwards (`core/tests/concurrency.py`). Any
  unexpected exception or a hang longer than 60 seconds fails the test.
- **Why:** only real, committed transactions on separate connections exercise row locks
  and constraints. No sleeps or retries are used, because they would hide races
  instead of proving their absence.

## D17. Late payments may confirm an expired booking (Phase 6)

- **Decision:** `expired` is terminal except for one documented move,
  `expired -> confirmed`, allowed only inside `bookings.services.confirm_paid_booking`
  while the webhook holds the seat locks and only if every seat is still free
  (`LATE_PAYMENT_RECOVERY`). Otherwise the payment is refunded in full and the booking
  stays expired.
- **Why:** the requirements state both "expired is terminal" and "a late successful
  payment confirms if the seats are free". Making the exception explicit, in one
  function, keeps the rest of the state machine strict.

## D18. Insert the webhook event first, inside the processing transaction (Phase 6)

- **Decision:** `process_webhook` inserts the `WebhookEvent` row (unique `event_id`)
  before doing anything else, in the same transaction as the processing. A unique
  violation on that constraint means "already processed" and returns 200.
- **Why:** the database serialises concurrent duplicates on the unique index, so
  exactly one delivery processes the event. If processing fails, the insert rolls
  back too, so the provider's retry is not wrongly treated as a duplicate.
- **Alternative rejected:** "check whether the event exists, then process": two
  concurrent deliveries can both pass the check.

## D19. Signature errors: 401 for missing, 403 for invalid (Phase 6)

- **Decision:** a missing `X-Payment-Signature` returns 401; a wrong one returns 403.
  Unknown payment references and already-settled payments return 200 ("ignored").
- **Why:** 401 and 403 tell an integrator exactly what is wrong. Answering 200 to
  well-signed but irrelevant events stops the provider from retrying them forever.

## D20. Paying twice returns the same pending payment (Phase 6)

- **Decision:** `start_payment` locks the booking and returns an existing pending
  payment (200) instead of opening another (201). A partial unique constraint allows
  at most one pending payment per booking.
- **Why:** double clicks and client retries are normal. One pending payment per
  booking means one checkout and no double charge.

## D21. Money for an unconfirmable booking is always refunded in full (Phase 6)

- **Decision:** a successful payment for a booking that is already confirmed,
  cancelled, or expired with seats taken gets a 100% `Refund`
  (`payments.services.REFUND_FOR_RESULT`). The amount must equal the booking total, or
  the payment is marked failed and nothing is confirmed.
- **Why:** the customer must never pay for something they do not get.

## D22. Refund policy lives in settings (Phase 7)

- **Decision:** `REFUND_POLICY_TIERS` in `config/settings/base.py` (minimum hours of notice
  -> refund percent); `bookings/refund_policy.py` only reads it and does the arithmetic.
- **Why:** the requirements keep the policy out of views and code paths; changing a tier
  is a one-line settings change, and the tests prove the code follows the setting.

## D23. Cancellation rules (Phase 7)

- **Decision:** cancelling twice returns 200 with the original result
  (`already_cancelled: true`), never 409 and never a second refund. Cancelling after the
  event started returns 409 `event_started`. A 0% refund creates no `Refund` row (the
  `Refund.amount > 0` constraint keeps refund rows meaningful). An organizer event
  cancellation always refunds 100%.
- **Why:** idempotent cancel makes client retries and double clicks safe. Cancelling an
  event that is already running has no business meaning.

## D24. Every row lock is FOR NO KEY UPDATE (Phase 7)

- **Decision:** all `select_for_update()` calls use `no_key=True`.
- **Why:** a concurrency test found a real deadlock: `cancel_event` held `FOR UPDATE` on
  the event while a hold's `INSERT INTO booking` needed `FOR KEY SHARE` on the same row
  for its foreign-key check. `FOR NO KEY UPDATE` keeps every lock-versus-lock exclusion
  but does not block foreign-key checks, which removes this whole class of deadlock.
  Primary keys never change in this project, so nothing is lost.

## D25. Reports are correlated subqueries on the event queryset (Phase 8)

- **Decision:** every report figure is a `Subquery` (count or sum per event, wrapped in
  `Coalesce` so empty sets give 0) annotated onto the organizer's events
  (`events.reports.reportable_events`). Occupancy is computed from those integers in
  Python with a zero-seat guard.
- **Why:** joining seats, items, payments and refunds in one `GROUP BY` would multiply
  rows and inflate the sums. Independent subqueries keep each figure correct, and the
  whole dashboard is still a constant number of queries (2 for the paginated list,
  2 for one event with its daily series, 1 for top events).
- **Revenue definition:** gross counts every payment that succeeded, including ones
  refunded later; refunds are subtracted separately, so net revenue is what was kept.

## D26. One error envelope for every API error (Phase 9)

- **Decision:** `core.exceptions.api_exception_handler` wraps DRF's own errors too
  (validation, 401, 403, 404, 405, 429) in `{"error": {"code", "message", "details"}}`.
  Field errors go to `details`; throttling adds `retry_after_seconds`; `WWW-Authenticate`
  and `Retry-After` headers are kept. Validation stays 400 (`validation_error`), and an
  expired hold is 409 `hold_expired` (not 410), so every state conflict shares one status.
- **Why:** clients branch on one field (`error.code`) instead of guessing between
  `{"detail": ...}`, `{"field": [...]}` and custom shapes.

## D27. OpenAPI with drf-spectacular, validated in the test suite (Phase 9)

- **Decision:** every view is annotated with `extend_schema` (request, responses, error
  codes, examples for hold, pay and the webhook). A test runs
  `manage.py spectacular --validate --fail-on-warn`.
- **Why:** documentation that is checked on every test run cannot silently drift from
  the code. The docs are public, like the API's public endpoints; restricting them in
  production can be decided at deployment.

## D28. Security audit outcomes (Phase 11)

- **Fixed: webhook crash on non-ASCII signatures.** `hmac.compare_digest` raises
  `TypeError` for non-ASCII `str` input, so a forged header returned 500. Signatures
  are now compared as bytes (`payments.provider.FakePaymentProvider.is_valid_signature`).
- **Fixed: throttle bypass through `X-Forwarded-For`.** With `NUM_PROXIES` unset, DRF
  identifies anonymous clients by the client-supplied header, so rotating it defeated
  the login limit. `NUM_PROXIES` now defaults to 0 (use the TCP peer address) and is set
  from the environment behind a proxy.
- **Hardened:** HttpOnly session and CSRF cookies, `SameSite=Lax`, `X-Frame-Options: DENY`;
  production refuses `SECRET_KEY` under 50 characters, `PAYMENT_WEBHOOK_SECRET` under 32
  and `ALLOWED_HOSTS=*`.
- **Accepted:** HSTS preload stays opt-in (`SECURE_HSTS_PRELOAD`); preloading is hard to
  undo, so the `security.W021` warning is silenced only while preload is off.
- **Not added:** a global anonymous/user throttle. The sensitive endpoints (register,
  login, refresh, hold, pay) have their own scopes, and a blanket limit would also hit
  the provider's webhook retries.
- **Not available:** Django 5.2 has no built-in Content Security Policy (it arrived in
  Django 6.0). Revisit when upgrading; templates already avoid inline user content.
- **Docs are public:** `/api/docs/` and `/api/schema/` expose no data, only the API shape.

## D29. Per-event subqueries instead of JOIN aggregates (Phase 12)

- **Decision:** list annotations (`seat_count`, `min_price`) and report figures use
  correlated subqueries (`events/subqueries.py`). The seat map uses one scalar subquery per
  seat that returns the derived status of its single active item.
- **Why (measured):** `annotate(Count("seats"))` made PostgreSQL join and aggregate every
  seat of every upcoming event, and Django keeps aggregate annotations in the pagination
  `COUNT(*)`: 105 ms per page on 90,000 seats, 3.7 ms after. The seat map's two `EXISTS`
  checks became hashed subplans over all booking items: 10.4 ms, 1.4 ms after. The scalar
  subquery is only valid because the partial unique constraint guarantees at most one
  active item per seat.
- **Tried and rejected:** counting report sold/held seats from the seat side was slower
  (45 ms vs 30 ms) and was reverted.

## D30. Server-rendered pages with progressive enhancement (Phase 13A)

- **Decision:** Django templates, Bootstrap 5.3.8 and Bootstrap Icons 1.13.2 from jsDelivr
  with Subresource Integrity hashes, a small design system in `static/css/app.css`, and
  four tiny external scripts (no inline JavaScript, no build step).
- **Why:** the seat grid is a list of real checkboxes, so it works without JavaScript and
  with a keyboard or screen reader; JavaScript only adds the selection limit, a live
  total, the countdown and a periodic status refresh. Keeping scripts external keeps a
  future Content Security Policy simple.
- **Accessibility:** seat states use colour, an icon and screen-reader text; badges always
  show text; visible focus outlines; colours chosen for WCAG AA contrast in light and
  dark mode; reduced-motion respected.

## D31. "Pay (test mode)" uses the real webhook path (Phase 13A)

- **Decision:** the button calls `payments.services.simulate_test_payment`, which starts
  the payment and then builds, signs and processes the provider's webhook through
  `process_webhook`.
- **Why:** the demo exercises the same signature check, idempotency and locking as a
  real gateway callback; nothing writes the booking status directly.

## D32. Demo data is created through the services (Phase 13A)

- **Decision:** `seed_demo` creates users, an event, seats and bookings only through the
  normal services, checks what exists before creating anything, and picks other free
  seats when its usual seats are taken. It refuses to run when `DEBUG` is off.
- **Why:** demo data then obeys every business rule, and re-running it is always safe.

## D33. Unpublish only without active bookings (Phase 13B)

- **Decision:** `unpublish_event` moves a published event back to draft only when it has
  no confirmed booking and no valid hold. It locks the event and then all its seats,
  in the same order as `cancel_event`.
- **Why:** hiding an event that people have paid for would strand them; that case is a
  cancellation with refunds. The seat locks make a hold in progress finish (and count)
  first, and a later hold sees the draft after its own seat locks.

## D34. Organizer UI without extra libraries (Phase 13B)

- **Decision:** organizer pages live in `events/organizer_views.py`; price tiers use a
  Django formset; occupancy is a native `<progress>` element; the optional Chart.js chart
  was not added; templates contain no inline styles or scripts.
- **Why:** a separate module keeps the public views short; native elements are
  accessible by default and need no JavaScript; fewer dependencies are easier to explain.

## D35. Idempotency-Key: claim the key first, replay errors too (F1)

- **Decision:** the key row is inserted before the action, in the same transaction, so
  concurrent retries wait and then replay instead of returning "in progress". Business
  errors (4xx from services) are stored and replayed; validation errors and crashes are
  not. The key is scoped to user and endpoint and bound to a SHA-256 fingerprint of the
  method, path and body.
- **Why:** waiting gives the client the real answer on the first retry. Replaying a 409
  keeps a retry from accidentally succeeding later and confusing the client. Not storing
  400s lets the client fix a typo and reuse its key.

## D36. A promo use behaves like a held seat (F2)

- **Decision:** the hold reserves a use; an unpaid hold that expires or is cancelled gives
  it back; a confirmed booking keeps it. A late payment that recovers an expired booking
  must re-reserve the use, or it is refunded in full. Codes are managed by admins in the
  Django admin; the counter is read-only there.
- **Why:** counting at payment time would let several holds share the last use and fail
  at payment; never giving uses back would let abandoned holds burn codes. Treating a use
  like a seat reuses the same proven pattern: lock, check, change, and a database
  constraint as the last line of defence.

