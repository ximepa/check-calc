"""Domain models for splitting and settling shared checks."""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Sum
from django.utils import timezone

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value):
    """Round a Decimal to two places, the way a cash register would."""
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def allocate(total, weights):
    """Split ``total`` across ``weights`` without losing or inventing cents.

    Each share is rounded down to whole cents first; the leftover cents are
    then handed out one at a time to the largest remainders (the classic
    largest-remainder method), so the shares always add back up to ``total``.
    """
    total = money(total)
    weights = [Decimal(weight) for weight in weights]
    weight_sum = sum(weights, ZERO)
    if not weights:
        return []
    if weight_sum <= 0:
        # Nothing to weight by — spread the total evenly instead.
        weights = [Decimal(1)] * len(weights)
        weight_sum = Decimal(len(weights))

    exact = [total * weight / weight_sum for weight in weights]
    shares = [value.quantize(CENTS, rounding="ROUND_DOWN") for value in exact]
    remainder = int(((total - sum(shares, ZERO)) / CENTS).to_integral_value())

    order = sorted(
        range(len(shares)),
        key=lambda index: (exact[index] - shares[index], weights[index]),
        reverse=True,
    )
    for position in range(remainder):
        shares[order[position % len(order)]] += CENTS
    return shares


class Participant(models.Model):
    """Somebody who can share in a check and pay towards it."""

    name = models.CharField(max_length=120, unique=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive participants stay on past checks but are hidden from new ones.",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Check(models.Model):
    """A single bill: its line items, its extras, and who owes what."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        OPEN = "open", "Open"
        SETTLED = "settled", "Settled"
        VOID = "void", "Void"

    title = models.CharField(max_length=200)
    place = models.CharField(max_length=200, blank=True)
    occurred_on = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)

    tax_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
        help_text="Applied to the subtotal after the discount.",
    )
    tip_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
        help_text="Applied to the subtotal after the discount.",
    )
    discount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
        help_text="Flat amount taken off the subtotal before tax and tip.",
    )

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-occurred_on", "-id"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["-occurred_on"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.occurred_on})"

    # -- money ---------------------------------------------------------

    @property
    def subtotal(self):
        """Sum of every line item, before discount, tax and tip.

        Falls back to a per-check aggregate, but uses the ``_items_total``
        annotation when the caller (the admin changelist) supplied one, so a
        list of checks stays a single query.
        """
        annotated = getattr(self, "_items_total", None)
        if annotated is not None:
            return money(annotated)
        total = self.items.aggregate(
            total=Sum(F("unit_price") * F("quantity"), output_field=models.DecimalField())
        )["total"]
        return money(total or ZERO)

    @property
    def discount_amount(self):
        """The discount, capped so a check can never go negative."""
        return money(min(self.discount, self.subtotal))

    @property
    def taxable_base(self):
        return money(self.subtotal - self.discount_amount)

    @property
    def tax_amount(self):
        return money(self.taxable_base * self.tax_percent / 100)

    @property
    def tip_amount(self):
        return money(self.taxable_base * self.tip_percent / 100)

    @property
    def total(self):
        """What the table actually owes the restaurant."""
        return money(self.taxable_base + self.tax_amount + self.tip_amount)

    @property
    def paid_total(self):
        annotated = getattr(self, "_paid_total", None)
        if annotated is not None:
            return money(annotated)
        total = self.payments.aggregate(total=Sum("amount"))["total"]
        return money(total or ZERO)

    @property
    def outstanding(self):
        """Still to be collected across the whole check."""
        return money(self.total - self.paid_total)

    @property
    def is_balanced(self):
        return self.outstanding == ZERO

    # -- splitting -----------------------------------------------------

    def item_totals_by_participant(self):
        """Map each participant to the value of the items they share in.

        An item with no shares assigned is treated as shared by everyone who
        appears anywhere else on the check, so a half-filled check still adds
        up to something sensible.
        """
        items = list(self.items.prefetch_related("shares__participant"))
        named = {}
        unassigned_value = ZERO

        for item in items:
            shares = list(item.shares.all())
            if not shares:
                unassigned_value += item.line_total
                continue
            splits = allocate(item.line_total, [share.weight for share in shares])
            for share, amount in zip(shares, splits):
                named[share.participant] = named.get(share.participant, ZERO) + amount

        if unassigned_value > ZERO and named:
            splits = allocate(unassigned_value, [Decimal(1)] * len(named))
            for participant, amount in zip(list(named), splits):
                named[participant] += amount

        return named

    def settlement(self):
        """Per-participant owed / paid / balance rows, largest debt first.

        Extras (discount, tax, tip) ride along in proportion to what each
        participant ate, and the rows are guaranteed to sum to ``total``.
        """
        item_totals = self.item_totals_by_participant()
        payments = {}
        for payment in self.payments.select_related("participant"):
            payments[payment.participant] = payments.get(payment.participant, ZERO) + payment.amount

        participants = list(dict.fromkeys(list(item_totals) + list(payments)))
        if not participants:
            return []

        owed_values = allocate(self.total, [item_totals.get(p, ZERO) for p in participants])

        rows = []
        for participant, owed in zip(participants, owed_values):
            paid = money(payments.get(participant, ZERO))
            rows.append(
                {
                    "participant": participant,
                    "items": money(item_totals.get(participant, ZERO)),
                    "owed": owed,
                    "paid": paid,
                    "balance": money(owed - paid),
                }
            )
        rows.sort(key=lambda row: (-row["balance"], row["participant"].name))
        return rows


class CheckItem(models.Model):
    """One line on a check, optionally split between several participants."""

    # Django reserves ``Model.check()`` for the system-check framework, so the
    # attribute is ``bill`` while everything user-facing still says "check".
    bill = models.ForeignKey(
        Check, on_delete=models.CASCADE, related_name="items", verbose_name="check"
    )
    name = models.CharField(max_length=200)
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(ZERO)]
    )
    quantity = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("1.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    participants = models.ManyToManyField(
        Participant,
        through="ItemShare",
        related_name="items",
        blank=True,
    )
    position = models.PositiveIntegerField(default=0, help_text="Sort order on the check.")

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "check item"

    def __str__(self):
        return f"{self.name} x{self.quantity:g}"

    @property
    def line_total(self):
        return money(self.unit_price * self.quantity)


class ItemShare(models.Model):
    """A participant's stake in one line item, weighted for uneven splits."""

    item = models.ForeignKey(CheckItem, on_delete=models.CASCADE, related_name="shares")
    participant = models.ForeignKey(
        Participant, on_delete=models.PROTECT, related_name="item_shares"
    )
    weight = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("1.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Relative stake: 1 and 1 splits evenly, 2 and 1 splits two thirds / one third.",
    )

    class Meta:
        ordering = ["participant__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "participant"], name="unique_share_per_item_participant"
            )
        ]

    def __str__(self):
        return f"{self.participant} → {self.item}"


