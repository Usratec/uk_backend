import logging
from dataclasses import dataclass

import boto3  # type: ignore[import-untyped]  # ships no types
from botocore.config import Config  # type: ignore[import-untyped]  # ships no types
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]  # ships no types
from django.conf import settings

logger = logging.getLogger(__name__)

MISSING_KEY_CODES = ("404", "NoSuchKey", "NotFound")


class ObjectStorageError(Exception):
    """The bucket could not be reached, or refused the request."""


class NotConfiguredError(ObjectStorageError):
    """No bucket is configured, so there is nowhere for a direct upload to go."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    size_bytes: int
    content_type: str
    etag: str


@dataclass(frozen=True)
class UploadedPart:
    number: int
    size_bytes: int
    etag: str


def is_configured() -> bool:
    return bool(settings.S3_CONFIGURED)


def _client():
    if not is_configured():
        raise NotConfiguredError("No uploads bucket is configured.")

    options: dict = {
        "region_name": settings.S3_REGION,
        "config": Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
    }
    # Left out rather than passed empty, so boto3 derives the AWS endpoint and finds the ECS task
    # role instead of being handed blanks. An endpoint is how R2 and MinIO are reached.
    if settings.S3_ENDPOINT_URL:
        options["endpoint_url"] = settings.S3_ENDPOINT_URL
    if settings.S3_ACCESS_KEY_ID and settings.S3_SECRET_ACCESS_KEY:
        options["aws_access_key_id"] = settings.S3_ACCESS_KEY_ID
        options["aws_secret_access_key"] = settings.S3_SECRET_ACCESS_KEY

    return boto3.client("s3", **options)


def _bucket() -> str:
    return settings.S3_BUCKET_NAME


def _fail(action: str, error: Exception) -> ObjectStorageError:
    logger.exception("The uploads bucket refused to %s", action)
    return ObjectStorageError(f"The storage bucket could not {action}.")


def presigned_put_url(*, key: str, content_type: str, expires_in: int) -> str:
    try:
        return _client().generate_presigned_url(
            "put_object",
            Params={"Bucket": _bucket(), "Key": key, "ContentType": content_type},
            ExpiresIn=expires_in,
        )
    except (BotoCoreError, ClientError) as error:
        raise _fail("authorize an upload", error) from error


def create_multipart_upload(*, key: str, content_type: str) -> str:
    try:
        response = _client().create_multipart_upload(Bucket=_bucket(), Key=key, ContentType=content_type)
    except (BotoCoreError, ClientError) as error:
        raise _fail("start a multipart upload", error) from error
    return str(response["UploadId"])


def presigned_part_urls(*, key: str, upload_id: str, part_numbers: list[int], expires_in: int) -> dict[int, str]:
    client = _client()
    urls: dict[int, str] = {}
    try:
        for number in part_numbers:
            urls[number] = client.generate_presigned_url(
                "upload_part",
                Params={"Bucket": _bucket(), "Key": key, "UploadId": upload_id, "PartNumber": number},
                ExpiresIn=expires_in,
            )
    except (BotoCoreError, ClientError) as error:
        raise _fail("authorize an upload part", error) from error
    return urls


def list_parts(*, key: str, upload_id: str) -> list[UploadedPart]:
    client = _client()
    parts: list[UploadedPart] = []
    marker = 0
    try:
        while True:
            response = client.list_parts(
                Bucket=_bucket(), Key=key, UploadId=upload_id, PartNumberMarker=marker, MaxParts=1000
            )
            for part in response.get("Parts", []):
                parts.append(
                    UploadedPart(
                        number=int(part["PartNumber"]),
                        size_bytes=int(part["Size"]),
                        etag=str(part["ETag"]).strip('"'),
                    )
                )
            if not response.get("IsTruncated"):
                break
            marker = int(response["NextPartNumberMarker"])
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "NoSuchUpload":
            return []
        raise _fail("list the upload's parts", error) from error
    except BotoCoreError as error:
        raise _fail("list the upload's parts", error) from error
    return sorted(parts, key=lambda part: part.number)


def complete_multipart_upload(*, key: str, upload_id: str, parts: list[UploadedPart]) -> None:
    try:
        _client().complete_multipart_upload(
            Bucket=_bucket(),
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": [{"PartNumber": part.number, "ETag": part.etag} for part in parts]},
        )
    except (BotoCoreError, ClientError) as error:
        raise _fail("finish the multipart upload", error) from error


def abort_multipart_upload(*, key: str, upload_id: str) -> None:
    try:
        _client().abort_multipart_upload(Bucket=_bucket(), Key=key, UploadId=upload_id)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "NoSuchUpload":
            return
        raise _fail("abandon the multipart upload", error) from error
    except BotoCoreError as error:
        raise _fail("abandon the multipart upload", error) from error


def head_object(*, key: str) -> StoredObject | None:
    try:
        response = _client().head_object(Bucket=_bucket(), Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in MISSING_KEY_CODES:
            return None
        raise _fail("read the uploaded object", error) from error
    except BotoCoreError as error:
        raise _fail("read the uploaded object", error) from error

    return StoredObject(
        key=key,
        size_bytes=int(response.get("ContentLength") or 0),
        content_type=str(response.get("ContentType") or ""),
        etag=str(response.get("ETag") or "").strip('"'),
    )


def download_to(*, key: str, destination: str) -> None:
    try:
        _client().download_file(Bucket=_bucket(), Key=key, Filename=destination)
    except (BotoCoreError, ClientError) as error:
        raise _fail("fetch the source video", error) from error


def upload_file(*, key: str, path: str, content_type: str, cache_control: str = "") -> None:
    extra = {"ContentType": content_type}
    if cache_control:
        extra["CacheControl"] = cache_control
    try:
        _client().upload_file(Filename=path, Bucket=_bucket(), Key=key, ExtraArgs=extra)
    except (BotoCoreError, ClientError) as error:
        raise _fail("store the processed video", error) from error


def delete_prefix(*, prefix: str) -> int:
    if not prefix or not prefix.strip("/"):
        raise ObjectStorageError("Refusing to delete an empty prefix.")

    client = _client()
    removed = 0
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix):
            keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if not keys:
                continue
            client.delete_objects(Bucket=_bucket(), Delete={"Objects": keys, "Quiet": True})
            removed += len(keys)
    except (BotoCoreError, ClientError) as error:
        raise _fail("clear the processed video", error) from error
    return removed


def delete_object(*, key: str) -> None:
    try:
        _client().delete_object(Bucket=_bucket(), Key=key)
    except (BotoCoreError, ClientError) as error:
        raise _fail("remove the object", error) from error
