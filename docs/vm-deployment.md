# ModelBytes VM Deployment

This guide moves the publisher from Railway to a self-managed VM while keeping the existing GitHub/Claude routine handoff intact.

## Target Shape

- `/opt/modelbytes` is a Git checkout of `SovereignSignal/modelbytes`.
- Docker Compose runs a local Postgres service and a one-shot ModelBytes app container.
- `modelbytes-publish.timer` runs daily at 16:00 UTC.
- `modelbytes-db-backup.timer` runs daily after the publish window.
- Secrets live in `/etc/modelbytes/*.env`, not in the repo.
- Logs live in `journalctl` and Docker.

The curator routine can keep committing `pending/YYYY-MM-DD.txt` to GitHub. The VM publisher pulls `master` before every run, sees the pending file, posts it, and records the UTC date in Postgres.

## Files

- `deploy/vm/docker-compose.yml` - app + local Postgres.
- `deploy/vm/modelbytes.env.example` - app env template.
- `deploy/vm/postgres.env.example` - Postgres env template.
- `deploy/vm/systemd/modelbytes-publish.*` - daily publish service/timer.
- `deploy/vm/systemd/modelbytes-db-backup.*` - daily backup service/timer.
- `deploy/vm/backup-postgres.sh` - compressed `pg_dump` helper.

## VM Setup

Install base packages:

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates curl docker.io docker-compose-plugin
```

Create the service user and directories:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin modelbytes
sudo usermod -aG docker modelbytes
sudo mkdir -p /opt /etc/modelbytes /var/backups/modelbytes
sudo chown modelbytes:modelbytes /var/backups/modelbytes
```

Clone the repo:

```bash
sudo git clone https://github.com/SovereignSignal/modelbytes.git /opt/modelbytes
sudo chown -R modelbytes:modelbytes /opt/modelbytes
```

Create env files from the templates:

```bash
sudo install -m 600 -o modelbytes -g modelbytes \
  /opt/modelbytes/deploy/vm/modelbytes.env.example \
  /etc/modelbytes/modelbytes.env

sudo install -m 600 -o modelbytes -g modelbytes \
  /opt/modelbytes/deploy/vm/postgres.env.example \
  /etc/modelbytes/postgres.env
```

Edit both files. The `POSTGRES_PASSWORD` in `postgres.env` must match the password embedded in `modelbytes.env` `DATABASE_URL`.

## Database Migration

Export Railway Postgres, then restore into the VM Postgres. Use Railway's public database URL or the Railway dashboard connection helper; do not paste secrets into shell history.

High-level flow:

```bash
# On a trusted machine with pg_dump/psql:
pg_dump "$RAILWAY_DATABASE_PUBLIC_URL" > modelbytes.sql

# On the VM:
sudo -u modelbytes docker compose -f /opt/modelbytes/deploy/vm/docker-compose.yml up -d postgres
cat modelbytes.sql | sudo -u modelbytes docker compose -f /opt/modelbytes/deploy/vm/docker-compose.yml exec -T postgres \
  psql -U modelbytes modelbytes
```

Before cutover, verify the expected tables:

```bash
sudo -u modelbytes docker compose -f /opt/modelbytes/deploy/vm/docker-compose.yml exec -T postgres \
  psql -U modelbytes modelbytes -c '\dt'
```

## Dry Run

Build and run preview mode:

```bash
cd /opt/modelbytes
sudo -u modelbytes docker compose -f deploy/vm/docker-compose.yml build modelbytes
sudo -u modelbytes docker compose -f deploy/vm/docker-compose.yml up -d postgres
sudo -u modelbytes docker compose -f deploy/vm/docker-compose.yml run --rm modelbytes python monitor.py --preview
```

Preview mode fetches live sources and renders the candidate digest without sending Telegram or seeding the `models` table. On a fresh VM database, this should show what the fallback pipeline would post if the curator file were missing.

## Install Timers

```bash
sudo cp /opt/modelbytes/deploy/vm/systemd/modelbytes-publish.service /etc/systemd/system/
sudo cp /opt/modelbytes/deploy/vm/systemd/modelbytes-publish.timer /etc/systemd/system/
sudo cp /opt/modelbytes/deploy/vm/systemd/modelbytes-db-backup.service /etc/systemd/system/
sudo cp /opt/modelbytes/deploy/vm/systemd/modelbytes-db-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now modelbytes-db-backup.timer
```

Do not enable `modelbytes-publish.timer` until Railway is paused. Railway and the VM use separate Postgres databases after migration; if both timers are active, duplicate Telegram posts are possible.

Cut over:

```bash
# Pause or disable the Railway cron/service first.
sudo systemctl enable --now modelbytes-publish.timer
sudo systemctl list-timers 'modelbytes-*'
```

## Operations

Manual publish:

```bash
sudo systemctl start modelbytes-publish.service
journalctl -u modelbytes-publish.service -n 100 --no-pager
```

Manual backup:

```bash
sudo systemctl start modelbytes-db-backup.service
ls -lh /var/backups/modelbytes
```

Follow logs:

```bash
journalctl -u modelbytes-publish.service -f
```

Rollback:

1. Disable the VM publish timer.
2. Re-enable Railway cron/service.
3. Confirm only one publisher is active before the next 16:00 UTC window.

## Follow-Ups

- Ship backups off-box after each local `pg_dump`.
- Add alerting for failed systemd units.
- Add a `monitor.py --health` command for source/database/Telegram diagnostics.
- Split fetchers into a `sources/` package if source count keeps growing.
