#!/bin/sh
# Warm Cloudflare's cache for one published run before its pointer goes live:
# GET, through the public dataset hostname, every artifact the viewer will
# ask for — the manifest, each bundle, its half-resolution variant and its
# poster — so the first viewer finds them at the edge instead of paying the
# fill from R2.
#
# Two things the probes that motivated this showed, and the script depends on:
#
#   * The edge keys its cache per `Origin` (the bucket answers with
#     `Vary: Origin`), so a request without the site's Origin header warms a
#     variant no browser will ever hit. Every request here carries it.
#   * A range request makes the edge fetch the whole object, but a client
#     that disconnects after one byte leaves that fill crawling (27 MB took a
#     minute to become servable). Downloading each artifact in full is what
#     makes the fill complete at the speed of the pipe, and with Smart Tiered
#     Cache on the zone that fill lands in the upper tier every other data
#     center pulls from, not only in the one nearest this machine.
#
# The H.264 companions are skipped: they are opt-in (`?use_h264=true`) and
# would double the bytes warmed for viewers that never request them.
#
# Usage: scripts/warm_edge_cache.sh <model> <run>
# Reads web/public/data/<model>.<run>/manifest.json and the model's pointer
# file for the manifest's own ?v=. DATA_DIR, DATA_URL, SITE_ORIGIN and
# WARM_JOBS override the local data root, the hostnames and the concurrency.
# Exits non-zero if any fetch failed; the caller decides whether that blocks
# anything.
set -eu

model=$1
run=$2
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
data=${DATA_DIR:-$root/web/public/data}
dir=$data/$model.$run
case $model in
  gfs) pointer=$data/latest.json ;;
  *) pointer=$data/latest-$model.json ;;
esac
[ -f "$dir/manifest.json" ] || { echo "no manifest at $dir/manifest.json" >&2; exit 1; }
[ -f "$pointer" ] || { echo "no pointer at $pointer" >&2; exit 1; }

export WARM_BASE="${DATA_URL:-https://dataset.ringsaturn.me/xue}/$model.$run"
export WARM_ORIGIN=${SITE_ORIGIN:-https://xue.ringsaturn.me}
jobs=${WARM_JOBS:-6}

# One `<path>?v=<crc32>` per line, the manifest first: it is the first thing
# a viewer fetches, and the pointer is where its crc32 lives.
artifacts=$(
  printf 'manifest.json?v=%s\n' "$(jq -r .manifestCrc32 "$pointer")"
  jq -r '.bundles[]
         | ., .variants[], (.poster // empty)
         | "\(.path)?v=\(.crc32)"' "$dir/manifest.json"
)

# Status, bytes, seconds, edge cache status and the artifact, one line each.
# `%header{}` needs curl >= 7.83; a `000` status is a transport failure.
report=$(
  printf '%s\n' "$artifacts" | xargs -P "$jobs" -n 1 sh -c '
    curl -sS -o /dev/null -H "Origin: $WARM_ORIGIN" \
      -w "%{http_code} %{size_download} %{time_total} %header{cf-cache-status} $1\n" \
      "$WARM_BASE/$1" || printf "000 0 0 - %s\n" "$1"' sh
)

printf '%s\n' "$report" | sort -k5
count=$(printf '%s\n' "$report" | wc -l | tr -d ' ')
bytes=$(printf '%s\n' "$report" | awk '{ s += $2 } END { printf "%.0f", s / 1e6 }')
failed=$(printf '%s\n' "$report" | awk '$1 !~ /^2/' | wc -l | tr -d ' ')
echo "warmed $count artifacts, $bytes MB, via $WARM_BASE"
[ "$failed" -eq 0 ] || { echo "$failed artifacts failed to warm" >&2; exit 1; }
