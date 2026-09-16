#!/bin/sh
# A live observation feed (MRMS, the JMA nowcast): one round of the rolling
# window every ROUND_MINUTES, looped until UNTIL_MINUTE past the hour this
# job started in, so an hourly cron job hands the feed to the next without
# a gap or an overlap (.github/workflows/publish-mrms.yml and
# publish-jma.yml own the schedules; this script owns the rounds).
#
# A round is: the feed's newest frame against the round that is live
# (`make live-window`) → nothing new, nothing written; else `build-bin
# --run latest --hours HOURS --round HHMM` (the whole window fetched —
# frames already on disk are reused, those of the previous run linked
# across — and converted from scratch, so a round's bytes depend on its
# frames alone) → `make upload-r2` with the round (bundles, manifest and
# window record synced into <model>.<run>/<HHMM>/, warmed, then the pointer)
# → the rounds before the newest two of the run deleted, then the runs
# before the newest two. The pointer is written last and only by a round
# that got that far; a round that fails leaves the previous one live.
#
# With FRAME_CACHE=true (the JMA feed, whose frames are decoded from the
# agency's tiles), the decoded-frame cache is pulled from the bucket before
# the first build and pushed back after every upload (`make pull-r2-frames`
# / `push-r2-frames`), so a fresh runner asks the agency only for the
# frames nobody has fetched yet, and the frames older than FRAMES_KEEP_DAYS
# are pruned once an hour.
#
# With HANDOVER_CHECK set to a command (scripts/successor_queued.sh: is a
# newer run of this workflow waiting on the concurrency group?), the loop
# runs it before each round and ends the job normally when it answers yes,
# so the hour's job hands the feed to the next one the moment it arrives
# and the deadline is only the backstop for a cron that never comes.
#
# Three consecutive failed rounds end the job — a broken feed or a broken
# runner is not helped by trying every five minutes for an hour — and the
# next hour's cron starts over.
#
# One log line per round carries what the plan's exit criteria are read
# from: the newest slot, its age when the pointer was written, and each
# step's seconds.
#
# Environment: MODEL (mrms), ROUND_MINUTES (5), UNTIL_MINUTE (55: the last
# round of the hour starts no later than this minute past the hour the job
# started in; past 60 it runs into the next hour, for a workflow whose next
# job cancels this one when it starts), HOURS (4: the
# three-hour window plus the hour in progress; 3 for JMA, whose listing is
# three hours deep), PROFILE (balanced), KEEP (2), FORCE (true builds a
# round even when the live one is current), DRY_RUN (--dryrun previews the
# uploads and prunes), ONCE (true runs one round and exits — a manual
# check), FRAME_CACHE (true syncs the decoded-frame cache with the bucket),
# FRAMES_KEEP_DAYS (7), HANDOVER_CHECK (a command; empty runs to the
# deadline). PYTHON names the interpreter (the Makefile's default is the
# project's .venv).
set -u

model=${MODEL:-mrms}
frame_cache=${FRAME_CACHE:-false}
frames_keep_days=${FRAMES_KEEP_DAYS:-7}
round_minutes=${ROUND_MINUTES:-5}
until_minute=${UNTIL_MINUTE:-55}
hours=${HOURS:-4}
profile=${PROFILE:-balanced}
keep=${KEEP:-2}
force=${FORCE:-false}
dry_run=${DRY_RUN:-}
once=${ONCE:-false}
handover_check=${HANDOVER_CHECK:-}
python=${PYTHON:-.venv/bin/python}
data=web/public/data
raw=data/raw

now=$(date -u +%s)
# The hour this job serves: rounds stop at its UNTIL_MINUTE, whenever the
# job actually started (a late cron shortens the hour, never runs into the
# next one's). A start past the deadline still gets one round.
hour_start=$(( now / 3600 * 3600 ))
deadline=$(( hour_start + until_minute * 60 ))

if [ "$frame_cache" = true ]; then
  # The frames of the window's hours, as far as the bucket has them; a
  # pull that fails only costs tile requests, so it is not a failed round.
  make pull-r2-frames MODEL=$model HOURS="$hours" || echo "::warning::pulling the frame cache failed; frames will be fetched from the source"
fi

