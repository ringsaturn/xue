#!/bin/sh
# Exit 0 when a newer run of this workflow is waiting behind this one on the
# concurrency group, 1 otherwise (including when the question cannot be
# answered: no token, no API, a transient error — the caller then carries
# on, and the deadline still ends the job).
#
# scripts/window_rounds.sh runs this before each round (HANDOVER_CHECK) so
# an hourly job hands the feed to the next one as soon as that one has
# arrived, whenever the cron actually fired, and ends normally: the
# summary, the artifact and the pointer check all still run, and the
# Actions listing reads success for a healthy hour. Needs GH_TOKEN with
# `actions: read` on the repository.
set -u
[ -n "${GITHUB_RUN_ID:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ] && [ -n "${GITHUB_WORKFLOW_REF:-}" ] || exit 1
# GITHUB_WORKFLOW_REF is owner/repo/.github/workflows/<file>@refs/heads/<branch>.
workflow=${GITHUB_WORKFLOW_REF%%@*}
workflow=${workflow##*/}
# A run held by the concurrency group reads "pending"; one waiting for a
# runner reads "queued". Run ids only grow, so a larger id is a newer run.
for status in pending queued waiting; do
  ids=$(gh api "repos/$GITHUB_REPOSITORY/actions/workflows/$workflow/runs?status=$status&per_page=10" \
    --jq '.workflow_runs[].id' 2>/dev/null) || continue
  for id in $ids; do
    if [ "$id" -gt "$GITHUB_RUN_ID" ]; then
      echo "run $id is waiting behind this one"
      exit 0
    fi
  done
done
exit 1
