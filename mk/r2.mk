# Runs on the R2 dataset bucket: upload, take live, prune, read back.
# Needs an R2 token's key pair in AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
# and CLOUDFLARE_ACCOUNT_ID (the endpoint host). Any other S3-compatible bucket
# works the same with R2_ENDPOINT, R2_BUCKET and R2_PREFIX from the environment.

# Published runs of one model to keep (one: the live run, no history).
KEEP ?= 1
# `--dryrun` to preview an upload or a prune.
DRY_RUN ?=
# WARM=false skips the edge-cache warm-up between the assets and the pointer
# (a bucket with no CDN in front).
WARM ?= true
LATEST_FILE = $(if $(filter gfs,$(MODEL)),latest.json,latest-$(MODEL).json)
STAC_CATALOG = catalog.json
STAC_COLLECTION = collection.json
STAC_ITEM = item.json
STAC_INDEX = index.html
# The catalog directory `upload-r2-stac-collection` pushes: a model, `showcase`
# or a point product.
STAC_DIR ?= $(MODEL)

R2_BUCKET ?= dataset
R2_PREFIX ?= xue
R2_ENDPOINT ?= https://$(CLOUDFLARE_ACCOUNT_ID).r2.cloudflarestorage.com
R2_ROOT = s3://$(R2_BUCKET)/$(R2_PREFIX)
AWS ?= aws
S3 = $(AWS) s3 --endpoint-url $(R2_ENDPOINT)
# R2 has no regions and refuses AWS CLI v2's default checksum headers.
AWS_DEFAULT_REGION ?= auto
AWS_REQUEST_CHECKSUM_CALCULATION ?= when_required
AWS_RESPONSE_CHECKSUM_VALIDATION ?= when_required
# The multipart threshold lives only in the CLI's config file. Raising it above
# every artifact makes a store shard one PutObject instead of seven Class A
# operations (scripts/aws-config has the measurement).
AWS_CONFIG_FILE ?= $(CURDIR)/scripts/aws-config
export AWS_DEFAULT_REGION AWS_REQUEST_CHECKSUM_CALCULATION AWS_RESPONSE_CHECKSUM_VALIDATION AWS_CONFIG_FILE

# Prints the CRC32 of the file it is given, as the pointers carry it.
CRC32 = $(PYTHON) -c "import sys, zlib; print(f'{zlib.crc32(open(sys.argv[1], \"rb\").read()) & 0xFFFFFFFF:08x}')"
MD5 = $(PYTHON) -c "import sys, hashlib; print(hashlib.md5(open(sys.argv[1], 'rb').read()).hexdigest())"

# $(call put_if_changed,local file,key under R2_ROOT,content type): a no-cache
# document every round rewrites with the same bytes (the root catalog and its
# landing page) is put only when R2 holds other bytes. A PutObject is Class A;
# the HEAD is Class B. A single-part object's ETag is its MD5, and every object
# is single-part (scripts/aws-config). A failed HEAD reads as changed.
put_if_changed = remote=$$($(AWS) s3api head-object --endpoint-url $(R2_ENDPOINT) --bucket $(R2_BUCKET) \
		--key $(R2_PREFIX)/$(2) --query ETag --output text 2>/dev/null || true); \
	if [ "$$remote" = "\"$$($(MD5) $(1))\"" ]; then echo "$(2) unchanged on R2, not uploaded"; \
	else $(S3) cp $(1) $(R2_ROOT)/$(2) --no-progress $(DRY_RUN) --content-type "$(3)" --cache-control "no-cache"; fi

.PHONY: upload-r2 upload-r2-bundles upload-r2-manifest upload-r2-stac-item check-pointer upload-r2-pointer upload-r2-stac-collection warm-r2 prune-r2 prune-r2-rounds prune-r2-segments live-run live-pointer live-manifest live-store-groups live-window publish-columns cut-columns check-live-run

# A shared store's group document (docs/zarr-profile.md): named by every
# entry of its tier and rewritten by a top-up under a new ?v=, so it goes up
# with the manifest, never with a fanned-out job's arrays.
SHARED_GROUPS = bundles*.zarr/zarr.json
# A partial group document, merged by assemble-run; never uploaded.
STORE_PARTS = *zarr.part.*.json

