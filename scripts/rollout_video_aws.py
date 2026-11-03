"""Release one immutable image to an already bootstrapped UK video stack and web."""

import argparse
import copy
import json
import time
from pathlib import Path

import boto3  # type: ignore[import-untyped]

from scripts.configure_video_web import configure

ACCOUNT = "501588779980"


def wait_for_service(ecs, cluster, name, revision, timeout=1800):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = ecs.describe_services(cluster=cluster, services=[name])
        if response.get("failures") or len(response["services"]) != 1:
            raise RuntimeError("ECS service could not be read")
        service = response["services"][0]
        if service["taskDefinition"] != revision:
            raise RuntimeError("Release was replaced or rolled back")
        deployments = service["deployments"]
        if any(item.get("rolloutState") == "FAILED" for item in deployments):
            raise RuntimeError("ECS deployment failed")
        if (
            service["desiredCount"] > 0
            and service["runningCount"] == service["desiredCount"]
            and service["pendingCount"] == 0
            and len(deployments) == 1
            and deployments[0].get("rolloutState") == "COMPLETED"
        ):
            return
        time.sleep(15)
    raise TimeoutError("ECS deployment did not stabilize")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--environment", choices=["dev", "stage"], required=True)
    parser.add_argument("--stack", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--web-service", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--api-host", required=True)
    parser.add_argument("--cookie-domain", required=True)
    parser.add_argument("--apply", action="store_true", help="Without this flag all calls are read-only")
    args = parser.parse_args()
    region = "eu-west-2"
    session = boto3.Session(profile_name=args.profile, region_name=region)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise RuntimeError("Refusing a release outside the UK AWS account")
    if not args.image.startswith(f"{ACCOUNT}.dkr.ecr.{region}.amazonaws.com/"):
        raise RuntimeError("Use the UK ECR repository and immutable digest")
    cloudformation, ecs = session.client("cloudformation"), session.client("ecs")
    stack = cloudformation.describe_stacks(StackName=args.stack)["Stacks"][0]
    parameters = {item["ParameterKey"]: item["ParameterValue"] for item in stack["Parameters"]}
    if parameters["Environment"] != args.environment or parameters["Cluster"] != args.cluster:
        raise RuntimeError("Stack environment or cluster does not match")
    if parameters["StartServices"] != "true":
        raise RuntimeError("Complete initial broker/scheduler bootstrap before using this release command")
    if parameters["WebServiceName"] != args.web_service:
        raise RuntimeError("Web service does not match the selected video stack")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack["Outputs"]}
    response = ecs.describe_services(cluster=args.cluster, services=[args.web_service])
    if response.get("failures") or len(response["services"]) != 1:
        raise RuntimeError("Web service could not be read")
    web = response["services"][0]
    original_revision = web["taskDefinition"]
    task = ecs.describe_task_definition(taskDefinition=original_revision, include=["TAGS"])
    registration = configure(
        copy.deepcopy(task["taskDefinition"]),
        outputs,
        container_name=args.container,
        region=region,
        cookie_domain=args.cookie_domain,
        api_host=args.api_host,
        image=args.image,
    )
    if task.get("tags"):
        registration["tags"] = task["tags"]
    plan = {
        "stack": args.stack,
        "web_service": args.web_service,
        "image": args.image,
        "previous_web_revision": original_revision,
        "previous_stack_image": parameters["Image"],
    }
    print(json.dumps(plan))  # noqa: T201 - names and revisions only, never runtime settings
    if not args.apply:
        return
    revision = ecs.register_task_definition(**registration)["taskDefinition"]["taskDefinitionArn"]
    # Migrate once, using the exact runtime that will be released. Never migrate in workers.
    migration = ecs.run_task(
        cluster=args.cluster,
        taskDefinition=revision,
        count=1,
        launchType="FARGATE",
        networkConfiguration=web["networkConfiguration"],
        overrides={
            "containerOverrides": [
                {"name": args.container, "command": ["python", "manage.py", "migrate", "--noinput"]}
            ]
        },
    )
    if migration.get("failures") or len(migration["tasks"]) != 1:
        raise RuntimeError("Migration task could not start")
    migration_arn = migration["tasks"][0]["taskArn"]
    ecs.get_waiter("tasks_stopped").wait(
        cluster=args.cluster, tasks=[migration_arn], WaiterConfig={"Delay": 6, "MaxAttempts": 100}
    )
    stopped = ecs.describe_tasks(cluster=args.cluster, tasks=[migration_arn])["tasks"][0]
    target = next(item for item in stopped["containers"] if item["name"] == args.container)
    if target.get("exitCode") != 0:
        raise RuntimeError("Migration did not finish successfully; web and workers were not updated")
    current = ecs.describe_services(cluster=args.cluster, services=[args.web_service])["services"][0]
    if current["taskDefinition"] != original_revision:
        raise RuntimeError("Web changed during migrations; refusing to replace another release")
    parameter_updates = [
        {"ParameterKey": key, "ParameterValue": args.image}
        if key == "Image"
        else {"ParameterKey": key, "UsePreviousValue": True}
        for key in parameters
    ]
    template = Path(__file__).resolve().parents[1] / "infrastructure/video/stack.yaml"
    try:
        cloudformation.update_stack(
            StackName=args.stack,
            TemplateBody=template.read_text(),
            Parameters=parameter_updates,
            Capabilities=["CAPABILITY_IAM"],
        )
    except cloudformation.exceptions.ClientError as error:
        if "No updates are to be performed" not in str(error):
            raise
    else:
        cloudformation.get_waiter("stack_update_complete").wait(
            StackName=args.stack, WaiterConfig={"Delay": 15, "MaxAttempts": 120}
        )
    ecs.update_service(cluster=args.cluster, service=args.web_service, taskDefinition=revision)
    wait_for_service(ecs, args.cluster, args.web_service, revision)
    print("Web and video stack release completed. Run the documented AWS playback checks.")  # noqa: T201


if __name__ == "__main__":
    main()
