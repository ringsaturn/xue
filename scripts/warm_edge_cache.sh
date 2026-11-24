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
# A Zarr store (`zarr` descriptor on a bundle or a variant, `series` on a
# full bundle) is a handful of objects: the group and array documents, each
# array's one shard — the whole array, read by range the way a bundle is,
# and warmed whole the way a bundle is — and the coordinate arrays, which a
# viewer never reads. The objects are named off the local group document,
# since a chunk key is deterministic: every variable of every bundle in a
# shared store's `xue_bundles` (`bundles[.<tier>].zarr`,
# docs/zarr-profile.md), or of a one-bundle store's `xue`. A shared store
# is named by every entry of its tier and warmed once. The series store is
# warmed too: a pinned point reads it, and its shard is warmed whole so the
# first pin does not pay the fill from R2. A rolling window's segmented store
# warms the segments this round wrote; the ones it names from an earlier
# round are not on disk and were warmed by that round.
#
# A column store (`columns` on the manifest, docs/zarr-profile.md) is warmed
# whole, though its shards run to ~140 MB: a browser reads a few kilobytes of
# one by range, but the edge answers the first range request of a cold object
# by filling all of it from R2, so every shard a viewer pins into would
# otherwise cost that fill on the first pin of its latitude band. It is named
# off the files on disk, since the store is cut on the machine that warms it.
# Its objects sit under their own ?v= (the group document's CRC-32), so a
# manifest without `columns` warms exactly as before; `--columns` is the
# step that warms it alone (its ?v= exists before the manifest naming it is
# uploaded, so the manifest is warmed afterwards with `--manifest-only`), and the
# whole-run scope takes it too when it is on disk.
#
# Usage: scripts/warm_edge_cache.sh <model> <run> [--artifacts-of <manifest>]
#                                                [--manifest-only]
#                                                [--assembled]
#                                                [--columns]
# Without a flag: reads web/public/data/<model>.<run>/manifest.json and the
# model's pointer file for the manifest's own ?v=, and warms everything —
# the whole-run publish. The scheduled publish builds a run in pieces and
# warms in pieces: `--artifacts-of` warms what one partial manifest
# (manifest.part.<group>.json) names except the shared stores, whose ?v= is
# not known until assembly writes their group documents; `--assembled`
# warms the assembled manifest and the shared stores it names, once both
# are uploaded (only their group documents need be on disk); and
# `--manifest-only` warms the manifest alone. ROUND names one
# round of a rolling window (`build-bin --round`): the run's artifacts and
# manifest then sit in <model>.<run>/<ROUND>/, locally and on the CDN.
# DATA_DIR, DATA_URL, SITE_ORIGIN and WARM_JOBS override the local data
# root, the hostnames and the concurrency.
#
# Each GET is bounded (WARM_MAX_TIME, default 300 s) and retried once: a
# fill that stalls at the edge — seen taking 18 minutes for a 15 MB object
# and then not completing — must not hold a whole publish, and a second
# request usually finds the fill done. Exits non-zero if any fetch still
# failed; the caller decides whether that blocks anything.
set -eu

model=$1
run=$2
scope=all
source=
case ${3:-} in
  "") ;;
  --artifacts-of) scope=artifacts; source=${4:?--artifacts-of needs a manifest file} ;;
  --manifest-only) scope=manifest ;;
  --assembled) scope=assembled ;;
  --columns) scope=columns ;;
  *) echo "unknown option: $3" >&2; exit 2 ;;
esac
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
data=${DATA_DIR:-$root/web/public/data}
dir=$data/$model.$run${ROUND:+/$ROUND}
case $model in
  gfs) pointer=$data/latest.json ;;
  *) pointer=$data/latest-$model.json ;;
esac
[ "$scope" = artifacts ] || [ -f "$pointer" ] || { echo "no pointer at $pointer" >&2; exit 1; }
[ "$scope" = manifest ] || [ -n "$source" ] || source=$dir/manifest.json
[ -z "$source" ] || [ -f "$source" ] || { echo "no manifest at $source" >&2; exit 1; }

if [ "$scope" = columns ]; then
  columns=$(jq -r '.columns.path // empty' "$source")
  [ -n "$columns" ] && [ -f "$dir/$columns/zarr.json" ] || { echo "no column store on disk for $source" >&2; exit 1; }
fi

