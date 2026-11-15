"""Pagination shared by every list endpoint."""

from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    """``?page=2&page_size=50``; the page size is capped so one request stays cheap."""

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100
