# Code Map

Where everything lives and what each part is responsible for.

## Project layout

| Path | Responsibility |
|---|---|
| `manage.py` | Django command-line entry point (defaults to `config.settings.local`) |
| `config/settings/base.py` | Shared settings, all read from environment variables |
| `config/settings/local.py` | Development and CI settings |
| `config/settings/production.py` | HTTPS-only hardening; refuses weak secrets and `ALLOWED_HOSTS=*` |
| `config/urls.py` | Root URLs: `/admin/`, `/api/...`, template pages at `/` |
| `config/wsgi.py`, `config/asgi.py` | Server entry points (default to production settings) |
| `templates/base.html` | Layout: sticky role-aware navbar, toast messages, footer, Bootstrap 5.3.8 with SRI |
| `templates/partials/` | `templates/partials/field.html`, `templates/partials/pagination.html`, `templates/partials/status_badge.html`, `templates/partials/event_status_badge.html` |
| `templates/accounts/`, `templates/events/`, `templates/bookings/` | Page templates per app |
| `templates/events/organizer/` | Organizer page templates (list, form, manage, seats, cancel, report) |
| `templates/403.html`, `templates/404.html`, `templates/500.html`, `templates/429.html` | Custom error pages (500 is standalone: it renders without a request) |
| `static/css/app.css` | Design system: tokens, dark mode, seat grid, toasts, focus styles |
| `static/js/theme.js` | Follows the OS light/dark preference |
| `static/js/app.js` | Toasts; submit buttons lock with a spinner after one click |
| `static/js/seat-map.js` | Seat selection limit, summary, 20 s status refresh from the API |
| `static/js/countdown.js` | Hold countdown; disables Pay at zero (the server stays the authority) |
| `docs/` | Project documentation (index in `README.md`) |
| `.github/workflows/ci.yml` | CI: lint, checks, migrations check and tests on PostgreSQL |

## Apps

### `core`: shared infrastructure

| File | Responsibility |
|---|---|
| `core/clock.py` | `now()`: the single source of the current time (patched in tests) |
| `core/health.py` | `is_database_available()`: database health check |
| `core/api_views.py` | `HealthCheckView` for `GET /api/health/` |
| `core/api_urls.py` | API routes owned by `core`: health, `/api/schema/`, `/api/docs/` |
| `core/exceptions.py` | `DomainError` and subclasses; `api_exception_handler` (one error envelope for every API error) |
| `core/openapi.py` | `ErrorResponseSerializer`, `error_responses()` for the Swagger docs |
| `core/pagination.py` | `StandardPagination` (`?page=` and `?page_size=`, max 100) |
| `core/forms.py` | `BootstrapFormMixin` (Bootstrap classes on every widget) |
| `core/throttling.py` | `page_request_allowed()`: the API's throttle scopes applied to template form posts |
| `core/context_processors.py` | `booking_settings` (exposes `hold_minutes` to templates) |
| `core/management/commands/seed_demo.py` | `manage.py seed_demo`: idempotent demo data |
| `core/models.py` | `IdempotencyRecord` (unique per user, scope and key) |
| `core/idempotency.py` | `run_idempotent()`, `purge_expired_keys()`: Idempotency-Key support for hold and pay |
| `core/management/commands/purge_idempotency_keys.py` | `manage.py purge_idempotency_keys` |
| `core/test_runner.py` | `ProjectTestRunner` (quiet logs, fast password hashing in tests) |
| `core/tests/test_foundation.py` | Phase 0 tests |
| `core/tests/test_security.py` | Security regressions (webhook signature, throttle identity), cookie/JWT settings, production deploy checks |
| `core/tests/test_query_budget.py` | Query budgets for hold, pay, webhook and cancel (constant for 1 or 6 seats) |
| `core/tests/test_docs.py` | Documentation accuracy: every path and code name in the docs exists |
| `core/tests/test_idempotency.py` | Idempotency-Key replay, 422 on reuse, scoping, expiry, purge, concurrent retries |
| `core/tests/test_seed_demo.py` | `seed_demo` creates the demo data once and never duplicates it |
| `core/tests/test_full_suite.py` | URL round-trips, migration state, ownership (IDOR) matrix for every object endpoint |
| `core/tests/test_api_polish.py` | Error envelope for every status, OpenAPI, throttling, filters, full-API pass |
| `core/tests/concurrency.py` | `run_concurrently()`: start tasks on real threads behind a barrier (test helper) |

### `accounts`: users, roles and authentication

