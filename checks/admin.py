"""Admin interface for building, splitting and settling checks."""

from decimal import Decimal

from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Count, DecimalField, F, IntegerField, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from .models import Check, CheckItem, ItemShare, Participant, Payment

admin.site.site_header = "Check Calc administration"
admin.site.site_title = "Check Calc"
admin.site.index_title = "Checks, participants and settlements"

ZERO = Decimal("0.00")


def currency(amount):
    """Render a Decimal as right-aligned money, negatives in red."""
    if amount is None:
        return format_html('<span class="cc-money">&mdash;</span>')
    symbol = getattr(settings, "CURRENCY_SYMBOL", "$")
    negative = amount < 0
    return format_html(
        '<span class="cc-money" style="color:{};white-space:nowrap">{}{}{}</span>',
        "#b32d2e" if negative else "inherit",
        "-" if negative else "",
        symbol,
        f"{abs(amount):,.2f}",
    )


MONEY = DecimalField(max_digits=12, decimal_places=2)


def _per_check(model, aggregate, default=ZERO, output_field=None):
    """Aggregate one related table per check.

    A plain ``annotate(Sum(...), Sum(...))`` over two different relations
    multiplies the rows together; a correlated subquery per relation keeps
    each total honest and the changelist at one query.
    """
    output_field = output_field or MONEY
    return Coalesce(
        Subquery(
            model.objects.filter(bill=OuterRef("pk"))
            .order_by()
            .values("bill")
            .annotate(total=aggregate)
            .values("total")[:1],
            output_field=output_field,
        ),
        default,
        output_field=output_field,
    )


class ItemShareInline(admin.TabularInline):
    """Who shares a line item, and in what proportion."""

    model = ItemShare
    extra = 2
    autocomplete_fields = ["participant"]
    verbose_name_plural = "Shared by"


class CheckItemInline(admin.TabularInline):
    model = CheckItem
    extra = 3
    fields = ("position", "name", "unit_price", "quantity", "line_total_display", "shared_by")
    readonly_fields = ("line_total_display", "shared_by")
    ordering = ("position", "id")

    @admin.display(description="Line total")
    def line_total_display(self, obj):
        if obj.pk is None:
            return "—"
        return currency(obj.line_total)

    @admin.display(description="Shared by")
    def shared_by(self, obj):
        """Names of the sharers, with a link to edit the split."""
        if obj.pk is None:
            return "Save the check, then open the item to split it."
        names = ", ".join(share.participant.name for share in obj.shares.all())
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:checks_checkitem_change", args=[obj.pk]),
            names or "everyone (unassigned)",
        )

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("shares__participant")


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 1
    autocomplete_fields = ["participant"]
    fields = ("participant", "amount", "method", "paid_at", "reference")


class SettlementStatusFilter(admin.SimpleListFilter):
    """Filter by whether the money actually collected covers the check."""

    title = "settlement"
    parameter_name = "settlement"

    def lookups(self, request, model_admin):
        return [
            ("balanced", "Fully paid"),
            ("outstanding", "Money outstanding"),
            ("unpaid", "Nothing paid yet"),
        ]

    def queryset(self, request, queryset):
        value = self.value()
        if value is None:
            return queryset
        # Settlement depends on the per-check totals, so it is evaluated in
        # Python; the changelist annotations keep that to a single query.
        ids = []
        for check in queryset:
            outstanding = check.outstanding
            if value == "balanced" and outstanding <= ZERO:
                ids.append(check.pk)
            elif value == "outstanding" and outstanding > ZERO:
                ids.append(check.pk)
            elif value == "unpaid" and check.paid_total == ZERO:
                ids.append(check.pk)
        return queryset.filter(pk__in=ids)


