"""Tests for check arithmetic, receipt parsing, and the admin screens."""

import base64
import io
import json
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
    ParsedReceipt,
    ReceiptParseError,
    parse_receipt_image,
    prepare_image,
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
        upload = ReceiptUpload.objects.create(image=upload_file())
        usage = {"model": "claude-opus-5", "input_tokens": 900, "output_tokens": 120}

        with mock.patch(
            "checks.parsing.parse_receipt_image",
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
        upload = ReceiptUpload.objects.create(image=upload_file())

        with mock.patch(
            "checks.parsing.parse_receipt_image",
            side_effect=ReceiptParseError("Too blurred to read."),
        ):
            self.assertFalse(upload.parse())

        upload.refresh_from_db()
        self.assertEqual(upload.status, ReceiptUpload.Status.FAILED)
        self.assertEqual(upload.error, "Too blurred to read.")
        self.assertIsNone(upload.parsed_data)

    def test_creating_a_check_links_it_back_to_the_upload(self):
        upload = ReceiptUpload.objects.create(
            image=upload_file(), parsed_data=SAMPLE_PARSE, status=ReceiptUpload.Status.PARSED
        )
        check = upload.create_check()

        upload.refresh_from_db()
        self.assertEqual(upload.bill, check)
        self.assertEqual(upload.status, ReceiptUpload.Status.IMPORTED)
        self.assertEqual(check.uploads.get(), upload)

    def test_a_check_cannot_be_created_before_the_receipt_is_read(self):
        upload = ReceiptUpload.objects.create(image=upload_file())
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
            "checks.parsing.parse_receipt_image",
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
            {"image": upload_file(), "participants": [], **extra},
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
        upload = ReceiptUpload.objects.create(image=upload_file())
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
            image=upload_file(), parsed_data=SAMPLE_PARSE, status=ReceiptUpload.Status.PARSED
        )
        self.client.post(
            reverse("admin:checks_receiptupload_changelist"),
            {"action": "create_checks", "_selected_action": [str(upload.pk)]},
            follow=True,
        )
        upload.refresh_from_db()
        self.assertEqual(upload.bill.items.count(), 2)
        self.parse_mock.assert_not_called()

    def test_the_upload_screens_render(self):
        upload = ReceiptUpload.objects.create(
            image=upload_file(), parsed_data=SAMPLE_PARSE, status=ReceiptUpload.Status.PARSED
        )
        changelist = self.client.get(reverse("admin:checks_receiptupload_changelist"))
        change = self.client.get(
            reverse("admin:checks_receiptupload_change", args=[upload.pk])
        )
        self.assertEqual(changelist.status_code, 200)
        self.assertEqual(change.status_code, 200)
        self.assertContains(change, "Margherita pizza")
        self.assertContains(change, "What Claude read")


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
        parsed, usage = parse_receipt_image(make_image(size=(3000, 4000)))

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
        self.assertEqual(usage, {"model": "claude-opus-5", "input_tokens": 1234, "output_tokens": 210})

    @override_settings(ANTHROPIC_API_KEY="sk-ant-stub")
    def test_the_photo_is_downscaled_before_it_is_sent(self):
        parse_receipt_image(make_image(size=(3000, 4000)))

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
            parse_receipt_image(make_image())
        self.assertIn("photo of a dog", str(caught.exception))

    @override_settings(ANTHROPIC_API_KEY="sk-ant-stub")
    def test_a_receipt_with_no_readable_items_is_rejected(self):
        self.server.payload = dict(SAMPLE_PARSE, items=[], reader_notes="")
        with self.assertRaises(ReceiptParseError):
            parse_receipt_image(make_image())

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
        parsed, _ = parse_receipt_image(make_image())
        check = build_check_from_parsed(parsed)

        self.assertEqual(check.subtotal, D("235.00"))
        self.assertEqual(check.tax_percent, D("20.00"))
        self.assertEqual(check.tip_percent, D("10.00"))
        self.assertEqual(check.total, D("305.50"))
        self.assertNotIn("Total check:", check.notes)
