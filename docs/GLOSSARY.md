# Glossary

Every technical term used in this project, in one plain line, with where it appears.

| Term | Meaning | Where in the code |
|---|---|---|
| `Decimal` | Exact decimal numbers for money; floats would round 0.1 + 0.2 wrongly | `bookings/refund_policy.py`, `bookings/promotions.py`, every `DecimalField` |
| `ROUND_HALF_UP` | Rounding where .5 goes up (49.995 becomes 50.00) | `bookings/refund_policy.py::refund_amount`, `bookings/promotions.py::calculate_discount` |
| `transaction.atomic` | Runs a block as one database transaction: all of it commits or none of it | `bookings/services.py::hold_seats`, `payments/services.py::process_webhook` |
| Savepoint | A nested `atomic` block that can roll back without undoing the outer transaction | `bookings/services.py::expire_booking` (inside a hold) |
| `select_for_update` | `SELECT ... FOR UPDATE`: locks the selected rows until the transaction ends | `bookings/services.py::_lock_seats` |
| `FOR NO KEY UPDATE` | A row lock (`select_for_update(no_key=True)`) that does not block foreign-key checks | every lock in the project; see `docs/CONCURRENCY.md` |
| `FOR KEY SHARE` | The light lock PostgreSQL takes on a referenced row when inserting a foreign key | explained in `docs/CONCURRENCY.md` (the deadlock story) |
| Row lock | A lock on single table rows, so other transactions wait before changing them | seats, bookings, payments, promo codes |
| Lock ordering | Always taking locks in the same order (Event, Seats, Booking, Payment, Promo) so no deadlock is possible | `bookings/services.py` module docstring |
| Deadlock | Two transactions each waiting for a lock the other holds; PostgreSQL aborts one | prevented by lock ordering; tested in `bookings/tests/test_concurrency.py` |
| Partial unique index | A unique rule that applies only to rows matching a condition (`WHERE is_active`) | `bookings/models.py::BookingItem` |
| `CheckConstraint` | A rule PostgreSQL checks on every insert and update (`price >= 0`) | `events/models.py`, `bookings/models.py`, `payments/models.py` |
| `IntegrityError` | The error raised when a database constraint is violated | `bookings/services.py::_conflict_from_integrity_error` |
| Race condition | A bug that appears only when two requests run at the same moment | all `test_*concurrency*.py` files |
| `TransactionTestCase` | A Django test case that really commits, so other threads' connections see the data | `bookings/tests/test_concurrency.py` |
| `threading.Barrier` | Makes several threads wait and then start together | `core/tests/concurrency.py::run_concurrently` |
| Idempotency | Doing the same request twice has the same effect as doing it once | webhooks, `Idempotency-Key`, cancel |
| Idempotency key | A client-chosen header value that marks retries of the same request | `core/idempotency.py::run_idempotent` |
| Webhook | An HTTP call the payment provider makes to us to report a payment result | `payments/api_views.py::PaymentWebhookView` |
| HMAC-SHA256 | A signature computed from the body and a shared secret; proves who sent it | `payments/provider.py::FakePaymentProvider.sign` |
| `hmac.compare_digest` | Compares secrets in constant time so timing leaks nothing | `payments/provider.py::FakePaymentProvider.is_valid_signature` |
| Hold | A pending booking that reserves seats for `HOLD_TTL_MINUTES` | `bookings/services.py::hold_seats` |
| TTL | Time to live: how long a hold lasts (10 minutes by default) | `config/settings/base.py` (`HOLD_TTL_MINUTES`) |
| Lazy expiry | Expiring a stale hold at the moment someone needs its seats | `bookings/services.py::release_expired_holds` |
| Late webhook | A successful payment that arrives after its hold expired | `bookings/services.py::confirm_paid_booking` |
| State machine | The allowed status changes of a booking | `bookings/services.py` (`ALLOWED_TRANSITIONS`) |
| Derived status | A value computed from other data instead of being stored | `events/selectors.py::list_seats_with_status` |
| `select_related` | Loads a foreign-key object in the same query with a JOIN | `bookings/selectors.py::bookings_visible_to` |
| `prefetch_related` | Loads related lists in one extra query instead of one per row | `bookings/selectors.py::bookings_visible_to` |
| N+1 queries | One query for a list plus one per item; avoided everywhere and tested | `assertNumQueries` tests |
| `assertNumQueries` | A test assertion that fails if a block runs more or fewer queries than expected | `events/tests/test_public_events_api.py`, `core/tests/test_query_budget.py` |
| Correlated subquery | A subquery that runs per outer row (here: per event), using an index | `events/subqueries.py::count_per_event` |
| `annotate` | Adds a computed column to each row of a queryset | `events/selectors.py::list_public_events` |
| `aggregate` | Computes one value over many rows (count, sum) | `bookings/selectors.py::booking_summary_for` |
| `EXPLAIN ANALYZE` | Runs a query and shows how PostgreSQL executed it and how long it took | measurements in `docs/DECISIONS.md` (D29) |
| JWT | A signed token that proves who the API caller is | `config/settings/base.py` (`SIMPLE_JWT`) |
| Access / refresh token | Short-lived (15 min) API token / longer-lived token to get new access tokens | `accounts/api_views.py::LoginView` |
| Token rotation and blacklist | Each refresh token works once; used tokens are recorded as revoked | `accounts/services.py::revoke_refresh_token` |
| Session authentication | Login stored in a server-side session cookie (used by the browser pages) | `accounts/views.py::EmailLoginView` |
| CSRF | Cross-site request forgery; blocked by a secret token in every form | `{% csrf_token %}` in templates |
| IDOR | Insecure direct object reference: reading someone else's object by changing an id | `core/tests/test_full_suite.py::OwnershipMatrixTests` |
| Throttling | Limiting requests per client per minute | `config/settings/base.py`, `core/throttling.py::page_request_allowed` |
| `X-Forwarded-For` | A header proxies add with the client IP; clients can fake it | `config/settings/base.py` (`NUM_PROXIES`) |
| `TextChoices` | A Django enum of allowed string values for a field | `bookings/models.py::BookingStatus` |
| `BigAutoField` | 64-bit auto-increment primary keys | `config/settings/base.py` (`DEFAULT_AUTO_FIELD`) |
| Selector | A read-only query function (no writes) | `events/selectors.py`, `bookings/selectors.py` |
| Service | A function that contains a business rule and writes data | `bookings/services.py`, `payments/services.py` |
| Domain error | An exception for a broken business rule, turned into an HTTP error in one place | `core/exceptions.py::DomainError` |
| OpenAPI / Swagger | A machine-readable API description and its interactive web page | `/api/docs/`, `core/openapi.py` |
| SRI | Subresource Integrity: a hash that makes the browser reject a changed CDN file | `templates/base.html` |
| Progressive enhancement | The page works without JavaScript; scripts only add comfort | `static/js/seat-map.js` |
| Promo code | A discount code with a limited number of uses | `bookings/promotions.py` |
