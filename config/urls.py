"""
Root URL configuration.

REST API routes live under ``/api/`` (``api_urls.py`` in each app). Server-rendered
template pages are mounted at the site root (``urls.py`` in each app).
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("core.api_urls")),
    path("api/auth/", include("accounts.api_urls")),
    path("api/", include("events.api_urls")),
    path("api/bookings/", include("bookings.api_urls")),
    path("api/", include("payments.api_urls")),
    path("accounts/", include("accounts.urls")),
    path("", include("events.urls")),
    path("", include("bookings.urls")),
]
