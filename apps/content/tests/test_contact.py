from datetime import timedelta

import pytest
from django.conf import settings
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.content import services
from apps.content.models import RETRYABLE_DELIVERY, ContactDelivery, ContactSubmission, ContactTopic

pytestmark = pytest.mark.django_db

CONTACT_URL = reverse("contact-create")

VALID = {
    "name": "Ada Lovelace",
    "email": "ada@example.com",
    "topic": "billing",
    "message": "My card was charged twice.",
}


def submit(api, **changes):
    payload = {**VALID, **changes}
    return api.post(CONTACT_URL, {key: value for key, value in payload.items() if value is not None}, format="json")


def aged(submission: ContactSubmission, hours: int = 1) -> ContactSubmission:
    ContactSubmission.objects.filter(pk=submission.pk).update(created_at=timezone.now() - timedelta(hours=hours))
    return submission


class TestSubmit:
    def test_a_visitor_who_is_not_signed_in_can_send_a_message(self, api):
        response = submit(api)

        assert response.status_code == 201
        assert response.data == {"message": "Your message has been sent successfully."}

    def test_the_message_is_saved(self, api):
        submit(api)

        submission = ContactSubmission.objects.get()
        assert submission.name == "Ada Lovelace"
        assert submission.email == "ada@example.com"
        assert submission.topic == ContactTopic.BILLING
        assert submission.message == "My card was charged twice."
        assert submission.user is None

    def test_a_signed_in_visitor_can_send_a_message(self, api, student):
        api.force_authenticate(student)

        assert submit(api).status_code == 201

    def test_a_signed_in_visitors_message_is_tied_to_their_account(self, api, student):
        api.force_authenticate(student)

        submit(api)

        assert ContactSubmission.objects.get().user == student

    def test_a_user_id_in_the_payload_is_ignored(self, api, root):
        submit(api, user=root.pk)

        assert ContactSubmission.objects.get().user is None

    def test_whitespace_around_the_fields_is_trimmed(self, api):
        submit(api, name="  Ada  ", email="  ada@example.com  ", message="  Hello  ")

        submission = ContactSubmission.objects.get()
        assert (submission.name, submission.email, submission.message) == ("Ada", "ada@example.com", "Hello")

    def test_the_topic_is_optional(self, api):
        response = submit(api, topic=None)

        assert response.status_code == 201
        assert ContactSubmission.objects.get().topic == ""


class TestDelivery:
    def test_it_reaches_the_support_inbox(self, api):
        submit(api)

        assert [message.to for message in mail.outbox] == [[settings.SUPPORT_EMAIL]]

    def test_the_reply_goes_to_the_visitor(self, api):
        submit(api)

        assert mail.outbox[0].reply_to == ["ada@example.com"]

    def test_the_subject_names_the_topic(self, api):
        submit(api)

        assert mail.outbox[0].subject == "New Contact Form Submission - Billing"

    def test_the_subject_says_so_when_no_topic_was_chosen(self, api):
        submit(api, topic=None)

        assert mail.outbox[0].subject == "New Contact Form Submission - No topic"

    def test_everything_the_visitor_submitted_is_in_the_email(self, api):
        submit(api)

        body = mail.outbox[0].body
        assert "Ada Lovelace" in body
        assert "ada@example.com" in body
        assert "Billing" in body
        assert "My card was charged twice." in body

    def test_the_account_is_named_when_the_visitor_was_signed_in(self, api, student):
        api.force_authenticate(student)

        submit(api)

        assert str(student.pk) in mail.outbox[0].body
        assert student.email in mail.outbox[0].body

    def test_an_anonymous_message_says_there_is_no_account(self, api):
        submit(api)

        assert "not signed in" in mail.outbox[0].body

    def test_a_delivered_message_is_recorded_as_sent(self, api):
        submit(api)

        submission = ContactSubmission.objects.get()
        assert submission.delivery_status == ContactDelivery.SENT
        assert submission.delivered_at is not None

    def test_sent_is_recorded_only_once_the_mail_backend_has_taken_the_message(self, api, monkeypatch):
        seen = {}
        real_send_now = services.email.send_now

        def watch(**kwargs):
            seen["status_while_sending"] = ContactSubmission.objects.get().delivery_status
            return real_send_now(**kwargs)

        monkeypatch.setattr("apps.content.services.email.send_now", watch)

        submit(api)

        assert seen["status_while_sending"] == ContactDelivery.PENDING
        assert ContactSubmission.objects.get().delivery_status == ContactDelivery.SENT

    def test_sent_says_the_provider_accepted_it_not_that_the_inbox_received_it(self, api):
        submit(api)

        status_field = ContactSubmission._meta.get_field("delivery_status")
        assert ContactDelivery(ContactDelivery.SENT).label == "Handed to the mail provider"
        assert "not that it reached the inbox" in status_field.help_text

    def test_the_visitors_text_cannot_add_email_headers(self, api):
        submit(api, name="Ada\nBcc: someone@example.com", message="Hello")

        assert "Bcc" not in dict(mail.outbox[0].message().items())


