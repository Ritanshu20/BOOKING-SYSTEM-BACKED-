"""
Public event pages: the event list and the event detail with its seat grid.

Holding seats from the grid posts to ``bookings.views.HoldSeatsPageView``. Both pages
read through ``events.selectors``, the same code the REST API uses.
"""

from itertools import groupby

from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views import View

from core import clock
from core.exceptions import NotFoundError
from events import selectors
from events.forms import EventSearchForm
from events.models import Event

EVENTS_PER_PAGE = 12


class EventListView(View):
    """Home page: published upcoming events with search, date filter and pagination."""

    def get(self, request: HttpRequest) -> HttpResponse:
        form = EventSearchForm(request.GET)
        events = selectors.list_public_events(now=clock.now(), **form.selector_filters())
        page = Paginator(events, EVENTS_PER_PAGE).get_page(request.GET.get("page"))
        context = {"form": form, "page": page, "query_string": _query_without_page(request)}
        return render(request, "events/event_list.html", context)


class EventDetailView(View):
    """One published event and its seat grid."""

    def get(self, request: HttpRequest, event_id: int) -> HttpResponse:
        event = get_public_event_or_404(event_id)
        return render_event_detail(request, event)


def get_public_event_or_404(event_id: int) -> Event:
    try:
        return selectors.get_public_event(event_id=event_id)
    except NotFoundError as exc:
        raise Http404(exc.message) from exc


def render_event_detail(
    request: HttpRequest, event: Event, *, status: int = 200, selected_ids=()
) -> HttpResponse:
    """Render the event page with a fresh seat grid (also used after a failed hold)."""
    seats = list(selectors.list_seats_with_status(event=event, now=clock.now()))
    rows = [
        (row_label, list(row_seats))
        for row_label, row_seats in groupby(seats, key=lambda seat: seat.row_label)
    ]
    context = {
        "event": event,
        "rows": rows,
        "has_seats": bool(seats),
        "selected_ids": set(selected_ids),
        "bookable": event.starts_at > clock.now(),
    }
    return render(request, "events/event_detail.html", context, status=status)


def _query_without_page(request: HttpRequest) -> str:
    """Keep the current filters when moving between pages."""
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()
