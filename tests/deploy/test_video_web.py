import copy

import pytest

from scripts.configure_video_web import configure

IMAGE = "501588779980.dkr.ecr.eu-west-2.amazonaws.com/backend@sha256:" + "a" * 64


def payload():
    return {
        "family": "web",
        "revision": 7,
        "taskDefinitionArn": "read-only-arn",
        "taskRoleArn": "keep-task-role",
        "executionRoleArn": "keep-execution-role",
        "containerDefinitions": [
            {
                "name": "web",
                "image": "old",
                "entryPoint": ["bash", "scripts/render-start.sh"],
                "environment": [
                    {"name": "DATABASE_URL", "value": "keep-database"},
                    {"name": "S3_BUCKET_NAME", "value": "keep-old-media"},
                    {"name": "CLOUDFRONT_PRIVATE_KEY", "value": "remove-plaintext"},
                ],
                "secrets": [
                    {"name": "POSTMARK_SERVER_TOKEN", "valueFrom": "keep-email-secret"},
                    {"name": "CLOUDFRONT_DOMAIN", "valueFrom": "remove-old-domain"},
                ],
                "healthCheck": {"command": ["CMD", "existing-probe"]},
            }
        ],
    }


def outputs():
    return {
        "VideoBucketName": "new-video",
        "CloudFrontDomain": "media.dev.example.test",
        "CloudFrontKeyPairId": "public-key-id",
        "SigningSecretArn": "private-key-secret-arn",
    }


def run(task=None, **kwargs):
    return configure(
        task or payload(),
        outputs(),
        container_name="web",
        region="eu-west-2",
        cookie_domain=kwargs.get("cookie_domain", ".dev.example.test"),
        api_host=kwargs.get("api_host", "api.dev.example.test"),
        image=kwargs.get("image", IMAGE),
    )


def test_web_update_preserves_existing_media_database_roles_and_secrets():
    original = payload()
    result = run(copy.deepcopy(original))
    container = result["containerDefinitions"][0]
    environment = {item["name"]: item["value"] for item in container["environment"]}
    secrets = {item["name"]: item["valueFrom"] for item in container["secrets"]}
    assert environment["DATABASE_URL"] == "keep-database"
    assert environment["S3_BUCKET_NAME"] == "keep-old-media"
    assert environment["VIDEO_S3_BUCKET_NAME"] == "new-video"
    assert environment["RUN_WORKER_IN_WEB"] == "false"
    assert "CLOUDFRONT_PRIVATE_KEY" not in environment
    assert secrets == {
        "POSTMARK_SERVER_TOKEN": "keep-email-secret",
        "CLOUDFRONT_PRIVATE_KEY": "private-key-secret-arn",
    }
    assert result["taskRoleArn"] == original["taskRoleArn"]
    assert result["executionRoleArn"] == original["executionRoleArn"]
    assert container["healthCheck"] == original["containerDefinitions"][0]["healthCheck"]
    assert container["command"][0] == "gunicorn"
    assert "entryPoint" not in container
    assert "revision" not in result and "taskDefinitionArn" not in result


@pytest.mark.parametrize("domain", [".other.test", "", ".example.test.invalid"])
def test_unrelated_cookie_domain_is_rejected(domain):
    with pytest.raises(ValueError, match="Cookie domain"):
        run(cookie_domain=domain)


def test_mutable_image_and_missing_container_are_rejected():
    with pytest.raises(ValueError, match="immutable"):
        run(image="backend:latest")
    task = payload()
    task["containerDefinitions"][0]["name"] = "different"
    with pytest.raises(ValueError, match="existing web"):
        run(task)


def test_cookie_domain_must_also_cover_the_api_host():
    with pytest.raises(ValueError, match="Cookie domain"):
        run(api_host="api.unrelated.test")
