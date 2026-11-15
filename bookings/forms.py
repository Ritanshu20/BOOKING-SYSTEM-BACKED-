"""Forms for the booking pages."""

from django import forms

from events.models import MAX_SEATS_PER_BOOKING_LIMIT


class HoldSeatsForm(forms.Form):
    """The seats ticked on the seat grid. Business rules are checked by ``hold_seats``."""

    seat_ids = forms.TypedMultipleChoiceField(
        coerce=int, error_messages={"required": "Choose at least one seat."}
    )

    promo_code = forms.CharField(required=False, max_length=40, label="Promo code")

    def __init__(self, *args, seat_ids: list[int], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["seat_ids"].choices = [(seat_id, seat_id) for seat_id in seat_ids]

    def clean_seat_ids(self) -> list[int]:
        seat_ids = self.cleaned_data["seat_ids"]
        if len(seat_ids) > MAX_SEATS_PER_BOOKING_LIMIT:
            raise forms.ValidationError("Too many seats selected.")
        return seat_ids
