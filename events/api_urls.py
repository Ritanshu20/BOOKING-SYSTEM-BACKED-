from django.urls import path

from events import api_views

urlpatterns = [
    path("events/", api_views.PublicEventListView.as_view(), name="api-event-list"),
    path(
        "events/<int:event_id>/", api_views.PublicEventDetailView.as_view(), name="api-event-detail"
    ),
    path(
        "events/<int:event_id>/seats/", api_views.EventSeatMapView.as_view(), name="api-event-seats"
    ),
    path(
        "organizer/events/",
        api_views.ManagedEventListCreateView.as_view(),
        name="api-organizer-event-list",
    ),
    path(
        "organizer/events/<int:event_id>/",
        api_views.ManagedEventDetailView.as_view(),
        name="api-organizer-event-detail",
    ),
    path(
        "organizer/events/<int:event_id>/generate-seats/",
        api_views.GenerateSeatsView.as_view(),
        name="api-organizer-generate-seats",
    ),
    path(
        "organizer/events/<int:event_id>/publish/",
        api_views.PublishEventView.as_view(),
        name="api-organizer-publish",
    ),
    path(
        "organizer/events/<int:event_id>/unpublish/",
        api_views.UnpublishEventView.as_view(),
        name="api-organizer-unpublish",
    ),
    path(
        "organizer/events/<int:event_id>/cancel/",
        api_views.CancelEventView.as_view(),
        name="api-organizer-cancel-event",
    ),
    path(
        "organizer/reports/events/",
        api_views.EventReportListView.as_view(),
        name="api-organizer-report-list",
    ),
    path(
        "organizer/reports/events/<int:event_id>/",
        api_views.EventReportDetailView.as_view(),
        name="api-organizer-report-detail",
    ),
    path(
        "organizer/reports/top-events/",
        api_views.TopEventsView.as_view(),
        name="api-organizer-top-events",
    ),
]
