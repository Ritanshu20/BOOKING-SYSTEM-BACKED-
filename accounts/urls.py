from django.contrib.auth.views import LogoutView
from django.urls import path

from accounts.views import EmailLoginView, SignUpView

urlpatterns = [
    path("register/", SignUpView.as_view(), name="register"),
    path("login/", EmailLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
]
