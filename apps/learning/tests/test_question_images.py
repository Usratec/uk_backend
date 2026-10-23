from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.learning import services
from apps.learning.models import MediaAsset, MediaKind, Question, QuestionOption, QuestionType

from .conftest import png_bytes
from .test_admin import change_url, preview_url

pytestmark = pytest.mark.django_db

ADD_URL = reverse("admin:learning_question_add")


def image_bytes(image_format: str = "GIF", colour: str = "blue") -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (2, 2), colour).save(buffer, format=image_format)
    return buffer.getvalue()


def animated_bytes(image_format: str = "GIF", frames: int = 3) -> bytes:
    from PIL import Image

    buffer = BytesIO()
    images = [Image.new("RGB", (4, 4), colour) for colour in ("red", "green", "blue")[:frames]]
    images[0].save(buffer, format=image_format, save_all=True, append_images=images[1:], duration=100, loop=0)
    return buffer.getvalue()


def upload(name: str = "junction.png", content: bytes | None = None, content_type: str = "image/png"):
    return SimpleUploadedFile(name, content if content is not None else png_bytes(), content_type=content_type)


def question_form(chapter, **overrides) -> dict:
    form = {
        "chapter": chapter.pk,
        "key": "junction-ahead",
        "question_type": QuestionType.SINGLE,
        "prompt": "What is the sign ahead telling you?",
        "media_sign": "",
        "media_image": "",
        "explanation": "",
        "learn_more": "",
        "publish_from": "",
        "options-TOTAL_FORMS": "2",
        "options-INITIAL_FORMS": "0",
        "options-MIN_NUM_FORMS": "0",
        "options-MAX_NUM_FORMS": "1000",
        "options-0-option_id": "a",
        "options-0-text": "Give way",
        "options-0-order": "1",
        "options-0-is_correct": "on",
        "options-0-sign": "",
        "options-0-image": "",
        "options-1-option_id": "b",
        "options-1-text": "Stop",
        "options-1-order": "2",
        "options-1-sign": "",
        "options-1-image": "",
    }
    form.update(overrides)
    return form


def existing_form(question, **overrides) -> dict:
    options = list(question.options.order_by("order", "option_id"))
    form = question_form(
        question.chapter,
        key=question.key,
        prompt=question.prompt,
        question_type=question.question_type,
    )
    form.update(
        {
            "options-TOTAL_FORMS": str(len(options)),
            "options-INITIAL_FORMS": str(len(options)),
        }
    )
    for index, option in enumerate(options):
        form.update(
            {
                f"options-{index}-id": option.pk,
                f"options-{index}-question": question.pk,
                f"options-{index}-option_id": option.option_id,
                f"options-{index}-text": option.text,
                f"options-{index}-order": option.order,
                f"options-{index}-sign": option.sign_id or "",
                f"options-{index}-image": option.image_id or "",
            }
        )
        if option.is_correct:
            form[f"options-{index}-is_correct"] = "on"
    form.update(overrides)
    return form


