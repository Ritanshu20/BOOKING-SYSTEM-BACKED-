"""
Per-event aggregate subqueries, shared by the event lists and the reports.

Why not ``annotate(Count("seats"))``? That JOIN makes PostgreSQL aggregate the seats of
every matching event even when the page shows only 20, and Django cannot drop an
aggregate annotation from the pagination ``COUNT(*)``. Measured on 300 events with
90,000 seats it cost about 100 ms per page (docs/DECISIONS.md, D29). A correlated
subquery runs only for the rows actually returned and uses the inner table's
foreign-key index.

Every ``queryset`` passed in must already be filtered to the outer event with
``OuterRef("pk")``; ``event_field`` is the lookup from its rows to that event.
"""

from django.db.models import Count, DecimalField, IntegerField, Min, QuerySet, Subquery, Sum, Value
from django.db.models.functions import Coalesce

MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)


def count_per_event(queryset: QuerySet, event_field: str) -> Coalesce:
    """Number of rows for the outer event (0 when there are none)."""
    counted = _grouped(queryset, event_field).annotate(result=Count("pk")).values("result")
    return Coalesce(Subquery(counted, output_field=IntegerField()), Value(0))


def sum_per_event(queryset: QuerySet, event_field: str, amount_field: str) -> Coalesce:
    """Sum of ``amount_field`` for the outer event (0.00 when there are no rows)."""
    summed = _grouped(queryset, event_field).annotate(result=Sum(amount_field)).values("result")
    return Coalesce(Subquery(summed, output_field=MONEY_FIELD), Value(0), output_field=MONEY_FIELD)


def min_per_event(queryset: QuerySet, event_field: str, field: str) -> Subquery:
    """Smallest ``field`` value for the outer event (NULL when there are no rows)."""
    smallest = _grouped(queryset, event_field).annotate(result=Min(field)).values("result")
    return Subquery(smallest, output_field=MONEY_FIELD)


def _grouped(queryset: QuerySet, event_field: str) -> QuerySet:
    # Grouping by the event column yields exactly one row per outer event.
    return queryset.order_by().values(event_field)
