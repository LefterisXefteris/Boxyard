#!/usr/bin/env python3
"""
Simple Docker container manager.

This script wraps common `docker` commands so you can list, start, stop,
restart, remove, inspect logs, and create one or more containers.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from typing import Sequence


def run_docker(args: Sequence[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a docker command and return the completed process."""
    command = ["docker", *args]
    try:
        return subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=capture,
        )
    except FileNotFoundError:
        print("Error: Docker CLI was not found. Install Docker and try again.", file=sys.stderr)
        sys.exit(127)


def require_docker() -> None:
    if shutil.which("docker") is None:
        print("Error: Docker CLI was not found. Install Docker and try again.", file=sys.stderr)
        sys.exit(127)


def exit_if_failed(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        sys.exit(result.returncode)


def list_containers(show_all: bool) -> None:
    args = ["ps"]
    if show_all:
        args.append("--all")
    exit_if_failed(run_docker(args))


def apply_to_containers(action: str, containers: list[str]) -> None:
    for container in containers:
        print(f"{action.capitalize()}ing {container}...")
        result = run_docker([action, container], capture=True)
        if result.returncode == 0:
            print(result.stdout.strip())
        else:
            print(f"Failed to {action} {container}: {result.stderr.strip()}", file=sys.stderr)


def remove_containers(containers: list[str], force: bool) -> None:
    args = ["rm"]
    if force:
        args.append("--force")
    for container in containers:
        print(f"Removing {container}...")
        result = run_docker([*args, container], capture=True)
        if result.returncode == 0:
            print(result.stdout.strip())
        else:
            print(f"Failed to remove {container}: {result.stderr.strip()}", file=sys.stderr)


def show_logs(container: str, follow: bool, tail: str | None) -> None:
    args = ["logs"]
    if follow:
        args.append("--follow")
    if tail is not None:
        args.extend(["--tail", tail])
    args.append(container)
    exit_if_failed(run_docker(args))


def create_container(
    image: str,
    name: str | None,
    ports: list[str],
    env: list[str],
    network: str | None,
    detach: bool,
    command: list[str],
) -> None:
    args = ["run"]
    if detach:
        args.append("--detach")
    if name:
        args.extend(["--name", name])
    if network:
        args.extend(["--network", network])
    for port in ports:
        args.extend(["--publish", port])
    for item in env:
        args.extend(["--env", item])
    args.append(image)
    args.extend(command)
    exit_if_failed(run_docker(args))


def list_networks() -> None:
    exit_if_failed(run_docker(["network", "ls"]))


def create_network(name: str, driver: str) -> None:
    exit_if_failed(run_docker(["network", "create", "--driver", driver, name]))


def remove_network(name: str) -> None:
    exit_if_failed(run_docker(["network", "rm", name]))


def connect_network(network: str, containers: list[str]) -> None:
    for container in containers:
        print(f"Connecting {container} to {network}...")
        result = run_docker(["network", "connect", network, container], capture=True)
        if result.returncode == 0:
            print(result.stdout.strip() or "connected")
        else:
            print(f"Failed to connect {container}: {result.stderr.strip()}", file=sys.stderr)


def disconnect_network(network: str, containers: list[str]) -> None:
    for container in containers:
        print(f"Disconnecting {container} from {network}...")
        result = run_docker(["network", "disconnect", network, container], capture=True)
        if result.returncode == 0:
            print(result.stdout.strip() or "disconnected")
        else:
            print(f"Failed to disconnect {container}: {result.stderr.strip()}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage one or more Docker containers from a simple Python CLI."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ps_parser = subparsers.add_parser("list", help="List running containers")
    ps_parser.add_argument("-a", "--all", action="store_true", help="Show stopped containers too")

    for action in ("start", "stop", "restart"):
        action_parser = subparsers.add_parser(action, help=f"{action.capitalize()} containers")
        action_parser.add_argument("containers", nargs="+", help="Container names or IDs")

    rm_parser = subparsers.add_parser("remove", help="Remove containers")
    rm_parser.add_argument("containers", nargs="+", help="Container names or IDs")
    rm_parser.add_argument("-f", "--force", action="store_true", help="Force remove running containers")

    logs_parser = subparsers.add_parser("logs", help="Show container logs")
    logs_parser.add_argument("container", help="Container name or ID")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    logs_parser.add_argument("--tail", help="Number of lines to show from the end of the logs")

    run_parser = subparsers.add_parser("run", help="Create and run a new container")
    run_parser.add_argument("image", help="Docker image, for example nginx:latest")
    run_parser.add_argument("container_command", nargs=argparse.REMAINDER, help="Optional command for the container")
    run_parser.add_argument("--name", help="Container name")
    run_parser.add_argument("-p", "--port", action="append", default=[], help="Port mapping, for example 8080:80")
    run_parser.add_argument("-e", "--env", action="append", default=[], help="Environment variable, for example KEY=value")
    run_parser.add_argument("--network", help="Attach the container to a Docker network")
    run_parser.add_argument("--foreground", action="store_true", help="Run in foreground instead of detached mode")

    network_parser = subparsers.add_parser("network", help="Manage Docker networks")
    network_subparsers = network_parser.add_subparsers(dest="network_command", required=True)

    network_subparsers.add_parser("list", help="List Docker networks")

    network_create_parser = network_subparsers.add_parser("create", help="Create a Docker network")
    network_create_parser.add_argument("name", help="Network name")
    network_create_parser.add_argument("--driver", default="bridge", help="Network driver. Defaults to bridge")

    network_remove_parser = network_subparsers.add_parser("remove", help="Remove a Docker network")
    network_remove_parser.add_argument("name", help="Network name")

    network_connect_parser = network_subparsers.add_parser("connect", help="Connect containers to a network")
    network_connect_parser.add_argument("network", help="Network name")
    network_connect_parser.add_argument("containers", nargs="+", help="Container names or IDs")

    network_disconnect_parser = network_subparsers.add_parser("disconnect", help="Disconnect containers from a network")
    network_disconnect_parser.add_argument("network", help="Network name")
    network_disconnect_parser.add_argument("containers", nargs="+", help="Container names or IDs")

    return parser


def main() -> None:
    require_docker()
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "list":
        list_containers(args.all)
    elif args.command in {"start", "stop", "restart"}:
        apply_to_containers(args.command, args.containers)
    elif args.command == "remove":
        remove_containers(args.containers, args.force)
    elif args.command == "logs":
        show_logs(args.container, args.follow, args.tail)
    elif args.command == "run":
        create_container(
            image=args.image,
            name=args.name,
            ports=args.port,
            env=args.env,
            network=args.network,
            detach=not args.foreground,
            command=args.container_command,
        )
    elif args.command == "network":
        if args.network_command == "list":
            list_networks()
        elif args.network_command == "create":
            create_network(args.name, args.driver)
        elif args.network_command == "remove":
            remove_network(args.name)
        elif args.network_command == "connect":
            connect_network(args.network, args.containers)
        elif args.network_command == "disconnect":
            disconnect_network(args.network, args.containers)
        else:
            parser.error(f"Unknown network command: {args.network_command}")
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
