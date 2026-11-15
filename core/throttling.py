"""
Rate limits for template-page form posts.

The REST API is throttled by DRF scopes (``auth``, ``hold``, ``pay``). The browser pages
post to plain Django views, so this module applies the *same* scopes and rates to them;
an attacker cannot avoid the login limit by using the HTML form instead of the API.
"""

from django.http import HttpRequest
from rest_framework.throttling import SimpleRateThrottle


class PageRateThrottle(SimpleRateThrottle):
    """A DRF rate throttle usable from a normal Django view."""

    def __init__(self, scope: str) -> None:
        # The scope must be set before SimpleRateThrottle reads its rate.
        self.scope = scope
        super().__init__()

    def get_cache_key(self, request: HttpRequest, view) -> str:
        user = getattr(request, "user", None)
        ident = user.pk if user is not None and user.is_authenticated else self.get_ident(request)
        return self.cache_format % {"scope": f"page-{self.scope}", "ident": ident}


def page_request_allowed(request: HttpRequest, scope: str) -> bool:
    """Return False when the caller exceeded the ``scope`` rate (for example ``auth``)."""
    return PageRateThrottle(scope).allow_request(request, view=None)
