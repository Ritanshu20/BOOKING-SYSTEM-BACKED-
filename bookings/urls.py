from django.urls import path

from bookings import views

urlpatterns = [
    path("events/<int:event_id>/hold/", views.HoldSeatsPageView.as_view(), name="event-hold"),
    path("bookings/", views.MyBookingsView.as_view(), name="booking-list"),
    path("bookings/<int:booking_id>/", views.BookingDetailView.as_view(), name="booking-detail"),
    path("bookings/<int:booking_id>/pay/", views.PayBookingPageView.as_view(), name="booking-pay"),
    path(
        "bookings/<int:booking_id>/cancel/",
        views.CancelBookingPageView.as_view(),
        name="booking-cancel",
    ),
]
