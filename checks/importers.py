"""Build check records out of parsed receipt data.

Kept separate from the parser so the mapping can be tested — and re-run from
stored JSON — without calling any model.
"""

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Check, CheckItem, ItemShare, money
from .parsing import ParsedReceipt, to_decimal

ZERO = Decimal("0.00")
# tax_percent / tip_percent are DecimalField(max_digits=5, decimal_places=2).
MAX_PERCENT = Decimal("999.99")


def _percent_of(amount, base):
    """Express ``amount`` as a percentage of ``base``, clamped to the column."""
    if not amount or base <= ZERO:
        return ZERO
    percent = (Decimal(amount) / base * 100).quantize(Decimal("0.01"))
    return min(max(percent, ZERO), MAX_PERCENT)


def _parse_date(value):
    if not value:
        return timezone.localdate()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return timezone.localdate()


@transaction.atomic
def build_check_from_parsed(data, participants=None, status=Check.Status.DRAFT):
    """Create a draft Check (with items and shares) from parsed receipt data.

    Receipts print tax and tip as amounts, while a Check stores them as
    percentages of the discounted subtotal — so they are converted here, and
    any resulting drift from the printed total is written into the notes for a
    human to look at.
    """
    parsed = data if isinstance(data, ParsedReceipt) else ParsedReceipt.model_validate(data)

    items = []
    for position, item in enumerate(parsed.items, start=1):
        quantity = to_decimal(item.quantity, Decimal("1.00")) or Decimal("1.00")
        if quantity <= ZERO:
            quantity = Decimal("1.00")
        unit_price = to_decimal(item.unit_price, ZERO) or ZERO
        if unit_price <= ZERO and item.line_total:
            # Some receipts only print the row total; recover the unit price.
            unit_price = money(to_decimal(item.line_total, ZERO) / quantity)
        items.append((position, item.name.strip()[:200] or f"Item {position}", unit_price, quantity))

    subtotal = money(sum((price * qty for _, _, price, qty in items), ZERO))
    discount = to_decimal(parsed.discount, ZERO) or ZERO
    discount = money(min(max(discount, ZERO), subtotal))
    base = money(subtotal - discount)

    check = Check.objects.create(
        title=parsed.merchant or "Uploaded receipt",
        place=parsed.merchant or "",
        occurred_on=_parse_date(parsed.purchased_on),
        status=status,
        discount=discount,
        tax_percent=_percent_of(to_decimal(parsed.tax_amount, ZERO), base),
        tip_percent=_percent_of(to_decimal(parsed.tip_amount, ZERO), base),
        notes=_build_notes(parsed, subtotal),
    )

    created_items = [
        CheckItem.objects.create(
            bill=check, position=position, name=name, unit_price=price, quantity=quantity
        )
        for position, name, price, quantity in items
    ]

    for item in created_items:
        for participant in participants or []:
            ItemShare.objects.create(item=item, participant=participant)

    _append_total_mismatch(check, parsed)
    return check


def _build_notes(parsed, subtotal):
    """Carry the receipt's own context across as a note on the check."""
    lines = []
    if parsed.currency:
        lines.append(f"Currency on receipt: {parsed.currency}")
    printed_subtotal = to_decimal(parsed.subtotal)
    if printed_subtotal is not None and printed_subtotal != subtotal:
        lines.append(
            f"Printed subtotal {printed_subtotal} differs from the line items ({subtotal})."
        )
    if parsed.reader_notes:
        lines.append(f"Reader notes: {parsed.reader_notes}")
    return "\n".join(lines)


def _append_total_mismatch(check, parsed):
    """Flag it in the notes when the rebuilt total misses the printed one.

    Converting printed tax/tip amounts into percentages loses fractions of a
    cent, and a misread line item shows up here too — either way the human
    reviewing the draft should see it.
    """
    printed_total = to_decimal(parsed.total)
    if printed_total is None:
        return
    difference = check.total - printed_total
    if difference == ZERO:
        return
    note = (
        f"Total check: receipt says {printed_total}, these items add up to "
        f"{check.total} (difference {difference})."
    )
    check.notes = f"{check.notes}\n{note}".strip()
    check.save(update_fields=["notes"])
