# Walkthrough of the Key Flows

Each flow is traced from the URL to the database with the real file and function names.
The API and the browser pages call the same services, so each trace covers both.

## 1. Hold seats

**URLs:** `POST /api/bookings/hold/` (API) or `POST /events/{id}/hold/` (seat grid form).

| Step | Code | What happens |
|---|---|---|
| 1 | `bookings/api_views.py::HoldSeatsView.post` | Customers only (`IsCustomer`), throttled (`hold` scope). Wraps the work in `run_idempotent` |
| 2 | `core/idempotency.py::run_idempotent` | Without an `Idempotency-Key` header it just runs the action; with one it claims the key first |
| 3 | `bookings/api_views.py::HoldSeatsView.hold` | `HoldRequestSerializer` checks the shape: `event_id`, `seat_ids`, optional `promo_code` |
| 4 | `bookings/services.py::hold_seats` | Validates the seat list (`_validate_seat_id_list`), loads the published event, checks `max_seats_per_booking`, opens `transaction.atomic()` |
| 5 | `bookings/services.py::_lock_seats` | `SELECT ... FOR NO KEY UPDATE ... ORDER BY id` on the seats: concurrent holds for the same seats wait here |
| 6 | `bookings/services.py::_ensure_event_open_for_booking` | Re-reads the event after the locks (catches a cancellation that just committed) |
| 7 | `bookings/services.py::release_expired_holds` | Lazy expiry: stale holds on these seats go through `expire_booking` |
| 8 | `bookings/services.py::_ensure_user_has_no_active_hold` | One pending booking per user per event |
| 9 | `bookings/services.py::_ensure_seats_are_free` | Any active `BookingItem` on a requested seat raises 409 `seats_unavailable` |
| 10 | `bookings/promotions.py::reserve_promo_use` | Only with a promo code: locks the code row, reserves one use |
| 11 | `bookings/services.py::_create_hold` | Inserts the `Booking` (subtotal, discount, total, `expires_at = now + HOLD_TTL_MINUTES`) and its `BookingItem` rows |
| 12 | `bookings/services.py::_conflict_from_integrity_error` | Only if a database constraint fired: turns it into the same 409 |

The page version is `bookings/views.py::HoldSeatsPageView.post`: it calls the same
`hold_seats` and, on a conflict, re-renders the seat grid (`events/views.py::render_event_detail`)
with the message naming the taken seats.

```mermaid
sequenceDiagram
    autonumber
    actor C as Customer
    participant V as HoldSeatsView
    participant S as hold_seats
    participant DB as PostgreSQL
    C->>V: POST /api/bookings/hold/ {event_id, seat_ids}
    V->>S: hold_seats(user, event_id, seat_ids)
    S->>DB: BEGIN; SELECT seats FOR NO KEY UPDATE ORDER BY id
    S->>DB: re-read event; expire stale holds on these seats
    S->>DB: any active item for these seats?
    alt a seat is taken
        S-->>V: ConflictError seats_unavailable
        V-->>C: 409 {"error": {"code": "seats_unavailable", ...}}
    else all free
        S->>DB: INSERT booking (pending) + booking items (active); COMMIT
        V-->>C: 201 booking with expires_at
    end
```

## 2. Pay and confirm through the webhook

**URLs:** `POST /api/bookings/{id}/pay/`, then the provider calls `POST /api/payments/webhook/`.
The page button `POST /bookings/{id}/pay/` does both in one step.

| Step | Code | What happens |
|---|---|---|
| 1 | `payments/api_views.py::PayBookingView.pay` | Customers only, throttled (`pay`), idempotent with a key |
| 2 | `payments/services.py::start_payment` | Locks the caller's booking; it must be pending and not expired; returns the existing pending payment or creates one for `total_amount` |
| 3 | `payments/provider.py::FakePaymentProvider.build_webhook` | (Test mode) the provider builds the JSON body and signs it with HMAC-SHA256 |
| 4 | `payments/api_views.py::PaymentWebhookView.post` | No JWT; passes the raw body and the `X-Payment-Signature` header to the service |
| 5 | `payments/services.py::_verify_signature` | Missing signature 401, wrong signature 403 (`hmac.compare_digest` on bytes) |
| 6 | `payments/services.py::_parse_payload` | Invalid body 400 |
| 7 | `payments/services.py::process_webhook` | Inserts `WebhookEvent(event_id)` first: a duplicate fails on the unique index and returns 200 `duplicate` |
| 8 | `bookings/services.py::lock_booking_with_seats` | Locks the booking's seats, then the booking (same order as a hold) |
| 9 | `payments/services.py::_apply_event` | Locks the payment; ignores unknown or already-settled payments; marks `failed` events |
| 10 | `payments/services.py::_apply_successful_payment` | Amount must equal the booking total; marks the payment `succeeded` |
| 11 | `bookings/services.py::confirm_paid_booking` | Valid hold: confirm. Expired hold: `_confirm_after_expiry` (seats and promo use still free: confirm late; otherwise refund) |
| 12 | `payments/refunds.py::issue_refund` | Only when the booking cannot be confirmed: full refund |