# Run assets are immutable (?v=<crc32>); the pointer goes last and is what takes
# the run live. `cp`, not `sync`: a fresh directory has nothing to skip, so a
# sync only spends a listing. A failed warm-up never holds the pointer back.
upload-r2: ## Upload one built run (RUN, ROUND), warm the edge, take it live
	@set -e; dir=web/public/data/$(RUN_DIR); \
	[ -d "$$dir" ] || { echo "no built run at $$dir, pass RUN=YYYYMMDDHH"; exit 1; }; \
	$(MAKE) --no-print-directory check-pointer MODEL=$(MODEL) RUN=$(RUN) ROUND=$(ROUND); \
	segments=web/public/data/$(MODEL).segments; \
	if [ -n "$(ROUND)" ] && [ -d "$$segments" ]; then \
		$(S3) cp $$segments $(R2_ROOT)/$(MODEL).segments/ --recursive --no-progress $(DRY_RUN) \
			--exclude "$(STORE_PARTS)" --cache-control "public, max-age=31536000, immutable"; \
	fi; \
	$(S3) cp $$dir $(R2_ROOT)/$(RUN_DIR)/ --recursive --no-progress $(DRY_RUN) \
		--exclude "manifest.part.*.json" --exclude "$(STORE_PARTS)" --exclude "$(STAC_ITEM)" \
		--cache-control "public, max-age=31536000, immutable"; \
	$(MAKE) --no-print-directory upload-r2-stac-item MODEL=$(MODEL) RUN=$(RUN) ROUND=$(ROUND) DRY_RUN=$(DRY_RUN); \
	[ -n "$(DRY_RUN)" ] || [ "$(WARM)" != true ] || $(MAKE) --no-print-directory warm-r2 MODEL=$(MODEL) RUN=$(RUN) ROUND=$(ROUND) \
		|| echo "warming the edge cache failed; the run goes live cold"; \
	$(MAKE) --no-print-directory upload-r2-pointer MODEL=$(MODEL) RUN=$(RUN) DRY_RUN=$(DRY_RUN)

# One piece of a fanned-out publish (no manifest; parts stay local). Its
# shared stores' arrays are warmed by upload-r2-manifest once their group
# documents, and so their ?v=, exist. `cp`, not
# `sync`: concurrent pieces each listing the prefix drew ServiceUnavailable
# from R2. Three attempts, since one refused object should not fail a publish.
upload-r2-bundles: ## Upload this machine's bundle groups of a fanned-out run
	@set -e; dir=web/public/data/$(MODEL).$(RUN); \
	[ -d "$$dir" ] || { echo "no built bundles at $$dir, pass RUN=YYYYMMDDHH"; exit 1; }; \
	ls $$dir/manifest.part.*.json > /dev/null 2>&1 || { echo "no manifest.part.*.json in $$dir: build with --bundles"; exit 1; }; \
	for attempt in 1 2 3; do \
		$(S3) cp $$dir $(R2_ROOT)/$(MODEL).$(RUN)/ --recursive --no-progress $(DRY_RUN) \
			--exclude "manifest.json" --exclude "manifest.part.*.json" \
			--exclude "$(STORE_PARTS)" --exclude "$(SHARED_GROUPS)" \
			--cache-control "public, max-age=31536000, immutable" && break; \
		[ "$$attempt" -lt 3 ] || { echo "uploading the bundles failed three times"; exit 1; }; \
		echo "upload attempt $$attempt failed; retrying in $$((attempt * 20)) s"; sleep $$((attempt * 20)); \
	done; \
	[ -n "$(DRY_RUN)" ] || [ "$(WARM)" != true ] || for part in $$dir/manifest.part.*.json; do \
		scripts/warm_edge_cache.sh $(MODEL) $(RUN) --artifacts-of "$$part" \
			|| echo "warming the edge cache for $$part failed; those artifacts go live cold"; \
	done

upload-r2-manifest: ## Upload an assembled manifest, then take the run live
	@set -e; dir=web/public/data/$(MODEL).$(RUN); \
	[ -f "$$dir/manifest.json" ] || { echo "no manifest at $$dir/manifest.json: run assemble-run first"; exit 1; }; \
	$(MAKE) --no-print-directory check-pointer MODEL=$(MODEL) RUN=$(RUN); \
	$(S3) cp $$dir $(R2_ROOT)/$(MODEL).$(RUN)/ --recursive --no-progress $(DRY_RUN) \
		--exclude "*" --include "$(SHARED_GROUPS)" \
		--content-type application/json --cache-control "public, max-age=31536000, immutable"; \
	$(S3) cp $$dir/manifest.json $(R2_ROOT)/$(MODEL).$(RUN)/manifest.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "public, max-age=31536000, immutable"; \
	$(MAKE) --no-print-directory upload-r2-stac-item MODEL=$(MODEL) RUN=$(RUN) DRY_RUN=$(DRY_RUN); \
	[ -n "$(DRY_RUN)" ] || [ "$(WARM)" != true ] || scripts/warm_edge_cache.sh $(MODEL) $(RUN) --assembled \
		|| echo "warming the manifest and the shared stores failed; they go live cold"; \
	$(MAKE) --no-print-directory upload-r2-pointer MODEL=$(MODEL) RUN=$(RUN) DRY_RUN=$(DRY_RUN)

