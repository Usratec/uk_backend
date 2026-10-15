"""The CRM screens: who may open them, what they show, and what they let staff change."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus
from apps.catalog.models import Package
from apps.core.models import AuditEvent
from apps.crm.models import Candidate
from apps.entitlements import services as entitlements

pytestmark = pytest.mark.django_db

LIST_URL = reverse("admin:crm_candidate_changelist")
INDEX_URL = reverse("admin:index")


def detail_url(candidate) -> str:
    return reverse("admin:crm_candidate_change", args=[candidate.pk])


def change_form(candidate, **overrides) -> dict:
    return {
        "first_name": candidate.first_name,
        "last_name": candidate.last_name,
        "email": candidate.email,
        "phone": candidate.phone,
        "email_verified": "on" if candidate.email_verified else "",
        "is_active": "on" if candidate.is_active else "",
        # The inlines on the page post their own management form.
        "subscriptions-TOTAL_FORMS": "0",
        "subscriptions-INITIAL_FORMS": "0",
        "orders-TOTAL_FORMS": "0",
        "orders-INITIAL_FORMS": "0",
        **overrides,
    }


class TestWhoCanOpenTheCrm:
    def test_a_crm_manager_can(self, client, crm_manager, candidate):
        assert client.get(LIST_URL).status_code == 200
        assert client.get(detail_url(candidate)).status_code == 200

    def test_staff_without_the_permission_cannot(self, client, plain_staff, candidate):
        assert client.get(LIST_URL).status_code == 403
        assert client.get(detail_url(candidate)).status_code == 403

    def test_a_superuser_cannot_until_granted(self, client, superuser, candidate):
        """The CRM holds customers' personal details, so access is granted on purpose."""
        assert client.get(LIST_URL).status_code == 403

    def test_a_superuser_can_once_granted(self, client, superuser, candidate):
        from .conftest import grant

        grant(superuser, "view_candidate")

        assert client.get(LIST_URL).status_code == 200

    def test_a_superuser_keeps_the_rest_of_the_admin(self, client, superuser, candidate):
        """Only the explicitly gated areas are withheld, not the whole admin."""
        assert client.get(reverse("admin:accounts_user_changelist")).status_code == 200

    def test_a_disabled_account_cannot(self, client, crm_manager):
        crm_manager.is_active = False
        crm_manager.save(update_fields=["is_active"])

        assert client.get(LIST_URL).status_code in {302, 403}


class TestMenu:
    def test_a_crm_manager_sees_only_the_crm(self, client, crm_manager):
        response = client.get(INDEX_URL)

        app_labels = {app["app_label"] for app in response.context["available_apps"]}
        assert app_labels == {"crm"}

    def test_staff_without_crm_permissions_see_no_crm(self, client, plain_staff):
        response = client.get(INDEX_URL)

        app_labels = {app["app_label"] for app in response.context["available_apps"]}
        assert "crm" not in app_labels


class TestList:
    def test_shows_the_customer_and_their_plan(self, client, crm_manager, paying_candidate):
        response = client.get(LIST_URL)

        content = response.content.decode()
        assert "Casey Jones" in content
        assert "customer@example.com" in content
        assert "30 day access" in content

    def test_shows_initials_and_a_verified_badge(self, client, crm_manager, candidate):
        content = client.get(LIST_URL).content.decode()

        assert "CJ" in content
        assert "Verified" in content

    def test_a_customer_with_no_plan_shows_a_dash(self, client, crm_manager, candidate):
        content = client.get(LIST_URL).content.decode()

        assert "&mdash;" in content or "—" in content

    def test_staff_accounts_are_not_customers(self, client, crm_manager, candidate):
        """Colleagues must not appear in the customer list."""
        listed = client.get(LIST_URL).context["cl"].queryset

        assert list(listed.values_list("email", flat=True)) == ["customer@example.com"]

    def test_superusers_are_not_customers(self, client, crm_manager, superuser_not_logged_in):
        listed = client.get(LIST_URL).context["cl"].queryset

        assert "someroot@example.com" not in listed.values_list("email", flat=True)

    @pytest.mark.parametrize("term", ["Jones", "customer@example.com", "Casey"])
    def test_search_by_name_and_address(self, client, crm_manager, candidate, term):
        response = client.get(LIST_URL, {"q": term})

        assert "customer@example.com" in response.content.decode()

    def test_search_excludes_others(self, client, crm_manager, candidate):
        User.objects.create_user(email="someone.else@example.com", first_name="Else")

        content = client.get(LIST_URL, {"q": "Jones"}).content.decode()

        assert "someone.else@example.com" not in content

    def test_filter_by_package(self, client, crm_manager, paying_candidate, package):
        response = client.get(LIST_URL, {"subscriptions__package__id__exact": package.pk})

        assert "customer@example.com" in response.content.decode()

    def test_filter_by_verified(self, client, crm_manager, candidate):
        User.objects.create_user(email="unverified@example.com", first_name="Unv")

        content = client.get(LIST_URL, {"email_verified__exact": "0"}).content.decode()

        assert "unverified@example.com" in content
        assert "customer@example.com" not in content

    def test_newest_customers_come_first(self, client, crm_manager, candidate):
        newer = User.objects.create_user(email="newest@example.com", first_name="Newest")

        content = client.get(LIST_URL).content.decode()

        assert content.index("newest@example.com") < content.index("customer@example.com")
        assert newer.date_joined >= candidate.date_joined


