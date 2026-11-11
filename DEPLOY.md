# Deploying Beaver

Beaver runs on the server as a Docker Compose service. Local development still
uses `uv run main.py` (see README).

## How it is set up

| Piece | Where | Notes |
|---|---|---|
| Code | `~/Beaver` (git checkout of `origin/main`) | Deploy-only; never edit here |
| Secrets | `~/Beaver/.env` | `chmod 600`, gitignored, **not** in the image |
| Database | `~/Beaver/data/beaver.db` (+ `-wal`, `-shm`) | Owned by uid 10001, mounted at `/app/data` |
| Image | `python:3.14-slim` + uv | Runs as uid 10001, read-only root, no capabilities |

- `compose.yml` reads `.env` **on the host** and injects the values as
  environment variables. There is no `.env` file inside the container, and the
  app doesn't need one.
- The bot uses long polling, so no ports are published.
- Logs: Docker `json-file`, rotated at 3 × 10 MB.

## First-time setup

```sh
git clone https://github.com/paraversal/beaver ~/Beaver
cd ~/Beaver
cp .env.example .env && chmod 600 .env   # fill in BOT_TOKEN etc.
./deploy.sh
```

## Deploying an update

From the Mac: push to `main`. On the server:

```sh
~/Beaver/deploy.sh
```

The script resets the checkout to `origin/main` (so it also works after a
force-push), rebuilds the image, restarts the container and shows the last log
lines. It refuses to run if `.env` or `data/` are not gitignored, because the
reset must never touch them. **Never run `git clean` in this checkout:** it
would delete the database and the token.

## Day to day

| Task | Command (in `~/Beaver`) |
|---|---|
| Logs | `sudo docker compose logs -f` |
| Restart | `sudo docker compose restart` |
| Stop | `sudo docker compose down` |
| Shell in the container | `sudo docker compose exec beaver-bot sh` |

## Backups

The database is SQLite in WAL mode, so `beaver.db` alone may be missing recent
writes that are still in `beaver.db-wal`. Use one of these:

- **Stop, copy, start** (simplest, a few seconds of downtime):
  `sudo docker compose stop && <backup data/> && sudo docker compose start`
- **Online snapshot** with SQLite's backup API:
  ```sh
  sudo docker compose exec beaver-bot /app/.venv/bin/python -c \
    "import sqlite3; s=sqlite3.connect('data/beaver.db'); d=sqlite3.connect('data/beaver.backup.db'); s.backup(d); d.close()"
  ```
  then back up `data/beaver.backup.db`.

Also back up `.env`. Test a restore once.

## Staging

Run staging locally (`uv run main.py --env-file .env.staging`) with a separate
bot token. Don't run two pollers on the same token: Telegram's update queue has
one consumer.
