#!/usr/bin/env python3
"""
Deploy Docker images to AWS from the command line.

The first deployment target is EC2 through AWS Systems Manager. That means the
EC2 instance needs SSM access, but you do not need SSH access from your laptop.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
from typing import Sequence


def require_aws_cli() -> None:
    if shutil.which("aws") is None:
        print("Error: AWS CLI was not found. Install and configure AWS CLI first.", file=sys.stderr)
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


def status_line(status: str, label: str, detail: str) -> None:
    print(f"[{status:<7}] {label:<22} {detail}")


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

    print(f"Boxyard EC2 preflight: {args.instance_id}")
    print("")
    print("Instance")
    status_line("OK" if state == "running" else "WARN", "State", state)
    status_line("INFO", "Name", name)
    status_line("INFO", "Type", instance.get("InstanceType", "-"))
    status_line("INFO", "AMI", instance.get("ImageId", "-"))
    status_line("INFO", "VPC", instance.get("VpcId", "-"))
    status_line("INFO", "Subnet", instance.get("SubnetId", "-"))
    status_line("INFO", "Private IPv4", private_ip)
    status_line("INFO" if public_ip != "-" else "WARN", "Public IPv4", public_ip)
    status_line("OK" if metadata_tokens == "required" else "WARN", "IMDSv2", f"HttpTokens={metadata_tokens}")

    print("")
    print("SSM")
    if report["ssm_error"]:
        status_line("UNKNOWN", "SSM status", report["ssm_error"])
    elif ssm_instance:
        ping_status = ssm_instance.get("PingStatus", "unknown")
        agent_version = ssm_instance.get("AgentVersion", "-")
        status_line("OK" if ping_status == "Online" else "WARN", "SSM ping", ping_status)
        status_line("INFO", "SSM agent", agent_version)
    else:
        status_line("WARN", "SSM status", "not registered or not visible to this AWS identity")

    print("")
    print("IAM")
    if iam_report.get("profile"):
        status_line("INFO", "Instance profile", iam_report["profile"])
        status_line("INFO", "Role(s)", ", ".join(iam_report["roles"]) or "-")
        if iam_report.get("error"):
            status_line("UNKNOWN", "Role policies", iam_report["error"])
        else:
            status_line(
                "OK" if has_ssm_policy else "WARN",
                "SSM policy",
                "AmazonSSMManagedInstanceCore attached" if has_ssm_policy else "not found in attached policies",
            )
    else:
        status_line("WARN", "Instance profile", "none attached")

    print("")
    print("Security Groups")
    if report["security_group_error"]:
        status_line("UNKNOWN", "Security groups", report["security_group_error"])
    elif not report["security_groups"]:
        status_line("WARN", "Security groups", "none found")
    else:
        for group in report["security_groups"]:
            status_line("INFO", "Group", f"{group.get('GroupName', '-')} ({group.get('GroupId', '-')})")
        findings = report["security_findings"]
        if findings:
            for finding in findings:
                status_line(finding["severity"], finding["group"], finding["message"])
        else:
            status_line("OK", "Inbound exposure", "no public 0.0.0.0/0 or ::/0 inbound rules found")

    print("")
    print("Deploy Readiness")
    status_line("OK" if state == "running" else "WARN", "EC2 running", "required for deployment")
    if ssm_instance and ssm_instance.get("PingStatus") == "Online":
        status_line("OK", "SSM online", "Boxyard can send deployment commands")
    else:
        status_line("WARN", "SSM online", "required unless you add an SSH deployment path later")
    if public_ip == "-":
        status_line("INFO", "Public traffic", "no public IP; use private networking, VPN, ALB, or assign public access")


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


def add_common_aws_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", help="AWS CLI profile to use")
    parser.add_argument("--region", help="AWS region, for example eu-west-2")


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
    deploy_parser.add_argument("--name", default="app", help="Container name. Defaults to app")
    deploy_parser.add_argument("-p", "--port", action="append", default=[], help="Port mapping, for example 80:8080")
    deploy_parser.add_argument("-e", "--env", action="append", default=[], help="Environment variable, for example KEY=value")
    deploy_parser.add_argument("-v", "--volume", action="append", default=[], help="Volume mapping, for example /host:/app/data")
    deploy_parser.add_argument("--network", help="Docker network on the EC2 instance")
    deploy_parser.add_argument("--create-network", action="store_true", help="Create --network on the EC2 instance")
    deploy_parser.add_argument("--install-docker", action="store_true", help="Install and start Docker if missing")
    deploy_parser.add_argument("--no-replace", dest="replace", action="store_false", help="Fail if the container already exists")
    deploy_parser.add_argument("--wait", action="store_true", help="Wait for the SSM command to finish and print output")
    deploy_parser.add_argument("--poll-seconds", type=int, default=5, help="Poll interval for --wait")
    deploy_parser.add_argument("--show-script", action="store_true", help="Print the remote shell script before sending it")
    deploy_parser.add_argument("--dry-run", action="store_true", help="Print AWS CLI commands without running them")
    deploy_parser.add_argument("container_command", nargs=argparse.REMAINDER, help="Optional command for the container")
    deploy_parser.set_defaults(replace=True)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if getattr(args, "container_command", None) and args.container_command[:1] == ["--"]:
        args.container_command = args.container_command[1:]

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
        else:
            parser.error(f"Unknown ec2 command: {args.ec2_command}")
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