export WARM_BASE="${DATA_URL:-https://dataset.ringsaturn.me/xue}/$model.$run${ROUND:+/$ROUND}"
export WARM_ORIGIN=${SITE_ORIGIN:-https://xue.ringsaturn.me}
export WARM_MAX_TIME=${WARM_MAX_TIME:-300}
jobs=${WARM_JOBS:-6}

# One `<path>?v=<crc32>` per line, the manifest first: it is the first thing
# a viewer fetches, and the pointer is where its crc32 lives.
# Whether a store path names a shared store: its last component is
# bundles.zarr or bundles.<tier>.zarr, and not bundles.series.zarr.
shared() {
  printf '%s\n' "${1##*/}" | grep -Eq '^bundles(\.[a-z]+)?\.zarr$' && [ "${1##*/}" != bundles.series.zarr ]
}

# The stores the manifest names, one `<path> <crc32>` per line and each once,
# each checked to be on disk before anything is listed off it: a fanned-out
# piece's leave out the shared stores, the assembled step's are only those.
stores=
if [ "$scope" != manifest ] && [ "$scope" != columns ]; then
  stores=$(jq -r '.bundles[] | ., .variants[] | (.zarr // empty), (.series // empty)
    | if .segments then .segments[] else . end
    | "\(.path) \(.crc32)"' "$source" | sort -u | while read -r store crc; do
      case $scope in
        (artifacts) ! shared "$store" || continue ;;
        (assembled) { shared "$store" && [ -f "$dir/$store/zarr.json" ]; } || continue ;;
      esac
      case $store in (../*) [ -f "$dir/$store/zarr.json" ] || continue ;; esac
      printf '%s %s\n' "$store" "$crc"
    done)
  while read -r store crc; do
    [ -z "$store" ] || [ -f "$dir/$store/zarr.json" ] || { echo "no store at $dir/$store/zarr.json" >&2; exit 1; }
  done <<STORES
$stores
STORES
fi

artifacts=$(
  [ "$scope" = artifacts ] || [ "$scope" = columns ] || printf 'manifest.json?v=%s\n' "$(jq -r .manifestCrc32 "$pointer")"
  [ "$scope" = manifest ] || [ "$scope" = assembled ] || [ "$scope" = columns ] || jq -r '.bundles[]
         | ., .variants[], (.poster // empty)
         | select(.path != null)
         | "\(.path)?v=\(.crc32)"' "$source"
  while read -r store crc; do
    [ -n "$store" ] || continue
    printf '%s/zarr.json?v=%s\n' "$store" "$crc"
    jq -r '.attributes | (.xue_bundles // {"": .xue}) | .[] | .variables[].id' "$dir/$store/zarr.json" | while read -r array; do
      printf '%s/%s/zarr.json?v=%s\n%s/%s/c/0/0/0?v=%s\n' "$store" "$array" "$crc" "$store" "$array" "$crc"
    done
  done <<STORES
$stores
STORES
  # Every object of the column store, as it lies on disk. Only a manifest that
  # names `columns` has any; the `columns` scope checked them above, the
  # whole-run scope skips a store this machine does not hold.
  if [ "$scope" = all ] || [ "$scope" = columns ]; then
    jq -r '.columns // empty | "\(.path) \(.crc32)"' "$source" | while read -r store crc; do
      [ -n "$store" ] || continue
      if [ ! -f "$dir/$store/zarr.json" ]; then
        continue
      fi
      (cd "$dir/$store" && find . -type f | sed 's:^\./::' | LC_ALL=C sort) | while read -r object; do
        printf '%s/%s?v=%s\n' "$store" "$object" "$crc"
      done
    done
  fi
)

# Status, bytes, seconds, edge cache status and the artifact, one line each.
# `%header{}` needs curl >= 7.83; a `000` status is a transport failure.
# Two attempts: a second GET of a stalled object is served once the fill
# completes, and the reported line is the attempt that succeeded.
report=$(
  printf '%s\n' "$artifacts" | xargs -P "$jobs" -n 1 sh -c '
    for attempt in 1 2; do
      line=$(curl -sS -o /dev/null -H "Origin: $WARM_ORIGIN" --max-time "$WARM_MAX_TIME" \
        -w "%{http_code} %{size_download} %{time_total} %header{cf-cache-status} $1\n" \
        "$WARM_BASE/$1") || line="000 0 0 - $1"
      case $line in 2*) break ;; esac
    done
    printf "%s\n" "$line"' sh
)

printf '%s\n' "$report" | sort -k5
count=$(printf '%s\n' "$report" | wc -l | tr -d ' ')
bytes=$(printf '%s\n' "$report" | awk '{ s += $2 } END { printf "%.0f", s / 1e6 }')
failed=$(printf '%s\n' "$report" | awk '$1 !~ /^2/' | wc -l | tr -d ' ')
echo "warmed $count artifacts, $bytes MB, via $WARM_BASE"
[ "$failed" -eq 0 ] || { echo "$failed artifacts failed to warm" >&2; exit 1; }
