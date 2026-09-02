"""Tests for check arithmetic, receipt parsing, and the admin screens."""

import base64
import io
import json
import logging
import os
import tempfile
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .importers import build_check_from_parsed
from .models import (
    Check,
    CheckItem,
    ItemShare,
    Participant,
    Payment,
    ReceiptUpload,
    allocate,
    money,
)
from .parsing import (
    MAX_EDGE,
    ClaudeBackend,
    GeminiBackend,
    OllamaBackend,
    ParsedReceipt,
    ReceiptParseError,
    gemini_schema,
    get_backend,
    parse_receipt_document,
    prepare_document,
    prepare_image,
    receipt_json_schema,
)


def D(value):
    return Decimal(value)


class AllocateTests(TestCase):
    def test_even_split_of_an_odd_amount_keeps_every_cent(self):
        shares = allocate(D("10.00"), [1, 1, 1])
        self.assertEqual(sum(shares), D("10.00"))
        self.assertEqual(sorted(shares), [D("3.33"), D("3.33"), D("3.34")])

    def test_weights_drive_the_proportions(self):
        shares = allocate(D("90.00"), [2, 1])
        self.assertEqual(shares, [D("60.00"), D("30.00")])

    def test_zero_weights_fall_back_to_an_even_split(self):
        self.assertEqual(allocate(D("9.00"), [0, 0, 0]), [D("3.00")] * 3)

    def test_no_weights_means_no_shares(self):
        self.assertEqual(allocate(D("9.00"), []), [])


class CheckTotalsTests(TestCase):
    def setUp(self):
        self.check = Check.objects.create(
            title="Lunch",
            occurred_on="2026-05-01",
            tax_percent=D("10.00"),
            tip_percent=D("20.00"),
            discount=D("10.00"),
        )
        CheckItem.objects.create(
            bill=self.check, name="Soup", unit_price=D("20.00"), quantity=D("2")
        )
        CheckItem.objects.create(
            bill=self.check, name="Bread", unit_price=D("10.00"), quantity=D("1")
        )

    def test_totals_apply_the_discount_before_tax_and_tip(self):
        self.assertEqual(self.check.subtotal, D("50.00"))
        self.assertEqual(self.check.discount_amount, D("10.00"))
        self.assertEqual(self.check.taxable_base, D("40.00"))
        self.assertEqual(self.check.tax_amount, D("4.00"))
        self.assertEqual(self.check.tip_amount, D("8.00"))
        self.assertEqual(self.check.total, D("52.00"))

    def test_discount_never_pushes_the_check_below_zero(self):
        self.check.discount = D("500.00")
        self.assertEqual(self.check.discount_amount, D("50.00"))
        self.assertEqual(self.check.total, D("0.00"))

    def test_outstanding_tracks_payments(self):
        ada = Participant.objects.create(name="Ada")
        self.assertEqual(self.check.outstanding, D("52.00"))
        Payment.objects.create(bill=self.check, participant=ada, amount=D("52.00"))
        self.assertEqual(self.check.paid_total, D("52.00"))
        self.assertEqual(self.check.outstanding, D("0.00"))
        self.assertTrue(self.check.is_balanced)


class SettlementTests(TestCase):
    def setUp(self):
        self.ada = Participant.objects.create(name="Ada")
        self.grace = Participant.objects.create(name="Grace")
        self.check = Check.objects.create(
            title="Dinner", occurred_on="2026-05-02", tax_percent=D("10.00")
        )

    def add_item(self, name, price, sharers, quantity="1"):
        item = CheckItem.objects.create(
            bill=self.check, name=name, unit_price=D(price), quantity=D(quantity)
        )
        for participant, weight in sharers:
            ItemShare.objects.create(item=item, participant=participant, weight=D(weight))
        return item

    def test_each_participant_pays_for_their_own_items_plus_a_share_of_tax(self):
        self.add_item("Steak", "60.00", [(self.ada, "1")])
        self.add_item("Salad", "40.00", [(self.grace, "1")])

        rows = {row["participant"]: row for row in self.check.settlement()}
        self.assertEqual(rows[self.ada]["items"], D("60.00"))
        self.assertEqual(rows[self.ada]["owed"], D("66.00"))
        self.assertEqual(rows[self.grace]["owed"], D("44.00"))
        self.assertEqual(
            sum(row["owed"] for row in rows.values()), self.check.total
        )

    def test_weighted_shares_split_a_single_item(self):
        self.add_item("Wine", "30.00", [(self.ada, "2"), (self.grace, "1")])
        rows = {row["participant"]: row for row in self.check.settlement()}
        self.assertEqual(rows[self.ada]["items"], D("20.00"))
        self.assertEqual(rows[self.grace]["items"], D("10.00"))

    def test_unassigned_items_are_shared_by_everyone_on_the_check(self):
        self.add_item("Steak", "50.00", [(self.ada, "1")])
        self.add_item("Salad", "50.00", [(self.grace, "1")])
        self.add_item("Table bread", "10.00", [])

        rows = {row["participant"]: row for row in self.check.settlement()}
        self.assertEqual(rows[self.ada]["items"], D("55.00"))
        self.assertEqual(rows[self.grace]["items"], D("55.00"))

    def test_rows_always_add_up_to_the_total_even_with_awkward_cents(self):
        self.check.tax_percent = D("7.35")
        self.check.tip_percent = D("18.00")
        self.check.save()
        self.add_item("Shared platter", "33.33", [(self.ada, "1"), (self.grace, "1")])
        self.add_item("Coffee", "4.15", [(self.ada, "1")])

        rows = self.check.settlement()
        self.assertEqual(sum(row["owed"] for row in rows), self.check.total)

    def test_a_payer_with_no_items_still_appears_with_a_credit(self):
        self.add_item("Steak", "50.00", [(self.ada, "1")])
        Payment.objects.create(bill=self.check, participant=self.grace, amount=D("55.00"))

        rows = {row["participant"]: row for row in self.check.settlement()}
        self.assertEqual(rows[self.grace]["owed"], D("0.00"))
        self.assertEqual(rows[self.grace]["balance"], D("-55.00"))
        self.assertEqual(rows[self.ada]["balance"], D("55.00"))

    def test_empty_check_settles_to_nothing(self):
        self.assertEqual(self.check.settlement(), [])


