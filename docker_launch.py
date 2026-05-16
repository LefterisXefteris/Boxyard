#!/usr/bin/env python3
"""
Launch common Docker containers by short name.

Examples:
  python3 docker_launch sqlite
  python3 docker_launch postgres redis nginx --network boxyard-net --create-network
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


def build_run_command(
    args: argparse.Namespace,
    preset: Preset,
    *,
    name: str,
) -> list[str]:
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
    parser.add_argument("kinds", nargs="*", help="Container kinds, for example sqlite nginx redis postgres")
    parser.add_argument("--list", action="store_true", help="Show available container presets")
    parser.add_argument("--name", help="Container name. Defaults to the selected kind.")
    parser.add_argument("--name-prefix", help="Prefix names when launching multiple containers")
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


def parse_args(parser: argparse.ArgumentParser) -> argparse.Namespace:
    argv = sys.argv[1:]
    if "--" in argv:
        separator_index = argv.index("--")
        args = parser.parse_args(argv[:separator_index])
        args.container_command = argv[separator_index + 1 :]
    else:
        args = parser.parse_args(argv)
        args.container_command = []
    return args


def validate_preset(parser: argparse.ArgumentParser, kind: str) -> Preset:
    preset = PRESETS.get(kind)
    if preset is None:
        available = ", ".join(sorted(PRESETS))
        parser.error(f"unknown container kind {kind!r}. Available: {available}")
    return preset


def container_name(args: argparse.Namespace, kind: str, seen: dict[str, int]) -> str:
    if args.name:
        return args.name

    base_name = f"{args.name_prefix}-{kind}" if args.name_prefix else kind
    seen[base_name] = seen.get(base_name, 0) + 1
    if seen[base_name] == 1:
        return base_name
    return f"{base_name}-{seen[base_name]}"


def validate_multi_launch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if len(args.kinds) <= 1:
        return

    unsupported: list[str] = []
    if args.name:
        unsupported.append("--name")
    if args.image:
        unsupported.append("--image")
    if args.port:
        unsupported.append("--port")
    if args.env:
        unsupported.append("--env")
    if args.volume:
        unsupported.append("--volume")
    if args.foreground:
        unsupported.append("--foreground")
    if args.container_command:
        unsupported.append("container command after --")

    if unsupported:
        parser.error(
            "multi-container launch does not support "
            + ", ".join(unsupported)
            + ". Launch one container at a time for per-container options."
        )


def main() -> None:
    parser = build_parser()
    args = parse_args(parser)

    if args.list:
        list_presets()
        return

    if not args.kinds:
        parser.error("choose one or more container kinds, or use --list")

    if args.create_network and not args.network:
        parser.error("--create-network requires --network")

    validate_multi_launch(parser, args)
    presets = [(kind, validate_preset(parser, kind)) for kind in args.kinds]

    if not args.dry_run:
        require_docker()

    if args.create_network:
        run_checked(build_network_create_command(args.network), dry_run=args.dry_run)

    seen_names: dict[str, int] = {}
    for kind, preset in presets:
        name = container_name(args, kind, seen_names)
        return_code = run(build_run_command(args, preset, name=name), dry_run=args.dry_run)
        if return_code != 0:
            sys.exit(return_code)
        if preset.note:
            print(preset.note.replace("<name>", name))
    sys.exit(return_code)


if __name__ == "__main__":
    main()