| File | Responsibility |
|---|---|
| `accounts/models.py` | `User` (email login, `role`), `Role` choices, `UserManager` |
| `accounts/services.py` | `register_user()`, `revoke_refresh_token()` |
| `accounts/serializers.py` | `RegisterSerializer` (password validators), `UserSerializer`, `LogoutSerializer` |
| `accounts/api_views.py` | `RegisterView`, `LoginView`, `RefreshView`, `LogoutView`, `MeView` |
| `accounts/api_urls.py` | Routes under `/api/auth/` |
| `accounts/permissions.py` | `IsCustomer`, `IsOrganizer`, `IsOrganizerOrAdmin`, `IsAdminRole`; `scope_to_owner()` |
| `accounts/forms.py` | `SignUpForm`, `EmailLoginForm` |
| `accounts/views.py`, `accounts/urls.py` | Pages: sign up, log in, log out (throttled like the API) |
| `accounts/admin.py` | Django admin for users |
| `accounts/tests/factories.py` | `create_user()` helper for tests |
| `accounts/tests/test_auth_api.py` | Register, login, refresh, logout, `/me`, throttling |
| `accounts/tests/test_permissions.py` | Role permissions and `scope_to_owner()` |
| `accounts/tests/test_pages.py` | Account pages, page throttling, role-aware navbar |

### `events`: events and seats

| File | Responsibility |
|---|---|
| `events/models.py` | `Event`, `Seat`, `EventStatus`, `EventQuerySet` (`published()`, `upcoming()`) |
| `events/services.py` | `create_event()`, `update_event()`, `delete_event()`, `generate_seats()`, `publish_event()`, `unpublish_event()`, `PriceTier` |
| `events/selectors.py` | `list_public_events()`, `get_public_event()`, `list_events_for_manager()`, `get_event_for_manager()`, `list_seats_with_status()`, `SeatStatus` |
| `events/serializers.py` | Public and organizer event serializers, filters, seat generation input, seat map output, report output |
| `events/subqueries.py` | `count_per_event()`, `sum_per_event()`, `min_per_event()`: indexed per-event aggregate subqueries |
| `events/reports.py` | `reportable_events()`, `list_event_reports()`, `get_event_report()`, `list_top_events()`, `bookings_per_day()`, `occupancy_percent()`, `EventReport` |
| `events/api_views.py` | Public list/detail/seat map; organizer list/create/detail/generate-seats/publish/cancel; reports |
| `events/api_urls.py` | Routes under `/api/events/` and `/api/organizer/events/` |
| `events/forms.py` | `EventSearchForm`; organizer `EventForm`, `SeatGridForm`, `PriceTierFormSet` |
| `events/views.py`, `events/urls.py` | Pages: home / event list, event detail with the seat grid |
| `events/organizer_views.py` | Organizer pages: my events, create, manage, edit, seats, publish/unpublish, cancel, report |
| `events/admin.py` | Django admin for events and seats |
| `events/tests/factories.py` | `create_event()`, `create_seats()` helpers for tests |
| `events/tests/test_organizer_events_api.py` | CRUD, ownership (IDOR), publish rules |
| `events/tests/test_seat_generation.py` | Seat grid generation, tiers, constraints |
| `events/tests/test_public_events_api.py` | Public list, filters, pagination, detail, seat map, query counts |

| `events/tests/test_seat_status.py` | Derived seat status (available / held / booked) |
| `events/tests/test_pages.py` | Event list and seat grid pages, escaping, query counts |
| `events/tests/test_organizer_pages.py` | Organizer pages: roles, IDOR, forms, publish/unpublish, cancel confirmation, report numbers, error pages |
| `events/tests/test_reports.py` | Reports against hand-calculated numbers, zero cases, ownership, query counts |

### `bookings`: holds and the booking state machine

