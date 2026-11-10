"""Prepare an ECS registration payload locally; never print runtime settings."""

import argparse
import json
import os
import re
from pathlib import Path

READ_ONLY_FIELDS = {
    "taskDefinitionArn",
    "revision",
    "status",
    "requiresAttributes",
    "compatibilities",
    "registeredAt",
    "registeredBy",
    "deregisteredAt",
}


def configure(task, outputs, *, container_name, region, cookie_domain, api_host, image):
    if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("Use an immutable ECR sha256 image digest")
    domain = outputs["CloudFrontDomain"]
    parent = cookie_domain.lstrip(".").lower()
    if not parent or not all(host == parent or host.endswith(f".{parent}") for host in (domain, api_host)):
        raise ValueError("Cookie domain must cover the media domain and the API host")
    containers = [item for item in task["containerDefinitions"] if item["name"] == container_name]
    if len(containers) != 1:
        raise ValueError("Specify exactly one existing web container")
    container = containers[0]
    environment = {item["name"]: item["value"] for item in container.get("environment", [])}
    environment.update(
        {
            "VIDEO_S3_BUCKET_NAME": outputs["VideoBucketName"],
            "VIDEO_S3_REGION": region,
            "CLOUDFRONT_DOMAIN": domain,
            "CLOUDFRONT_KEY_PAIR_ID": outputs["CloudFrontKeyPairId"],
            "CLOUDFRONT_COOKIE_DOMAIN": cookie_domain,
            "VIDEO_ALLOW_UNSIGNED_PLAYBACK": "False",
            "VIDEO_TRANSCODING_ENABLED": "True",
            "VIDEO_TASK_QUEUE": "video",
            "CELERY_TASK_DEFAULT_QUEUE": "celery",
            "RUN_WORKER_IN_WEB": "false",
        }
    )
    environment.pop("CLOUDFRONT_PRIVATE_KEY", None)
    secrets = {item["name"]: item["valueFrom"] for item in container.get("secrets", [])}
    # These stack-owned settings must never be shadowed by old ECS secrets entries.
    for name in environment:
        if name.startswith(("VIDEO_", "CLOUDFRONT_")) or name in {"CELERY_TASK_DEFAULT_QUEUE", "RUN_WORKER_IN_WEB"}:
            secrets.pop(name, None)
    secrets["CLOUDFRONT_PRIVATE_KEY"] = outputs["SigningSecretArn"]
    container["environment"] = [{"name": key, "value": value} for key, value in sorted(environment.items())]
    container["secrets"] = [{"name": key, "valueFrom": value} for key, value in sorted(secrets.items())]
    container["image"] = image
    # Web must not launch the Render prototype's embedded worker/scheduler.
    container.pop("entryPoint", None)
    container["command"] = [
        "gunicorn",
        "config.wsgi:application",
        "--bind",
        "0.0.0.0:8000",
        "--worker-class",
        "gthread",
        "--threads",
        "8",
        "--timeout",
        "60",
        "--access-logfile",
        "-",
        "--error-logfile",
        "-",
    ]
    return {key: value for key, value in task.items() if key not in READ_ONLY_FIELDS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--stack-outputs", type=Path, required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--cookie-domain", required=True)
    parser.add_argument("--api-host", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = json.loads(args.input.read_text())
    stacks = json.loads(args.stack_outputs.read_text())["Stacks"]
    if len(stacks) != 1 or not stacks[0]["StackId"].startswith(
        f"arn:aws:cloudformation:{args.region}:501588779980:stack/"
    ):
        raise ValueError("Expected exactly one stack in the UK AWS account")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stacks[0]["Outputs"]}
    task = source["taskDefinition"]
    if not task["taskDefinitionArn"].startswith(f"arn:aws:ecs:{args.region}:501588779980:"):
        raise ValueError("Task definition must belong to the UK AWS account")
    result = configure(
        task,
        outputs,
        container_name=args.container,
        region=args.region,
        cookie_domain=args.cookie_domain,
        api_host=args.api_host,
        image=args.image,
    )
    if source.get("tags"):
        result["tags"] = source["tags"]
    # Refuse overwrite and always create private files, independent of the caller's umask.
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print("Private ECS registration payload prepared; no AWS changes made.")  # noqa: T201


if __name__ == "__main__":
    main()
