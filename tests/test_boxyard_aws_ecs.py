import argparse
import unittest
from unittest import mock

import boxyard_aws


def ecs_args(**overrides):
    values = {
        "profile": None,
        "region": "eu-west-2",
        "name": "support-agent",
        "cluster": "boxyard-agents",
        "image_uri": "123456789012.dkr.ecr.eu-west-2.amazonaws.com/support-agent",
        "tag": "latest",
        "context": ".",
        "dockerfile": "Dockerfile",
        "cpu": "512",
        "memory": "1024",
        "desired_count": 1,
        "subnet": ["subnet-a", "subnet-b"],
        "security_group": [],
        "env": [],
        "secret": [],
        "execution_role_arn": None,
        "task_role_arn": None,
        "agent_container_command": [],
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class EcsAgentShipTests(unittest.TestCase):
    def test_normalize_image_uri_appends_tag(self):
        image = boxyard_aws.normalize_image_uri(
            "123456789012.dkr.ecr.eu-west-2.amazonaws.com/support-agent",
            "v1",
        )
        self.assertEqual(image, "123456789012.dkr.ecr.eu-west-2.amazonaws.com/support-agent:v1")

    def test_create_cluster_command(self):
        args = ecs_args(profile="dev")
        self.assertEqual(
            boxyard_aws.ecs_create_cluster_command(args),
            ["aws", "--profile", "dev", "--region", "eu-west-2", "ecs", "create-cluster", "--cluster-name", "boxyard-agents"],
        )

    def test_create_log_group_command(self):
        args = ecs_args()
        self.assertEqual(
            boxyard_aws.ecs_create_log_group_command(args),
            ["aws", "--region", "eu-west-2", "logs", "create-log-group", "--log-group-name", "/boxyard/agents/support-agent"],
        )

    def test_task_definition_payload_maps_env_secrets_and_command(self):
        args = ecs_args(
            env=["OPENAI_MODEL=gpt-4.1", "MODE=worker"],
            secret=["OPENAI_API_KEY=arn:aws:ssm:eu-west-2:123456789012:parameter/openai"],
            execution_role_arn="arn:aws:iam::123456789012:role/ecsTaskExecutionRole",
            task_role_arn="arn:aws:iam::123456789012:role/agentTaskRole",
            agent_container_command=["python", "-m", "agent.worker"],
        )
        payload = boxyard_aws.ecs_task_definition_payload(args, "repo/support-agent:latest")

        self.assertEqual(payload["family"], "support-agent")
        self.assertEqual(payload["networkMode"], "awsvpc")
        self.assertEqual(payload["requiresCompatibilities"], ["FARGATE"])
        self.assertEqual(payload["executionRoleArn"], "arn:aws:iam::123456789012:role/ecsTaskExecutionRole")
        self.assertEqual(payload["taskRoleArn"], "arn:aws:iam::123456789012:role/agentTaskRole")
        container = payload["containerDefinitions"][0]
        self.assertEqual(container["name"], "support-agent")
        self.assertEqual(container["image"], "repo/support-agent:latest")
        self.assertEqual(container["environment"], [{"name": "OPENAI_MODEL", "value": "gpt-4.1"}, {"name": "MODE", "value": "worker"}])
        self.assertEqual(
            container["secrets"],
            [{"name": "OPENAI_API_KEY", "value": "arn:aws:ssm:eu-west-2:123456789012:parameter/openai"}],
        )
        self.assertEqual(container["command"], ["python", "-m", "agent.worker"])
        self.assertEqual(container["logConfiguration"]["options"]["awslogs-group"], "/boxyard/agents/support-agent")
        self.assertEqual(container["logConfiguration"]["options"]["awslogs-region"], "eu-west-2")

    def test_service_commands_use_create_or_update_shape(self):
        args = ecs_args(security_group=["sg-123"])
        self.assertEqual(
            boxyard_aws.ecs_create_service_command(args, "td-arn"),
            [
                "aws",
                "--region",
                "eu-west-2",
                "ecs",
                "create-service",
                "--cluster",
                "boxyard-agents",
                "--service-name",
                "support-agent",
                "--task-definition",
                "td-arn",
                "--launch-type",
                "FARGATE",
                "--desired-count",
                "1",
                "--network-configuration",
                "awsvpcConfiguration={subnets=[subnet-a,subnet-b],securityGroups=[sg-123],assignPublicIp=DISABLED}",
            ],
        )
        self.assertIn("update-service", boxyard_aws.ecs_update_service_command(args, "td-arn"))

    def test_parser_requires_image_uri_and_subnet(self):
        parser = boxyard_aws.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["ecs", "agent", "ship", "--name", "support-agent"])

    def test_parser_accepts_repeatable_values_and_command(self):
        parser = boxyard_aws.build_parser()
        args = parser.parse_args(
            [
                "ecs",
                "agent",
                "ship",
                "--region",
                "eu-west-2",
                "--name",
                "support-agent",
                "--image-uri",
                "repo/support-agent:latest",
                "--subnet",
                "subnet-a",
                "--subnet",
                "subnet-b",
                "--env",
                "A=B",
                "--secret",
                "TOKEN=arn",
                "--command",
                "python",
                "-m",
                "agent.worker",
            ]
        )
        self.assertEqual(args.command, "ecs")
        self.assertEqual(args.ecs_command, "agent")
        self.assertEqual(args.agent_command, "ship")
        self.assertEqual(args.subnet, ["subnet-a", "subnet-b"])
        self.assertEqual(args.env, ["A=B"])
        self.assertEqual(args.secret, ["TOKEN=arn"])
        self.assertEqual(args.agent_container_command, ["python", "-m", "agent.worker"])

    def test_dry_run_smoke_does_not_require_aws_or_docker(self):
        parser = boxyard_aws.build_parser()
        args = parser.parse_args(
            [
                "ecs",
                "agent",
                "ship",
                "--region",
                "eu-west-2",
                "--name",
                "support-agent",
                "--image-uri",
                "repo/support-agent:latest",
                "--subnet",
                "subnet-a",
                "--dry-run",
                "--show-commands",
            ]
        )
        with mock.patch.object(boxyard_aws, "require_aws_cli") as require_aws, mock.patch.object(
            boxyard_aws, "require_docker_cli"
        ) as require_docker:
            boxyard_aws.ship_ecs_agent(args)
        require_aws.assert_not_called()
        require_docker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
