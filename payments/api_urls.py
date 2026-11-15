from django.urls import path

from payments import api_views

urlpatterns = [
    path(
        "bookings/<int:booking_id>/pay/",
        api_views.PayBookingView.as_view(),
        name="api-booking-pay",
    ),
    path("payments/webhook/", api_views.PaymentWebhookView.as_view(), name="api-payment-webhook"),
]
