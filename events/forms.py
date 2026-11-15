"""Forms for the public event pages and the organizer pages."""

from django import forms

from core.forms import BootstrapFormMixin
from events.models import MAX_SEATS_PER_BOOKING_LIMIT, Event
from events.services import MAX_ROWS, MAX_SEATS_PER_ROW, PriceTier


class EventSearchForm(BootstrapFormMixin, forms.Form):
    """Search and date filter for the event list (all fields optional)."""

    q = forms.CharField(
        required=False,
        max_length=100,
        label="Search",
        widget=forms.TextInput(attrs={"placeholder": "Search events", "type": "search"}),
    )
    date_from = forms.DateField(
        required=False, label="From", widget=forms.DateInput(attrs={"type": "date"})
    )
    date_to = forms.DateField(
        required=False, label="To", widget=forms.DateInput(attrs={"type": "date"})
    )

    def clean(self) -> dict:
        cleaned = super().clean()
        date_from, date_to = cleaned.get("date_from"), cleaned.get("date_to")
        if date_from and date_to and date_from > date_to:
            raise forms.ValidationError("The start date must be on or before the end date.")
        return cleaned

    def selector_filters(self) -> dict:
        """Translate valid form data into ``list_public_events`` keyword arguments."""
        if not self.is_valid():
            return {}
        return {
            "search": self.cleaned_data["q"] or None,
            "date_from": self.cleaned_data["date_from"],
            "date_to": self.cleaned_data["date_to"],
        }


class EventForm(BootstrapFormMixin, forms.ModelForm):
    """Create or edit an event. Times are entered and shown in UTC."""

    starts_at = forms.DateTimeField(
        label="Starts at (UTC)",
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    max_seats_per_booking = forms.IntegerField(
        min_value=1,
        max_value=MAX_SEATS_PER_BOOKING_LIMIT,
        initial=6,
        label="Max seats per booking",
    )

    class Meta:
        model = Event
        fields = ("title", "description", "venue", "starts_at", "max_seats_per_booking")
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}


class SeatGridForm(BootstrapFormMixin, forms.Form):
    rows = forms.IntegerField(min_value=1, max_value=MAX_ROWS, label="Rows (A to Z)")
    seats_per_row = forms.IntegerField(
        min_value=1, max_value=MAX_SEATS_PER_ROW, label="Seats per row"
    )


class PriceTierForm(BootstrapFormMixin, forms.Form):
    """One tier: the next ``rows`` rows, counted from the front, cost ``price``."""

    rows = forms.IntegerField(min_value=1, max_value=MAX_ROWS, label="Rows")
    price = forms.DecimalField(min_value=0, max_digits=12, decimal_places=2, label="Price")


PriceTierFormSet = forms.formset_factory(
    PriceTierForm, extra=2, min_num=1, max_num=5, validate_min=True, validate_max=True
)


def price_tiers_from(formset) -> list[PriceTier]:
    """Turn the filled-in tier rows of a valid formset into ``PriceTier`` values."""
    return [
        PriceTier(row_count=form.cleaned_data["rows"], price=form.cleaned_data["price"])
        for form in formset.forms
        if form.cleaned_data
    ]
