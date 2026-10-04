# Runs on the R2 dataset bucket: upload, take live, prune, read back.
# Needs an R2 token's key pair in AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
# and CLOUDFLARE_ACCOUNT_ID (the endpoint host).

# Published runs of one model to keep (one: the live run, no history).
KEEP ?= 1
# `--dryrun` to preview an upload or a prune.
DRY_RUN ?=
# WARM=false skips the edge-cache warm-up between the assets and the pointer.
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

.PHONY: upload-r2 upload-r2-bundles upload-r2-manifest upload-r2-stac-item check-pointer upload-r2-pointer upload-r2-stac-collection warm-r2 prune-r2 prune-r2-rounds live-run live-manifest live-window

# Run assets are immutable (?v=<crc32>); the pointer goes last and is what takes
# the run live. `cp`, not `sync`: a fresh directory has nothing to skip, so a
# sync only spends a listing. A failed warm-up never holds the pointer back.
upload-r2: ## Upload one built run (RUN, ROUND), warm the edge, take it live
	@set -e; dir=web/public/data/$(RUN_DIR); \
	[ -d "$$dir" ] || { echo "no built run at $$dir, pass RUN=YYYYMMDDHH"; exit 1; }; \
	$(MAKE) --no-print-directory check-pointer MODEL=$(MODEL) RUN=$(RUN) ROUND=$(ROUND); \
	$(S3) cp $$dir $(R2_ROOT)/$(RUN_DIR)/ --recursive --no-progress $(DRY_RUN) \
		--exclude "manifest.part.*.json" --exclude "$(STAC_ITEM)" \
		--cache-control "public, max-age=31536000, immutable"; \
	$(MAKE) --no-print-directory upload-r2-stac-item MODEL=$(MODEL) RUN=$(RUN) ROUND=$(ROUND) DRY_RUN=$(DRY_RUN); \
	[ -n "$(DRY_RUN)" ] || [ "$(WARM)" != true ] || $(MAKE) --no-print-directory warm-r2 MODEL=$(MODEL) RUN=$(RUN) ROUND=$(ROUND) \
		|| echo "warming the edge cache failed; the run goes live cold"; \
	$(MAKE) --no-print-directory upload-r2-pointer MODEL=$(MODEL) RUN=$(RUN) DRY_RUN=$(DRY_RUN)

# One piece of a fanned-out publish (no manifest; parts stay local). `cp`, not
# `sync`: concurrent pieces each listing the prefix drew ServiceUnavailable
# from R2. Three attempts, since one refused object should not fail a publish.
upload-r2-bundles: ## Upload this machine's bundle groups of a fanned-out run
	@set -e; dir=web/public/data/$(MODEL).$(RUN); \
	[ -d "$$dir" ] || { echo "no built bundles at $$dir, pass RUN=YYYYMMDDHH"; exit 1; }; \
	ls $$dir/manifest.part.*.json > /dev/null 2>&1 || { echo "no manifest.part.*.json in $$dir: build with --bundles"; exit 1; }; \
	for attempt in 1 2 3; do \
		$(S3) cp $$dir $(R2_ROOT)/$(MODEL).$(RUN)/ --recursive --no-progress $(DRY_RUN) \
			--exclude "manifest.json" --exclude "manifest.part.*.json" \
			--cache-control "public, max-age=31536000, immutable" && break; \
		[ "$$attempt" -lt 3 ] || { echo "uploading the bundles failed three times"; exit 1; }; \
		echo "upload attempt $$attempt failed; retrying in $$((attempt * 20)) s"; sleep $$((attempt * 20)); \
	done; \
	[ -n "$(DRY_RUN)" ] || for part in $$dir/manifest.part.*.json; do \
		scripts/warm_edge_cache.sh $(MODEL) $(RUN) --artifacts-of "$$part" \
			|| echo "warming the edge cache for $$part failed; those artifacts go live cold"; \
	done