class TestDeliveryFailures:
    @pytest.fixture
    def mail_down(self, monkeypatch):
        def explode(**kwargs):
            raise OSError("the mail server is refusing connections")

        monkeypatch.setattr("apps.content.services.email.send_now", explode)

    def test_the_visitor_is_still_told_their_message_was_received(self, api, mail_down):
        assert submit(api).status_code == 201

    def test_the_message_is_kept_so_it_is_not_lost(self, api, mail_down):
        submit(api)

        assert ContactSubmission.objects.get().message == "My card was charged twice."

    def test_a_message_that_could_not_be_delivered_is_not_recorded_as_sent(self, api, mail_down):
        submit(api)

        submission = ContactSubmission.objects.get()
        assert submission.delivery_status == ContactDelivery.PENDING
        assert submission.delivered_at is None

    def test_the_delivery_job_records_a_failure_once_its_retries_run_out(self, api, mail_down):
        from apps.content.tasks import deliver_contact_message

        submission = ContactSubmission.objects.create(**VALID)

        deliver_contact_message.apply(kwargs={"submission_id": submission.pk}, retries=3)

        assert ContactSubmission.objects.get().delivery_status == ContactDelivery.FAILED

    def test_a_failed_message_is_still_eligible_for_the_next_sweep(self, api, mail_down):
        from apps.content.tasks import deliver_contact_message

        submission = ContactSubmission.objects.create(**VALID)

        deliver_contact_message.apply(kwargs={"submission_id": submission.pk}, retries=3)

        assert ContactSubmission.objects.get().delivery_status in RETRYABLE_DELIVERY

    def test_a_failed_message_is_delivered_once_the_mail_provider_is_working_again(self, api, monkeypatch):
        from apps.content.tasks import deliver_contact_message, deliver_pending_contact_messages

        def explode(**kwargs):
            raise OSError("the mail server is refusing connections")

        real_send_now = services.email.send_now
        monkeypatch.setattr("apps.content.services.email.send_now", explode)
        submission = aged(ContactSubmission.objects.create(**VALID))

        deliver_contact_message.apply(kwargs={"submission_id": submission.pk}, retries=3)
        assert ContactSubmission.objects.get().delivery_status == ContactDelivery.FAILED

        monkeypatch.setattr("apps.content.services.email.send_now", real_send_now)

        assert deliver_pending_contact_messages() == 1
        assert ContactSubmission.objects.get().delivery_status == ContactDelivery.SENT
        assert mail.outbox[0].to == [settings.SUPPORT_EMAIL]

    def test_a_sweep_that_fails_again_leaves_the_message_eligible_rather_than_abandoning_it(self, api, monkeypatch):
        from apps.content.tasks import deliver_pending_contact_messages

        attempts = []

        def explode(**kwargs):
            attempts.append(kwargs["subject"])
            raise OSError("the mail server is refusing connections")

        monkeypatch.setattr("apps.content.services.email.send_now", explode)
        aged(ContactSubmission.objects.create(**{**VALID, "delivery_status": ContactDelivery.FAILED}))

        assert deliver_pending_contact_messages() == 0
        assert len(attempts) == 1
        assert ContactSubmission.objects.get().delivery_status in RETRYABLE_DELIVERY

    def test_the_sweep_keeps_trying_a_message_across_several_runs(self, api, monkeypatch):
        from apps.content.tasks import deliver_pending_contact_messages

        attempts = []

        def explode(**kwargs):
            attempts.append(kwargs["subject"])
            raise OSError("the mail server is refusing connections")

        monkeypatch.setattr("apps.content.services.email.send_now", explode)
        aged(ContactSubmission.objects.create(**VALID))

        for _ in range(3):
            deliver_pending_contact_messages()

        assert len(attempts) == 3

    def test_the_delivery_job_does_not_send_a_message_twice(self, api):
        from apps.content.tasks import deliver_contact_message

        submit(api)
        mail.outbox.clear()

        deliver_contact_message.apply(kwargs={"submission_id": ContactSubmission.objects.get().pk})

        assert mail.outbox == []

    def test_the_failure_is_not_shown_to_the_visitor(self, api, mail_down):
        response = submit(api)

        assert "refusing connections" not in str(response.data)

    def test_a_message_left_pending_is_delivered_by_the_sweep(self, api):
        from apps.content.tasks import deliver_pending_contact_messages

        aged(ContactSubmission.objects.create(**{**VALID, "delivery_status": ContactDelivery.PENDING}))

        assert deliver_pending_contact_messages() == 1
        assert ContactSubmission.objects.get().delivery_status == ContactDelivery.SENT
        assert mail.outbox[0].to == [settings.SUPPORT_EMAIL]

    def test_the_sweep_leaves_a_message_that_was_already_sent_alone(self, api):
        from apps.content.tasks import deliver_pending_contact_messages

        submit(api)
        mail.outbox.clear()

        assert deliver_pending_contact_messages() == 0
        assert mail.outbox == []


