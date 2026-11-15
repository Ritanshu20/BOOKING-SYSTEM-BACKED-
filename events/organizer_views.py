"""
Organizer pages: my events, create and edit, seat generation, publish and unpublish,
cancel with a confirmation, and the event report.

Organizers (and admins) only: customers get 403. Every page loads the event through
``events.selectors.get_event_for_manager``, so another organizer's event is a 404.
Writes go through ``events.services`` and ``bookings.cancellation``; numbers come
from ``events.reports``, the same code as the API.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views import View

from bookings import cancellation
from core import clock
from core.exceptions import DomainError, NotFoundError
from events import reports, selectors, services
from events.forms import EventForm, PriceTierFormSet, SeatGridForm, price_tiers_from
from events.models import Event, EventStatus

EVENTS_PER_PAGE = 10


class OrganizerRequiredMixin(LoginRequiredMixin):
    """Logged-in organizers and admins only; customers get 403."""

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        user = request.user
        if user.is_authenticated and not (user.is_organizer or user.is_admin_role):
            raise PermissionDenied("Organizer pages are for organizers.")
        return super().dispatch(request, *args, **kwargs)


def get_managed_event_or_404(request: HttpRequest, event_id: int) -> Event:
    # SAFETY: scoped to the caller's own events (admins: all).
    try:
        return selectors.get_event_for_manager(user=request.user, event_id=event_id)
    except NotFoundError as exc:
        raise Http404(exc.message) from exc


class MyEventsView(OrganizerRequiredMixin, View):
    def get(self, request: HttpRequest) -> HttpResponse:
        events = selectors.list_events_for_manager(user=request.user)
        page = Paginator(events, EVENTS_PER_PAGE).get_page(request.GET.get("page"))
        return render(request, "events/organizer/event_list.html", {"page": page})


class EventCreateView(OrganizerRequiredMixin, View):
    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, "events/organizer/event_form.html", {"form": EventForm()})

    def post(self, request: HttpRequest) -> HttpResponse:
        if not request.user.is_organizer:
            raise PermissionDenied("Only organizers create events.")
        form = EventForm(request.POST)
        if not form.is_valid():
            return render(request, "events/organizer/event_form.html", {"form": form}, status=400)
        try:
            event = services.create_event(organizer=request.user, **form.cleaned_data)
        except DomainError as exc:
            form.add_error("starts_at", exc.message)
            return render(request, "events/organizer/event_form.html", {"form": form}, status=400)
        messages.success(request, "Event created as a draft. Add seats next.")
        return redirect("organizer-event-seats", event_id=event.pk)


class EventManageView(OrganizerRequiredMixin, View):
    """One event's control panel: details, status and the actions allowed right now."""

    def get(self, request: HttpRequest, event_id: int) -> HttpResponse:
        event = get_managed_event_or_404(request, event_id)
        context = {
            "event": event,
            "can_generate_seats": event.status == EventStatus.DRAFT and event.seat_count == 0,
            "can_publish": event.status == EventStatus.DRAFT and event.seat_count > 0,
            "can_unpublish": event.status == EventStatus.PUBLISHED,
            "can_cancel": event.status != EventStatus.CANCELLED,
            "can_edit": event.status != EventStatus.CANCELLED,
        }
        return render(request, "events/organizer/event_manage.html", context)


class EventEditView(OrganizerRequiredMixin, View):
    def get(self, request: HttpRequest, event_id: int) -> HttpResponse:
        event = get_managed_event_or_404(request, event_id)
        context = {"form": EventForm(instance=event), "event": event}
        return render(request, "events/organizer/event_form.html", context)

    def post(self, request: HttpRequest, event_id: int) -> HttpResponse:
        event = get_managed_event_or_404(request, event_id)
        form = EventForm(request.POST, instance=event)
        if form.is_valid():
            try:
                services.update_event(event=event, **form.cleaned_data)
            except DomainError as exc:
                form.add_error(None, exc.message)
            else:
                messages.success(request, "Event updated.")
                return redirect("organizer-event-manage", event_id=event.pk)
        context = {"form": form, "event": event}
        return render(request, "events/organizer/event_form.html", context, status=400)


