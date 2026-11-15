from django.urls import path

from events import organizer_views
from events.views import EventDetailView, EventListView

urlpatterns = [
    path("", EventListView.as_view(), name="home"),
    path("events/<int:event_id>/", EventDetailView.as_view(), name="event-detail"),
    path("organizer/events/", organizer_views.MyEventsView.as_view(), name="organizer-events"),
    path(
        "organizer/events/new/",
        organizer_views.EventCreateView.as_view(),
        name="organizer-event-create",
    ),
    path(
        "organizer/events/<int:event_id>/",
        organizer_views.EventManageView.as_view(),
        name="organizer-event-manage",
    ),
    path(
        "organizer/events/<int:event_id>/edit/",
        organizer_views.EventEditView.as_view(),
        name="organizer-event-edit",
    ),
    path(
        "organizer/events/<int:event_id>/seats/",
        organizer_views.GenerateSeatsPageView.as_view(),
        name="organizer-event-seats",
    ),
    path(
        "organizer/events/<int:event_id>/publish/",
        organizer_views.PublishEventPageView.as_view(),
        name="organizer-event-publish",
    ),
    path(
        "organizer/events/<int:event_id>/unpublish/",
        organizer_views.UnpublishEventPageView.as_view(),
        name="organizer-event-unpublish",
    ),
    path(
        "organizer/events/<int:event_id>/cancel/",
        organizer_views.CancelEventPageView.as_view(),
        name="organizer-event-cancel",
    ),
    path(
        "organizer/events/<int:event_id>/report/",
        organizer_views.EventReportPageView.as_view(),
        name="organizer-event-report",
    ),
]