| File | Responsibility |
|---|---|
| `bookings/models.py` | `Booking` (subtotal, discount, total), `BookingItem` (partial unique constraints), `PromoCode`, `BookingStatus`, `DiscountType`, `BookingQuerySet` |
| `bookings/promotions.py` | `calculate_discount()`, `reserve_promo_use()`, `reserve_use_for_recovery()`, `release_promo_use()` (row-locked counter) |
| `bookings/services.py` | `ALLOWED_TRANSITIONS`, `LATE_PAYMENT_RECOVERY`, `ensure_transition_allowed()`, `hold_seats()`, `expire_booking()`, `release_expired_holds()`, `expire_due_holds()`, `mark_cancelled()`, `lock_booking_with_seats()`, `confirm_paid_booking()`, `ConfirmationResult` |
| `bookings/management/commands/expire_holds.py` | `manage.py expire_holds`: runs `expire_due_holds()` |
| `bookings/cancellation.py` | `cancel_booking()`, `preview_cancellation()`, `cancel_event()`, `preview_event_cancellation()` (refunds through `issue_refund`) |
| `bookings/refund_policy.py` | `refund_percent_for_notice()`, `refund_amount()`, `quote_refund()`, `RefundQuote` |
| `bookings/selectors.py` | `bookings_visible_to()`, `get_booking_for_user()` (ownership built in), `filter_bookings()`, `booking_summary_for()` |
| `bookings/serializers.py` | `HoldRequestSerializer`, `BookingSerializer` (effective status), cancellation result serializers |
| `bookings/api_views.py` | `HoldSeatsView`, `BookingListView`, `BookingDetailView`, `CancelBookingView`, `BookingSummaryView` |
| `bookings/api_urls.py` | Routes under `/api/bookings/` |
| `bookings/forms.py` | `HoldSeatsForm` |
| `bookings/views.py`, `bookings/urls.py` | Pages: hold from the grid, booking detail, pay (test mode), cancel with refund preview, my bookings |
| `bookings/templatetags/booking_tags.py` | `{% status_badge booking %}` (colour + icon + text) |
| `bookings/admin.py` | Read-only Django admin for bookings |
| `bookings/tests/factories.py` | `create_booking()` helper for tests |
| `bookings/tests/test_hold_service.py` | Hold rules, lazy expiry, constraint fallback, state machine |
| `bookings/tests/test_booking_api.py` | Hold endpoint, roles, ownership (IDOR), query count |
| `bookings/tests/test_expiry.py` | Expiry boundary, idempotency, command output, hold-vs-sweep race |
| `bookings/tests/test_promotions.py` | Promo arithmetic, validity, uses given back or kept, late payment, constraints, API/page, 20-thread race for the last use |
| `bookings/tests/test_summary.py` | Customer "my bookings" summary |
| `bookings/tests/test_refund_policy.py` | Refund tiers at exact boundaries, rounding, cap |
| `bookings/tests/test_cancellation.py` | Customer cancel: tiers, pending cancel, idempotency, ownership, API |
| `bookings/tests/test_event_cancellation.py` | Organizer event cancel: full refunds, seats freed, idempotency, permissions |
| `bookings/tests/test_cancellation_concurrency.py` | Concurrent double cancel; event cancel vs. holds |
| `bookings/tests/test_pages.py` | Customer pages: hold, conflict message, pay happy path, IDOR, CSRF, refund preview |
| `bookings/tests/test_pages_concurrency.py` | Two browsers hold the same seat through the view |
| `bookings/tests/test_concurrency.py` | Real-thread races: 20 users one seat, overlapping holds, double-click, DB constraint |

### `payments`: test-mode payments, webhooks and refunds

| File | Responsibility |
|---|---|
| `payments/models.py` | `Payment`, `WebhookEvent`, `Refund` and their status, outcome and reason choices |
| `payments/provider.py` | `FakePaymentProvider` (references, HMAC signing, constant-time verification, `build_webhook`) |
| `payments/services.py` | `start_payment()`, `process_webhook()` (signature, parsing, idempotency, late-webhook rule), `simulate_test_payment()` |
| `payments/refunds.py` | `issue_refund()`: the only way to create a refund |
| `payments/serializers.py` | `PaymentSerializer`; `WebhookPayloadSerializer`, `WebhookResponseSerializer` (documentation and output) |
| `payments/api_views.py` | `PayBookingView`, `PaymentWebhookView` |
| `payments/api_urls.py` | `/api/bookings/{id}/pay/`, `/api/payments/webhook/` |
| `payments/admin.py` | Read-only admin for payments, webhook events and refunds |
| `payments/tests/helpers.py` | `create_hold_with_payment()`, `signed_webhook()`, `post_webhook()` |
| `payments/tests/test_pay_api.py` | Pay endpoint rules, ownership, one pending payment |
| `payments/tests/test_webhooks.py` | Signatures, duplicates, amount check, failed payment, late webhook, refunds |
| `payments/tests/test_payment_concurrency.py` | Concurrent duplicate webhook; payment vs. expiry race |

## API endpoints

