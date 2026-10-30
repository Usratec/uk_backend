from django.db import models

from apps.core import images


def _strip_pending_upload(instance: models.Model, attname: str, *, images_only: bool) -> None:
    field_file = getattr(instance, attname, None)
    if not field_file or field_file._committed or not field_file.name:
        return

    raw = field_file.file
    if images.is_sanitized(raw):
        return

    cleaned = images.without_metadata(raw) if images_only else images.without_metadata_if_image(raw)
    if cleaned is not raw:
        setattr(instance, attname, cleaned)


class SanitizedImageField(models.ImageField):
    def pre_save(self, model_instance: models.Model, add: bool):
        _strip_pending_upload(model_instance, self.attname, images_only=True)
        return super().pre_save(model_instance, add)


class SanitizedFileField(models.FileField):
    def pre_save(self, model_instance: models.Model, add: bool):
        _strip_pending_upload(model_instance, self.attname, images_only=False)
        return super().pre_save(model_instance, add)