class GenerateSeatsPageView(OrganizerRequiredMixin, View):
    template_name = "events/organizer/seat_form.html"

    def get(self, request: HttpRequest, event_id: int) -> HttpResponse:
        event = get_managed_event_or_404(request, event_id)
        context = {"event": event, "grid_form": SeatGridForm(), "tiers": PriceTierFormSet()}
        return render(request, self.template_name, context)

    def post(self, request: HttpRequest, event_id: int) -> HttpResponse:
        event = get_managed_event_or_404(request, event_id)
        grid_form = SeatGridForm(request.POST)
        tiers = PriceTierFormSet(request.POST)
        context = {"event": event, "grid_form": grid_form, "tiers": tiers}
        if not (grid_form.is_valid() and tiers.is_valid()):
            return render(request, self.template_name, context, status=400)
        try:
            created = services.generate_seats(
                event=event,
                rows=grid_form.cleaned_data["rows"],
                seats_per_row=grid_form.cleaned_data["seats_per_row"],
                tiers=price_tiers_from(tiers),
            )
        except DomainError as exc:
            grid_form.add_error(None, exc.message)
            return render(request, self.template_name, context, status=exc.status_code)
        messages.success(request, f"{created} seats created. Publish when you are ready.")
        return redirect("organizer-event-manage", event_id=event.pk)


class PublishEventPageView(OrganizerRequiredMixin, View):
    def post(self, request: HttpRequest, event_id: int) -> HttpResponse:
        event = get_managed_event_or_404(request, event_id)
        return _run_action(request, event, services.publish_event, "Event published.")


class UnpublishEventPageView(OrganizerRequiredMixin, View):
    def post(self, request: HttpRequest, event_id: int) -> HttpResponse:
        event = get_managed_event_or_404(request, event_id)
        return _run_action(request, event, services.unpublish_event, "Event moved back to draft.")


class CancelEventPageView(OrganizerRequiredMixin, View):
    """GET: confirmation with the bookings and money affected. POST: cancel the event."""

    def get(self, request: HttpRequest, event_id: int) -> HttpResponse:
        event = get_managed_event_or_404(request, event_id)
        preview = cancellation.preview_event_cancellation(event=event)
        return render(
            request, "events/organizer/event_cancel.html", {"event": event, "preview": preview}
        )

    def post(self, request: HttpRequest, event_id: int) -> HttpResponse:
        event = get_managed_event_or_404(request, event_id)
        try:
            result = cancellation.cancel_event(user=request.user, event_id=event.pk)
        except DomainError as exc:
            messages.error(request, exc.message)
            return redirect("organizer-event-manage", event_id=event.pk)
        if result.already_cancelled:
            messages.info(request, "This event was already cancelled.")
        else:
            messages.success(
                request,
                f"Event cancelled: {result.cancelled_bookings} bookings cancelled, "
                f"{result.refunds_issued} refunds totalling {result.refunded_total}.",
            )
        return redirect("organizer-event-manage", event_id=event.pk)


class EventReportPageView(OrganizerRequiredMixin, View):
    def get(self, request: HttpRequest, event_id: int) -> HttpResponse:
        try:
            report = reports.get_event_report(user=request.user, event_id=event_id, now=clock.now())
        except NotFoundError as exc:
            raise Http404(exc.message) from exc
        daily = reports.bookings_per_day(event_id=report.event_id)
        return render(
            request, "events/organizer/event_report.html", {"report": report, "daily": daily}
        )


def _run_action(request: HttpRequest, event: Event, action, success_message: str) -> HttpResponse:
    """Run a one-click event action and report the result as a message."""
    try:
        action(event=event)
    except DomainError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, success_message)
    return redirect("organizer-event-manage", event_id=event.pk)
