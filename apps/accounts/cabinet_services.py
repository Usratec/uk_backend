"""Managing your own account: your profile, your email address and your password."""

import logging
from datetime import timedelta
from typing import Any

from django.core.files.uploadedfile import UploadedFile
from django.utils import timezone

from apps.accounts import emails, services
from apps.accounts.models import SecurityToken, TokenPurpose, User
from apps.core import images

logger = logging.getLogger(__name__)

EMAIL_CHANGE_VALID_FOR = timedelta(hours=24)


class EmailAlreadyTakenError(Exception):
    """Another account already uses that address."""


class ProfilePhotoError(Exception):
    """The file the learner chose cannot be used as a profile photo."""


def update_profile(user: User, changes: dict[str, Any]) -> User:
    if not changes:
        return user

    fields = list(changes)

    if "marketing_email_opt_in" in changes and changes["marketing_email_opt_in"] != user.marketing_email_opt_in:
        user.marketing_email_opt_in_at = timezone.now()
        fields.append("marketing_email_opt_in_at")

    for field, value in changes.items():
        setattr(user, field, value)

    user.save(update_fields=[*fields, "updated_at"])
    return user


PHOTO_UPLOAD_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}

PHOTO_UPLOAD_FORMATS = {"JPEG", "PNG", "WEBP"}

MAX_PHOTO_BYTES = 10 * 1024 * 1024


def _check_photo_name_and_size(upload: UploadedFile) -> None:
    name = upload.name or ""
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in PHOTO_UPLOAD_TYPES:
        allowed = ", ".join(sorted({label.upper() for label in PHOTO_UPLOAD_TYPES}))
        raise ProfilePhotoError(f"A profile photo must be a {allowed} file.")

    declared = (getattr(upload, "content_type", "") or "").lower()
    if declared and declared not in set(PHOTO_UPLOAD_TYPES.values()):
        raise ProfilePhotoError(f"'{declared}' is not a picture this site can show.")

    if (upload.size or 0) > MAX_PHOTO_BYTES:
        raise ProfilePhotoError(f"A profile photo must be under {MAX_PHOTO_BYTES // (1024 * 1024)} MB.")


def _verify_is_a_photo(upload: UploadedFile) -> None:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(upload) as image:
            image_format = image.format
            image.verify()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as error:
        raise ProfilePhotoError("That file is not a picture this site can show.") from error
    finally:
        upload.seek(0)

    if image_format not in PHOTO_UPLOAD_FORMATS:
        raise ProfilePhotoError(f"{image_format or 'That file'} is not a picture this site can show.")


def set_profile_photo(user: User, upload: UploadedFile) -> User:
    _check_photo_name_and_size(upload)
    _verify_is_a_photo(upload)

    previous = user.photo.name or ""
    user.photo = upload
    try:
        user.save(update_fields=["photo", "updated_at"])
    except images.ImageMetadataError as error:
        raise ProfilePhotoError(str(error)) from error

    if previous:
        _delete_stored_photo(user, previous)

    return user


def remove_profile_photo(user: User) -> User:
    previous = user.photo.name or ""
    if not previous:
        return user

    user.photo = ""
    user.save(update_fields=["photo", "updated_at"])
    _delete_stored_photo(user, previous)
    return user


def _delete_stored_photo(user: User, name: str) -> None:
    try:
        user.photo.storage.delete(name)
    except Exception:
        logger.exception("Could not delete the replaced profile photo %s of account %s", name, user.pk)


def request_email_change(user: User, new_email: str) -> None:
    """Email a confirmation link to the *new* address.

    Sending it to the new address is what proves the person asking actually owns it. The previous
    version sent the link to the old address and marked the new one verified on request, so
    somebody could attach an address they did not control to their account — and purchases are
    matched to accounts by email.

    Nothing changes on the account until the link is followed.
    """
    if services.find_by_email(new_email) is not None:
        raise EmailAlreadyTakenError

    token = SecurityToken.objects.issue(
        TokenPurpose.EMAIL_CHANGE,
        user,
        EMAIL_CHANGE_VALID_FOR,
        payload={"new_email": new_email},
    )
    emails.send_email_change_confirmation(user, new_email, token)


def confirm_email_change(raw_token: str) -> User | None:
    """Apply a confirmed email change. Returns None if the link cannot be used.

    Raises ``EmailAlreadyTakenError`` if somebody else claimed the address in the meantime.
    """
    token = SecurityToken.objects.consume(TokenPurpose.EMAIL_CHANGE, raw_token)
    if token is None:
        return None

    new_email = token.payload.get("new_email", "")
    if not new_email:
        logger.error("Email change token %s carried no address", token.pk)
        return None

    # Checked again here, not only when the change was requested: two people could have asked for
    # the same address, and the race is decided by who confirms first.
    taken = User.objects.filter(email__iexact=new_email).exclude(pk=token.user_id).exists()
    if taken:
        raise EmailAlreadyTakenError

    user = token.user
    previous_email = user.email
    user.email = new_email.lower()
    # Verified now, and only now: the link was delivered to this address and followed.
    user.email_verified = True
    user.save(update_fields=["email", "email_verified", "updated_at"])

    services.invalidate_tokens(user, [TokenPurpose.EMAIL_CHANGE])

    # Signed out everywhere, as a password change does. Whoever asked for this change has to
    # prove themselves again against the account as it now stands.
    services.revoke_refresh_tokens(user)

    # The last message the old address will ever get, and the only way its owner learns the
    # account has moved: everything after this goes to the new inbox.
    emails.send_quietly(emails.send_email_changed_notice, user, previous_email, user.email)

    logger.info("Changed the email address of account %s", user.pk)
    return user


def change_password(user: User, new_password: str) -> None:
    """Set a new password for somebody who is already signed in and knew their old one."""
    services.set_password(user, new_password)
