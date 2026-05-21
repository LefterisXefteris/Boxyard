#!/usr/bin/env python3
"""
Deploy Docker images to AWS from the command line.

The first deployment target is EC2 through AWS Systems Manager. That means the
EC2 instance needs SSM access, but you do not need SSH access from your laptop.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from typing import Sequence


BOX_WIDTH = 88
STATUS_STYLES = {
    "OK": ("OK", "\033[32m"),
    "WARN": ("WARN", "\033[33m"),
    "INFO": ("INFO", "\033[36m"),
    "UNKNOWN": ("UNKNOWN", "\033[35m"),
}
RESET = "\033[0m"
DIM = "\033[2m"
CYAN = "\033[36m"
BOLD = "\033[1m"


def require_aws_cli() -> None:
    if shutil.which("aws") is None:
        print("Error: AWS CLI was not found. Install and configure AWS CLI first.", file=sys.stderr)
        sys.exit(127)


def require_docker_cli() -> None:
    if shutil.which("docker") is None:
        print("Error: Docker CLI was not found. Install Docker and try again.", file=sys.stderr)
        sys.exit(127)


def aws_base_args(args: argparse.Namespace) -> list[str]:
    command = ["aws"]
    if getattr(args, "profile", None):
        command.extend(["--profile", args.profile])
    if getattr(args, "region", None):
        command.extend(["--region", args.region])
    return command


def run(
    command: Sequence[str],
    *,
    capture: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    printable = " ".join(shlex.quote(part) for part in command)
    if not quiet or dry_run:
        print(printable)
    if dry_run:
        return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.run(command, check=False, text=True, capture_output=capture)


def exit_if_failed(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        sys.exit(result.returncode)


def print_step(message: str) -> None:
    print(colorize(f"\n=> {message}", BOLD))


def auth_status(args: argparse.Namespace) -> None:
    require_aws_cli()
    result = run([*aws_base_args(args), "sts", "get-caller-identity"], capture=True)
    exit_if_failed(result)
    print(result.stdout.strip())


def auth_sso(args: argparse.Namespace) -> None:
    require_aws_cli()
    command = [*aws_base_args(args), "configure", "sso"]
    exit_if_failed(run(command))


def auth_login(args: argparse.Namespace) -> None:
    require_aws_cli()
    command = [*aws_base_args(args), "sso", "login"]
    exit_if_failed(run(command))


def aws_json(args: argparse.Namespace, aws_args: Sequence[str]) -> dict:
    result = run([*aws_base_args(args), *aws_args], capture=True, quiet=True)
    exit_if_failed(result)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        print(f"Error: AWS CLI returned invalid JSON: {error}", file=sys.stderr)
        sys.exit(1)


def aws_json_optional(args: argparse.Namespace, aws_args: Sequence[str]) -> tuple[dict | None, str | None]:
    result = run([*aws_base_args(args), *aws_args], capture=True, quiet=True)
    if result.returncode != 0:
        return None, result.stderr.strip() or "AWS command failed"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as error:
        return None, f"AWS CLI returned invalid JSON: {error}"


def get_instance(args: argparse.Namespace) -> dict:
    payload = aws_json(
        args,
        [
            "ec2",
            "describe-instances",
            "--instance-ids",
            args.instance_id,
            "--output",
            "json",
        ],
    )
    reservations = payload.get("Reservations", [])
    instances = [instance for reservation in reservations for instance in reservation.get("Instances", [])]
    if not instances:
        print(f"Error: EC2 instance not found: {args.instance_id}", file=sys.stderr)
        sys.exit(1)
    return instances[0]


def get_ssm_instance(args: argparse.Namespace) -> tuple[dict | None, str | None]:
    payload, error = aws_json_optional(
        args,
        [
            "ssm",
            "describe-instance-information",
            "--filters",
            f"Key=InstanceIds,Values={args.instance_id}",
            "--output",
            "json",
        ],
    )
    if error:
        return None, error
    items = payload.get("InstanceInformationList", []) if payload else []
    return (items[0], None) if items else (None, None)


def get_security_groups(args: argparse.Namespace, group_ids: list[str]) -> tuple[list[dict], str | None]:
    if not group_ids:
        return [], None
    payload, error = aws_json_optional(
        args,
        [
            "ec2",
            "describe-security-groups",
            "--group-ids",
            *group_ids,
            "--output",
            "json",
        ],
    )
    if error:
        return [], error
    return payload.get("SecurityGroups", []) if payload else [], None


def instance_profile_name(instance: dict) -> str | None:
    arn = instance.get("IamInstanceProfile", {}).get("Arn")
    if not arn:
        return None
    return arn.rsplit("/", 1)[-1]


def get_iam_role_report(args: argparse.Namespace, profile_name: str | None) -> dict:
    if not profile_name:
        return {"profile": None, "roles": [], "attached_policies": [], "error": None}

    profile_payload, profile_error = aws_json_optional(
        args,
        ["iam", "get-instance-profile", "--instance-profile-name", profile_name, "--output", "json"],
    )
    if profile_error:
        return {"profile": profile_name, "roles": [], "attached_policies": [], "error": profile_error}

    roles = profile_payload.get("InstanceProfile", {}).get("Roles", []) if profile_payload else []
    attached_policies: list[dict] = []
    errors: list[str] = []
    for role in roles:
        role_name = role.get("RoleName")
        if not role_name:
            continue
        policy_payload, policy_error = aws_json_optional(
            args,
            ["iam", "list-attached-role-policies", "--role-name", role_name, "--output", "json"],
        )
        if policy_error:
            errors.append(f"{role_name}: {policy_error}")
            continue
        attached_policies.extend(policy_payload.get("AttachedPolicies", []) if policy_payload else [])

    return {
        "profile": profile_name,
        "roles": [role.get("RoleName") for role in roles if role.get("RoleName")],
        "attached_policies": attached_policies,
        "error": "; ".join(errors) if errors else None,
    }


def permission_targets_world(permission: dict) -> bool:
    for item in permission.get("IpRanges", []):
        if item.get("CidrIp") == "0.0.0.0/0":
            return True
    for item in permission.get("Ipv6Ranges", []):
        if item.get("CidrIpv6") == "::/0":
            return True
    return False


def permission_matches_port(permission: dict, port: int) -> bool:
    if permission.get("IpProtocol") == "-1":
        return True
    from_port = permission.get("FromPort")
    to_port = permission.get("ToPort")
    if from_port is None or to_port is None:
        return False
    return from_port <= port <= to_port


def security_group_findings(security_groups: list[dict]) -> list[dict]:
    findings: list[dict] = []
    for group in security_groups:
        group_name = group.get("GroupName", "-")
        group_id = group.get("GroupId", "-")
        for permission in group.get("IpPermissions", []):
            if not permission_targets_world(permission):
                continue
            protocol = permission.get("IpProtocol", "-")
            from_port = permission.get("FromPort")
            to_port = permission.get("ToPort")
            port_text = "all ports" if protocol == "-1" else f"{from_port}-{to_port}"
            if protocol == "-1":
                severity = "WARN"
                message = "allows public inbound access to all protocols and ports"
            elif permission_matches_port(permission, 22):
                severity = "WARN"
                message = "allows public SSH access"
            else:
                severity = "INFO"
                message = f"allows public inbound access on {port_text}"
            findings.append(
                {
                    "severity": severity,
                    "group": f"{group_name} ({group_id})",
                    "protocol": protocol,
                    "ports": port_text,
                    "message": message,
                }
            )
    return findings


def supports_color() -> bool:
    return sys.stdout.isatty() and not bool(os.environ.get("NO_COLOR"))


def colorize(text: str, color: str) -> str:
    if not supports_color():
        return text
    return f"{color}{text}{RESET}"


def badge(status: str) -> str:
    label, color = STATUS_STYLES.get(status, (status, "\033[37m"))
    return colorize(f"[ {label.ljust(7)}]", color)


def rule(title: str) -> None:
    text = f" {title} "
    line = "-" * max(0, BOX_WIDTH - len(text))
    print(colorize(f"\n{text}{line}", DIM))


def header(title: str, subtitle: str) -> None:
    border = "=" * BOX_WIDTH
    print(colorize(border, CYAN))
    print(colorize(title, BOLD))
    print(subtitle)
    print(colorize(border, CYAN))


def kv_rows(rows: list[tuple[str, str]]) -> None:
    label_width = max((len(label) for label, _ in rows), default=0)
    for label, value in rows:
        formatted_label = colorize(label.ljust(label_width), DIM)
        print(f"  {formatted_label}  {value}")


def check_rows(rows: list[tuple[str, str, str]]) -> None:
    label_width = max((len(label) for _, label, _ in rows), default=0)
    for status, label, detail in rows:
        print(f"  {badge(status)}  {label.ljust(label_width)}  {detail}")


def summary_bar(ok_count: int, warn_count: int, info_count: int, unknown_count: int) -> str:
    parts = [
        colorize(f"OK {ok_count}", "\033[32m"),
        colorize(f"WARN {warn_count}", "\033[33m"),
        colorize(f"INFO {info_count}", "\033[36m"),
        colorize(f"UNKNOWN {unknown_count}", "\033[35m"),
    ]
    return "  ".join(parts)


def tag_value(instance: dict, key: str) -> str | None:
    for tag in instance.get("Tags", []):
        if tag.get("Key") == key:
            return tag.get("Value")
    return None


def build_ec2_report(args: argparse.Namespace) -> dict:
    instance = get_instance(args)
    ssm_instance, ssm_error = get_ssm_instance(args)
    group_ids = [group.get("GroupId") for group in instance.get("SecurityGroups", []) if group.get("GroupId")]
    security_groups, security_group_error = get_security_groups(args, group_ids)
    iam_report = get_iam_role_report(args, instance_profile_name(instance))
    return {
        "instance": instance,
        "ssm_instance": ssm_instance,
        "ssm_error": ssm_error,
        "security_groups": security_groups,
        "security_group_error": security_group_error,
        "iam": iam_report,
        "security_findings": security_group_findings(security_groups),
    }


def render_ec2_report(args: argparse.Namespace, report: dict) -> None:
    instance = report["instance"]
    state = instance.get("State", {}).get("Name", "unknown")
    name = tag_value(instance, "Name") or "-"
    public_ip = instance.get("PublicIpAddress") or "-"
    private_ip = instance.get("PrivateIpAddress") or "-"
    metadata_tokens = instance.get("MetadataOptions", {}).get("HttpTokens", "unknown")
    ssm_instance = report["ssm_instance"]
    iam_report = report["iam"]
    attached_policy_names = [policy.get("PolicyName", "") for policy in iam_report.get("attached_policies", [])]
    has_ssm_policy = "AmazonSSMManagedInstanceCore" in attached_policy_names
    instance_rows = [
        ("State", state),
        ("Name", name),
        ("Type", instance.get("InstanceType", "-")),
        ("AMI", instance.get("ImageId", "-")),
        ("VPC", instance.get("VpcId", "-")),
        ("Subnet", instance.get("SubnetId", "-")),
        ("Private IPv4", private_ip),
        ("Public IPv4", public_ip),
        ("IMDSv2", f"HttpTokens={metadata_tokens}"),
    ]
    checks: list[tuple[str, str, str]] = [
        ("OK" if state == "running" else "WARN", "EC2 state", state),
        ("OK" if metadata_tokens == "required" else "WARN", "IMDSv2", f"HttpTokens={metadata_tokens}"),
    ]

    if report["ssm_error"]:
        ssm_rows = [("UNKNOWN", "SSM status", report["ssm_error"])]
    elif ssm_instance:
        ping_status = ssm_instance.get("PingStatus", "unknown")
        agent_version = ssm_instance.get("AgentVersion", "-")
        ssm_rows = [
            ("OK" if ping_status == "Online" else "WARN", "SSM ping", ping_status),
            ("INFO", "SSM agent", agent_version),
        ]
    else:
        ssm_rows = [("WARN", "SSM status", "not registered or not visible to this AWS identity")]
    checks.extend(ssm_rows)

    if iam_report.get("profile"):
        iam_rows = [
            ("INFO", "Instance profile", iam_report["profile"]),
            ("INFO", "Role(s)", ", ".join(iam_report["roles"]) or "-"),
        ]
        if iam_report.get("error"):
            iam_rows.append(("UNKNOWN", "Role policies", iam_report["error"]))
        else:
            iam_rows.append(
                (
                    "OK" if has_ssm_policy else "WARN",
                    "SSM policy",
                    "AmazonSSMManagedInstanceCore attached" if has_ssm_policy else "not found in attached policies",
                )
            )
    else:
        iam_rows = [("WARN", "Instance profile", "none attached")]
    checks.extend(iam_rows)

    if report["security_group_error"]:
        security_rows = [("UNKNOWN", "Security groups", report["security_group_error"])]
    elif not report["security_groups"]:
        security_rows = [("WARN", "Security groups", "none found")]
    else:
        security_rows = [
            ("INFO", "Group", f"{group.get('GroupName', '-')} ({group.get('GroupId', '-')})")
            for group in report["security_groups"]
        ]
        findings = report["security_findings"]
        if findings:
            for finding in findings:
                security_rows.append((finding["severity"], finding["group"], finding["message"]))
        else:
            security_rows.append(("OK", "Inbound exposure", "no public 0.0.0.0/0 or ::/0 inbound rules found"))
    checks.extend(security_rows)

    if ssm_instance and ssm_instance.get("PingStatus") == "Online":
        readiness_rows = [("OK", "SSM online", "Boxyard can send deployment commands")]
    else:
        readiness_rows = [("WARN", "SSM online", "required unless you add an SSH deployment path later")]
    if public_ip == "-":
        readiness_rows.append(
            ("INFO", "Public traffic", "no public IP; use private networking, VPN, ALB, or assign public access")
        )
    checks.extend(readiness_rows)

    counts = {status: sum(1 for row in checks if row[0] == status) for status in ("OK", "WARN", "INFO", "UNKNOWN")}

    header("Boxyard EC2 Preflight", f"{args.instance_id}  |  {name}  |  {args.region or 'default region'}")
    print("")
    print(summary_bar(counts["OK"], counts["WARN"], counts["INFO"], counts["UNKNOWN"]))
    rule("Instance")
    kv_rows(instance_rows)
    rule("SSM")
    check_rows(ssm_rows)
    rule("IAM")
    check_rows(iam_rows)
    rule("Security Groups")
    check_rows(security_rows)
    rule("Deploy Readiness")
    readiness_rows.insert(0, ("OK" if state == "running" else "WARN", "EC2 running", "required for deployment"))
    check_rows(readiness_rows)
    print("")


def inspect_ec2(args: argparse.Namespace) -> None:
    require_aws_cli()
    report = build_ec2_report(args)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return
    render_ec2_report(args, report)


def install_docker_commands() -> list[str]:
    return [
        "if ! command -v docker >/dev/null 2>&1; then "
        "if command -v yum >/dev/null 2>&1; then sudo yum install -y docker; "
        "elif command -v apt-get >/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y docker.io; "
        "else echo 'No supported package manager found for Docker install' >&2; exit 1; fi; fi",
        "sudo systemctl enable docker || true",
        "sudo systemctl start docker || sudo service docker start",
    ]


def docker_run_command(args: argparse.Namespace) -> str:
    command = ["sudo", "docker", "run", "-d", "--restart", "unless-stopped", "--name", args.name]

    if args.network:
        command.extend(["--network", args.network])
    for port in args.port:
        command.extend(["--publish", port])
    for env in args.env:
        command.extend(["--env", env])
    for volume in args.volume:
        command.extend(["--volume", volume])

    command.append(args.image)
    command.extend(args.container_command)
    return " ".join(shlex.quote(part) for part in command)


def remote_deploy_commands(args: argparse.Namespace) -> list[str]:
    commands = ["set -e"]
    if args.install_docker:
        commands.extend(install_docker_commands())
    if args.create_network:
        if not args.network:
            raise ValueError("--create-network requires --network")
        commands.append(f"sudo docker network create {shlex.quote(args.network)} || true")
    commands.append(f"sudo docker pull {shlex.quote(args.image)}")
    if args.replace:
        commands.append(f"sudo docker rm -f {shlex.quote(args.name)} || true")
    else:
        commands.append(
            f"if sudo docker container inspect {shlex.quote(args.name)} >/dev/null 2>&1; "
            f"then echo 'Container {shlex.quote(args.name)} already exists' >&2; exit 1; fi"
        )
    commands.append(docker_run_command(args))
    commands.append(f"sudo docker ps --filter name={shlex.quote(args.name)}")
    return commands


def send_ssm_command(args: argparse.Namespace, commands: list[str]) -> str:
    parameters = json.dumps({"commands": commands})
    command = [
        *aws_base_args(args),
        "ssm",
        "send-command",
        "--instance-ids",
        args.instance_id,
        "--document-name",
        "AWS-RunShellScript",
        "--comment",
        f"Boxyard deploy {args.name}",
        "--parameters",
        parameters,
        "--query",
        "Command.CommandId",
        "--output",
        "text",
    ]
    result = run(command, capture=True, dry_run=args.dry_run)
    exit_if_failed(result)
    command_id = result.stdout.strip()
    if command_id:
        print(f"SSM command id: {command_id}")
    return command_id


def wait_for_ssm_command(args: argparse.Namespace, command_id: str) -> None:
    if args.dry_run or not command_id:
        return

    while True:
        result = run(
            [
                *aws_base_args(args),
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                args.instance_id,
            ],
            capture=True,
        )
        exit_if_failed(result)
        payload = json.loads(result.stdout)
        status = payload.get("Status")
        if status in {"Success", "Cancelled", "Failed", "TimedOut"}:
            if payload.get("StandardOutputContent"):
                print(payload["StandardOutputContent"].strip())
            if payload.get("StandardErrorContent"):
                print(payload["StandardErrorContent"].strip(), file=sys.stderr)
            if status != "Success":
                sys.exit(1)
            return
        print(f"Waiting for SSM command {command_id}: {status}")
        time.sleep(args.poll_seconds)


def deploy_ec2(args: argparse.Namespace) -> None:
    if args.create_network and not args.network:
        print("Error: --create-network requires --network", file=sys.stderr)
        sys.exit(2)
    try:
        commands = remote_deploy_commands(args)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(2)

    if args.show_script:
        print("\n".join(commands))

    if not args.dry_run:
        require_aws_cli()

    command_id = send_ssm_command(args, commands)
    if args.wait:
        wait_for_ssm_command(args, command_id)


def normalize_image_uri(image_uri: str, tag: str) -> str:
    image_name = image_uri.rsplit("/", 1)[-1]
    if ":" in image_name:
        return image_uri
    return f"{image_uri}:{tag}"


def image_registry(image_uri: str) -> str | None:
    first_part = image_uri.split("/", 1)[0]
    if "." not in first_part and ":" not in first_part and first_part != "localhost":
        return None
    return first_part


def is_ecr_registry(registry: str | None) -> bool:
    return bool(registry and ".dkr.ecr." in registry and ".amazonaws.com" in registry)


def docker_build(args: argparse.Namespace, image_uri: str) -> None:
    command = ["docker", "build", "--tag", image_uri, "--file", args.dockerfile, args.context]
    print_step("Build image")
    exit_if_failed(run(command, dry_run=args.dry_run))


def docker_ecr_login(args: argparse.Namespace, registry: str) -> None:
    print_step("Authenticate Docker to ECR")
    password_command = [*aws_base_args(args), "ecr", "get-login-password"]
    login_command = ["docker", "login", "--username", "AWS", "--password-stdin", registry]

    if args.dry_run:
        print(" ".join(shlex.quote(part) for part in password_command) + " | " + " ".join(shlex.quote(part) for part in login_command))
        return

    password_result = run(password_command, capture=True, quiet=True)
    exit_if_failed(password_result)
    printable_login = " ".join(shlex.quote(part) for part in login_command)
    print(printable_login)
    login_result = subprocess.run(
        login_command,
        input=password_result.stdout,
        check=False,
        text=True,
        capture_output=True,
    )
    exit_if_failed(login_result)
    if login_result.stdout:
        print(login_result.stdout.strip())


def docker_push(args: argparse.Namespace, image_uri: str) -> None:
    print_step("Push image")
    exit_if_failed(run(["docker", "push", image_uri], dry_run=args.dry_run))


def parse_key_value_items(items: list[str], *, label: str) -> list[dict]:
    parsed: list[dict] = []
    for item in items:
        if "=" not in item:
            print(f"Error: {label} must use KEY=value format: {item}", file=sys.stderr)
            sys.exit(2)
        name, value = item.split("=", 1)
        if not name:
            print(f"Error: {label} name cannot be empty: {item}", file=sys.stderr)
            sys.exit(2)
        parsed.append({"name": name, "value": value})
    return parsed


def ecs_log_group_name(agent_name: str) -> str:
    return f"/boxyard/agents/{agent_name}"


def ecs_log_region(args: argparse.Namespace) -> str:
    region = args.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        print(
            "Error: ECS agent ship requires --region, AWS_REGION, or AWS_DEFAULT_REGION for CloudWatch logs.",
            file=sys.stderr,
        )
        sys.exit(2)
    return region


def ecs_create_cluster_command(args: argparse.Namespace) -> list[str]:
    return [*aws_base_args(args), "ecs", "create-cluster", "--cluster-name", args.cluster]


def ecs_describe_cluster_command(args: argparse.Namespace) -> list[str]:
    return [*aws_base_args(args), "ecs", "describe-clusters", "--clusters", args.cluster]


def ecs_create_log_group_command(args: argparse.Namespace) -> list[str]:
    return [*aws_base_args(args), "logs", "create-log-group", "--log-group-name", ecs_log_group_name(args.name)]


def ecs_task_definition_payload(args: argparse.Namespace, image_uri: str) -> dict:
    container: dict = {
        "name": args.name,
        "image": image_uri,
        "essential": True,
        "environment": parse_key_value_items(args.env, label="--env"),
        "secrets": parse_key_value_items(args.secret, label="--secret"),
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": ecs_log_group_name(args.name),
                "awslogs-region": ecs_log_region(args),
                "awslogs-stream-prefix": args.name,
            },
        },
    }
    if args.agent_container_command:
        container["command"] = args.agent_container_command

    payload: dict = {
        "family": args.name,
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": str(args.cpu),
        "memory": str(args.memory),
        "containerDefinitions": [container],
    }
    if args.execution_role_arn:
        payload["executionRoleArn"] = args.execution_role_arn
    if args.task_role_arn:
        payload["taskRoleArn"] = args.task_role_arn
    return payload


def ecs_register_task_definition_command(args: argparse.Namespace, payload: dict) -> list[str]:
    return [
        *aws_base_args(args),
        "ecs",
        "register-task-definition",
        "--cli-input-json",
        json.dumps(payload),
    ]


def ecs_network_configuration(args: argparse.Namespace) -> str:
    values = [f"subnets=[{','.join(args.subnet)}]", "assignPublicIp=DISABLED"]
    if args.security_group:
        values.insert(1, f"securityGroups=[{','.join(args.security_group)}]")
    return "awsvpcConfiguration={" + ",".join(values) + "}"


def ecs_describe_service_command(args: argparse.Namespace) -> list[str]:
    return [
        *aws_base_args(args),
        "ecs",
        "describe-services",
        "--cluster",
        args.cluster,
        "--services",
        args.name,
    ]


def ecs_create_service_command(args: argparse.Namespace, task_definition: str) -> list[str]:
    return [
        *aws_base_args(args),
        "ecs",
        "create-service",
        "--cluster",
        args.cluster,
        "--service-name",
        args.name,
        "--task-definition",
        task_definition,
        "--launch-type",
        "FARGATE",
        "--desired-count",
        str(args.desired_count),
        "--network-configuration",
        ecs_network_configuration(args),
    ]


def ecs_update_service_command(args: argparse.Namespace, task_definition: str) -> list[str]:
    return [
        *aws_base_args(args),
        "ecs",
        "update-service",
        "--cluster",
        args.cluster,
        "--service",
        args.name,
        "--task-definition",
        task_definition,
        "--desired-count",
        str(args.desired_count),
        "--network-configuration",
        ecs_network_configuration(args),
    ]


def create_or_reuse_ecs_cluster(args: argparse.Namespace) -> None:
    print_step("Create or reuse ECS cluster")
    if args.dry_run:
        run(ecs_describe_cluster_command(args), dry_run=True)
        run(ecs_create_cluster_command(args), dry_run=True)
        return

    result = run(ecs_describe_cluster_command(args), capture=True, quiet=True)
    exit_if_failed(result)
    payload = json.loads(result.stdout)
    clusters = payload.get("clusters", [])
    if clusters and clusters[0].get("status") != "INACTIVE":
        print(f"Cluster already exists: {args.cluster}")
        return

    exit_if_failed(run(ecs_create_cluster_command(args), dry_run=args.dry_run))


def create_or_reuse_log_group(args: argparse.Namespace) -> None:
    print_step("Ensure CloudWatch log group")
    result = run(ecs_create_log_group_command(args), capture=True, dry_run=args.dry_run)
    if result.returncode == 0:
        if result.stdout:
            print(result.stdout.strip())
        return
    if "ResourceAlreadyExistsException" in result.stderr:
        print(f"Log group already exists: {ecs_log_group_name(args.name)}")
        return
    exit_if_failed(result)


def register_ecs_task_definition(args: argparse.Namespace, image_uri: str) -> str:
    print_step("Register ECS task definition")
    payload = ecs_task_definition_payload(args, image_uri)
    if args.show_commands:
        print(json.dumps(payload, indent=2))
    result = run(ecs_register_task_definition_command(args, payload), capture=True, dry_run=args.dry_run)
    exit_if_failed(result)
    if args.dry_run:
        return f"{args.name}:dry-run"
    response = json.loads(result.stdout)
    task_definition = response.get("taskDefinition", {}).get("taskDefinitionArn")
    if not task_definition:
        print("Error: ECS did not return a task definition ARN", file=sys.stderr)
        sys.exit(1)
    print(f"Task definition: {task_definition}")
    return task_definition


def ecs_service_exists(args: argparse.Namespace) -> bool:
    result = run(ecs_describe_service_command(args), capture=True, quiet=True)
    exit_if_failed(result)
    payload = json.loads(result.stdout)
    services = payload.get("services", [])
    return bool(services and services[0].get("status") != "INACTIVE")


def create_or_update_ecs_service(args: argparse.Namespace, task_definition: str) -> None:
    print_step("Create or update ECS service")
    if args.dry_run:
        run(ecs_describe_service_command(args), dry_run=True)
        run(ecs_create_service_command(args, task_definition), dry_run=True)
        run(ecs_update_service_command(args, task_definition), dry_run=True)
        return

    if ecs_service_exists(args):
        exit_if_failed(run(ecs_update_service_command(args, task_definition)))
    else:
        exit_if_failed(run(ecs_create_service_command(args, task_definition)))


def wait_for_ecs_service(args: argparse.Namespace) -> None:
    if args.dry_run or not args.wait:
        return
    print_step("Wait for ECS service stability")
    exit_if_failed(
        run(
            [
                *aws_base_args(args),
                "ecs",
                "wait",
                "services-stable",
                "--cluster",
                args.cluster,
                "--services",
                args.name,
            ]
        )
    )


def print_ecs_agent_summary(args: argparse.Namespace, task_definition: str) -> None:
    print_step("ECS agent deployment")
    print(f"Cluster: {args.cluster}")
    print(f"Service: {args.name}")
    print(f"Task definition: {task_definition}")
    print(f"Logs: aws logs tail {shlex.quote(ecs_log_group_name(args.name))} --follow")
    print(
        "Status: "
        + " ".join(
            shlex.quote(part)
            for part in [
                *aws_base_args(args),
                "ecs",
                "describe-services",
                "--cluster",
                args.cluster,
                "--services",
                args.name,
            ]
        )
    )


def ship_ecs_agent(args: argparse.Namespace) -> None:
    image_uri = normalize_image_uri(args.image_uri, args.tag)
    registry = image_registry(image_uri)

    if not args.dry_run:
        require_aws_cli()
        require_docker_cli()

    if not args.skip_build:
        docker_build(args, image_uri)

    if not args.skip_push:
        if args.ecr_login or (args.ecr_login is None and is_ecr_registry(registry)):
            if not registry:
                print("Error: --ecr-login requires a registry image URI", file=sys.stderr)
                sys.exit(2)
            docker_ecr_login(args, registry)
        docker_push(args, image_uri)

    create_or_reuse_ecs_cluster(args)
    create_or_reuse_log_group(args)
    task_definition = register_ecs_task_definition(args, image_uri)
    create_or_update_ecs_service(args, task_definition)
    wait_for_ecs_service(args)
    print_ecs_agent_summary(args, task_definition)


def ship_ec2(args: argparse.Namespace) -> None:
    image_uri = normalize_image_uri(args.image_uri, args.tag)
    registry = image_registry(image_uri)

    if not args.dry_run:
        require_aws_cli()
        require_docker_cli()

    if not args.skip_build:
        docker_build(args, image_uri)

    if not args.skip_push:
        if args.ecr_login or (args.ecr_login is None and is_ecr_registry(registry)):
            if not registry:
                print("Error: --ecr-login requires a registry image URI", file=sys.stderr)
                sys.exit(2)
            docker_ecr_login(args, registry)
        docker_push(args, image_uri)

    deploy_args = argparse.Namespace(**vars(args))
    deploy_args.image = image_uri
    print_step("Deploy image to EC2")
    deploy_ec2(deploy_args)


def add_common_aws_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", help="AWS CLI profile to use")
    parser.add_argument("--region", help="AWS region, for example eu-west-2")


def add_container_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", default="app", help="Container name. Defaults to app")
    parser.add_argument("-p", "--port", action="append", default=[], help="Port mapping, for example 80:8080")
    parser.add_argument("-e", "--env", action="append", default=[], help="Environment variable, for example KEY=value")
    parser.add_argument("-v", "--volume", action="append", default=[], help="Volume mapping, for example /host:/app/data")
    parser.add_argument("--network", help="Docker network on the EC2 instance")
    parser.add_argument("--create-network", action="store_true", help="Create --network on the EC2 instance")
    parser.add_argument("--install-docker", action="store_true", help="Install and start Docker if missing")
    parser.add_argument("--no-replace", dest="replace", action="store_false", help="Fail if the container already exists")
    parser.add_argument("--wait", action="store_true", help="Wait for the SSM command to finish and print output")
    parser.add_argument("--poll-seconds", type=int, default=5, help="Poll interval for --wait")
    parser.add_argument("--show-script", action="store_true", help="Print the remote shell script before sending it")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them")
    parser.add_argument("container_command", nargs=argparse.REMAINDER, help="Optional command for the container")
    parser.set_defaults(replace=True)


def add_ecs_agent_ship_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", required=True, help="Agent and ECS service name")
    parser.add_argument("--image-uri", required=True, help="Final image URI, usually an ECR URI")
    parser.add_argument("--subnet", action="append", required=True, help="Private subnet ID. Repeat for multiple subnets")
    parser.add_argument("--cluster", default="boxyard-agents", help="ECS cluster name. Defaults to boxyard-agents")
    parser.add_argument("--context", default=".", help="Docker build context. Defaults to current directory")
    parser.add_argument("--dockerfile", default="Dockerfile", help="Dockerfile path. Defaults to Dockerfile")
    parser.add_argument("--tag", default="latest", help="Tag to append when --image-uri has no tag")
    parser.add_argument("--cpu", default="512", help="Fargate task CPU units. Defaults to 512")
    parser.add_argument("--memory", default="1024", help="Fargate task memory MiB. Defaults to 1024")
    parser.add_argument("--desired-count", type=int, default=1, help="ECS desired task count. Defaults to 1")
    parser.add_argument("--security-group", action="append", default=[], help="Security group ID. Repeat for multiple groups")
    parser.add_argument("-e", "--env", action="append", default=[], help="Environment variable, for example KEY=value")
    parser.add_argument("--secret", action="append", default=[], help="Secret mapping, for example KEY=secret-or-parameter-arn")
    parser.add_argument("--execution-role-arn", help="Existing ECS task execution role ARN")
    parser.add_argument("--task-role-arn", help="Existing ECS task role ARN for the agent")
    parser.add_argument("--skip-build", action="store_true", help="Skip docker build")
    parser.add_argument("--skip-push", action="store_true", help="Skip docker push")
    ecr_group = parser.add_mutually_exclusive_group()
    ecr_group.add_argument("--ecr-login", dest="ecr_login", action="store_true", help="Force ECR docker login before push")
    ecr_group.add_argument("--no-ecr-login", dest="ecr_login", action="store_false", help="Skip automatic ECR docker login")
    parser.set_defaults(ecr_login=None)
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them")
    parser.add_argument("--show-commands", action="store_true", help="Print generated ECS JSON and AWS commands")
    parser.add_argument("--wait", action="store_true", help="Wait for the ECS service to become stable")
    parser.add_argument(
        "--command",
        dest="agent_container_command",
        nargs=argparse.REMAINDER,
        default=[],
        help="Container command for the agent task",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deploy Boxyard containers to AWS.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_parser = subparsers.add_parser("auth", help="Authenticate or inspect AWS identity")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", required=True)

    status_parser = auth_subparsers.add_parser("status", help="Show the current AWS identity")
    add_common_aws_options(status_parser)

    sso_parser = auth_subparsers.add_parser("sso", help="Configure AWS SSO")
    add_common_aws_options(sso_parser)

    login_parser = auth_subparsers.add_parser("login", help="Log in with an existing AWS SSO profile")
    add_common_aws_options(login_parser)

    ec2_parser = subparsers.add_parser("ec2", help="Deploy Docker images to EC2")
    ec2_subparsers = ec2_parser.add_subparsers(dest="ec2_command", required=True)

    inspect_parser = ec2_subparsers.add_parser("inspect", help="Show EC2 deployment readiness and security checks")
    add_common_aws_options(inspect_parser)
    inspect_parser.add_argument("--instance-id", required=True, help="Target EC2 instance ID")
    inspect_parser.add_argument("--json", action="store_true", help="Print raw inspection data as JSON")

    deploy_parser = ec2_subparsers.add_parser("deploy", help="Deploy an image to an EC2 instance through SSM")
    add_common_aws_options(deploy_parser)
    deploy_parser.add_argument("--instance-id", required=True, help="Target EC2 instance ID")
    deploy_parser.add_argument("--image", required=True, help="Docker image to deploy, for example nginx:latest")
    add_container_run_options(deploy_parser)

    ship_parser = ec2_subparsers.add_parser("ship", help="Build, push, and deploy a Dockerfile app to EC2")
    add_common_aws_options(ship_parser)
    ship_parser.add_argument("--instance-id", required=True, help="Target EC2 instance ID")
    ship_parser.add_argument("--image-uri", required=True, help="Final image URI, usually an ECR URI")
    ship_parser.add_argument("--tag", default="latest", help="Tag to append when --image-uri has no tag")
    ship_parser.add_argument("--context", default=".", help="Docker build context. Defaults to current directory")
    ship_parser.add_argument("--dockerfile", default="Dockerfile", help="Dockerfile path. Defaults to Dockerfile")
    ship_parser.add_argument("--skip-build", action="store_true", help="Skip docker build")
    ship_parser.add_argument("--skip-push", action="store_true", help="Skip docker push")
    ecr_group = ship_parser.add_mutually_exclusive_group()
    ecr_group.add_argument("--ecr-login", dest="ecr_login", action="store_true", help="Force ECR docker login before push")
    ecr_group.add_argument("--no-ecr-login", dest="ecr_login", action="store_false", help="Skip automatic ECR docker login")
    ship_parser.set_defaults(ecr_login=None)
    add_container_run_options(ship_parser)

    ecs_parser = subparsers.add_parser("ecs", help="Deploy Docker images to ECS")
    ecs_subparsers = ecs_parser.add_subparsers(dest="ecs_command", required=True)

    agent_parser = ecs_subparsers.add_parser("agent", help="Deploy containerized agents")
    agent_subparsers = agent_parser.add_subparsers(dest="agent_command", required=True)

    agent_ship_parser = agent_subparsers.add_parser("ship", help="Build, push, and deploy an agent to ECS Fargate")
    add_common_aws_options(agent_ship_parser)
    add_ecs_agent_ship_options(agent_ship_parser)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if getattr(args, "container_command", None) and args.container_command[:1] == ["--"]:
        args.container_command = args.container_command[1:]
    if getattr(args, "agent_container_command", None) and args.agent_container_command[:1] == ["--"]:
        args.agent_container_command = args.agent_container_command[1:]

    if args.command == "auth":
        if args.auth_command == "status":
            auth_status(args)
        elif args.auth_command == "sso":
            auth_sso(args)
        elif args.auth_command == "login":
            auth_login(args)
        else:
            parser.error(f"Unknown auth command: {args.auth_command}")
    elif args.command == "ec2":
        if args.ec2_command == "inspect":
            inspect_ec2(args)
        elif args.ec2_command == "deploy":
            deploy_ec2(args)
        elif args.ec2_command == "ship":
            ship_ec2(args)
        else:
            parser.error(f"Unknown ec2 command: {args.ec2_command}")
    elif args.command == "ecs":
        if args.ecs_command == "agent":
            if args.agent_command == "ship":
                ship_ecs_agent(args)
            else:
                parser.error(f"Unknown ECS agent command: {args.agent_command}")
        else:
            parser.error(f"Unknown ECS command: {args.ecs_command}")
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
