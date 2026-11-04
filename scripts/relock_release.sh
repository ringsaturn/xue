#!/usr/bin/env bash
# The relock commit of a release (docs/contribution/releasing.md, "The flow",
# step 4), made on the branch that holds the tag:
#
#   scripts/relock_release.sh v0.45.0            # commit and push
#   DRY_RUN=1 scripts/relock_release.sh v0.45.0  # everything but the push
#
# Run it from a checkout whose remote branches and tags are fetched. It finds
# the branch holding the tag (`release/*`, else `main` once that branch is
# gone), waits until the own index lists the version's wheels, raises the
# floor in pyproject.toml to the version's minor series, relocks xuepy, checks
# that the lock holds exactly that version, and commits and pushes. A lock
# that already holds the version is left alone (exit 0, no commit).
#
# Knobs: REMOTE (origin), XUEPY_INDEX (the Pages index), ATTEMPTS × SLEEP
# seconds for the index poll (40 × 30). With GITHUB_OUTPUT set it writes
# `branch=` and `relocked=` for the workflow.
set -euo pipefail

tag="${1:?usage: relock_release.sh vX.Y.Z}"
[[ "$tag" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || { echo "not a release tag: ${tag}" >&2; exit 1; }
version="${tag#v}"
major="${BASH_REMATCH[1]}"
minor="${BASH_REMATCH[2]}"
floor="xuepy>=${major}.${minor},<${major}.$((minor + 1))"
remote="${REMOTE:-origin}"
index="${XUEPY_INDEX:-https://ringsaturn.github.io/xue/simple}"
output="${GITHUB_OUTPUT:-/dev/null}"

git rev-parse -q --verify "refs/tags/${tag}^{commit}" >/dev/null || { echo "tag ${tag} is not fetched" >&2; exit 1; }

# The release branch that holds the tag; main once the branch is gone.
branch=""
while IFS= read -r name; do
  case "$name" in
    "${remote}"/release/*)
      [ -z "$branch" ] || { echo "${tag} is on several release branches: ${branch}, ${name#"${remote}"/}" >&2; exit 1; }
      branch="${name#"${remote}"/}"
      ;;
  esac
done < <(git branch -r --contains "refs/tags/${tag}" --format='%(refname:short)')
if [ -z "$branch" ] && git merge-base --is-ancestor "refs/tags/${tag}" "${remote}/main"; then
  branch=main
fi
[ -n "$branch" ] || { echo "no release/* branch or main holds ${tag}" >&2; exit 1; }
echo "relocking ${branch} on xuepy ${version} (floor ${floor})"
echo "branch=${branch}" >>"$output"
git checkout -q -B "$branch" "${remote}/${branch}"

# The index is rendered by a Pages deploy that the CSV job only dispatches.
found=""
for attempt in $(seq 1 "${ATTEMPTS:-40}"); do
  # Read the page whole: `curl | grep -q` lets grep close the pipe on the
  # first match, which curl reports as a failure under pipefail.
  page="$(curl -fsS "${index}/xuepy/" || true)"
  if grep -q "xuepy-${version}-" <<<"$page"; then
    found=1
    break
  fi
  echo "${index}/xuepy/ does not list ${version} yet (attempt ${attempt})"
  sleep "${SLEEP:-30}"
done
[ -n "$found" ] || { echo "the index never listed xuepy ${version}" >&2; exit 1; }

python3 - "$floor" <<'PY'
import pathlib, re, sys
floor = sys.argv[1]
path = pathlib.Path("pyproject.toml")
text, count = re.subn(r'"xuepy>=[^"]*"', f'"{floor}"', path.read_text())
if count != 1:
    sys.exit(f"pyproject.toml names the xuepy floor {count} times, expected once")
path.write_text(text)
PY
uv lock --refresh-package xuepy

locked="$(awk '/^name = "xuepy"$/ { found = 1 } found && /^version = / { gsub(/"/, "", $3); print $3; exit }' uv.lock)"
[ "$locked" = "$version" ] || { echo "uv.lock holds xuepy ${locked}, not ${version}" >&2; exit 1; }

if git diff --quiet -- pyproject.toml uv.lock; then
  echo "already relocked on xuepy ${version}"
  echo "relocked=0" >>"$output"
  exit 0
fi
git add pyproject.toml uv.lock
git -c commit.gpgsign=false commit -q -m "chore: relock on the published xuepy ${version}"
git --no-pager show --stat --oneline HEAD
if [ "${DRY_RUN:-}" = 1 ]; then
  echo "dry run: not pushing to ${remote}/${branch}"
  echo "relocked=0" >>"$output"
  exit 0
fi
git push "$remote" "HEAD:refs/heads/${branch}"
echo "relocked=1" >>"$output"
