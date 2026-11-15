from django.urls import path

from accounts.api_views import LoginView, LogoutView, MeView, RefreshView, RegisterView

urlpatterns = [
    path("register/", RegisterView.as_view(), name="api-auth-register"),
    path("login/", LoginView.as_view(), name="api-auth-login"),
    path("refresh/", RefreshView.as_view(), name="api-auth-refresh"),
    path("logout/", LogoutView.as_view(), name="api-auth-logout"),
    path("me/", MeView.as_view(), name="api-auth-me"),
]