class TestUploadingInTheQuestionForm:
    def test_a_picture_uploaded_with_a_question_is_saved_and_attached(self, editor_client, chapter, editor):
        response = editor_client.post(ADD_URL, question_form(chapter, upload_image=upload()))

        assert response.status_code == 302
        asset = MediaAsset.objects.get()
        assert asset.kind == MediaKind.IMAGE
        assert asset.uploaded_by == editor
        assert asset.size_bytes > 0
        assert Question.objects.get(key="junction-ahead").media_image == asset

    def test_an_existing_picture_can_still_be_chosen_from_the_library(self, editor_client, chapter, editor):
        chosen = services.image_asset_from_upload(upload=upload(), uploaded_by=editor)

        editor_client.post(ADD_URL, question_form(chapter, media_image=chosen.pk))

        assert Question.objects.get(key="junction-ahead").media_image == chosen
        assert MediaAsset.objects.count() == 1

    def test_an_unsupported_file_is_refused_with_a_message(self, editor_client, chapter):
        response = editor_client.post(
            ADD_URL, question_form(chapter, upload_image=upload("scan.bmp", image_bytes("BMP"), "image/bmp"))
        )

        assert response.status_code == 200
        assert "Only" in response.content.decode()
        assert not Question.objects.filter(key="junction-ahead").exists()
        assert MediaAsset.objects.count() == 0

    def test_a_picture_over_the_limit_is_refused(self, monkeypatch):
        monkeypatch.setattr(services, "MAX_IMAGE_UPLOAD_BYTES", 4)

        with pytest.raises(services.ImageUploadError, match="MB"):
            services.check_image_upload(upload())

    def test_uploading_again_replaces_the_picture(self, editor_client, chapter, editor):
        first = services.image_asset_from_upload(upload=upload(), uploaded_by=editor)
        editor_client.post(ADD_URL, question_form(chapter, media_image=first.pk))
        question = Question.objects.get(key="junction-ahead")

        editor_client.post(
            change_url(question),
            existing_form(question, media_image=first.pk, upload_image=upload("later.png", png_bytes("green"))),
        )

        question.refresh_from_db()
        assert question.media_image != first

    def test_the_picture_can_be_removed_again(self, editor_client, chapter, editor):
        chosen = services.image_asset_from_upload(upload=upload(), uploaded_by=editor)
        editor_client.post(ADD_URL, question_form(chapter, media_image=chosen.pk))
        question = Question.objects.get(key="junction-ahead")

        editor_client.post(change_url(question), existing_form(question, media_image=""))

        question.refresh_from_db()
        assert question.media_image is None
        assert MediaAsset.objects.filter(pk=chosen.pk).exists()

    def test_reopening_the_question_shows_the_picture(self, editor_client, chapter):
        editor_client.post(ADD_URL, question_form(chapter, upload_image=upload()))
        question = Question.objects.get(key="junction-ahead")

        page = editor_client.get(change_url(question)).content.decode()

        assert MediaAsset.objects.get().file.url in page

    def test_the_same_picture_twice_is_one_asset(self, editor):
        first = services.image_asset_from_upload(upload=upload(), uploaded_by=editor)
        second = services.image_asset_from_upload(upload=upload(), uploaded_by=editor)

        assert first.pk == second.pk
        assert MediaAsset.objects.count() == 1


class TestEveryFormatWeClaimToSupport:
    @pytest.mark.parametrize(
        ("name", "content", "content_type", "expected_format"),
        [
            ("junction.jpg", image_bytes("JPEG"), "image/jpeg", "JPEG"),
            ("junction.jpeg", image_bytes("JPEG"), "image/jpeg", "JPEG"),
            ("junction.png", png_bytes(), "image/png", "PNG"),
            ("junction.webp", image_bytes("WEBP"), "image/webp", "WEBP"),
            ("junction.gif", image_bytes("GIF"), "image/gif", "GIF"),
        ],
    )
    def test_a_still_arrives_unchanged(self, editor_client, chapter, name, content, content_type, expected_format):
        from PIL import Image

        response = editor_client.post(
            ADD_URL, question_form(chapter, upload_image=upload(name, content, content_type))
        )

        assert response.status_code == 302
        asset = MediaAsset.objects.get()
        stored = asset.file.read()
        assert stored == content
        assert asset.content_type == content_type
        assert Image.open(BytesIO(stored)).format == expected_format

    @pytest.mark.parametrize(("name", "image_format"), [("moving.gif", "GIF"), ("moving.webp", "WEBP")])
    def test_an_animation_is_still_moving_afterwards(self, editor_client, chapter, name, image_format):
        from PIL import Image

        content = animated_bytes(image_format)

        editor_client.post(ADD_URL, question_form(chapter, upload_image=upload(name, content, f"image/{name[-3:]}")))

        stored = MediaAsset.objects.get().file.read()
        assert stored == content
        assert getattr(Image.open(BytesIO(stored)), "n_frames", 1) == 3