class MoneyTests(TestCase):
    def test_money_rounds_half_up(self):
        self.assertEqual(money(D("1.005")), D("1.01"))
        self.assertEqual(money(D("1.004")), D("1.00"))


class AdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("admin", "admin@example.com", "pass1234")
        cls.ada = Participant.objects.create(name="Ada")
        cls.check = Check.objects.create(title="Brunch", occurred_on="2026-05-03")
        cls.item = CheckItem.objects.create(
            bill=cls.check, name="Pancakes", unit_price=D("12.00"), quantity=D("1")
        )
        ItemShare.objects.create(item=cls.item, participant=cls.ada)
        Payment.objects.create(bill=cls.check, participant=cls.ada, amount=D("12.00"))

    def setUp(self):
        self.client.force_login(self.user)

    def test_every_registered_changelist_and_change_form_renders(self):
        pages = [
            ("checks_check", self.check.pk),
            ("checks_checkitem", self.item.pk),
            ("checks_participant", self.ada.pk),
            ("checks_payment", Payment.objects.get().pk),
        ]
        for name, pk in pages:
            with self.subTest(model=name):
                self.assertEqual(self.client.get(reverse(f"admin:{name}_changelist")).status_code, 200)
                self.assertEqual(self.client.get(reverse(f"admin:{name}_change", args=[pk])).status_code, 200)
                self.assertEqual(self.client.get(reverse(f"admin:{name}_add")).status_code, 200)

    def test_check_changelist_shows_the_calculated_total(self):
        response = self.client.get(reverse("admin:checks_check_changelist"))
        self.assertContains(response, "$12.00")

    def test_check_change_form_shows_the_settlement_table(self):
        response = self.client.get(reverse("admin:checks_check_change", args=[self.check.pk]))
        self.assertContains(response, "Who owes what")
        self.assertContains(response, "Ada")

    def test_a_credit_is_rendered_as_negative_money(self):
        Payment.objects.create(bill=self.check, participant=self.ada, amount=D("8.00"))
        response = self.client.get(reverse("admin:checks_check_change", args=[self.check.pk]))
        self.assertContains(response, "-$8.00")

    def test_settlement_filter_narrows_the_changelist(self):
        unpaid = Check.objects.create(title="Coffee run", occurred_on="2026-05-04")
        CheckItem.objects.create(
            bill=unpaid, name="Latte", unit_price=D("5.00"), quantity=D("1")
        )
        url = reverse("admin:checks_check_changelist")

        outstanding = self.client.get(url, {"settlement": "outstanding"})
        self.assertContains(outstanding, "Coffee run")
        self.assertNotContains(outstanding, "Brunch")

        balanced = self.client.get(url, {"settlement": "balanced"})
        self.assertContains(balanced, "Brunch")
        self.assertNotContains(balanced, "Coffee run")

    def test_mark_settled_skips_checks_with_money_outstanding(self):
        unpaid = Check.objects.create(title="Coffee run", occurred_on="2026-05-04")
        CheckItem.objects.create(
            bill=unpaid, name="Latte", unit_price=D("5.00"), quantity=D("1")
        )
        response = self.client.post(
            reverse("admin:checks_check_changelist"),
            {
                "action": "mark_settled",
                "_selected_action": [str(self.check.pk), str(unpaid.pk)],
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.check.refresh_from_db()
        unpaid.refresh_from_db()
        self.assertEqual(self.check.status, Check.Status.SETTLED)
        self.assertEqual(unpaid.status, Check.Status.DRAFT)

    def test_duplicate_action_copies_items_and_shares(self):
        self.client.post(
            reverse("admin:checks_check_changelist"),
            {"action": "duplicate_check", "_selected_action": [str(self.check.pk)]},
            follow=True,
        )
        copy = Check.objects.get(title="Brunch (copy)")
        self.assertEqual(copy.status, Check.Status.DRAFT)
        self.assertEqual(copy.items.count(), 1)
        self.assertEqual(copy.items.get().shares.get().participant, self.ada)
        self.assertEqual(copy.payments.count(), 0)

    def test_check_changelist_does_not_scale_queries_with_rows(self):
        """Money columns come from annotations, so rows must be free."""
        url = reverse("admin:checks_check_changelist")

        with CaptureQueriesContext(connection) as one_row:
            self.client.get(url)

        for index in range(5):
            extra = Check.objects.create(title=f"Check {index}", occurred_on="2026-05-05")
            CheckItem.objects.create(
                bill=extra, name="Item", unit_price=D("3.00"), quantity=D("1")
            )
            Payment.objects.create(bill=extra, participant=self.ada, amount=D("1.00"))

        with CaptureQueriesContext(connection) as six_rows:
            self.client.get(url)

        self.assertEqual(len(six_rows), len(one_row))

    def test_root_url_redirects_to_the_admin(self):
        response = self.client.get("/")
        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)


# ---------------------------------------------------------------------------
# Receipt upload and parsing
# ---------------------------------------------------------------------------


def make_image(size=(80, 120), colour=(240, 240, 240)):
    """A small in-memory JPEG, good enough for an ImageField."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def upload_file(name="receipt.jpg", **kwargs):
    return SimpleUploadedFile(name, make_image(**kwargs), content_type="image/jpeg")


RECEIPT_LINES = [
    "TRATTORIA NOVA",
    "Via Roma 14, Bologna",
    "2026-05-10 19:42",
    "Margherita pizza      14.50",
    "Espresso  x2           6.00",
    "SUBTOTAL              20.50",
    "TAX 10%                2.05",
    "TOTAL                 22.55",
]


def text_pdf(lines):
    """A minimal one-page PDF carrying a real text layer."""
    stream = (
        "BT /F1 12 Tf 40 760 Td 14 TL\n"
        + "".join(f"({line}) Tj T*\n" for line in lines)
        + "ET"
    )
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n{body}\nendobj\n".encode())
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def scanned_pdf(pages=1):
    """A PDF made of images only — no text layer, like a flatbed scan."""
    from PIL import Image

    sheets = [Image.new("RGB", (600, 850), (252, 252, 250)) for _ in range(pages)]
    out = io.BytesIO()
    sheets[0].save(out, format="PDF", save_all=True, append_images=sheets[1:])
    return out.getvalue()


SAMPLE_PARSE = {
    "is_receipt": True,
    "merchant": "Trattoria Nova",
    "purchased_on": "2026-05-10",
    "currency": "USD",
    "items": [
        {"name": "Margherita pizza", "quantity": 1, "unit_price": 14.50, "line_total": 14.50},
        {"name": "Espresso", "quantity": 2, "unit_price": 3.00, "line_total": 6.00},
    ],
    "subtotal": 20.50,
    "discount": None,
    "tax_amount": 2.05,
    "tip_amount": None,
    "total": 22.55,
    "reader_notes": "",
}


class PrepareImageTests(TestCase):
    def test_large_photos_are_downscaled_and_re_encoded(self):
        data, media_type = prepare_image(make_image(size=(4000, 3000)))
        self.assertEqual(media_type, "image/jpeg")

        from PIL import Image

        self.assertEqual(max(Image.open(io.BytesIO(data)).size), MAX_EDGE)

    def test_small_photos_keep_their_size(self):
        data, _ = prepare_image(make_image(size=(400, 300)))

        from PIL import Image

        self.assertEqual(Image.open(io.BytesIO(data)).size, (400, 300))

    def test_a_file_that_is_not_an_image_is_reported_clearly(self):
        with self.assertRaises(ReceiptParseError):
            prepare_image(b"this is not a picture")


class BuildCheckTests(TestCase):
    def test_a_parsed_receipt_becomes_a_draft_check(self):
        check = build_check_from_parsed(SAMPLE_PARSE)

        self.assertEqual(check.status, Check.Status.DRAFT)
        self.assertEqual(check.title, "Trattoria Nova")
        self.assertEqual(str(check.occurred_on), "2026-05-10")
        self.assertEqual(check.items.count(), 2)
        self.assertEqual(check.subtotal, D("20.50"))

    def test_a_printed_tax_amount_becomes_a_percentage(self):
        check = build_check_from_parsed(SAMPLE_PARSE)
        self.assertEqual(check.tax_percent, D("10.00"))
        self.assertEqual(check.tax_amount, D("2.05"))
        self.assertEqual(check.total, D("22.55"))

    def test_quantities_and_unit_prices_survive_the_round_trip(self):
        check = build_check_from_parsed(SAMPLE_PARSE)
        espresso = check.items.get(name="Espresso")
        self.assertEqual(espresso.quantity, D("2.00"))
        self.assertEqual(espresso.unit_price, D("3.00"))
        self.assertEqual(espresso.line_total, D("6.00"))

    def test_a_unit_price_is_recovered_from_a_row_total(self):
        data = dict(
            SAMPLE_PARSE,
            items=[{"name": "Beers", "quantity": 4, "unit_price": 0, "line_total": 18.00}],
            subtotal=18.00,
            tax_amount=None,
            total=18.00,
        )
        item = build_check_from_parsed(data).items.get()
        self.assertEqual(item.unit_price, D("4.50"))
        self.assertEqual(item.line_total, D("18.00"))

    def test_a_discount_larger_than_the_bill_is_capped(self):
        data = dict(SAMPLE_PARSE, discount=500.00, tax_amount=None, total=0)
        check = build_check_from_parsed(data)
        self.assertEqual(check.discount, D("20.50"))
        self.assertEqual(check.total, D("0.00"))

    def test_participants_are_put_on_every_item(self):
        ada = Participant.objects.create(name="Ada")
        grace = Participant.objects.create(name="Grace")
        check = build_check_from_parsed(SAMPLE_PARSE, participants=[ada, grace])

        for item in check.items.all():
            self.assertEqual(item.shares.count(), 2)
        rows = {row["participant"]: row for row in check.settlement()}
        self.assertEqual(sum(row["owed"] for row in rows.values()), check.total)

    def test_a_total_that_does_not_reconcile_is_flagged_in_the_notes(self):
        data = dict(SAMPLE_PARSE, total=99.99)
        check = build_check_from_parsed(data)
        self.assertIn("receipt says 99.99", check.notes)

    def test_reader_notes_are_carried_onto_the_check(self):
        data = dict(SAMPLE_PARSE, reader_notes="Third line was smudged.")
        self.assertIn("Third line was smudged.", build_check_from_parsed(data).notes)

    def test_an_unreadable_date_falls_back_to_today(self):
        data = dict(SAMPLE_PARSE, purchased_on="not a date")
        self.assertEqual(build_check_from_parsed(data).occurred_on, timezone.localdate())

    def test_a_missing_merchant_still_produces_a_usable_title(self):
        data = dict(SAMPLE_PARSE, merchant=None)
        self.assertEqual(build_check_from_parsed(data).title, "Uploaded receipt")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ReceiptUploadTests(TestCase):
    def test_parsing_stores_the_result_and_token_usage(self):
        upload = ReceiptUpload.objects.create(document=upload_file())
        usage = {"model": "claude-opus-5", "input_tokens": 900, "output_tokens": 120}

        with mock.patch(
            "checks.parsing.parse_receipt_document",
            return_value=(ParsedReceipt.model_validate(SAMPLE_PARSE), usage),
        ):
            self.assertTrue(upload.parse())

        upload.refresh_from_db()
        self.assertEqual(upload.status, ReceiptUpload.Status.PARSED)
        self.assertEqual(upload.parsed_data["merchant"], "Trattoria Nova")
        self.assertEqual(upload.model_used, "claude-opus-5")
        self.assertEqual(upload.input_tokens, 900)
        self.assertIsNotNone(upload.parsed_at)

    def test_a_failure_is_recorded_on_the_row_rather_than_raised(self):
        upload = ReceiptUpload.objects.create(document=upload_file())

        with mock.patch(
            "checks.parsing.parse_receipt_document",
            side_effect=ReceiptParseError("Too blurred to read."),
        ):
            self.assertFalse(upload.parse())

        upload.refresh_from_db()
        self.assertEqual(upload.status, ReceiptUpload.Status.FAILED)
        self.assertEqual(upload.error, "Too blurred to read.")
        self.assertIsNone(upload.parsed_data)

    def test_creating_a_check_links_it_back_to_the_upload(self):
        upload = ReceiptUpload.objects.create(
            document=upload_file(), parsed_data=SAMPLE_PARSE, status=ReceiptUpload.Status.PARSED
        )
        check = upload.create_check()

        upload.refresh_from_db()
        self.assertEqual(upload.bill, check)
        self.assertEqual(upload.status, ReceiptUpload.Status.IMPORTED)
        self.assertEqual(check.uploads.get(), upload)

    def test_a_check_cannot_be_created_before_the_receipt_is_read(self):
        upload = ReceiptUpload.objects.create(document=upload_file())
        with self.assertRaises(ValueError):
            upload.create_check()


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ReceiptAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("admin", "admin@example.com", "pass1234")
        cls.ada = Participant.objects.create(name="Ada")

    def setUp(self):
        self.client.force_login(self.user)
        patcher = mock.patch(
            "checks.parsing.parse_receipt_document",
            return_value=(
                ParsedReceipt.model_validate(SAMPLE_PARSE),
                {"model": "claude-opus-5", "input_tokens": 900, "output_tokens": 120},
            ),
        )
        self.parse_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def post_upload(self, **extra):
        return self.client.post(
            reverse("admin:checks_receiptupload_add"),
            {"document": upload_file(), "participants": [], **extra},
            follow=True,
        )

    def test_uploading_a_photo_reads_it_and_creates_a_draft_check(self):
        response = self.post_upload()
        self.assertEqual(response.status_code, 200)

        upload = ReceiptUpload.objects.get()
        self.assertEqual(upload.status, ReceiptUpload.Status.IMPORTED)
        self.assertEqual(upload.uploaded_by, self.user)

        check = upload.bill
        self.assertIsNotNone(check)
        self.assertEqual(check.title, "Trattoria Nova")
        self.assertEqual(check.items.count(), 2)
        self.assertEqual(check.total, D("22.55"))
        self.assertContains(response, "created draft check")

    def test_participants_chosen_at_upload_are_put_on_the_items(self):
        self.post_upload(participants=[str(self.ada.pk)])
        check = ReceiptUpload.objects.get().bill
        for item in check.items.all():
            self.assertEqual(item.shares.get().participant, self.ada)

    def test_a_failed_read_is_shown_to_the_user_not_raised(self):
        self.parse_mock.side_effect = ReceiptParseError("That is a photo of a cat.")
        response = self.post_upload()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "That is a photo of a cat.")
        upload = ReceiptUpload.objects.get()
        self.assertEqual(upload.status, ReceiptUpload.Status.FAILED)
        self.assertIsNone(upload.bill)

    def test_auto_parsing_can_be_switched_off(self):
        with self.settings(RECEIPT_PARSE_ON_UPLOAD=False):
            self.post_upload()
        upload = ReceiptUpload.objects.get()
        self.assertEqual(upload.status, ReceiptUpload.Status.PENDING)
        self.parse_mock.assert_not_called()

    def test_the_read_action_parses_without_creating_a_check(self):
        upload = ReceiptUpload.objects.create(document=upload_file())
        self.client.post(
            reverse("admin:checks_receiptupload_changelist"),
            {"action": "parse_receipts", "_selected_action": [str(upload.pk)]},
            follow=True,
        )
        upload.refresh_from_db()
        self.assertEqual(upload.status, ReceiptUpload.Status.PARSED)
        self.assertIsNone(upload.bill)

    def test_the_create_action_builds_a_check_from_stored_data(self):
        upload = ReceiptUpload.objects.create(
            document=upload_file(), parsed_data=SAMPLE_PARSE, status=ReceiptUpload.Status.PARSED
        )
        self.client.post(
            reverse("admin:checks_receiptupload_changelist"),
            {"action": "create_checks", "_selected_action": [str(upload.pk)]},
            follow=True,
        )
        upload.refresh_from_db()
        self.assertEqual(upload.bill.items.count(), 2)
        self.parse_mock.assert_not_called()

    def test_an_amount_that_cannot_be_read_renders_as_a_dash(self):
        # An unusable amount in the stored JSON makes to_decimal() return None,
        # which the money column has to render without interpolating anything.
        ReceiptUpload.objects.create(
            document=upload_file(),
            parsed_data=dict(SAMPLE_PARSE, total="n/a", tax_amount="?"),
            status=ReceiptUpload.Status.PARSED,
        )
        changelist = self.client.get(reverse("admin:checks_receiptupload_changelist"))
        change = self.client.get(
            reverse("admin:checks_receiptupload_change", args=[ReceiptUpload.objects.get().pk])
        )
        self.assertEqual(changelist.status_code, 200)
        self.assertEqual(change.status_code, 200)
        self.assertContains(change, "\u2014")

    def test_the_upload_screens_render(self):
        upload = ReceiptUpload.objects.create(
            document=upload_file(), parsed_data=SAMPLE_PARSE, status=ReceiptUpload.Status.PARSED
        )
        changelist = self.client.get(reverse("admin:checks_receiptupload_changelist"))
        change = self.client.get(
            reverse("admin:checks_receiptupload_change", args=[upload.pk])
        )
        self.assertEqual(changelist.status_code, 200)
        self.assertEqual(change.status_code, 200)
        self.assertContains(change, "Margherita pizza")
        self.assertContains(change, "What the model read")


class StubAnthropicServer(ThreadingHTTPServer):
    """A local stand-in for the Messages API that records what it was sent."""

    daemon_threads = True
    request_body = None
    payload = None


class _StubHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.request_body = json.loads(
            self.rfile.read(int(self.headers["Content-Length"]))
        )
        body = json.dumps(
            {
                "id": "msg_stub",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "content": [{"type": "text", "text": json.dumps(self.server.payload)}],
                "usage": {"input_tokens": 1234, "output_tokens": 210},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ClaudeRequestTests(TestCase):
    """Exercise the real SDK path against a stub, so the request shape is pinned.

    The mocked tests above check what we do with a parsed receipt; these check
    that what we send to Claude is a well-formed vision request in the first
    place, without spending a token.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = StubAnthropicServer(("127.0.0.1", 0), _StubHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def setUp(self):
        self.server.payload = SAMPLE_PARSE
        self.server.request_body = None
        patcher = mock.patch.dict(
            os.environ, {"ANTHROPIC_BASE_URL": self.base_url, "NO_PROXY": "127.0.0.1"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @override_settings(ANTHROPIC_API_KEY="sk-ant-stub")
    def test_the_request_is_a_vision_call_with_a_json_schema(self):
        parsed, usage = parse_receipt_document(make_image(size=(3000, 4000)))

        body = self.server.request_body
        self.assertEqual(body["model"], "claude-opus-5")
        self.assertTrue(body["system"])

        image, text = body["messages"][0]["content"]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["source"]["media_type"], "image/jpeg")
        self.assertEqual(text["type"], "text")

        schema = body["output_config"]["format"]
        self.assertEqual(schema["type"], "json_schema")
        self.assertFalse(schema["schema"]["additionalProperties"])

        self.assertEqual(parsed.merchant, "Trattoria Nova")
        self.assertEqual(
            usage,
            {
                "backend": "claude",
                "pages": 1,
                "model": "claude-opus-5",
                "input_tokens": 1234,
                "output_tokens": 210,
            },
        )

    @override_settings(ANTHROPIC_API_KEY="sk-ant-stub")
    def test_the_photo_is_downscaled_before_it_is_sent(self):
        parse_receipt_document(make_image(size=(3000, 4000)))

        source = self.server.request_body["messages"][0]["content"][0]["source"]
        decoded = base64.standard_b64decode(source["data"])

        from PIL import Image

        self.assertLessEqual(max(Image.open(io.BytesIO(decoded)).size), MAX_EDGE)

    @override_settings(ANTHROPIC_API_KEY="sk-ant-stub")
    def test_an_image_that_is_not_a_receipt_is_rejected_with_the_reason(self):
        self.server.payload = dict(
            SAMPLE_PARSE, is_receipt=False, items=[], reader_notes="This is a photo of a dog."
        )
        with self.assertRaises(ReceiptParseError) as caught:
            parse_receipt_document(make_image())
        self.assertIn("photo of a dog", str(caught.exception))

    @override_settings(ANTHROPIC_API_KEY="sk-ant-stub")
    def test_a_receipt_with_no_readable_items_is_rejected(self):
        self.server.payload = dict(SAMPLE_PARSE, items=[], reader_notes="")
        with self.assertRaises(ReceiptParseError):
            parse_receipt_document(make_image())

    @override_settings(ANTHROPIC_API_KEY="sk-ant-stub")
    def test_a_full_round_trip_produces_a_reconciled_check(self):
        self.server.payload = {
            "is_receipt": True,
            "merchant": "Kafe Pid Lypoyu",
            "purchased_on": "2026-04-02",
            "currency": "UAH",
            "items": [
                {"name": "Borshch", "quantity": 2, "unit_price": 95.0, "line_total": 190.0},
                {"name": "Kompot", "quantity": 1, "unit_price": 45.0, "line_total": 45.0},
            ],
            "subtotal": 235.0,
            "discount": None,
            "tax_amount": 47.0,
            "tip_amount": 23.5,
            "total": 305.5,
            "reader_notes": "",
        }
        parsed, _ = parse_receipt_document(make_image())
        check = build_check_from_parsed(parsed)

        self.assertEqual(check.subtotal, D("235.00"))
        self.assertEqual(check.tax_percent, D("20.00"))
        self.assertEqual(check.tip_percent, D("10.00"))
        self.assertEqual(check.total, D("305.50"))
        self.assertNotIn("Total check:", check.notes)


class PrepareDocumentTests(TestCase):
    def test_a_pdf_with_a_text_layer_is_read_as_text(self):
        document = prepare_document(
            text_pdf(RECEIPT_LINES), "bill.pdf"
        )
        self.assertEqual(document.kind, "text")
        self.assertIn("Margherita pizza", document.text)
        self.assertFalse(document.image_bytes)

    def test_a_scanned_pdf_falls_back_to_rendering_the_pages(self):
        document = prepare_document(scanned_pdf(), "scan.pdf")
        self.assertEqual(document.kind, "image")
        self.assertEqual(document.media_type, "image/jpeg")
        self.assertEqual(document.pages, 1)

    def test_a_multi_page_scan_is_stitched_into_one_image(self):
        one = prepare_document(scanned_pdf(pages=1), "scan.pdf")
        two = prepare_document(scanned_pdf(pages=2), "scan.pdf")

        from PIL import Image

        self.assertEqual(two.pages, 2)
        height = lambda doc: Image.open(io.BytesIO(doc.image_bytes)).size[1]
        self.assertGreater(height(two), height(one))

    def test_a_pdf_is_detected_from_its_content_not_its_name(self):
        self.assertEqual(prepare_document(text_pdf(RECEIPT_LINES), "photo.jpg").kind, "text")

    def test_a_pdf_whose_text_layer_is_nearly_empty_is_treated_as_a_scan(self):
        # A stray watermark is not a text layer worth reading.
        self.assertEqual(prepare_document(text_pdf(["x 1.00"]), "scan.pdf").kind, "image")

    def test_photos_still_take_the_image_path(self):
        self.assertEqual(prepare_document(make_image(), "receipt.jpg").kind, "image")

    def test_a_corrupt_file_is_reported_clearly(self):
        logging.disable(logging.CRITICAL)  # pypdf logs the damage itself
        self.addCleanup(logging.disable, logging.NOTSET)
        with self.assertRaises(ReceiptParseError):
            prepare_document(b"%PDF-1.4 but not really", "broken.pdf")


class SchemaTests(TestCase):
    def test_the_shared_schema_has_no_references_left_in_it(self):
        self.assertNotIn("$ref", json.dumps(receipt_json_schema()))
        self.assertNotIn("$defs", json.dumps(receipt_json_schema()))

    def test_the_gemini_schema_uses_its_own_dialect(self):
        schema = gemini_schema()
        self.assertEqual(schema["type"], "OBJECT")
        self.assertEqual(schema["properties"]["items"]["type"], "ARRAY")
        self.assertEqual(schema["properties"]["items"]["items"]["type"], "OBJECT")
        # Optional fields become nullable rather than a union with null.
        self.assertTrue(schema["properties"]["merchant"]["nullable"])
        self.assertEqual(schema["properties"]["merchant"]["type"], "STRING")
        self.assertNotIn("anyOf", json.dumps(schema))
        self.assertNotIn("additionalProperties", json.dumps(schema))


class BackendSelectionTests(TestCase):
    @override_settings(RECEIPT_PARSER_BACKEND="gemini")
    def test_an_explicit_backend_is_used(self):
        self.assertIsInstance(get_backend(), GeminiBackend)

    @override_settings(RECEIPT_PARSER_BACKEND="nope")
    def test_an_unknown_backend_names_the_valid_ones(self):
        with self.assertRaises(ReceiptParseError) as caught:
            get_backend()
        self.assertIn("gemini", str(caught.exception))

    @override_settings(
        RECEIPT_PARSER_BACKEND="auto", GEMINI_API_KEY="k", ANTHROPIC_API_KEY="k"
    )
    def test_auto_prefers_the_free_tier_when_both_keys_exist(self):
        self.assertIsInstance(get_backend(), GeminiBackend)

    @override_settings(RECEIPT_PARSER_BACKEND="auto", GEMINI_API_KEY="", ANTHROPIC_API_KEY="k")
    def test_auto_falls_back_to_claude_when_only_that_key_exists(self):
        self.assertIsInstance(get_backend(), ClaudeBackend)

    @override_settings(RECEIPT_PARSER_BACKEND="auto", GEMINI_API_KEY="", ANTHROPIC_API_KEY="")
    def test_auto_lands_on_ollama_when_nothing_is_configured(self):
        self.assertIsInstance(get_backend(), OllamaBackend)


class StubBackendServer(ThreadingHTTPServer):
    daemon_threads = True
    request_body = None
    response_body = None
    status = 200


class _BackendHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.request_body = json.loads(
            self.rfile.read(int(self.headers["Content-Length"]))
        )
        self.server.request_headers = {k.lower(): v for k, v in self.headers.items()}
        body = json.dumps(self.server.response_body).encode()
        self.send_response(self.server.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FreeBackendTests(TestCase):
    """Drive the Gemini and Ollama HTTP paths against a local stub."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = StubBackendServer(("127.0.0.1", 0), _BackendHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def setUp(self):
        self.server.request_body = None
        self.server.status = 200

    # -- Gemini ---------------------------------------------------------

    def gemini_reply(self, payload=None, **extra):
        self.server.response_body = {
            "candidates": [
                {
                    "content": {"parts": [{"text": json.dumps(payload or SAMPLE_PARSE)}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 800, "candidatesTokenCount": 150},
            "modelVersion": "gemini-2.5-flash",
            **extra,
        }
        return override_settings(
            RECEIPT_GEMINI_ENDPOINT=self.base_url + "/v1beta/models/{model}:generateContent"
        )

    @override_settings(GEMINI_API_KEY="test-key", RECEIPT_PARSER_BACKEND="gemini")
    def test_gemini_sends_an_inline_image_and_its_own_schema(self):
        with self.gemini_reply():
            parsed, usage = parse_receipt_document(make_image(size=(2400, 3000)), "receipt.jpg")

        body = self.server.request_body
        self.assertEqual(body["contents"][0]["parts"][0]["inline_data"]["mime_type"], "image/jpeg")
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(body["generationConfig"]["responseSchema"]["type"], "OBJECT")
        self.assertTrue(body["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(self.server.request_headers["x-goog-api-key"], "test-key")

        self.assertEqual(parsed.merchant, "Trattoria Nova")
        self.assertEqual(usage["backend"], "gemini")
        self.assertEqual(usage["input_tokens"], 800)

    @override_settings(GEMINI_API_KEY="test-key", RECEIPT_PARSER_BACKEND="gemini")
    def test_gemini_gets_text_rather_than_a_picture_for_a_digital_pdf(self):
        with self.gemini_reply():
            parse_receipt_document(text_pdf(RECEIPT_LINES), "bill.pdf")

        parts = self.server.request_body["contents"][0]["parts"]
        self.assertEqual(len(parts), 1)
        self.assertIn("Margherita pizza", parts[0]["text"])

    @override_settings(GEMINI_API_KEY="", RECEIPT_PARSER_BACKEND="gemini")
    def test_gemini_without_a_key_explains_where_to_get_one(self):
        with self.assertRaises(ReceiptParseError) as caught:
            parse_receipt_document(make_image(), "receipt.jpg")
        self.assertIn("aistudio.google.com", str(caught.exception))

    @override_settings(GEMINI_API_KEY="test-key", RECEIPT_PARSER_BACKEND="gemini")
    def test_a_gemini_quota_error_surfaces_the_reason(self):
        with self.gemini_reply():
            self.server.status = 429
            self.server.response_body = {"error": {"message": "Quota exceeded for free tier"}}
            with self.assertRaises(ReceiptParseError) as caught:
                parse_receipt_document(make_image(), "receipt.jpg")
        self.assertIn("Quota exceeded", str(caught.exception))

    @override_settings(GEMINI_API_KEY="test-key", RECEIPT_PARSER_BACKEND="gemini")
    def test_a_blocked_prompt_is_reported(self):
        with self.gemini_reply():
            self.server.response_body = {"promptFeedback": {"blockReason": "SAFETY"}}
            with self.assertRaises(ReceiptParseError) as caught:
                parse_receipt_document(make_image(), "receipt.jpg")
        self.assertIn("SAFETY", str(caught.exception))

    # -- Ollama ---------------------------------------------------------

    def ollama_reply(self, content=None):
        self.server.response_body = {
            "model": "llama3.2-vision",
            "message": {"role": "assistant", "content": content or json.dumps(SAMPLE_PARSE)},
            "prompt_eval_count": 700,
            "eval_count": 130,
        }
        return override_settings(
            OLLAMA_HOST=self.base_url, RECEIPT_PARSER_BACKEND="ollama"
        )

    def test_ollama_sends_the_image_and_a_json_schema(self):
        with self.ollama_reply():
            parsed, usage = parse_receipt_document(make_image(), "receipt.jpg")

        body = self.server.request_body
        self.assertEqual(body["model"], "llama3.2-vision")
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(len(body["messages"][1]["images"]), 1)
        self.assertEqual(body["format"]["type"], "object")

        self.assertEqual(parsed.merchant, "Trattoria Nova")
        self.assertEqual(usage, {
            "backend": "ollama",
            "pages": 1,
            "model": "llama3.2-vision",
            "input_tokens": 700,
            "output_tokens": 130,
        })

    def test_a_local_model_that_fences_its_json_is_still_understood(self):
        fenced = "```json\n" + json.dumps(SAMPLE_PARSE) + "\n```"
        with self.ollama_reply(content=fenced):
            parsed, _ = parse_receipt_document(make_image(), "receipt.jpg")
        self.assertEqual(parsed.merchant, "Trattoria Nova")

    def test_output_that_is_not_json_is_reported_not_crashed(self):
        with self.ollama_reply(content="I think this is a receipt for pizza."):
            with self.assertRaises(ReceiptParseError) as caught:
                parse_receipt_document(make_image(), "receipt.jpg")
        self.assertIn("did not return JSON", str(caught.exception))

    def test_output_with_the_wrong_field_types_is_reported(self):
        broken = dict(SAMPLE_PARSE, items=[{"name": "Pizza", "unit_price": "free"}])
        with self.ollama_reply(content=json.dumps(broken)):
            with self.assertRaises(ReceiptParseError) as caught:
                parse_receipt_document(make_image(), "receipt.jpg")
        self.assertIn("unusable fields", str(caught.exception))

    @override_settings(
        OLLAMA_HOST="http://127.0.0.1:1", RECEIPT_PARSER_BACKEND="ollama"
    )
    def test_ollama_not_running_says_how_to_start_it(self):
        with self.assertRaises(ReceiptParseError) as caught:
            parse_receipt_document(make_image(), "receipt.jpg")
        self.assertIn("ollama pull", str(caught.exception))


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class PdfUploadTests(TestCase):
    """A PDF bill should reach a check the same way a photo does."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("admin", "admin@example.com", "pass1234")

    def setUp(self):
        self.client.force_login(self.user)

    def test_uploading_a_pdf_creates_a_check(self):
        pdf = SimpleUploadedFile(
            "bill.pdf", text_pdf(RECEIPT_LINES), content_type="application/pdf"
        )
        with mock.patch(
            "checks.parsing.parse_receipt_document",
            return_value=(
                ParsedReceipt.model_validate(SAMPLE_PARSE),
                {"backend": "gemini", "pages": 1, "model": "gemini-2.5-flash",
                 "input_tokens": 800, "output_tokens": 150},
            ),
        ):
            response = self.client.post(
                reverse("admin:checks_receiptupload_add"),
                {"document": pdf, "participants": []},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        upload = ReceiptUpload.objects.get()
        self.assertTrue(upload.is_pdf)
        self.assertEqual(upload.backend, "gemini")
        self.assertEqual(upload.bill.items.count(), 2)

    def test_the_pdf_change_page_offers_the_file_instead_of_a_thumbnail(self):
        upload = ReceiptUpload.objects.create(
            document=SimpleUploadedFile("bill.pdf", text_pdf(RECEIPT_LINES)),
            parsed_data=SAMPLE_PARSE,
            status=ReceiptUpload.Status.PARSED,
        )
        response = self.client.get(
            reverse("admin:checks_receiptupload_change", args=[upload.pk])
        )
        self.assertContains(response, "application/pdf")

    def test_an_unsupported_file_type_is_rejected_by_the_form(self):
        response = self.client.post(
            reverse("admin:checks_receiptupload_add"),
            {"document": SimpleUploadedFile("notes.txt", b"just text"), "participants": []},
        )
        self.assertContains(response, "File extension")
        self.assertFalse(ReceiptUpload.objects.exists())
