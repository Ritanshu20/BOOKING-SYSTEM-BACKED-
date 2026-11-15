"""
Customer booking pages: hold seats from the grid, view a booking, pay in test mode,
cancel with a refund preview, and "My bookings".

Every action calls the same services as the REST API (``bookings.services``,
``bookings.cancellation``, ``payments.services``); these views only translate between
HTML forms, messages and redirects. Pages use session authentication and CSRF.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views import View

from bookings import cancellation, selectors, services
from bookings.forms import HoldSeatsForm
from bookings.models import Booking, BookingStatus
from core import clock
from core.exceptions import DomainError, NotFoundError
from core.throttling import page_request_allowed
from events.views import get_public_event_or_404, render_event_detail
from payments import services as payment_services
from payments.models import WebhookOutcome

BOOKINGS_PER_PAGE = 10
THROTTLED_TEMPLATE = "429.html"

PAYMENT_MESSAGES = {
    WebhookOutcome.CONFIRMED: (messages.SUCCESS, "Payment received. Your booking is confirmed."),
    WebhookOutcome.CONFIRMED_LATE: (
        messages.SUCCESS,
        "Payment received after the hold expired, but your seats were still free. Confirmed.",
    ),
    WebhookOutcome.REFUNDED_LATE: (
        messages.WARNING,
        "Your hold expired and the seats were taken, so the payment was refunded in full.",
    ),
    WebhookOutcome.REFUNDED_DUPLICATE: (
        messages.WARNING,
        "This booking was already paid. The extra payment was refunded in full.",
    ),
    WebhookOutcome.REFUNDED_CANCELLED: (
        messages.WARNING,
        "This booking was cancelled, so the payment was refunded in full.",
    ),
}


class CustomerRequiredMixin(LoginRequiredMixin):
    """Logged-in customers only; other roles get 403."""

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if request.user.is_authenticated and not request.user.is_customer:
            raise PermissionDenied("Only customers can book seats.")
        return super().dispatch(request, *args, **kwargs)


class HoldSeatsPageView(CustomerRequiredMixin, View):
    """POST from the seat grid: hold the ticked seats, then show the booking."""

    def post(self, request: HttpRequest, event_id: int) -> HttpResponse:
        event = get_public_event_or_404(event_id)
        if not page_request_allowed(request, "hold"):
            return render(request, THROTTLED_TEMPLATE, status=429)
        form = HoldSeatsForm(request.POST, seat_ids=list(event.seats.values_list("pk", flat=True)))
        if not form.is_valid():
            messages.error(request, "Choose at least one available seat.")
            return render_event_detail(request, event, status=400)
        try:
            booking = services.hold_seats(
                user=request.user,
                event_id=event.pk,
                seat_ids=form.cleaned_data["seat_ids"],
                promo_code=form.cleaned_data["promo_code"] or None,
            )
        except DomainError as exc:
            # The grid is re-rendered from fresh data, so the taken seats now show as taken.
            messages.error(request, exc.message)
            return render_event_detail(request, event, status=exc.status_code)
        messages.success(request, "Seats held. Complete the payment before the timer runs out.")
        return redirect("booking-detail", booking_id=booking.pk)


class MyBookingsView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest) -> HttpResponse:
        bookings = selectors.bookings_visible_to(request.user)
        page = Paginator(bookings, BOOKINGS_PER_PAGE).get_page(request.GET.get("page"))
        summary = selectors.booking_summary_for(user=request.user, now=clock.now())
        return render(request, "bookings/booking_list.html", {"page": page, "summary": summary})


class BookingDetailView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, booking_id: int) -> HttpResponse:
        booking = get_visible_booking_or_404(request, booking_id)
        now = clock.now()
        context = {
            "booking": booking,
            "is_owner": booking.user_id == request.user.pk,
            "status": booking.status_at(now),
            "can_pay": booking.status_at(now) == BookingStatus.PENDING,
        }
        return render(request, "bookings/booking_detail.html", context)


class PayBookingPageView(CustomerRequiredMixin, View):
    """POST "Pay (test mode)": runs the real signed-webhook processing path."""

    def post(self, request: HttpRequest, booking_id: int) -> HttpResponse:
        if not page_request_allowed(request, "pay"):
            return render(request, THROTTLED_TEMPLATE, status=429)
        try:
            outcome = payment_services.simulate_test_payment(
                user=request.user, booking_id=booking_id
            )
        except NotFoundError as exc:
            raise Http404(exc.message) from exc
        except DomainError as exc:
            messages.error(request, exc.message)
            return redirect("booking-detail", booking_id=booking_id)
        level, text = PAYMENT_MESSAGES.get(
            outcome, (messages.ERROR, "The payment did not go through. Please try again.")
        )
        messages.add_message(request, level, text)
        return redirect("booking-detail", booking_id=booking_id)


class CancelBookingPageView(CustomerRequiredMixin, View):
    """GET shows the refund preview; POST confirms the cancellation."""

    def get(self, request: HttpRequest, booking_id: int) -> HttpResponse:
        booking = get_own_booking_or_404(request, booking_id)
        quote = cancellation.preview_cancellation(booking=booking, now=clock.now())
        context = {
            "booking": booking,
            "quote": quote,
            "status": booking.status_at(clock.now()),
            "refund_tiers": settings.REFUND_POLICY_TIERS,
        }
        return render(request, "bookings/booking_cancel.html", context)

    def post(self, request: HttpRequest, booking_id: int) -> HttpResponse:
        try:
            result = cancellation.cancel_booking(user=request.user, booking_id=booking_id)
        except NotFoundError as exc:
            raise Http404(exc.message) from exc
        except DomainError as exc:
            messages.error(request, exc.message)
            return redirect("booking-detail", booking_id=booking_id)
        messages.success(request, _cancellation_message(result))
        return redirect("booking-list")


def get_visible_booking_or_404(request: HttpRequest, booking_id: int) -> Booking:
    # SAFETY: the selector scopes by owner (or event organizer); others get 404.
    try:
        return selectors.get_booking_for_user(user=request.user, booking_id=booking_id)
    except NotFoundError as exc:
        raise Http404(exc.message) from exc


def get_own_booking_or_404(request: HttpRequest, booking_id: int) -> Booking:
    booking = get_visible_booking_or_404(request, booking_id)
    if booking.user_id != request.user.pk:
        raise Http404("Booking not found.")
    return booking


def _cancellation_message(result: cancellation.CancellationResult) -> str:
    if result.already_cancelled:
        return "This booking was already cancelled."
    if result.refund is None:
        return "Booking cancelled. No refund applies."
    return f"Booking cancelled. {result.refund.amount} will be refunded."