class TestUploadingForEachAnswer:
    def answers_form(self, chapter, count: int = 4) -> dict:
        form = question_form(chapter, question_type=QuestionType.IMAGE)
        form["options-TOTAL_FORMS"] = str(count)
        for index, option_id in enumerate("abcdefgh"[:count]):
            form.update(
                {
                    f"options-{index}-option_id": option_id,
                    f"options-{index}-text": "",
                    f"options-{index}-order": str(index + 1),
                    f"options-{index}-sign": "",
                    f"options-{index}-image": "",
                }
            )
            form.pop(f"options-{index}-is_correct", None)
        form["options-0-is_correct"] = "on"
        return form

    def test_four_answers_take_four_pictures(self, editor_client, chapter):
        form = self.answers_form(chapter)
        colours = ["red", "green", "blue", "white"]
        for index, colour in enumerate(colours):
            form[f"options-{index}-upload_image"] = upload(f"{colour}.png", png_bytes(colour))

        response = editor_client.post(ADD_URL, form)

        assert response.status_code == 302
        options = Question.objects.get(key="junction-ahead").options.order_by("order")
        titles = [option.image.title if option.image else "" for option in options]
        assert titles == [f"{colour}.png" for colour in colours]
        assert MediaAsset.objects.count() == 4

    def test_each_picture_stays_with_its_own_answer_after_reopening(self, editor_client, chapter):
        form = self.answers_form(chapter)
        for index, colour in enumerate(["red", "green", "blue", "white"]):
            form[f"options-{index}-upload_image"] = upload(f"{colour}.png", png_bytes(colour))
        editor_client.post(ADD_URL, form)
        question = Question.objects.get(key="junction-ahead")
        attached = {option.option_id: option.image_id for option in question.options.all()}

        editor_client.post(change_url(question), existing_form(question))

        question.refresh_from_db()
        assert {option.option_id: option.image_id for option in question.options.all()} == attached

    def test_an_answer_can_keep_a_picture_chosen_from_the_library(self, editor_client, chapter, editor):
        chosen = services.image_asset_from_upload(upload=upload(), uploaded_by=editor)
        form = self.answers_form(chapter, count=2)
        form["options-1-image"] = chosen.pk

        editor_client.post(ADD_URL, form)

        assert Question.objects.get(key="junction-ahead").options.get(option_id="b").image == chosen

    def test_an_unsupported_file_on_one_answer_saves_nothing(self, editor_client, chapter):
        form = self.answers_form(chapter, count=2)
        form["options-0-upload_image"] = upload("red.png", png_bytes())
        form["options-1-upload_image"] = upload("scan.bmp", image_bytes("BMP"), "image/bmp")

        response = editor_client.post(ADD_URL, form)

        assert response.status_code == 200
        assert not Question.objects.filter(key="junction-ahead").exists()
        assert MediaAsset.objects.count() == 0


class TestWhatStudentsAndStaffSee:
    def with_picture(self, question: Question, editor) -> MediaAsset:
        asset = services.image_asset_from_upload(upload=upload(), uploaded_by=editor)
        question.media_image = asset
        question.save(update_fields=["media_image"])
        return asset

    def test_a_question_sends_its_picture_to_the_student(self, make_question, editor):
        question = make_question()
        asset = self.with_picture(question, editor)

        media = services.student_view_of_question(question)["media"]

        assert media == {"kind": "image", "url": asset.file.url}

    def test_an_answer_sends_its_own_picture(self, make_question, editor):
        question = make_question(question_type=QuestionType.IMAGE)
        asset = services.image_asset_from_upload(upload=upload(), uploaded_by=editor)
        QuestionOption.objects.filter(question=question, option_id="a").update(image=asset)

        options = {option["id"]: option for option in services.student_view_of_question(question)["options"]}

        assert options["a"]["image"] == asset.file.url
        assert "image" not in options["b"]

    def test_the_preview_page_shows_them(self, editor_client, make_question, editor):
        question = make_question()
        asset = self.with_picture(question, editor)
        QuestionOption.objects.filter(question=question, option_id="b").update(image=asset)

        page = editor_client.get(preview_url(question)).content.decode()

        assert page.count(asset.file.url) == 2
