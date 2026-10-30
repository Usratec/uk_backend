"""The settings themselves, where getting one wrong stops the site rather than a feature.

Settings are read while Django is still importing, so anything that raises there is not a
failed request — it is a process that never starts, and a platform that answers 502 to
everything. These tests cover the ones that have done that.
"""

import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]


def boot(unset=(), **env) -> subprocess.CompletedProcess:
    """Start Django in a fresh process with these environment variables and report what happened.

    A subprocess rather than an import, because settings are read once per process: the damage
    being tested for only happens on a cold start. ``unset`` removes a variable entirely, which
    is not the same as setting it empty — django-environ treats an empty string as a value, so
    only a genuinely absent variable reproduces an unconfigured deploy.
    """
    import os

    environment = {
        **{key: value for key, value in os.environ.items() if key not in unset},
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "SECRET_KEY": "test-only-not-a-secret-0123456789-abcdefghijklmnopqrstuvwxyz",
        **env,
    }
    code = (
        "import django, os; django.setup(); print('started');"
        " os.environ.get('show_backend') and __import__('django.conf', fromlist=['settings'])"
        ".settings.STORAGES and print(__import__('django.conf', fromlist=['settings'])"
        ".settings.STORAGES['default']['BACKEND'])"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=PROJECT,
        env=environment,
    )


class TestUploadsNeverStopTheSiteStarting:
    def test_a_bucket_name_without_credentials_still_boots(self):
        """This took the whole site down: the deploy set the bucket name and not the keys.

        Reading a credential with no default raises while settings are importing, so Django never
        started and every request — signing in, paying, none of which need a bucket — answered
        502 behind it.
        """
        result = boot(
            unset=("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL"),
            S3_BUCKET_NAME="a-bucket",
        )

        assert "started" in result.stdout, result.stderr

    def test_it_says_loudly_that_uploads_are_going_to_disk(self):
        """Falling back quietly would lose files on the next deploy with nobody any the wiser."""
        result = boot(
            unset=("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL"),
            S3_BUCKET_NAME="a-bucket",
        )

        assert "lost on the next deploy" in result.stderr

    def test_no_bucket_at_all_boots_quietly(self):
        """The ordinary case for a developer: no bucket, no credentials, no warning."""
        result = boot(unset=("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"), S3_BUCKET_NAME="")

        assert "started" in result.stdout, result.stderr
        assert "lost on the next deploy" not in result.stderr


class TestWhenTheBucketIsFullyConfigured:
    def test_the_bucket_is_used(self):
        """All four present, so uploads go to the bucket rather than the disk."""
        result = boot(
            DJANGO_SETTINGS_MODULE="config.settings.local",
            S3_BUCKET_NAME="a-bucket",
            S3_ENDPOINT_URL="https://example.r2.cloudflarestorage.com",
            S3_ACCESS_KEY_ID="key",
            S3_SECRET_ACCESS_KEY="secret",
            show_backend="1",
        )

        assert "apps.core.storage.SafeS3Storage" in result.stdout, result.stderr