upload-r2-manifest: ## Upload an assembled manifest, then take the run live
	@set -e; dir=web/public/data/$(MODEL).$(RUN); \
	[ -f "$$dir/manifest.json" ] || { echo "no manifest at $$dir/manifest.json: run assemble-run first"; exit 1; }; \
	$(MAKE) --no-print-directory check-pointer MODEL=$(MODEL) RUN=$(RUN); \
	$(S3) cp $$dir/manifest.json $(R2_ROOT)/$(MODEL).$(RUN)/manifest.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "public, max-age=31536000, immutable"; \
	$(MAKE) --no-print-directory upload-r2-stac-item MODEL=$(MODEL) RUN=$(RUN) DRY_RUN=$(DRY_RUN); \
	[ -n "$(DRY_RUN)" ] || scripts/warm_edge_cache.sh $(MODEL) $(RUN) --manifest-only \
		|| echo "warming the manifest failed; the run goes live cold"; \
	$(MAKE) --no-print-directory upload-r2-pointer MODEL=$(MODEL) RUN=$(RUN) DRY_RUN=$(DRY_RUN)

# Not ?v=-addressed (a top-up rewrites it in place), so served no-cache.
upload-r2-stac-item:
	@set -e; dir=web/public/data/$(RUN_DIR); \
	[ -f "$$dir/$(STAC_ITEM)" ] || { echo "no $(STAC_ITEM) in $$dir; the run predates the catalog"; exit 0; }; \
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
	$(S3) cp web/public/data/$(STAC_CATALOG) $(R2_ROOT)/$(STAC_CATALOG) --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"; \
	$(S3) cp web/public/data/$(STAC_INDEX) $(R2_ROOT)/$(STAC_INDEX) --no-progress $(DRY_RUN) \
		--content-type "text/html; charset=utf-8" --cache-control "no-cache"

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
		| sed 's:/$$::' | grep "^$(MODEL)\." | sort -r | tail -n +$$(($(KEEP) + 1))); do \
		if [ "$$run" != "$(MODEL).$$live" ]; then \
			echo "Deleting $$run..."; \
			$(S3) rm $(R2_ROOT)/$$run/ --recursive --only-show-errors $(DRY_RUN); \
		fi; \
	done

# Keeps the newest ROUNDS_KEEP rounds of the live run and the one the pointer
# names. Rounds are named by build minute (HHMM), so a run straddling midnight
# sorts wrong lexically; `round_order` folds a round more than twelve hours
# before the newest into the next day. Only the live run directory is listed.
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
	listing=$$($(S3) ls $(R2_ROOT)/$$dir/) \
		|| { echo "listing $$dir failed, refusing to prune"; exit 1; }; \
	rounds=$$(printf '%s\n' "$$listing" | awk '/ PRE /{print $$2}' | sed 's:/$$::' | grep -E '^[0-9]{4}$$' || true); \
	[ -n "$$rounds" ] || exit 0; \
	for round in $$($(PYTHON) -c "$$round_order" "$$rounds" | tr ' ' '\n' | tail -n +$$(( $(ROUNDS_KEEP) + 1 ))); do \
		if [ "$$dir/$$round" != "$$live" ]; then \
			echo "Deleting $$dir/$$round..."; \
			$(S3) rm $(R2_ROOT)/$$dir/$$round/ --recursive --only-show-errors $(DRY_RUN); \
		fi; \
	done

live-run: ## Print the run the live pointer names
	@$(S3) cp $(R2_ROOT)/$(LATEST_FILE) - --only-show-errors | jq -r .run || true

# The base a top-up merges onto (`assemble-run --base-manifest`).
live-manifest: ## Print the live run's manifest
	@set -e; path=$$($(S3) cp $(R2_ROOT)/$(LATEST_FILE) - --only-show-errors 2>/dev/null | jq -r .manifestPath || true); \
	[ -n "$$path" ] || exit 0; \
	$(S3) cp $(R2_ROOT)/$$path - --only-show-errors

live-window: ## Print the live rolling window's window.json
	@set -e; path=$$($(S3) cp $(R2_ROOT)/$(LATEST_FILE) - --only-show-errors 2>/dev/null | jq -r .manifestPath || true); \
	[ -n "$$path" ] || exit 0; \
	$(S3) cp $(R2_ROOT)/$$(dirname $$path)/window.json - --only-show-errors 2>/dev/null || true