The page version is `bookings/views.py::PayBookingPageView.post` →
`payments/services.py::simulate_test_payment`, which runs steps 2, 3 and 7-12 with the
same code.

```mermaid
sequenceDiagram
    autonumber
    actor C as Customer
    participant API as PayBookingView
    participant P as FakePaymentProvider
    participant W as process_webhook
    participant DB as PostgreSQL
    C->>API: POST /api/bookings/{id}/pay/
    API->>DB: lock booking; INSERT payment (pending)
    API-->>C: 201 {provider_reference, amount}
    P->>W: POST /api/payments/webhook/ + X-Payment-Signature
    W->>W: verify HMAC (constant time), parse body
    W->>DB: BEGIN; INSERT webhook_event(event_id)
    W->>DB: lock seats, booking, payment
    W->>DB: payment succeeded; booking confirmed; COMMIT
    W-->>P: 200 {"status": "processed", "outcome": "confirmed"}
```

## 3. Hold expiry

Two paths, one function: `bookings/services.py::expire_booking`.

| Path | Code | When |
|---|---|---|
| Lazy | `bookings/services.py::release_expired_holds` (called by `hold_seats`) | someone asks for a seat that a stale hold still blocks |
| Sweep | `bookings/management/commands/expire_holds.py::Command.handle` → `bookings/services.py::expire_due_holds` | `python manage.py expire_holds`, for example from cron |

`expire_booking` locks the booking, re-checks `status == pending and expires_at <= now`,
sets `expired`, deactivates the items and gives back a promo use
(`bookings/promotions.py::release_promo_use`). If the booking already changed, it does
nothing, so running it twice or from two places at once is harmless.

```mermaid
sequenceDiagram
    autonumber
    participant Cron as expire_holds
    participant E as expire_booking
    participant DB as PostgreSQL
    Cron->>DB: SELECT ids of pending bookings with expires_at <= now
    loop each id
        Cron->>E: expire_booking(id, now)
        E->>DB: SELECT booking FOR NO KEY UPDATE
        alt still pending and due
            E->>DB: status = expired; items inactive; promo use released
        else already changed (paid, cancelled, expired)
            E-->>Cron: False (nothing to do)
        end
    end
```

## 4. Cancel and refund

**URLs:** `POST /api/bookings/{id}/cancel/`, or the page `GET /bookings/{id}/cancel/`
(refund preview) and `POST` (confirm). Organizers: `POST /api/organizer/events/{id}/cancel/`
or `/organizer/events/{id}/cancel/`.

| Step | Code | What happens |
|---|---|---|
| 1 | `bookings/views.py::CancelBookingPageView.get` | Shows `bookings/cancellation.py::preview_cancellation` (paid amount, percent, refund) |
| 2 | `bookings/cancellation.py::cancel_booking` | Locks the caller's booking (`_lock_own_booking`, booking row only) |
| 3 | | Already cancelled: return the original result (idempotent). Expired: 409 |
| 4 | | Pending: `bookings/services.py::mark_cancelled` frees the seats (and the promo use) |
| 5 | `bookings/cancellation.py::_cancel_confirmed_booking` | Event started: 409. Otherwise lock the succeeded payment, cancel, and refund by tier |
| 6 | `bookings/refund_policy.py::quote_refund` | 100% at 48 h or more, 50% from 24 h, 0% under 24 h (`settings.REFUND_POLICY_TIERS`), `Decimal`, half up |
| 7 | `payments/refunds.py::issue_refund` | Creates the `Refund` (one per payment) and marks the payment `refunded` |

An organizer cancellation (`bookings/cancellation.py::cancel_event`) locks the event, all
its seats and all active bookings, then cancels every booking and refunds every paid one
100%, in one transaction.

```mermaid
sequenceDiagram
    autonumber
    actor C as Customer
    participant V as CancelBookingView
    participant X as cancel_booking
    participant DB as PostgreSQL
    C->>V: POST /api/bookings/{id}/cancel/
    V->>X: cancel_booking(user, booking_id)
    X->>DB: BEGIN; SELECT booking FOR NO KEY UPDATE (own booking only)
    alt already cancelled
        X-->>V: original result (already_cancelled = true)
    else confirmed, event in 30 h
        X->>DB: lock payment; booking cancelled; items inactive
        X->>DB: INSERT refund (50% of paid); payment refunded; COMMIT
        X-->>V: booking + refund
    end
    V-->>C: 200
```