failures=0
rounds=0
pruned_frames=false
while :; do
  if [ -n "$handover_check" ] && sh -c "$handover_check"; then
    echo "the next job is waiting; handing the feed over"
    break
  fi
  round=$(date -u +%H%M)
  started=$(date -u +%s)
  ok=true

  # Where the bucket is, against where the live round ends. The live
  # window record is read fresh each round rather than remembered, so the
  # first round of a job knows what the previous job published.
  newest=$($python -c "from xuebuild.fetch import latest_observation_slot; from xuebuild.sources import source_spec; print(latest_observation_slot(source_spec('$model')).strftime('%Y-%m-%dT%H:%M:%SZ'))") || ok=false
  live=$(make -s live-window MODEL=$model 2>/dev/null | jq -r '.latestSlot // empty' 2>/dev/null || true)
  if [ "$ok" = true ] && [ "$newest" = "$live" ] && [ "$force" != true ] && [ -z "$dry_run" ]; then
    echo "round $round: the live window already ends at $newest, nothing to publish"
  elif [ "$ok" = true ]; then
    echo "round $round: bucket at $newest, live at ${live:-nothing}, building"
    build_seconds=0; upload_seconds=0; prune_seconds=0
    t0=$(date -u +%s)
    if $python -m xuebuild -v build-bin --model $model --run latest --hours "$hours" \
        --profile "$profile" --round "$round" --raw-dir $raw --output-dir $data > build-report.json; then
      run=$(jq -r .run build-report.json)
      built=$(jq -r .window.latestSlot build-report.json)
      build_seconds=$(( $(date -u +%s) - t0 ))
      t0=$(date -u +%s)
      if make upload-r2 MODEL=$model RUN="$run" ROUND="$round" DRY_RUN="$dry_run"; then
        upload_seconds=$(( $(date -u +%s) - t0 ))
        pointer_at=$(date -u +%s)
        t0=$pointer_at
        if [ "$frame_cache" = true ]; then
          make push-r2-frames MODEL=$model DRY_RUN="$dry_run" || echo "::warning::pushing the frame cache failed; the next round tries again"
          if [ "$pruned_frames" != true ]; then
            make prune-r2-frames MODEL=$model FRAMES_KEEP_DAYS="$frames_keep_days" DRY_RUN="$dry_run" && pruned_frames=true \
              || echo "::warning::pruning the frame cache failed; the next round tries again"
          fi
        fi
        if [ -z "$dry_run" ]; then
          make prune-r2-rounds MODEL=$model || echo "pruning the run's rounds failed; the next round tries again"
        fi
        make prune-r2 MODEL=$model KEEP="$keep" DRY_RUN="$dry_run" \
          || echo "pruning the superseded runs failed; the next round tries again"
        prune_seconds=$(( $(date -u +%s) - t0 ))
        built_epoch=$($python -c "from datetime import datetime, UTC; print(int(datetime.strptime('$built', '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC).timestamp()))")
        age=$(( pointer_at - built_epoch ))
        echo "round $round: run $run newest $built age ${age}s at pointer, build ${build_seconds}s upload ${upload_seconds}s prune ${prune_seconds}s total $(( $(date -u +%s) - started ))s"
        # The runner keeps only what the next round reuses: this run's raw
        # frames (the previous run's are linked into it on rotation, so
        # they can go once a round of the new run has fetched; the
        # decoded-frame cache, $raw/$model-frames/, is not a run directory
        # and stays), and the round just uploaded.
        for dir in $raw/$model.*/; do
          [ "$dir" = "$raw/$model.$run/" ] || rm -rf "$dir"
        done
        for dir in $data/$model.*/*/; do
          [ "$dir" = "$data/$model.$run/$round/" ] || rm -rf "$dir"
        done
        for dir in $data/$model.*/; do
          [ "$dir" = "$data/$model.$run/" ] || rm -rf "$dir"
        done
      else
        ok=false
      fi
    else
      ok=false
    fi
  fi

  rounds=$(( rounds + 1 ))
  if [ "$ok" = true ]; then
    failures=0
  else
    failures=$(( failures + 1 ))
    echo "::warning::round $round failed ($failures in a row)"
    if [ "$failures" -ge 3 ]; then
      echo "::error::three rounds failed in a row; giving up until the next hour's job"
      exit 1
    fi
  fi

  [ "$once" != true ] || break
  now=$(date -u +%s)
  [ "$now" -lt "$deadline" ] || break
  # Sleep to the next ROUND_MINUTES boundary rather than for a fixed
  # interval, so a slow round does not push the rest of the hour's rounds
  # later and later.
  next=$(( (now / (round_minutes * 60) + 1) * round_minutes * 60 ))
  [ "$next" -le "$deadline" ] || break
  sleep $(( next - now ))
done
echo "$rounds rounds this hour"