class Payment(models.Model):
    """Money a participant has actually handed over towards a check."""

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        TRANSFER = "transfer", "Bank transfer"
        OTHER = "other", "Other"

    bill = models.ForeignKey(
        Check, on_delete=models.CASCADE, related_name="payments", verbose_name="check"
    )
    participant = models.ForeignKey(
        Participant, on_delete=models.PROTECT, related_name="payments"
    )
    amount = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.CARD)
    paid_at = models.DateTimeField(default=timezone.now)
    reference = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-paid_at", "-id"]

    def __str__(self):
        return f"{self.participant} paid {self.amount}"


class ReceiptUpload(models.Model):
    """A photographed receipt, its parsed contents, and the check built from it."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting to be read"
        PARSED = "parsed", "Read"
        IMPORTED = "imported", "Check created"
        FAILED = "failed", "Could not be read"

    image = models.ImageField(upload_to="receipts/%Y/%m/")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    bill = models.ForeignKey(
        Check,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploads",
        verbose_name="check",
    )
    participants = models.ManyToManyField(
        Participant,
        blank=True,
        related_name="receipt_uploads",
        help_text="Everyone here is put on every parsed item. Leave empty to split later.",
    )

    parsed_data = models.JSONField(null=True, blank=True, editable=False)
    error = models.TextField(blank=True, editable=False)
    model_used = models.CharField(max_length=64, blank=True, editable=False)
    input_tokens = models.PositiveIntegerField(null=True, blank=True, editable=False)
    output_tokens = models.PositiveIntegerField(null=True, blank=True, editable=False)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receipt_uploads",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    parsed_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-uploaded_at", "-id"]
        verbose_name = "receipt upload"

    def __str__(self):
        merchant = (self.parsed_data or {}).get("merchant")
        return merchant or f"Receipt #{self.pk or 'new'}"

    @property
    def reader_notes(self):
        """What the model flagged as unreadable or worth checking."""
        return (self.parsed_data or {}).get("reader_notes", "")

    def parse(self, save=True):
        """Read the image with Claude and store the structured result.

        Failures are recorded on the row rather than raised, so a bad photo
        leaves an explanation in the admin instead of a 500.
        """
        from .parsing import ReceiptParseError, parse_receipt_image

        self.image.open("rb")
        try:
            raw = self.image.read()
        finally:
            self.image.close()

        try:
            parsed, usage = parse_receipt_image(raw)
        except ReceiptParseError as exc:
            self.status = self.Status.FAILED
            self.error = str(exc)
            self.parsed_data = None
        else:
            self.status = self.Status.PARSED
            self.error = ""
            self.parsed_data = parsed.model_dump()
            self.model_used = usage.get("model", "")
            self.input_tokens = usage.get("input_tokens")
            self.output_tokens = usage.get("output_tokens")
        self.parsed_at = timezone.now()

        if save:
            self.save(
                update_fields=[
                    "status",
                    "error",
                    "parsed_data",
                    "model_used",
                    "input_tokens",
                    "output_tokens",
                    "parsed_at",
                ]
            )
        return self.status == self.Status.PARSED

    def create_check(self):
        """Build a draft check from the parsed data and link it to this upload."""
        from .importers import build_check_from_parsed

        if not self.parsed_data:
            raise ValueError("This receipt has not been read yet.")

        check = build_check_from_parsed(
            self.parsed_data, participants=list(self.participants.all())
        )
        self.bill = check
        self.status = self.Status.IMPORTED
        self.save(update_fields=["bill", "status"])
        return check
