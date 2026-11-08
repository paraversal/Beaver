#!/usr/bin/env bash
# Deploy the latest origin/main on the server.
#
# The server checkout is deploy-only: it is reset to match the remote exactly,
# so this also works after a force-push. Untracked files (.env, data/) are
# never touched, as long as they stay in .gitignore. Never run `git clean` here.
set -euo pipefail
cd "$(dirname "$0")"

BRANCH="${1:-main}"

# Safety checks before touching anything
[[ -f .env ]] || { echo "error: .env missing (copy .env.example and fill it in)" >&2; exit 1; }
if [[ "$(stat -c %a .env)" != "600" ]]; then
    echo "warning: .env is not chmod 600, fixing" >&2
    chmod 600 .env
fi
for path in .env data/; do
    git check-ignore -q "$path" || { echo "error: $path is not gitignored; refusing to reset" >&2; exit 1; }
done

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "Discarding local changes to tracked files:" >&2
    git status --short --untracked-files=no >&2
fi

git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"
echo "Deploying $(git log -1 --format='%h %s')"

mkdir -p data
sudo chown -R 10001:10001 data

sudo docker compose up -d --build --remove-orphans
sleep 3
sudo docker compose ps
sudo docker compose logs --tail 30
