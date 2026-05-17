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


def run(command: Sequence[str], *, capture: bool = False, dry_run: bool = False) -> subprocess.CompletedProcess[str]:
    printable = " ".join(shlex.quote(part) for part in command)
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
        if args.ec2_command == "deploy":
            deploy_ec2(args)
        else:
            parser.error(f"Unknown ec2 command: {args.ec2_command}")
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