class TestDetail:
    def test_shows_the_account_and_registration_details(self, client, crm_manager, candidate):
        content = client.get(detail_url(candidate)).content.decode()

        assert "Casey" in content
        assert "07700 900000" in content
        assert "Registration" in content

    def test_shows_the_plan_read_only(self, client, crm_manager, paying_candidate):
        content = client.get(detail_url(paying_candidate)).content.decode()

        assert "30 day access" in content
        assert "Learning access ends" in content
        assert "Account closes" in content

    def test_a_period_bought_for_later_is_not_shown_as_expired(
        self, client, crm_reader_with_history, paying_candidate, package
    ):
        """Renewing early schedules the next period, and support read "Expired" as a failed payment."""
        order = Order.objects.create(
            package=package,
            email=paying_candidate.email,
            user=paying_candidate,
            status=OrderStatus.PAID,
            paid_at=timezone.now(),
            original_price=package.price,
            final_price=package.price,
        )
        entitlements.activate(order, paying_candidate)

        content = client.get(detail_url(paying_candidate)).content.decode()

        assert "Scheduled" in content
        assert "Expired" not in content

    def test_the_plan_cannot_be_edited_from_here(self, client, crm_manager, paying_candidate):
        """Access follows from purchases; changing a date by hand would make the record a lie."""
        content = client.get(detail_url(paying_candidate)).content.decode()

        assert 'name="package_expires_at"' not in content
        assert 'name="account_expires_at"' not in content


class TestViewOnlyStaffCannotChangeAnything:
    def test_the_form_is_read_only(self, client, crm_manager, candidate):
        content = client.get(detail_url(candidate)).content.decode()

        assert 'name="first_name"' not in content

    def test_a_crafted_post_changes_nothing(self, client, crm_manager, candidate):
        response = client.post(detail_url(candidate), change_form(candidate, first_name="Hacked"))

        assert response.status_code == 403
        candidate.refresh_from_db()
        assert candidate.first_name == "Casey"

    def test_candidates_cannot_be_added(self, client, crm_manager):
        assert client.get(reverse("admin:crm_candidate_add")).status_code == 403

    def test_candidates_cannot_be_deleted(self, client, crm_manager, candidate):
        url = reverse("admin:crm_candidate_delete", args=[candidate.pk])

        assert client.get(url).status_code == 403
        assert User.objects.filter(pk=candidate.pk).exists()


class TestEditingWithPermission:
    def test_allowed_fields_can_be_changed(self, client, crm_editor, candidate):
        response = client.post(detail_url(candidate), change_form(candidate, first_name="Cassandra"))

        assert response.status_code == 302
        candidate.refresh_from_db()
        assert candidate.first_name == "Cassandra"

    def test_the_change_is_recorded_with_who_and_what(self, client, crm_editor, candidate):
        client.post(detail_url(candidate), change_form(candidate, first_name="Cassandra"))

        event = AuditEvent.objects.get()
        assert event.action == "crm.candidate.updated"
        assert event.actor == crm_editor
        assert event.actor_email == "editor@example.com"
        assert event.changes == {"first_name": {"from": "Casey", "to": "Cassandra"}}
        assert event.target_label == candidate.email

    def test_saving_without_changing_anything_records_nothing(self, client, crm_editor, candidate):
        client.post(detail_url(candidate), change_form(candidate))

        assert AuditEvent.objects.count() == 0

    def test_an_address_belonging_to_someone_else_is_refused(self, client, crm_editor, candidate, google_candidate):
        response = client.post(detail_url(candidate), change_form(candidate, email=google_candidate.email))

        assert response.status_code == 200
        assert "Another account already uses that email address." in response.content.decode()
        candidate.refresh_from_db()
        assert candidate.email == "customer@example.com"

    def test_a_customer_can_be_suspended(self, client, crm_editor, candidate):
        client.post(detail_url(candidate), change_form(candidate, is_active=""))

        candidate.refresh_from_db()
        assert candidate.is_active is False
        assert AuditEvent.objects.get().changes == {"is_active": {"from": "True", "to": "False"}}


class TestCandidateModel:
    def test_initials_come_from_the_name(self, candidate):
        assert Candidate.objects.get(pk=candidate.pk).initials == "CJ"

    def test_initials_fall_back_to_the_address(self, db):
        nameless = User.objects.create_user(email="zz.person@example.com", first_name="")

        assert Candidate.objects.get(pk=nameless.pk).initials == "ZZ"

    def test_full_name_falls_back_to_the_address(self, db):
        nameless = User.objects.create_user(email="zz.person@example.com", first_name="")

        assert Candidate.objects.get(pk=nameless.pk).full_name == "zz.person@example.com"

    def test_the_current_plan_is_the_one_reaching_furthest_ahead(self, paying_candidate, package):
        longer = Package.objects.create(name="90 day access", duration_days=90, price=30)
        order = Order.objects.create(
            package=longer,
            email=paying_candidate.email,
            user=paying_candidate,
            status=OrderStatus.PAID,
            paid_at=timezone.now(),
            original_price=longer.price,
            final_price=longer.price,
        )
        entitlements.activate(order, paying_candidate)

        candidate = Candidate.objects.with_current_plan().get(pk=paying_candidate.pk)
        assert candidate.current_plan_name == "90 day access"
