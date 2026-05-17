# Boxyard

Boxyard is a small `uv`-based Python CLI for launching, managing, networking,
and deploying Docker containers.

It is meant to make common container workflows feel quick:

- launch useful local containers from presets like `sqlite`, `postgres`, `redis`, `nginx`, `mysql`, and `mongo`
- launch several containers in one command
- connect containers to shared Docker networks so they can talk by name
- manage existing containers and networks
- inspect AWS EC2 deployment readiness with a terminal dashboard
- deploy a Docker image to EC2 through AWS Systems Manager without SSH

## Preview

![Boxyard EC2 preflight dashboard](assets/boxyard-ec2-preflight.png)

Try the dashboard locally without AWS credentials:

```bash
uv run boxyard-aws ec2 inspect-demo
```

## Requirements

- Python 3.10+
- `uv`
- Docker installed and running for local container commands
- AWS CLI for AWS commands

Install `uv` if needed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Set Up

From this project folder:

```bash
uv sync
```

Run commands through `uv`:

```bash
uv run docker-launch --list
uv run docker-launch sqlite
uv run docker-manager list --all
uv run boxyard-aws ec2 inspect-demo
```

Or activate the virtual environment:

```bash
source .venv/bin/activate
docker-launch --list
docker-manager list --all
boxyard-aws ec2 inspect-demo
deactivate
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

List available presets:

```bash
uv run docker-launch --list
```

Preview a launch without creating a container:

```bash
uv run docker-launch sqlite --dry-run
```

## Launch Multiple Containers

Launch one container per preset:

```bash
uv run docker-launch postgres redis nginx --network boxyard-net --create-network
```

This creates containers named:

```text
postgres
redis
nginx
```

Use a prefix for grouped launches:

```bash
uv run docker-launch postgres redis nginx --name-prefix app --network boxyard-net
```

This creates:

```text
app-postgres
app-redis
app-nginx
```

## Docker Networking

Containers on the same user-created Docker network can talk to each other by
container name.

```bash
uv run docker-manager network create boxyard-net
uv run docker-launch postgres --name db --network boxyard-net
uv run docker-launch redis --name cache --network boxyard-net
uv run docker-launch alpine --name app --network boxyard-net
```

Inside the `app` container:

```text
db:5432
cache:6379
```

Manage networks:

```bash
uv run docker-manager network list
uv run docker-manager network create boxyard-net
uv run docker-manager network connect boxyard-net web api worker
uv run docker-manager network disconnect boxyard-net worker
uv run docker-manager network remove boxyard-net
```

## SQLite Example

Launch SQLite:

```bash
uv run docker-launch sqlite
```

Boxyard runs:

```bash
docker run --detach --name sqlite --volume ./sqlite-data:/data keinos/sqlite3:latest tail -f /dev/null
```

Open a SQLite database inside the container:

```bash
docker exec -it sqlite sqlite3 /data/app.db
```

Remove it:

```bash
uv run docker-manager remove sqlite --force
```

## Custom Launch Options

```bash
uv run docker-launch nginx --name web -p 8080:80
uv run docker-launch postgres --name db -e POSTGRES_PASSWORD=secret
uv run docker-launch redis --name cache -p 6380:6379
uv run docker-launch sqlite --name local-sqlite -v ./my-db:/data
uv run docker-launch postgres --name db --network boxyard-net
```

Anything after `--` is passed to the container:

```bash
uv run docker-launch alpine --name box -- sh -lc "echo hello"
```

## Manage Containers

```bash
uv run docker-manager list
uv run docker-manager list --all
uv run docker-manager logs sqlite --tail 50
uv run docker-manager stop sqlite
uv run docker-manager start sqlite
uv run docker-manager restart sqlite
uv run docker-manager remove sqlite --force
```

Manage several containers at once:

```bash
uv run docker-manager stop web api worker
uv run docker-manager remove web api worker --force
```

## AWS EC2 Inspection

Boxyard can inspect an EC2 instance before deployment and show:

- EC2 state, instance type, VPC, subnet, private IP, and public IP
- SSM online status for no-SSH deployment
- IAM instance profile and SSM policy when readable
- security group warnings for public SSH, public all-port rules, and public inbound rules
- IMDSv2 status
- deployment readiness

Demo the UI without AWS:

```bash
uv run boxyard-aws ec2 inspect-demo
```

Inspect a real EC2 instance:

```bash
uv run boxyard-aws ec2 inspect \
  --profile my-profile \
  --region eu-west-2 \
  --instance-id i-0123456789abcdef0
```

Print raw inspection data:

```bash
uv run boxyard-aws ec2 inspect \
  --region eu-west-2 \
  --instance-id i-0123456789abcdef0 \
  --json
```

## AWS Authentication

Check your AWS identity:

```bash
uv run boxyard-aws auth status --profile my-profile --region eu-west-2
```

Configure AWS SSO if needed:

```bash
uv run boxyard-aws auth sso
uv run boxyard-aws auth login --profile my-profile
```

If you use access keys instead of SSO:

```bash
aws configure
```

## Deploy to AWS EC2

Boxyard deploys to EC2 through AWS Systems Manager. That means your laptop does
not need SSH access to the instance.

One-time EC2 requirements:

- SSM Agent running
- IAM role with `AmazonSSMManagedInstanceCore`
- Docker installed, or use `--install-docker`
- the image is pullable by the EC2 instance

Preview a deployment:

```bash
uv run boxyard-aws ec2 deploy \
  --profile my-profile \
  --region eu-west-2 \
  --instance-id i-0123456789abcdef0 \
  --image nginx:latest \
  --name web \
  -p 80:80 \
  --dry-run \
  --show-script
```

Deploy:

```bash
uv run boxyard-aws ec2 deploy \
  --profile my-profile \
  --region eu-west-2 \
  --instance-id i-0123456789abcdef0 \
  --image nginx:latest \
  --name web \
  -p 80:80 \
  --install-docker \
  --wait
```

Deploy onto a Docker network on EC2:

```bash
uv run boxyard-aws ec2 deploy \
  --profile my-profile \
  --region eu-west-2 \
  --instance-id i-0123456789abcdef0 \
  --image redis:latest \
  --name cache \
  --network boxyard-net \
  --create-network \
  --wait
```

By default, Boxyard replaces an existing container with the same name. Use
`--no-replace` to fail instead.

## Plain Python Usage

The scripts still work directly with Python:

```bash
python3 docker_launch.py sqlite
python3 docker_manager.py list --all
python3 boxyard_aws.py ec2 inspect-demo
```
