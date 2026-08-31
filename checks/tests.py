"""Tests for check arithmetic and the admin screens that surface it."""

from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import TestCase
from django.urls import reverse

from .models import Check, CheckItem, ItemShare, Participant, Payment, allocate, money


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