class TestValidation:
    def test_a_missing_name_is_refused(self, api):
        response = submit(api, name=None)

        assert response.status_code == 400
        assert "name" in response.data

    def test_a_name_of_one_character_is_refused(self, api):
        response = submit(api, name="A")

        assert response.status_code == 400
        assert "name" in response.data

    def test_a_name_over_sixty_characters_is_refused(self, api):
        response = submit(api, name="A" * 61)

        assert response.status_code == 400
        assert "name" in response.data

    def test_a_name_of_exactly_sixty_characters_is_accepted(self, api):
        assert submit(api, name="A" * 60).status_code == 201

    def test_an_invalid_email_is_refused(self, api):
        response = submit(api, email="not-an-email")

        assert response.status_code == 400
        assert "email" in response.data

    def test_a_missing_email_is_refused(self, api):
        response = submit(api, email=None)

        assert response.status_code == 400
        assert "email" in response.data

    def test_a_missing_message_is_refused(self, api):
        response = submit(api, message=None)

        assert response.status_code == 400
        assert "message" in response.data

    def test_an_empty_message_is_refused(self, api):
        response = submit(api, message="")

        assert response.status_code == 400
        assert "message" in response.data

    def test_a_message_of_only_whitespace_is_refused(self, api):
        response = submit(api, message="   ")

        assert response.status_code == 400
        assert "message" in response.data

    def test_a_message_longer_than_the_limit_is_refused(self, api):
        response = submit(api, message="A" * (ContactSubmission.MESSAGE_MAX_LENGTH + 1))

        assert response.status_code == 400
        assert "message" in response.data

    def test_an_unknown_topic_is_refused(self, api):
        response = submit(api, topic="refunds")

        assert response.status_code == 400
        assert "topic" in response.data

    def test_nothing_is_saved_when_the_form_is_invalid(self, api):
        submit(api, email="not-an-email")

        assert ContactSubmission.objects.count() == 0
        assert mail.outbox == []

    def test_every_invalid_field_is_named_at_once(self, api):
        response = submit(api, name="A", email="not-an-email", topic="refunds")

        assert set(response.data) == {"name", "email", "topic"}