# Not ?v=-addressed (a top-up rewrites it in place), so served no-cache.
upload-r2-stac-item:
	@set -e; dir=web/public/data/$(RUN_DIR); \
	[ -f "$$dir/$(STAC_ITEM)" ] || { echo "no $(STAC_ITEM) in $$dir (a rolling round, or a run that predates the catalog)"; exit 0; }; \
	$(S3) cp $$dir/$(STAC_ITEM) $(R2_ROOT)/$(RUN_DIR)/$(STAC_ITEM) --no-progress $(DRY_RUN) \
		--content-type application/geo+json --cache-control "no-cache"

# A pointer that disagrees with its manifest strands every viewer on a 404.
check-pointer:
	@set -e; dir=web/public/data/$(RUN_DIR); \
	pointer_run=$$(jq -r .run web/public/data/$(LATEST_FILE)); \
	[ "$$pointer_run" = "$(RUN)" ] || { \
		echo "$(LATEST_FILE) names run $$pointer_run, not $(RUN) — a later build rewrote it;"; \
		echo "rebuild run $(RUN) (or upload run $$pointer_run) so the pointer matches the assets"; \
		exit 1; }; \
	pointer_path=$$(jq -r .manifestPath web/public/data/$(LATEST_FILE)); \
	[ "$$pointer_path" = "$(RUN_DIR)/manifest.json" ] || { \
		echo "$(LATEST_FILE) names $$pointer_path, not $(RUN_DIR)/manifest.json — pass the ROUND it was built with"; \
		exit 1; }; \
	pointer_crc=$$(jq -r .manifestCrc32 web/public/data/$(LATEST_FILE)); \
	manifest_crc=$$($(CRC32) $$dir/manifest.json); \
	[ "$$pointer_crc" = "$$manifest_crc" ] || { \
		echo "$(LATEST_FILE) carries manifest CRC32 $$pointer_crc but $$dir/manifest.json is $$manifest_crc;"; \
		echo "rebuild or reassemble run $(RUN) so the pointer matches the manifest"; \
		exit 1; }

upload-r2-pointer: ## Upload the live pointer and its STAC Collection
	@echo "Uploading $(LATEST_FILE) (takes $(MODEL) run $(RUN) live)..."; \
	$(S3) cp web/public/data/$(LATEST_FILE) $(R2_ROOT)/$(LATEST_FILE) --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache" \
	&& $(MAKE) --no-print-directory upload-r2-stac-collection MODEL=$(MODEL) DRY_RUN=$(DRY_RUN)

# Mutable like the pointer, so no-cache. The Item goes first: a Collection
# must never name a live Item that is not there yet.
upload-r2-stac-collection: ## Upload STAC_DIR's live Item, Collection and the root catalog
	@set -e; \
	[ -f web/public/data/$(STAC_DIR)/$(STAC_COLLECTION) ] || { echo "no $(STAC_DIR)/$(STAC_COLLECTION); nothing built the catalog"; exit 0; }; \
	echo "Uploading $(STAC_DIR)/$(STAC_ITEM), $(STAC_DIR)/$(STAC_COLLECTION), $(STAC_CATALOG) and $(STAC_INDEX)..."; \
	[ ! -f web/public/data/$(STAC_DIR)/$(STAC_ITEM) ] || \
	$(S3) cp web/public/data/$(STAC_DIR)/$(STAC_ITEM) $(R2_ROOT)/$(STAC_DIR)/$(STAC_ITEM) --no-progress $(DRY_RUN) \
		--content-type application/geo+json --cache-control "no-cache"; \
	$(S3) cp web/public/data/$(STAC_DIR)/$(STAC_COLLECTION) $(R2_ROOT)/$(STAC_DIR)/$(STAC_COLLECTION) --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"; \
	$(call put_if_changed,web/public/data/$(STAC_CATALOG),$(STAC_CATALOG),application/json); \
	$(call put_if_changed,web/public/data/$(STAC_INDEX),$(STAC_INDEX),text/html; charset=utf-8)

warm-r2: ## GET every artifact of an uploaded run through the CDN
	ROUND=$(ROUND) scripts/warm_edge_cache.sh $(MODEL) $(RUN)

prune-r2: ## Delete a model's runs beyond the newest KEEP, never the live one
	@set -e; \
	live=$$($(MAKE) -s --no-print-directory live-run MODEL=$(MODEL)); \
	if [ -z "$$live" ] && [ -n "$(DRY_RUN)" ]; then \
		echo "no live pointer for $(MODEL); a dry run of a source's first publish has nothing to prune"; exit 0; \
	fi; \
	[ -n "$$live" ] || { echo "no live pointer for $(MODEL), refusing to prune"; exit 1; }; \
	echo "live $(MODEL) run: $$live"; \
	listing=$$($(S3) ls $(R2_ROOT)/) \
		|| { echo "listing the bucket failed, refusing to prune"; exit 1; }; \
	for run in $$(printf '%s\n' "$$listing" | awk '/ PRE /{print $$2}' \
		| sed 's:/$$::' | grep "^$(MODEL)\." | grep -v "^$(MODEL)\.segments$$" | sort -r | tail -n +$$(($(KEEP) + 1))); do \
		if [ "$$run" != "$(MODEL).$$live" ]; then \
			echo "Deleting $$run..."; \
			$(S3) rm $(R2_ROOT)/$$run/ --recursive --only-show-errors $(DRY_RUN); \
		fi; \
	done