@admin.register(Check)
class CheckAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "occurred_on",
        "place",
        "status_badge",
        "item_count",
        "subtotal_display",
        "total_display",
        "paid_display",
        "outstanding_display",
    )
    list_filter = ("status", SettlementStatusFilter, "occurred_on")
    search_fields = ("title", "place", "notes", "items__name", "payments__participant__name")
    date_hierarchy = "occurred_on"
    ordering = ("-occurred_on", "-id")
    list_per_page = 25
    inlines = [CheckItemInline, PaymentInline]
    actions = ["mark_open", "mark_settled", "duplicate_check"]
    save_on_top = True

    fieldsets = (
        (None, {"fields": ("title", "place", "occurred_on", "status")}),
        (
            "Extras",
            {
                "fields": ("discount", "tax_percent", "tip_percent"),
                "description": "Discount comes off the subtotal first; tax and tip apply to what is left.",
            },
        ),
        ("Notes", {"fields": ("notes",), "classes": ("collapse",)}),
        (
            "Calculated",
            {"fields": ("totals_panel", "settlement_panel")},
        ),
        (
            "History",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )
    readonly_fields = ("totals_panel", "settlement_panel", "created_at", "updated_at")

    def get_queryset(self, request):
        """Pre-compute the money columns so the changelist stays cheap."""
        return (
            super()
            .get_queryset(request)
            .annotate(
                _items_total=_per_check(
                    CheckItem, Sum(F("unit_price") * F("quantity"), output_field=MONEY)
                ),
                _paid_total=_per_check(Payment, Sum("amount")),
                _item_count=_per_check(
                    CheckItem,
                    Count("id"),
                    default=0,
                    output_field=IntegerField(),
                ),
            )
        )

    # -- computed columns ----------------------------------------------

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        colours = {
            Check.Status.DRAFT: ("#6c757d", "#f2f2f2"),
            Check.Status.OPEN: ("#0b6bcb", "#e8f1fc"),
            Check.Status.SETTLED: ("#1e7a3c", "#e8f6ec"),
            Check.Status.VOID: ("#b32d2e", "#fdeaea"),
        }
        colour, background = colours.get(obj.status, ("#333", "#eee"))
        return format_html(
            '<span style="background:{};color:{};padding:2px 8px;'
            'border-radius:10px;font-weight:600;white-space:nowrap">{}</span>',
            background,
            colour,
            obj.get_status_display(),
        )

    @admin.display(description="Items", ordering="_item_count")
    def item_count(self, obj):
        # Note the explicit None check: passing obj.items.count() as getattr's
        # default would run that query for every row, annotation or not.
        annotated = getattr(obj, "_item_count", None)
        return obj.items.count() if annotated is None else annotated

    @admin.display(description="Subtotal", ordering="_items_total")
    def subtotal_display(self, obj):
        return currency(obj.subtotal)

    @admin.display(description="Total")
    def total_display(self, obj):
        return currency(obj.total)

    @admin.display(description="Paid", ordering="_paid_total")
    def paid_display(self, obj):
        return currency(obj.paid_total)

    @admin.display(description="Outstanding")
    def outstanding_display(self, obj):
        return currency(obj.outstanding)

    # -- change-form panels --------------------------------------------

    @admin.display(description="Totals")
    def totals_panel(self, obj):
        if obj.pk is None:
            return "Save the check to see its totals."
        rows = [
            ("Subtotal", obj.subtotal),
            ("Discount", -obj.discount_amount),
            (f"Tax ({obj.tax_percent:g}%)", obj.tax_amount),
            (f"Tip ({obj.tip_percent:g}%)", obj.tip_amount),
            ("Total", obj.total),
            ("Paid", obj.paid_total),
            ("Outstanding", obj.outstanding),
        ]
        body = format_html_join(
            "",
            '<tr><th style="text-align:left;padding:2px 16px 2px 0">{}</th>'
            '<td style="text-align:right">{}</td></tr>',
            ((label, currency(value)) for label, value in rows),
        )
        return format_html('<table style="border:0">{}</table>', body)

    @admin.display(description="Who owes what")
    def settlement_panel(self, obj):
        if obj.pk is None:
            return "Save the check, add items, then assign shares."
        rows = obj.settlement()
        if not rows:
            return "No shares or payments recorded yet."
        body = format_html_join(
            "",
            "<tr><td>{}</td><td style='text-align:right'>{}</td>"
            "<td style='text-align:right'>{}</td><td style='text-align:right'>{}</td>"
            "<td style='text-align:right'>{}</td></tr>",
            (
                (
                    row["participant"].name,
                    currency(row["items"]),
                    currency(row["owed"]),
                    currency(row["paid"]),
                    currency(row["balance"]),
                )
                for row in rows
            ),
        )
        return format_html(
            '<table style="border:0"><thead><tr>'
            "<th style='text-align:left'>Participant</th><th>Items</th>"
            "<th>Owed</th><th>Paid</th><th>Balance</th></tr></thead><tbody>{}</tbody></table>",
            body,
        )

    # -- actions --------------------------------------------------------

    @admin.action(description="Mark selected checks as open")
    def mark_open(self, request, queryset):
        updated = queryset.update(status=Check.Status.OPEN)
        self.message_user(request, f"{updated} check(s) marked open.", messages.SUCCESS)

    @admin.action(description="Mark selected checks as settled")
    def mark_settled(self, request, queryset):
        settled = 0
        skipped = []
        for check in queryset:
            if check.outstanding > ZERO:
                skipped.append(check.title)
                continue
            Check.objects.filter(pk=check.pk).update(status=Check.Status.SETTLED)
            settled += 1
        if settled:
            self.message_user(request, f"{settled} check(s) marked settled.", messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                "Still has money outstanding, left untouched: " + ", ".join(skipped),
                messages.WARNING,
            )

    @admin.action(description="Duplicate selected checks (items and shares)")
    def duplicate_check(self, request, queryset):
        created = 0
        for check in queryset:
            items = list(check.items.prefetch_related("shares"))
            check.pk = None
            check.status = Check.Status.DRAFT
            check.title = f"{check.title} (copy)"
            check.save()
            for item in items:
                shares = list(item.shares.all())
                item.pk = None
                item.bill = check
                item.save()
                for share in shares:
                    ItemShare.objects.create(
                        item=item, participant_id=share.participant_id, weight=share.weight
                    )
            created += 1
        self.message_user(request, f"Duplicated {created} check(s) as drafts.", messages.SUCCESS)


@admin.register(CheckItem)
class CheckItemAdmin(admin.ModelAdmin):
    list_display = ("name", "bill", "unit_price", "quantity", "line_total_display", "sharers")
    list_filter = ("bill__status", "bill")
    search_fields = ("name", "bill__title")
    autocomplete_fields = ["bill"]
    inlines = [ItemShareInline]
    ordering = ("-bill__occurred_on", "position", "id")

    @admin.display(description="Line total")
    def line_total_display(self, obj):
        return currency(obj.line_total)

    @admin.display(description="Shared by")
    def sharers(self, obj):
        names = [share.participant.name for share in obj.shares.all()]
        return ", ".join(names) if names else "everyone (unassigned)"

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("bill")
            .prefetch_related("shares__participant")
        )


