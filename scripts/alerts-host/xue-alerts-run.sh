#!/bin/sh
# One alerts round on a self-hosted publisher (xue-alerts.service): the
# same steps as .github/workflows/publish-alerts.yml, from a sparse checkout
# of main that follows the branch, so the host runs what Actions would.
#
# Needs AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (an R2 token that can
# write only the dataset bucket) and CLOUDFLARE_ACCOUNT_ID in the
# environment; the unit reads them from /etc/xue-alerts/r2.env.
# DRY_RUN=--dryrun builds and reports without writing to R2; FORCE=--force
# rebuilds a round that is already live (the manual rebuild the spec allows).
set -eu

REPO_URL=${XUE_REPO_URL:-https://github.com/ringsaturn/xue.git}
STATE=${STATE_DIRECTORY:-/var/lib/xue-alerts}
SRC=$STATE/xue
KEEP=${ALERTS_KEEP:-36}
DRY_RUN=${DRY_RUN:-}
FORCE=${FORCE:-}
# awscli v1 from PyPI, run by uv: no system package, and v1 sends none of
# the checksum headers R2 refuses.
AWS="uvx --quiet --from awscli aws"

if [ ! -d "$SRC/.git" ]; then
  git clone --quiet --no-checkout --filter=blob:none "$REPO_URL" "$SRC"
  # Cone mode keeps the root files (pyproject.toml, uv.lock, Makefile,
  # README and the licences the package metadata names).
  git -C "$SRC" sparse-checkout set --cone xuebuild mk scripts
fi
cd "$SRC"
git fetch --quiet origin main
git checkout --quiet --force --detach FETCH_HEAD
uv sync --frozen --quiet

# Every round starts from the live state on R2, never from local leftovers.
rm -rf web/public/data/alerts.* web/public/data/latest-alerts.json
make --no-print-directory live-alerts-index AWS="$AWS"

round=$(date -u +%Y%m%d%H)$(printf '%02d' $(( 5 * ($(date -u +%M | sed 's/^0//') / 5) )))
live=""
if [ -f web/public/data/latest-alerts.json ]; then
  live=$(jq -r .path web/public/data/latest-alerts.json | sed -n 's:^alerts\.\([0-9]\{12\}\)/index\.json$:\1:p')
fi
if [ "$live" = "$round" ] && [ -z "$FORCE" ]; then
  echo "alerts $round is already live; nothing to build"
  exit 0
fi

report=$STATE/alerts-report.json
.venv/bin/python -m xuebuild -v alerts-build --round "$round" $FORCE > "$report"
round=$(jq -r .round "$report")
jq -c '{round, alerts, ended, carried, sources: [.sources[] | "\(.id): \(if .ok then "ok \(.reports // 0)" else .error end)"]}' "$report"

if [ "$(jq -r .pointer "$report")" = "null" ]; then
  echo "no network's state arrived; the previous round stays live"
  exit 0
fi
make --no-print-directory upload-r2-alerts ROUND="$round" DRY_RUN="$DRY_RUN" AWS="$AWS"
make --no-print-directory prune-r2-alerts ALERTS_KEEP="$KEEP" DRY_RUN="$DRY_RUN" AWS="$AWS"