# Keeps the newest ROUNDS_KEEP rounds of the live run and the one the pointer
# names. Rounds are named by build minute (HHMM), so a run straddling midnight
# sorts wrong lexically; `round_order` folds a round more than twelve hours
# before the newest into the next day. Only the live run directory is listed,
# once and recursively: the keys of the rounds to go come out of that one
# listing and are deleted in one DeleteObjects call (free on R2), where `rm
# --recursive` would list each round again (a Class A operation every
# round). A refused batch falls back to `rm --recursive`.
ROUNDS_KEEP ?= 2
define round_order
from sys import argv
rounds = argv[1].split()
newest = max(rounds)
def key(r): return int(r) + (2400 if int(newest) - int(r) > 1200 else 0)
print(' '.join(sorted(rounds, key=key, reverse=True)))
endef
export round_order
prune-r2-rounds: ## Delete a rolling window's old rounds, in clock order
	@set -e; \
	pointer=$$($(S3) cp $(R2_ROOT)/$(LATEST_FILE) - --only-show-errors 2>/dev/null || true); \
	[ -n "$$pointer" ] || { echo "no live pointer for $(MODEL), refusing to prune"; exit 1; }; \
	live=$$(printf '%s' "$$pointer" | jq -r .manifestPath | xargs dirname); \
	dir=$$(printf '%s' "$$live" | xargs dirname); \
	echo "live $(MODEL) round: $$live"; \
	base="$(if $(R2_PREFIX),$(R2_PREFIX)/)$$dir/"; \
	batch=$$(mktemp); trap 'rm -f "$$batch"' EXIT; \
	keys=$$($(S3) ls $(R2_ROOT)/$$dir/ --recursive | awk '{print $$4}') \
		|| { echo "listing $$dir failed, refusing to prune"; exit 1; }; \
	rounds=$$(printf '%s\n' "$$keys" | awk -v base="$$base" 'index($$0, base) == 1 { print substr($$0, length(base) + 1) }' \
		| cut -d/ -f1 | grep -E '^[0-9]{4}$$' | sort -u || true); \
	[ -n "$$rounds" ] || exit 0; \
	for round in $$($(PYTHON) -c "$$round_order" "$$rounds" | tr ' ' '\n' | tail -n +$$(( $(ROUNDS_KEEP) + 1 ))); do \
		if [ "$$dir/$$round" != "$$live" ]; then \
			doomed=$$(printf '%s\n' "$$keys" | grep -F "$$base$$round/" || true); \
			count=$$(printf '%s\n' "$$doomed" | grep -c . || true); \
			echo "Deleting $$dir/$$round ($$count objects)..."; \
			if [ -n "$(DRY_RUN)" ]; then \
				printf '(dryrun) delete: s3://$(R2_BUCKET)/%s\n' $$doomed; \
			elif [ "$$count" -gt 0 ] && [ "$$count" -le 1000 ] \
				&& printf '%s\n' "$$doomed" | jq -R -s '{Objects: (split("\n") | map(select(length > 0)) | map({Key: .})), Quiet: true}' > "$$batch" \
				&& answer=$$($(AWS) s3api delete-objects --endpoint-url $(R2_ENDPOINT) --bucket $(R2_BUCKET) \
					--delete "file://$$batch" --output json) \
				&& [ "$$(printf '%s' "$$answer" | jq -s '(.[0].Errors // []) | length')" = 0 ]; then \
				:; \
			else \
				echo "the batch delete of $$dir/$$round was refused; removing it with a listing"; \
				$(S3) rm $(R2_ROOT)/$$dir/$$round/ --recursive --only-show-errors; \
			fi; \
		fi; \
	done

# A rolling window's segments (docs/zarr-profile.md, "Segmented stores") are
# shared by its rounds, so they go when no round left on the bucket names them:
# every run directory's rounds are read, and every segment directory none of
# their manifests names is deleted. Runs after prune-r2-rounds and prune-r2.
# The segment directories, <model>.segments/<start>/<digest>, come out of one
# recursive listing of <model>.segments/ (a page per thousand objects) rather
# than a listing per <start>: the bucket root, each run directory and that
# listing are all the ListObjects a round spends here.
define segments_stale
import json, posixpath, sys
named = set()
for argument in sys.argv[1:]:
    round_path, manifest = argument.split("=", 1)
    for bundle in json.load(open(manifest))["bundles"]:
        for entry in (bundle, *bundle.get("variants", [])):
            for segment in (entry.get("zarr") or {}).get("segments", []):
                named.add(posixpath.dirname(posixpath.normpath(posixpath.join(round_path, segment["path"]))))
for line in sys.stdin.read().split():
    if line not in named:
        print(line)
