from django.contrib import admin

from bookings.models import Booking, BookingItem, PromoCode


class BookingItemInline(admin.TabularInline):
    model = BookingItem
    extra = 0
    readonly_fields = ("seat", "price_at_booking", "is_active")
    can_delete = False


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    """Read-only view: booking status must only change through the booking services."""

    list_display = ("id", "user", "event", "status", "total_amount", "expires_at", "created_at")
    list_filter = ("status",)
    list_select_related = ("user", "event")
    readonly_fields = (
        "user",
        "event",
        "status",
        "expires_at",
        "subtotal_amount",
        "discount_amount",
        "total_amount",
        "promo_code",
        "confirmed_at",
        "cancelled_at",
        "created_at",
        "updated_at",
    )
    inlines = [BookingItemInline]

    def has_add_permission(self, request) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(PromoCode)
class PromoCodeAdmin(admin.ModelAdmin):
    """Admins create codes here; ``times_used`` changes only through the booking services."""

    list_display = (
        "code",
        "discount_type",
        "value",
        "event",
        "times_used",
        "max_uses",
        "is_active",
    )
    list_filter = ("discount_type", "is_active")
    search_fields = ("code",)
    readonly_fields = ("times_used", "created_at")
