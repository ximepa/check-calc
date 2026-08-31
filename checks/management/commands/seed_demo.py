"""Populate the SQLite database with a small, realistic demo check."""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from checks.models import Check, CheckItem, ItemShare, Participant, Payment


class Command(BaseCommand):
    help = "Create demo participants and checks so the admin has something to show."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing checks and participants first.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if options["reset"]:
            Payment.objects.all().delete()
            ItemShare.objects.all().delete()
            CheckItem.objects.all().delete()
            Check.objects.all().delete()
            Participant.objects.all().delete()
            self.stdout.write("Cleared existing demo data.")

        people = {}
        for name, email in [
            ("Ada", "ada@example.com"),
            ("Grace", "grace@example.com"),
            ("Linus", "linus@example.com"),
        ]:
            people[name], _ = Participant.objects.get_or_create(
                name=name, defaults={"email": email}
            )

        check, created = Check.objects.get_or_create(
            title="Friday dinner",
            defaults={
                "place": "Trattoria Nova",
                "occurred_on": timezone.localdate(),
                "status": Check.Status.OPEN,
                "tax_percent": Decimal("8.00"),
                "tip_percent": Decimal("15.00"),
                "discount": Decimal("5.00"),
                "notes": "Ada picked up the wine for the table.",
            },
        )
        if not created:
            self.stdout.write(self.style.WARNING("Demo check already exists; nothing added."))
            return

        lines = [
            ("Margherita pizza", "14.50", "1", ["Ada"]),
            ("Carbonara", "16.00", "1", ["Grace"]),
            ("Osso buco", "24.00", "1", ["Linus"]),
            ("Bottle of Chianti", "32.00", "1", ["Ada", "Grace", "Linus"]),
            ("Tiramisu", "9.00", "2", ["Grace", "Linus"]),
        ]
        for position, (name, price, quantity, sharers) in enumerate(lines, start=1):
            item = CheckItem.objects.create(
                bill=check,
                name=name,
                unit_price=Decimal(price),
                quantity=Decimal(quantity),
                position=position,
            )
            for sharer in sharers:
                ItemShare.objects.create(item=item, participant=people[sharer])

        Payment.objects.create(
            bill=check,
            participant=people["Ada"],
            amount=Decimal("60.00"),
            method=Payment.Method.CARD,
            reference="Card ending 4242",
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Created check '{check.title}': total {check.total}, outstanding {check.outstanding}."
            )
        )
        for row in check.settlement():
            self.stdout.write(
                f"  {row['participant'].name:<8} owes {row['owed']:>8}  paid {row['paid']:>8}"
                f"  balance {row['balance']:>8}"
            )
