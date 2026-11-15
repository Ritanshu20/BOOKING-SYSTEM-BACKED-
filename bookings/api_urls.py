from django.urls import path

from bookings import api_views

urlpatterns = [
    path("hold/", api_views.HoldSeatsView.as_view(), name="api-booking-hold"),
    path("", api_views.BookingListView.as_view(), name="api-booking-list"),
    path("summary/", api_views.BookingSummaryView.as_view(), name="api-booking-summary"),
    path("<int:booking_id>/", api_views.BookingDetailView.as_view(), name="api-booking-detail"),
    path(
        "<int:booking_id>/cancel/",
        api_views.CancelBookingView.as_view(),
        name="api-booking-cancel",
    ),
]
