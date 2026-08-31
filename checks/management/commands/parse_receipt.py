"""Read a receipt photo from the command line and create a check from it."""

from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from checks.models import Participant, ReceiptUpload


class Command(BaseCommand):
    help = "Parse a receipt image with Claude and create a draft check from it."

    def add_arguments(self, parser):
        parser.add_argument("image", type=Path, help="Path to the receipt photo.")
        parser.add_argument(
            "--participant",
            action="append",
            default=[],
            dest="participants",
            metavar="NAME",
            help="Put this participant on every parsed item (repeatable).",
        )
        parser.add_argument(
            "--no-check",
            action="store_true",
            help="Only read the receipt; do not create a check.",
        )

    def handle(self, *args, **options):
        path = options["image"]
        if not path.is_file():
            raise CommandError(f"No such file: {path}")

        people = []
        for name in options["participants"]:
            try:
                people.append(Participant.objects.get(name=name))
            except Participant.DoesNotExist:
                raise CommandError(f"No participant named {name!r}.")

        upload = ReceiptUpload()
        with path.open("rb") as handle:
            upload.image.save(path.name, File(handle), save=True)
        upload.participants.set(people)

        self.stdout.write(f"Reading {path.name} …")
        if not upload.parse():
            raise CommandError(upload.error)

        data = upload.parsed_data
        self.stdout.write(self.style.SUCCESS(f"Read {len(data['items'])} item(s)."))
        for item in data["items"]:
            self.stdout.write(f"  {item['name']:<40} {item['quantity']:>6g} x {item['unit_price']}")
        if data.get("reader_notes"):
            self.stdout.write(self.style.WARNING(f"Notes: {data['reader_notes']}"))

        if options["no_check"]:
            return

        check = upload.create_check()
        self.stdout.write(
            self.style.SUCCESS(
                f"Created draft check #{check.pk} '{check.title}' — total {check.total}."
            )
        )
        if check.notes:
            self.stdout.write(check.notes)