endef
export segments_stale
prune-r2-segments: ## Delete a rolling window's segments that no remaining round names
	@set -e; \
	tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	listing=$$($(S3) ls $(R2_ROOT)/) || { echo "listing the bucket failed, refusing to prune"; exit 1; }; \
	printf '%s\n' "$$listing" | awk '/ PRE /{print $$2}' | sed 's:/$$::' | grep -q "^$(MODEL)\.segments$$" || exit 0; \
	manifests=""; \
	for run in $$(printf '%s\n' "$$listing" | awk '/ PRE /{print $$2}' | sed 's:/$$::' | grep -E "^$(MODEL)\.[0-9]{10}$$" || true); do \
		rounds=$$($(S3) ls $(R2_ROOT)/$$run/) || { echo "listing $$run failed, refusing to prune"; exit 1; }; \
		for round in $$(printf '%s\n' "$$rounds" | awk '/ PRE /{print $$2}' | sed 's:/$$::' | grep -E '^[0-9]{4}$$' || true); do \
			file=$$tmp/$$run.$$round.json; \
			$(S3) cp $(R2_ROOT)/$$run/$$round/manifest.json $$file --only-show-errors 2>/dev/null || continue; \
			manifests="$$manifests $$run/$$round=$$file"; \
		done; \
	done; \
	[ -n "$$manifests" ] || { echo "no round of $(MODEL) left to read, refusing to prune its segments"; exit 1; }; \
	objects=$$($(S3) ls $(R2_ROOT)/$(MODEL).segments/ --recursive) || { echo "listing the segments failed, refusing to prune"; exit 1; }; \
	held=$$(printf '%s\n' "$$objects" | awk '{print $$4}' \
		| sed -n -E 's:^$(if $(R2_PREFIX),$(R2_PREFIX)/)($(MODEL)\.segments/[^/]+/[^/]+)/.+$$:\1:p' | sort -u); \
	for stale in $$(printf '%s\n' $$held | $(PYTHON) -c "$$segments_stale" $$manifests); do \
		echo "Deleting $$stale..."; \
		$(S3) rm $(R2_ROOT)/$$stale/ --recursive --only-show-errors $(DRY_RUN); \
	done

live-run: ## Print the run the live pointer names
	@$(S3) cp $(R2_ROOT)/$(LATEST_FILE) - --only-show-errors | jq -r .run || true

live-pointer: ## Print the live pointer
	@$(S3) cp $(R2_ROOT)/$(LATEST_FILE) - --only-show-errors 2>/dev/null || true

# The base a top-up merges onto (`assemble-run --base-manifest`).
live-manifest: ## Print the live run's manifest
	@set -e; path=$$($(S3) cp $(R2_ROOT)/$(LATEST_FILE) - --only-show-errors 2>/dev/null | jq -r .manifestPath || true); \
	[ -n "$$path" ] || exit 0; \
	$(S3) cp $(R2_ROOT)/$$path - --only-show-errors

# The live run's shared group documents, the base a top-up merges its parts
# onto (`assemble-run --base-stores`): every shared store LIVE_MANIFEST (the
# file `make live-manifest` printed) names, fetched to OUT/<store>/zarr.json.
# A live run published before shared stores names none, and OUT stays empty.
live-store-groups: ## Fetch the shared group documents LIVE_MANIFEST names into OUT
	@set -e; [ -n "$(OUT)" ] && [ -f "$(LIVE_MANIFEST)" ] || { echo "pass LIVE_MANIFEST=<file> OUT=<directory>"; exit 1; }; \
	mkdir -p $(OUT); \
	for store in $$(jq -r '.bundles[] | ., (.variants // [])[] | .zarr.path // empty' $(LIVE_MANIFEST) \
			| grep -E '(^|/)bundles(\.[a-z]+)?\.zarr$$' | grep -v 'bundles\.series\.zarr$$' | sort -u); do \
		mkdir -p $(OUT)/$$store; \
		$(S3) cp $(R2_ROOT)/$(MODEL).$(RUN)/$$store/zarr.json $(OUT)/$$store/zarr.json --only-show-errors; \
	done

live-window: ## Print the live rolling window's window.json
	@set -e; path=$$($(S3) cp $(R2_ROOT)/$(LATEST_FILE) - --only-show-errors 2>/dev/null | jq -r .manifestPath || true); \
	[ -n "$$path" ] || exit 0; \
	$(S3) cp $(R2_ROOT)/$$(dirname $$path)/window.json - --only-show-errors 2>/dev/null || true