class TestAdmin:
    CHANGELIST_URL = reverse("admin:content_contactsubmission_changelist")

    @pytest.fixture
    def submission(self) -> ContactSubmission:
        return ContactSubmission.objects.create(**VALID)

    def detail_url(self, submission: ContactSubmission) -> str:
        return reverse("admin:content_contactsubmission_change", args=[submission.pk])

    def test_a_support_agent_can_read_the_messages(self, client, support_agent, submission):
        assert client.get(self.CHANGELIST_URL).status_code == 200
        assert client.get(self.detail_url(submission)).status_code == 200

    def test_staff_without_the_permission_cannot(self, client, content_editor, submission):
        assert client.get(self.CHANGELIST_URL).status_code == 403

    def test_a_superuser_cannot_until_granted(self, client, root, submission):
        client.force_login(root)

        assert client.get(self.CHANGELIST_URL).status_code == 403

    def test_a_support_agent_cannot_change_a_message(self, client, support_agent, submission):
        response = client.post(self.detail_url(submission), {"name": "Edited"})

        assert response.status_code == 403
        assert ContactSubmission.objects.get().name == "Ada Lovelace"

    def test_a_support_agent_cannot_delete_a_message(self, client, support_agent, submission):
        url = reverse("admin:content_contactsubmission_delete", args=[submission.pk])

        assert client.post(url, {"post": "yes"}).status_code == 403
        assert ContactSubmission.objects.count() == 1

    def test_an_account_granted_change_and_delete_still_cannot_edit_or_delete_a_message(
        self, client, support_agent, submission
    ):
        from django.contrib.auth.models import Permission

        support_agent.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="content",
                codename__in=["change_contactsubmission", "delete_contactsubmission"],
            )
        )
        delete_url = reverse("admin:content_contactsubmission_delete", args=[submission.pk])

        assert client.post(self.detail_url(submission), {"name": "Edited"}).status_code == 403
        assert client.post(delete_url, {"post": "yes"}).status_code == 403
        assert ContactSubmission.objects.get().name == "Ada Lovelace"

    def test_a_support_agent_does_not_see_the_linked_accounts_email(self, client, support_agent, student):
        submission = ContactSubmission.objects.create(**{**VALID, "user": student})

        changelist = client.get(self.CHANGELIST_URL).content.decode()
        detail = client.get(self.detail_url(submission)).content.decode()

        assert student.email not in changelist
        assert student.email not in detail
        assert str(student.pk) in changelist

    def test_the_support_agent_role_exists_after_migrating(self, db):
        from django.contrib.auth.models import Group

        group = Group.objects.get(name="Support Agent")

        assert {
            f"{permission.content_type.app_label}.{permission.codename}"
            for permission in group.permissions.select_related("content_type")
        } == {"content.view_contactsubmission"}


class TestThrottling:
    def test_the_sixth_message_from_one_visitor_in_an_hour_is_refused(self, api):
        for _ in range(5):
            assert submit(api).status_code == 201

        assert submit(api).status_code == 429

    def test_a_signed_in_visitor_is_throttled_too(self, api, student):
        api.force_authenticate(student)

        for _ in range(10):
            assert submit(api).status_code == 201

        assert submit(api).status_code == 429

    def test_an_ordinary_submission_is_not_throttled(self, api):
        assert submit(api).status_code == 201
