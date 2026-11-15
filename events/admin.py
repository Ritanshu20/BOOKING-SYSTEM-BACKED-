from django.contrib import admin

from events.models import Event, Seat


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("title", "venue", "starts_at", "status", "organizer")
    list_filter = ("status",)
    search_fields = ("title", "venue")
    list_select_related = ("organizer",)


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ("__str__", "event", "price")
    list_filter = ("event",)
    list_select_related = ("event",)
