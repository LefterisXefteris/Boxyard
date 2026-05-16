#!/usr/bin/env python3
"""
Launch common Docker containers by short name.

Examples:
  python3 docker_launch sqlite
  python3 docker_launch nginx --name web -p 8080:80
  python3 docker_launch postgres --name db -e POSTGRES_PASSWORD=secret
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Preset:
    image: str
    ports: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    detach: bool = True
    note: str = ""


PRESETS: dict[str, Preset] = {
    "alpine": Preset(image="alpine:latest", command=("sleep", "infinity")),
    "ubuntu": Preset(image="ubuntu:latest", command=("sleep", "infinity")),
    "nginx": Preset(image="nginx:latest", ports=("8080:80",)),
    "redis": Preset(image="redis:latest", ports=("6379:6379",)),
    "postgres": Preset(
        image="postgres:latest",
        ports=("5432:5432",),
        env=("POSTGRES_PASSWORD=password", "POSTGRES_DB=app"),
    ),
    "mysql": Preset(
        image="mysql:latest",
        ports=("3306:3306",),
        env=("MYSQL_ROOT_PASSWORD=password", "MYSQL_DATABASE=app"),
    ),
    "mariadb": Preset(
        image="mariadb:latest",
        ports=("3306:3306",),
        env=("MARIADB_ROOT_PASSWORD=password", "MARIADB_DATABASE=app"),
    ),
    "mongo": Preset(image="mongo:latest", ports=("27017:27017",)),
    "sqlite": Preset(
        image="keinos/sqlite3:latest",
        volumes=("./sqlite-data:/data",),
        command=("tail", "-f", "/dev/null"),
        note="Keeps a tiny SQLite container running. Open it with: docker exec -it <name> sqlite3 /data/app.db",
    ),
}


def run(command: Sequence[str], *, dry_run: bool) -> int:
    printable = " ".join(shlex.quote(part) for part in command)
    print(printable)
    if dry_run:
        return 0
    try:
        return subprocess.run(command, check=False).returncode
    except FileNotFoundError:
        print("Error: Docker CLI was not found. Install Docker and try again.", file=sys.stderr)
        return 127


def run_checked(command: Sequence[str], *, dry_run: bool) -> int:
    return_code = run(command, dry_run=dry_run)
    if return_code != 0:
        sys.exit(return_code)
    return return_code


def require_docker() -> None:
    if shutil.which("docker") is None:
        print("Error: Docker CLI was not found. Install Docker and try again.", file=sys.stderr)
        sys.exit(127)


def build_run_command(args: argparse.Namespace, preset: Preset) -> list[str]:
    name = args.name or args.kind
    command = ["docker", "run"]

    detach = preset.detach and not args.foreground
    if detach:
        command.append("--detach")

    command.extend(["--name", name])

    if args.network:
        command.extend(["--network", args.network])

    ports = args.port or list(preset.ports)
    env = merge_env(list(preset.env), args.env)

    for port in ports:
        command.extend(["--publish", port])

    for item in env:
        command.extend(["--env", item])

    for volume in [*preset.volumes, *args.volume]:
        command.extend(["--volume", volume])

    command.append(args.image or preset.image)
    command.extend(args.container_command or preset.command)
    return command


def build_network_create_command(network: str) -> list[str]:
    return ["docker", "network", "create", network]


def merge_env(defaults: list[str], overrides: list[str]) -> list[str]:
    values: dict[str, str] = {}
    order: list[str] = []
    for item in [*defaults, *overrides]:
        key = item.split("=", 1)[0]
        if key not in values:
            order.append(key)
        values[key] = item
    return [values[key] for key in order]


def list_presets() -> None:
    width = max(len(name) for name in PRESETS)
    for name, preset in sorted(PRESETS.items()):
        ports = ", ".join(preset.ports) if preset.ports else "-"
        print(f"{name:<{width}}  {preset.image:<24} ports: {ports}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch common Docker containers by short name.")
    parser.add_argument("kind", nargs="?", help="Container kind, for example sqlite, nginx, redis, postgres")
    parser.add_argument("--list", action="store_true", help="Show available container presets")
    parser.add_argument("--name", help="Container name. Defaults to the selected kind.")
    parser.add_argument("--image", help="Override the Docker image for this launch")
    parser.add_argument("-p", "--port", action="append", default=[], help="Extra port mapping, for example 8080:80")
    parser.add_argument("-e", "--env", action="append", default=[], help="Extra environment variable, for example KEY=value")
    parser.add_argument("-v", "--volume", action="append", default=[], help="Extra volume, for example ./data:/data")
    parser.add_argument("--network", help="Attach the container to a Docker network")
    parser.add_argument("--create-network", action="store_true", help="Create --network before launching the container")
    parser.add_argument("--foreground", action="store_true", help="Run in foreground instead of detached mode")
    parser.add_argument("--dry-run", action="store_true", help="Print the docker command without running it")
    parser.epilog = "Anything after -- is passed to the container as its command."
    return parser


def main() -> None:
    parser = build_parser()
    args, container_command = parser.parse_known_args()
    if container_command[:1] == ["--"]:
        container_command = container_command[1:]
    args.container_command = container_command

    if args.list:
        list_presets()
        return

    if not args.kind:
        parser.error("choose a container kind, or use --list")

    preset = PRESETS.get(args.kind)
    if preset is None:
        available = ", ".join(sorted(PRESETS))
        parser.error(f"unknown container kind {args.kind!r}. Available: {available}")

    if not args.dry_run:
        require_docker()

    if args.create_network and not args.network:
        parser.error("--create-network requires --network")

    if args.create_network:
        run_checked(build_network_create_command(args.network), dry_run=args.dry_run)

    return_code = run(build_run_command(args, preset), dry_run=args.dry_run)
    if return_code == 0 and preset.note:
        print(preset.note.replace("<name>", args.name or args.kind))
    sys.exit(return_code)


if __name__ == "__main__":
    main()
