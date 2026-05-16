# Boxyard

This is a small Python app for launching and managing one or more Docker
containers. It is designed to run with `uv`, but it has no third-party Python
dependencies.

## Requirements

- Docker installed and running
- `uv` installed
- Python 3.10+

Install `uv` if you do not already have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Set Up With uv

From this project folder:

```bash
uv sync
```

That creates a local virtual environment in `.venv`.

Activate the virtual environment:

```bash
source .venv/bin/activate
```

When the virtual environment is active, you can use the installed commands:

```bash
docker-launch --list
docker-launch sqlite
docker-manager list --all
```

Deactivate the virtual environment when you are done:

```bash
deactivate
```

## Run Without Activating

You can also run everything through `uv` directly:

```bash
uv run docker-launch --list
uv run docker-launch sqlite
uv run docker-manager list --all
```

## Launch Containers

Launch common containers by short name:

```bash
uv run docker-launch sqlite
uv run docker-launch nginx
uv run docker-launch redis
uv run docker-launch postgres
uv run docker-launch mysql
uv run docker-launch mongo
```

Launch several containers in one command:

```bash
uv run docker-launch postgres redis nginx --network boxyard-net --create-network
```

That launches one container per preset:

```text
postgres  -> container name postgres
redis     -> container name redis
nginx     -> container name nginx
```

Use a name prefix when launching a group:

```bash
uv run docker-launch postgres redis nginx --name-prefix app --network boxyard-net
```

That creates containers named `app-postgres`, `app-redis`, and `app-nginx`.

Attach containers to the same Docker network so they can talk to each other:

```bash
uv run docker-manager network create boxyard-net
uv run docker-launch postgres --name db --network boxyard-net
uv run docker-launch redis --name cache --network boxyard-net
uv run docker-launch alpine --name app --network boxyard-net
```

Inside the `app` container, the other containers are reachable by name:

```text
db:5432
cache:6379
```

Create the network during launch:

```bash
uv run docker-launch postgres --name db --network boxyard-net --create-network
```

Preview the Docker command without creating a container:

```bash
uv run docker-launch sqlite --dry-run
```

List available launch presets:

```bash
uv run docker-launch --list
```

## SQLite Example

This command:

```bash
uv run docker-launch sqlite
```

runs this Docker command underneath:

```bash
docker run --detach --name sqlite --volume ./sqlite-data:/data keinos/sqlite3:latest tail -f /dev/null
```

It creates a running container named `sqlite`, mounts a local `./sqlite-data`
folder into the container at `/data`, and keeps the container alive.

Open a SQLite database inside the container:

```bash
docker exec -it sqlite sqlite3 /data/app.db
```

Remove the SQLite container:

```bash
uv run docker-manager remove sqlite --force
```

## Custom Names, Ports, Env Vars, and Volumes

```bash
uv run docker-launch nginx --name web -p 8080:80
uv run docker-launch postgres --name db -e POSTGRES_PASSWORD=secret
uv run docker-launch redis --name cache -p 6380:6379
uv run docker-launch sqlite --name local-sqlite -v ./my-db:/data
uv run docker-launch postgres --name db --network boxyard-net
```

Anything after `--` is passed to the container as its command:

```bash
uv run docker-launch alpine --name box -- sh -lc "echo hello"
```

## Manage Existing Containers

```bash
uv run docker-manager list
uv run docker-manager list --all
uv run docker-manager logs sqlite --tail 50
uv run docker-manager stop sqlite
uv run docker-manager start sqlite
uv run docker-manager restart sqlite
uv run docker-manager remove sqlite --force
```

Manage multiple containers at once:

```bash
uv run docker-manager stop web api worker
uv run docker-manager remove web api worker --force
```

## Manage Docker Networks

```bash
uv run docker-manager network list
uv run docker-manager network create boxyard-net
uv run docker-manager network connect boxyard-net web api worker
uv run docker-manager network disconnect boxyard-net worker
uv run docker-manager network remove boxyard-net
```

Containers on the same user-created bridge network can reach each other by
container name. For example, a container named `api` can connect to Postgres at
`db:5432` when both `api` and `db` are on `boxyard-net`.

## Plain Python Usage

The scripts still work directly with Python:

```bash
python3 docker_launch.py sqlite
python3 docker_manager.py list --all
```
