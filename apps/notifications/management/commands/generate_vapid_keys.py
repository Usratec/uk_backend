"""Create VAPID material in private files, never in command output."""

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management.base import BaseCommand, CommandError


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class Command(BaseCommand):
    help = "Generate Web Push VAPID keys in a secure directory. Existing files are never overwritten."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", required=True, type=Path)

    def handle(self, *args, **options):
        directory = options["output_dir"].expanduser().resolve()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        private = directory / "vapid-private.key"
        public = directory / "vapid-public.key"
        if private.exists() or public.exists():
            raise CommandError("VAPID output files already exist; choose an empty directory.")

        key = ec.generate_private_key(ec.SECP256R1())
        values = (
            (
                private,
                encoded(
                    key.private_bytes(
                        serialization.Encoding.DER,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                ),
            ),
            (
                public,
                encoded(
                    key.public_key().public_bytes(
                        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
                    )
                ),
            ),
        )
        for path, value in values:
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as output:
                    output.write(value + "\n")
            except OSError as exc:
                raise CommandError(f"Could not create VAPID key file {path}.") from exc
        self.stdout.write(f"Public key: {public}\nPrivate key: {private}")