# The column store (docs/zarr-profile.md, "Column store") has two paths.
#
# `cut-columns` is the publish's: the columns job, between build and finalize,
# cuts the store of a run that is not live yet and hands finalize a descriptor;
# finalize names it in the one manifest it writes (`assemble-run --columns`), so
# the run goes live once, with its store, and nothing here writes a manifest,
# an Item or the pointer.
#
# `publish-columns` is the exception, for a run that is already live without
# its store (a publish whose cut failed or overran, found by the next scheduled
# publish): cut after the fact from the map stores' codes already on the
# bucket, then attached with a second manifest and a second pointer flip. Everything here assumes it may have
# been overtaken: a new run, or a prune, between the job's start and any step
# means there is nothing to attach, which is a clean exit (a notice), not a
# failure. A pointer that cannot be READ is a failure, though: it must never pass
# for one that moved.
#
# Inputs are the run-relative objects `column-inputs` lists (each map store's
# group document and each member's shard), fetched into the run directory at
# those same paths with one bounded-parallel GET each (COLUMNS_JOBS at once,
# three attempts); nothing else of the run is read. `build-columns` checks each
# group document against the manifest's CRC32 and the local pointer against the
# run, so the manifest and the pointer are fetched the same way first. It cuts
# into the hidden `.columns.zarr` and renames onto `columns.zarr`; only
# `columns.zarr` is ever uploaded (never the run directory as a whole).
#
# Order of writes: store, its edge warm-up (its ?v= is the group document's
# CRC32, independent of the manifest), then the manifest and Item, the
# manifest's warm-up, the pointer. The live pointer must still name this run,
# manifest path and manifest CRC32 the job started from before the manifest
# goes up and again before the pointer does: the warm-ups between take minutes,
# and a pointer written over a newer run would roll viewers back to a pruned
# one. The manifest is rewritten at its own key before the pointer flips, as a
# top-up does: viewers read it under the pointer's ?v=, so the bytes under the
# old ?v= change at the origin only in the window before the flip. The store's
# own objects sit under their own ?v= (the group document's CRC32, which
# carries every shard's CRC32), so a changed cut is a new ?v=, never new bytes
# under an old one.
#
# A job that dies between the manifest and the pointer leaves a live manifest
# that names `columns` under a pointer carrying the CRC32 of the manifest
# before. The next run finds exactly that (the manifest minus `columns`,
# serialized as the writer does, hashes to the pointer's CRC32) and only
# re-flips: the Item and the pointer. A manifest that differs from its pointer's
# in anything else is an error.
COLUMNS_JOBS ?= 8
# A warm-up never costs the job more than this: the store is already on R2 and a cold edge is only slower.
COLUMNS_WARM_TIMEOUT ?= timeout 10m
# Shell pieces both paths share, expanded into their recipes: `retry` runs a
# command up to three times like upload-r2-bundles; `upload_store` puts the
# store's shards, then its documents (each object is new bytes under its own
# ?v=, never a rewrite); `fetch_inputs LIST` downloads each run-relative object
# of LIST into the run directory at the same path.
define COLUMNS_SHELL
retry() { n=1; while :; do "$$@" && return 0; [ "$$n" -lt 3 ] || return 1; \
	echo "attempt $$n failed; retrying in $$((n * 20)) s" >&2; sleep $$((n * 20)); n=$$((n + 1)); done; }; \
upload_store() { \
	$(S3) cp "$$dir/columns.zarr" $(R2_ROOT)/$(MODEL).$(RUN)/columns.zarr/ --recursive --no-progress $(DRY_RUN) \
		--exclude "*zarr.json" --content-type application/octet-stream \
		--cache-control "public, max-age=31536000, immutable" \
	&& $(S3) cp "$$dir/columns.zarr" $(R2_ROOT)/$(MODEL).$(RUN)/columns.zarr/ --recursive --no-progress $(DRY_RUN) \
		--exclude "*" --include "*zarr.json" --content-type application/json \
		--cache-control "public, max-age=31536000, immutable"; }; \
fetch_inputs() { xargs -P $(COLUMNS_JOBS) -n 1 sh -c 'for attempt in 1 2 3; do \
		mkdir -p "$$(dirname "$$0/$$1")" && $(S3) cp "$(R2_ROOT)/$(MODEL).$(RUN)/$$1" "$$0/$$1" --only-show-errors && exit 0; \
		sleep $$((attempt * 10)); done; exit 1' "$$dir" < "$$1"; };
endef

