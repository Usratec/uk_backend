import sys
from typing import cast
from unittest.mock import Mock

import pytest

from scripts import rollout_video_aws

IMAGE = "501588779980.dkr.ecr.eu-west-2.amazonaws.com/backend@sha256:" + "a" * 64


@pytest.fixture
def release(monkeypatch):
    cloudformation, ecs, sts = Mock(), Mock(), Mock()
    sts.get_caller_identity.return_value = {"Account": "501588779980"}
    outputs = {
        "VideoBucketName": "video",
        "CloudFrontDomain": "media.dev.example.test",
        "CloudFrontKeyPairId": "public-key",
        "SigningSecretArn": "private-key-secret-arn",
    }
    parameters = {
        "Environment": "dev",
        "Cluster": "cluster",
        "WebServiceName": "web",
        "StartServices": "true",
        "Image": "previous-image",
    }
    cloudformation.describe_stacks.return_value = {
        "Stacks": [
            {
                "Parameters": [{"ParameterKey": key, "ParameterValue": value} for key, value in parameters.items()],
                "Outputs": [{"OutputKey": key, "OutputValue": value} for key, value in outputs.items()],
            }
        ]
    }
    ecs.describe_services.return_value = {
        "services": [
            {
                "taskDefinition": "old-revision",
                "networkConfiguration": {"awsvpcConfiguration": {"subnets": ["subnet"]}},
            }
        ]
    }
    ecs.describe_task_definition.return_value = {
        "taskDefinition": {"family": "web", "containerDefinitions": [{"name": "web", "image": "old"}]}
    }
    ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "new-revision"}}
    ecs.run_task.return_value = {"tasks": [{"taskArn": "migration-task"}]}
    ecs.describe_tasks.return_value = {"tasks": [{"containers": [{"name": "web", "exitCode": 0}]}]}
    session = Mock()
    session.client.side_effect = {"sts": sts, "cloudformation": cloudformation, "ecs": ecs}.__getitem__
    monkeypatch.setattr(rollout_video_aws.boto3, "Session", Mock(return_value=session))
    monkeypatch.setattr(rollout_video_aws, "wait_for_service", Mock())
    arguments = [
        "rollout",
        "--environment",
        "dev",
        "--stack",
        "video-dev",
        "--cluster",
        "cluster",
        "--web-service",
        "web",
        "--container",
        "web",
        "--image",
        IMAGE,
        "--api-host",
        "api.dev.example.test",
        "--cookie-domain",
        ".dev.example.test",
    ]
    monkeypatch.setattr(sys, "argv", arguments)
    return cloudformation, ecs, sts, arguments


def test_release_preview_performs_no_mutations(release):
    cloudformation, ecs, _, _ = release
    rollout_video_aws.main()
    cloudformation.update_stack.assert_not_called()
    ecs.register_task_definition.assert_not_called()
    ecs.run_task.assert_not_called()
    ecs.update_service.assert_not_called()


def test_failed_migration_stops_release_before_workers_or_web_change(release):
    cloudformation, ecs, _, arguments = release
    arguments.append("--apply")
    ecs.describe_tasks.return_value["tasks"][0]["containers"][0]["exitCode"] = 1
    with pytest.raises(RuntimeError, match="Migration did not finish"):
        rollout_video_aws.main()
    cloudformation.update_stack.assert_not_called()
    ecs.update_service.assert_not_called()


def test_release_updates_workers_and_web_to_one_digest(release):
    cloudformation, ecs, _, arguments = release
    arguments.append("--apply")
    rollout_video_aws.main()
    update = cloudformation.update_stack.call_args.kwargs
    assert {"ParameterKey": "Image", "ParameterValue": IMAGE} in update["Parameters"]
    ecs.update_service.assert_called_once_with(cluster="cluster", service="web", taskDefinition="new-revision")
    cast(Mock, rollout_video_aws.wait_for_service).assert_called_once_with(ecs, "cluster", "web", "new-revision")


def test_wrong_aws_account_is_rejected_before_resource_calls(release):
    cloudformation, ecs, sts, arguments = release
    arguments.append("--apply")
    sts.get_caller_identity.return_value = {"Account": "210608587736"}
    with pytest.raises(RuntimeError, match="outside the UK"):
        rollout_video_aws.main()
    cloudformation.describe_stacks.assert_not_called()
    ecs.describe_services.assert_not_called()


def test_service_from_another_environment_is_rejected(release):
    cloudformation, ecs, _, _ = release
    parameters = cloudformation.describe_stacks.return_value["Stacks"][0]["Parameters"]
    next(item for item in parameters if item["ParameterKey"] == "WebServiceName")["ParameterValue"] = "stage-web"
    with pytest.raises(RuntimeError, match="Web service does not match"):
        rollout_video_aws.main()
    ecs.describe_services.assert_not_called()
