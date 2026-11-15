"""Read-only admin views: money records must only change through the payment services."""

from django.contrib import admin

from payments.models import Payment, Refund, WebhookEvent


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Payment)
class PaymentAdmin(ReadOnlyAdmin):
    list_display = ("provider_reference", "booking", "amount", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("provider_reference",)


@admin.register(WebhookEvent)
class WebhookEventAdmin(ReadOnlyAdmin):
    list_display = ("event_id", "event_type", "status", "outcome", "received_at")
    list_filter = ("status", "outcome")
    search_fields = ("event_id",)


@admin.register(Refund)
class RefundAdmin(ReadOnlyAdmin):
    list_display = ("id", "payment", "booking", "amount", "reason", "created_at")
    list_filter = ("reason",)