# What the columns job does before finalize, on the machine that just ran
# `assemble-run` locally over the parts (the manifest, the shared stores' group
# documents and the pointer are on disk, none of them uploaded). Only for a run
# R2 does not serve yet: a top-up or forced rebuild of the live run goes live
# without cutting (finalize keeps its base's store when no member was rebuilt,
# the attach path adds one on the next scheduled publish), so no cut ever changes
# what a live run's ?v= names. First `check-live-run`: a re-run of a red job
# must not cut for a run R2 has moved past. Then stop with no descriptor when the
# assembled manifest already names the store; otherwise fetch only the SHARDS
# `column-inputs` lists, never a group document, since the local ones are the
# ones the new manifest will name (R2's may be absent), cut and check the store,
# upload it, and write `columns.json` last: the run it was cut for and the CRC32
# of every shared store's group document it was cut against beside the store's
# own entry, which `assemble-run --columns` verifies against what finalize
# assembles. A job that fails or is cancelled leaves no descriptor and finalize
# takes the run live without a store. The edge warm-up is a step of its own in
# the workflow, bounded separately, so a slow one never loses a complete store.
COLUMNS_DESCRIPTOR = {run: $$run, stores: ([.bundles[] | (., (.variants // [])[]) | .zarr // empty | select(.path != null) \
	| select(.path | test("(^|/)bundles(\\.[a-z]+)?\\.zarr$$")) | select(.path | test("(^|/)bundles\\.series\\.zarr$$") | not) \
	| {(.path): .crc32}] | add // {}), columns: .columns}
cut-columns: ## Cut the assembled run's column store, upload it, write columns.json
	@set -eu; \
	dir=web/public/data/$(MODEL).$(RUN); \
	$(COLUMNS_SHELL) \
	rm -f columns.json columns.json.new; \
	$(MAKE) --no-print-directory check-live-run MODEL=$(MODEL) RUN=$(RUN) LIVE_SEEN="$(LIVE_SEEN)"; \
	missing=$$($(PYTHON) -m xuebuild columns-missing --model $(MODEL) --base-manifest "$$dir/manifest.json"); \
	[ "$$missing" = true ] || { \
		echo "::notice::$(MODEL) run $(RUN): the assembled manifest already names its column store; nothing to cut"; exit 0; }; \
	$(PYTHON) -m xuebuild column-inputs --manifest "$$dir/manifest.json" > column-inputs.all; \
	grep -v '/zarr\.json$$' column-inputs.all > column-inputs.txt; \
	echo "fetching $$(wc -l < column-inputs.txt | tr -d ' ') shards of $(MODEL).$(RUN)"; \
	fetch_inputs column-inputs.txt || { echo "fetching the column inputs failed"; exit 1; }; \
	rm -f column-inputs.txt column-inputs.all; \
	$(PYTHON) -m xuebuild -v build-columns --model $(MODEL) --run $(RUN) > columns-report.json; \
	retry upload_store; \
	jq -e --arg run $(RUN) '$(COLUMNS_DESCRIPTOR)' "$$dir/manifest.json" > columns.json.new && mv columns.json.new columns.json

# Before a write that takes a run live (finalize) or prepares one (cut-columns):
# a re-run of a red job carries the outputs of the first attempt, so R2's live
# pointer is read first and has to be the one resolve saw (LIVE_SEEN, "run:crc"),
# or already the one about to be written (ABOUT_MANIFEST, finalize only: a re-run
# after its own flip). Not "the newest run wins": publishing an older run by hand
# is how a bad one is rolled back. Without LIVE_SEEN (resolve found no pointer)
# only an older live run may be replaced. A pointer that cannot be read is an
# error; a source with no pointer yet is fine. A dry run writes nothing and
# checks nothing. `s3 ls` exits 1 for no match and 255 for an error, which is how
# absent and unreadable differ.
LIVE_SEEN ?=
ABOUT_MANIFEST ?=
check-live-run: ## Refuse to write when R2's live pointer is not one this run may replace
	@set -eu; [ -z "$(DRY_RUN)" ] || exit 0; \
	status=0; $(S3) ls $(R2_ROOT)/$(LATEST_FILE) > /dev/null || status=$$?; \
	[ "$$status" != 1 ] || { echo "no live pointer for $(MODEL) yet"; exit 0; }; \
	[ "$$status" = 0 ] || { echo "checking the live pointer failed (status $$status)"; exit 1; }; \
	text=$$($(S3) cp $(R2_ROOT)/$(LATEST_FILE) - --only-show-errors) || { echo "reading $(LATEST_FILE) failed"; exit 1; }; \
	live=$$(printf '%s' "$$text" | jq -r '"\(.run):\(.manifestCrc32)"'); \
	case "$$live" in [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]:*) ;; *) echo "the live pointer names '$$live'"; exit 1 ;; esac; \
	if [ -n "$(LIVE_SEEN)" ]; then [ "$$live" != "$(LIVE_SEEN)" ] || exit 0; \
	else [ "$${live%%:*}" -ge "$(RUN)" ] || exit 0; fi; \
	about=""; [ -z "$(ABOUT_MANIFEST)" ] || about=$$($(CRC32) "$(ABOUT_MANIFEST)"); \
	{ [ -n "$$about" ] && [ "$$live" = "$(RUN):$$about" ]; } || { \
		echo "the live pointer is $$live, not the one this publish started from ($(LIVE_SEEN)); refusing to write over it"; exit 1; }

# The CRC32 of a manifest as the writer serializes it without its `columns`.
MANIFEST_BASE_CRC32 = $(PYTHON) -c "import json, sys, zlib; m = json.load(open(sys.argv[1])); m.pop('columns', None); print(f'{zlib.crc32((json.dumps(m, ensure_ascii=False, indent=2) + chr(10)).encode()) & 0xFFFFFFFF:08x}')"

publish-columns: ## Cut the live run's column store, upload it, then manifest, STAC item and pointer
	@set -eu; \
	dir=web/public/data/$(MODEL).$(RUN); \
	stale() { echo "::notice::$(MODEL) run $(RUN): $$1; no column store attached"; exit 0; }; \
	$(COLUMNS_SHELL) \
	pointer_state() { \
		text=$$($(S3) cp $(R2_ROOT)/$(LATEST_FILE) - --only-show-errors) || { echo "reading $(LATEST_FILE) failed" >&2; return 1; }; \
		printf '%s' "$$text" | jq -r '"\(.run) \(.manifestPath) \(.manifestCrc32)"'; }; \
	still_live() { now=$$(pointer_state) || exit 1; [ "$$now" = "$$live" ] || stale "$$1"; }; \
	upload_manifest() { \
		$(S3) cp "$$dir/manifest.json" $(R2_ROOT)/$(MODEL).$(RUN)/manifest.json --no-progress $(DRY_RUN) \
			--content-type application/json --cache-control "public, max-age=31536000, immutable"; }; \
	attach() { \
		$(MAKE) --no-print-directory check-pointer MODEL=$(MODEL) RUN=$(RUN); \
		still_live "the run was replaced before its manifest was uploaded"; \
		retry upload_manifest; \
		retry $(MAKE) --no-print-directory upload-r2-stac-item MODEL=$(MODEL) RUN=$(RUN) DRY_RUN=$(DRY_RUN); \
		[ -n "$(DRY_RUN)" ] || [ "$(WARM)" != true ] || $(COLUMNS_WARM_TIMEOUT) scripts/warm_edge_cache.sh $(MODEL) $(RUN) --manifest-only \
			|| echo "warming the manifest failed; it goes live cold"; \
		still_live "the run was replaced before its pointer was written"; \
		retry $(MAKE) --no-print-directory upload-r2-pointer MODEL=$(MODEL) RUN=$(RUN) DRY_RUN=$(DRY_RUN); }; \
	live=$$(pointer_state) || exit 1; \
	[ "$${live%% *}" = "$(RUN)" ] || stale "the live pointer names '$${live%% *}'"; \
	set -- $$live; [ "$$2" = "$(MODEL).$(RUN)/manifest.json" ] || stale "the live manifest is $$2"; \
	started=$$3; \
	mkdir -p "$$dir"; \
	$(S3) cp $(R2_ROOT)/$(LATEST_FILE) web/public/data/$(LATEST_FILE) --only-show-errors; \
	$(S3) cp $(R2_ROOT)/$(MODEL).$(RUN)/manifest.json "$$dir/manifest.json" --only-show-errors; \
	have=$$($(CRC32) "$$dir/manifest.json"); \
	if [ "$$have" != "$$started" ]; then \
		if [ "$$(jq 'has("columns")' "$$dir/manifest.json")" != true ]; then \
			stale "the live manifest is not the one its pointer names (a publish is in flight)"; \
		fi; \
		[ "$$($(MANIFEST_BASE_CRC32) "$$dir/manifest.json")" = "$$started" ] || { \
			echo "the live manifest differs from the one its pointer names in more than its columns"; exit 1; }; \
		echo "the live manifest names its columns but the pointer does not: finishing the flip"; \
		jq --arg crc "$$have" '.manifestCrc32 = $$crc' web/public/data/$(LATEST_FILE) > web/public/data/$(LATEST_FILE).new; \
		mv web/public/data/$(LATEST_FILE).new web/public/data/$(LATEST_FILE); \
		$(PYTHON) -m xuebuild stac --model $(MODEL) --run $(RUN) > /dev/null; \
		attach; exit 0; \
	fi; \
	missing=$$($(PYTHON) -m xuebuild columns-missing --model $(MODEL) --base-manifest "$$dir/manifest.json"); \
	[ "$$missing" = true ] || stale "the live manifest already has its columns or the source has none"; \
	$(PYTHON) -m xuebuild column-inputs --manifest "$$dir/manifest.json" > column-inputs.txt; \
	echo "fetching $$(wc -l < column-inputs.txt | tr -d ' ') objects of $(MODEL).$(RUN)"; \
	if ! fetch_inputs column-inputs.txt; then \
		still_live "the run was replaced while its objects were read"; \
		echo "fetching the column inputs failed"; exit 1; \
	fi; \
	rm -f column-inputs.txt; \
	$(PYTHON) -m xuebuild -v build-columns --model $(MODEL) --run $(RUN) > columns-report.json; \
	still_live "the run was replaced while its store was cut"; \
	retry upload_store; \
	[ -n "$(DRY_RUN)" ] || [ "$(WARM)" != true ] || $(COLUMNS_WARM_TIMEOUT) scripts/warm_edge_cache.sh $(MODEL) $(RUN) --columns \
		|| echo "warming the column store failed; it goes live cold"; \
	attach