@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "phone", "is_active", "checks_shared", "total_paid")
    list_filter = ("is_active",)
    search_fields = ("name", "email", "phone")
    ordering = ("name",)
    fieldsets = (
        (None, {"fields": ("name", "is_active")}),
        ("Contact", {"fields": ("email", "phone")}),
        ("Notes", {"fields": ("notes",), "classes": ("collapse",)}),
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                _checks_shared=Count("item_shares__item__bill", distinct=True),
                _total_paid=Coalesce(
                    Subquery(
                        Payment.objects.filter(participant=OuterRef("pk"))
                        .order_by()
                        .values("participant")
                        .annotate(total=Sum("amount"))
                        .values("total")[:1],
                        output_field=DecimalField(max_digits=12, decimal_places=2),
                    ),
                    ZERO,
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                ),
            )
        )

    @admin.display(description="Checks", ordering="_checks_shared")
    def checks_shared(self, obj):
        return obj._checks_shared

    @admin.display(description="Total paid", ordering="_total_paid")
    def total_paid(self, obj):
        return currency(obj._total_paid)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("participant", "bill", "amount_display", "method", "paid_at", "reference")
    list_filter = ("method", "paid_at", "bill__status")
    search_fields = ("participant__name", "bill__title", "reference")
    autocomplete_fields = ["bill", "participant"]
    date_hierarchy = "paid_at"
    ordering = ("-paid_at",)

    @admin.display(description="Amount", ordering="amount")
    def amount_display(self, obj):
        return currency(obj.amount)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("participant", "bill")