| Method | Path | Who | View |
|---|---|---|---|
| GET | `/api/health/` | anyone | `core.api_views.HealthCheckView` |
| POST | `/api/auth/register/` | anyone (throttled) | `accounts.api_views.RegisterView` |
| POST | `/api/auth/login/` | anyone (throttled) | `accounts.api_views.LoginView` |
| POST | `/api/auth/refresh/` | anyone with a refresh token (throttled) | `accounts.api_views.RefreshView` |
| POST | `/api/auth/logout/` | authenticated | `accounts.api_views.LogoutView` |
| GET | `/api/auth/me/` | authenticated | `accounts.api_views.MeView` |
| GET | `/api/events/` | anyone | `events.api_views.PublicEventListView` |
| GET | `/api/events/{id}/` | anyone | `events.api_views.PublicEventDetailView` |
| GET | `/api/events/{id}/seats/` | anyone | `events.api_views.EventSeatMapView` |
| GET, POST | `/api/organizer/events/` | organizer (GET also admin) | `events.api_views.ManagedEventListCreateView` |
| GET, PATCH, DELETE | `/api/organizer/events/{id}/` | owning organizer, admin | `events.api_views.ManagedEventDetailView` |
| POST | `/api/organizer/events/{id}/generate-seats/` | owning organizer, admin | `events.api_views.GenerateSeatsView` |
| POST | `/api/organizer/events/{id}/publish/` | owning organizer, admin | `events.api_views.PublishEventView` |
| POST | `/api/bookings/hold/` | customer (throttled) | `bookings.api_views.HoldSeatsView` |
| GET | `/api/bookings/` | authenticated (scoped) | `bookings.api_views.BookingListView` |
| GET | `/api/bookings/{id}/` | owner, event organizer, admin | `bookings.api_views.BookingDetailView` |
| POST | `/api/bookings/{id}/pay/` | owning customer (throttled) | `payments.api_views.PayBookingView` |
| POST | `/api/payments/webhook/` | payment provider (HMAC signature) | `payments.api_views.PaymentWebhookView` |
| POST | `/api/bookings/{id}/cancel/` | booking owner, admin | `bookings.api_views.CancelBookingView` |
| POST | `/api/organizer/events/{id}/unpublish/` | owning organizer, admin | `events.api_views.UnpublishEventView` |
| POST | `/api/organizer/events/{id}/cancel/` | owning organizer, admin | `events.api_views.CancelEventView` |
| GET | `/api/bookings/summary/` | authenticated (own bookings) | `bookings.api_views.BookingSummaryView` |
| GET | `/api/organizer/reports/events/` | organizer, admin | `events.api_views.EventReportListView` |
| GET | `/api/organizer/reports/events/{id}/` | owning organizer, admin | `events.api_views.EventReportDetailView` |
| GET | `/api/organizer/reports/top-events/` | organizer, admin | `events.api_views.TopEventsView` |
| GET | `/api/schema/` | anyone | `drf_spectacular.views.SpectacularAPIView` |
| GET | `/api/docs/` | anyone | `drf_spectacular.views.SpectacularSwaggerView` |

## Template pages

| URL | Who | View |
|---|---|---|
| `/` | anyone | `events.views.EventListView` |
| `/events/{id}/` | anyone | `events.views.EventDetailView` |
| `/events/{id}/hold/` (POST) | customer | `bookings.views.HoldSeatsPageView` |
| `/bookings/` | logged in | `bookings.views.MyBookingsView` |
| `/bookings/{id}/` | owner, event organizer, admin | `bookings.views.BookingDetailView` |
| `/bookings/{id}/pay/` (POST) | owning customer | `bookings.views.PayBookingPageView` |
| `/bookings/{id}/cancel/` | owning customer | `bookings.views.CancelBookingPageView` |
| `/accounts/register/`, `/accounts/login/`, `/accounts/logout/` (POST) | anyone | `accounts.views` |
| `/organizer/events/` | organizer, admin | `events.organizer_views.MyEventsView` |
| `/organizer/events/new/` | organizer | `events.organizer_views.EventCreateView` |
| `/organizer/events/{id}/` | owning organizer, admin | `events.organizer_views.EventManageView` |
| `/organizer/events/{id}/edit/` | owning organizer, admin | `events.organizer_views.EventEditView` |
| `/organizer/events/{id}/seats/` | owning organizer, admin | `events.organizer_views.GenerateSeatsPageView` |
| `/organizer/events/{id}/publish/`, `/unpublish/` (POST) | owning organizer, admin | `PublishEventPageView`, `UnpublishEventPageView` |
| `/organizer/events/{id}/cancel/` | owning organizer, admin | `events.organizer_views.CancelEventPageView` |
| `/organizer/events/{id}/report/` | owning organizer, admin | `events.organizer_views.EventReportPageView` |

