# The area-weighted weather indicators (docs/indicators.md): one append-only
# JSONL per source and month, season running totals, a context table, and the
# index that names them all with byte offsets. Everything lands under
# indicators/ at the data root. The index is the only entry point, so it goes
# up last; the weights are built offline by scripts/indicators_weights.py and
# uploaded by hand.

# Where the product builds: the local mirror of the data root's indicators/.
INDICATORS_DIR ?= web/public/data/indicators
# The weights the build reads (scripts/indicators_weights.py writes here).
INDICATORS_WEIGHTS_DIR ?= data/indicators/weights
INDICATORS_SOURCES ?= gfs,ecmwf,aifs,ifshres
INDICATORS_ROUND = $(if $(ROUND),$(ROUND),now)
# The month files a build appends to: a row belongs to its run's UTC month,
# so just after the 1st a live run still appends to last month's file. Only
# these two are fetched before a build.
INDICATORS_MONTH ?= $(shell date -u +%Y-%m)
INDICATORS_PREV_MONTH ?= $(shell $(PYTHON) -c "import datetime as d; t = d.datetime.now(d.timezone.utc).date().replace(day=1) - d.timedelta(days=1); print(t.strftime('%Y-%m'))")
INDICATORS_R2 = $(R2_ROOT)/indicators

.PHONY: indicators-build live-indicators-index pull-r2-indicators-weights upload-r2-indicators upload-r2-indicators-weights

indicators-build: ## Build the indicators for SOURCES (INDICATORS_SOURCES) at ROUND
	$(PYTHON) -m xuebuild indicators-build --sources $(INDICATORS_SOURCES) --round $(INDICATORS_ROUND) --output-dir $(INDICATORS_DIR) --weights-dir $(INDICATORS_WEIGHTS_DIR) $(FORCE)

# The live index, this and last month's file of every source, and every season,
# climatology and context file: the month files are the next build's history,
# so each CRC32 is checked rather than trusted. A missing index is the first
# publish.
live-indicators-index: ## Pull the live indicators index and the files a build appends to
	@set -e; mkdir -p $(INDICATORS_DIR); \
	if ! $(S3) cp $(INDICATORS_R2)/index.json $(INDICATORS_DIR)/index.json --only-show-errors 2>/dev/null; then \
		rm -f $(INDICATORS_DIR)/index.json; echo "no live indicators index"; exit 0; \
	fi; \
	for file in $$(jq -r --arg month "$(INDICATORS_MONTH)" --arg prev "$(INDICATORS_PREV_MONTH)" '.files[] | .path | select((startswith("features/") | not) or endswith("/" + $$month + ".jsonl") or endswith("/" + $$prev + ".jsonl"))' $(INDICATORS_DIR)/index.json); do \
		named=$$(jq -r --arg file "$$file" '.files[] | select(.path == $$file) | .crc32' $(INDICATORS_DIR)/index.json); \
		mkdir -p "$(INDICATORS_DIR)/$$(dirname "$$file")"; \
		if $(S3) cp "$(INDICATORS_R2)/$$file" "$(INDICATORS_DIR)/$$file" --only-show-errors; then \
			crc=$$($(CRC32) "$(INDICATORS_DIR)/$$file"); \
			[ "$$crc" = "$$named" ] || { echo "the live $$file is CRC32 $$crc but the index says $$named"; exit 1; }; \
			echo "live indicators: $$file ($$(wc -c < "$(INDICATORS_DIR)/$$file" | tr -d ' ') bytes)"; \
		else \
			rm -f "$(INDICATORS_DIR)/$$file"; \
			echo "note: the index names $$file but R2 has no such object; that file starts again"; \
		fi; \
	done

pull-r2-indicators-weights: ## Pull the area-weight stores the indicators build reads
	@set -e; mkdir -p $(INDICATORS_WEIGHTS_DIR); \
	$(S3) cp $(INDICATORS_R2)/weights/ $(INDICATORS_WEIGHTS_DIR)/ --recursive --exclude "*" --include "*.zarr/*" --only-show-errors; \
	count=$$(find $(INDICATORS_WEIGHTS_DIR) -maxdepth 1 -name '*.zarr' -type d | wc -l | tr -d ' '); \
	[ "$$count" -gt 0 ] || { echo "no weights on R2; build them with scripts/indicators_weights.py and make upload-r2-indicators-weights"; exit 1; }; \
	echo "weights: $$count grids on disk"

# Month files, then the season, climatology and context files, then the
# index: each object before anything that names it, and the index moves only
# once R2 reports every size.
upload-r2-indicators: ## Upload the built indicators, the index last
	@set -e; dir=$(INDICATORS_DIR); \
	[ -f "$$dir/index.json" ] || { echo "no built indicators index at $$dir"; exit 1; }; \
	listing() { (cd "$$dir" && find "$$1" -type f -name "$$2" 2>/dev/null | sort) || true; }; \
	months=$$(listing features '*.jsonl'); seasons=$$(listing season '*.json'); \
	climate=$$(listing climatology '*.json'); context=$$(listing context '*.json'); \
	for file in $$months; do \
		$(S3) cp "$$dir/$$file" "$(INDICATORS_R2)/$$file" --no-progress $(DRY_RUN) \
			--content-type application/x-ndjson --cache-control "public, max-age=31536000, immutable"; \
	done; \
	for file in $$seasons $$climate $$context; do \
		$(S3) cp "$$dir/$$file" "$(INDICATORS_R2)/$$file" --no-progress $(DRY_RUN) \
			--content-type application/json --cache-control "no-cache"; \
	done; \
	[ -n "$(DRY_RUN)" ] || { \
		for file in $$months $$seasons $$climate $$context; do \
			local_bytes=$$(wc -c < "$$dir/$$file" | tr -d ' '); \
			remote_bytes=$$($(call r2_size,indicators/$$file)); \
			[ "$$remote_bytes" = "$$local_bytes" ] || { echo "indicators/$$file is '$$remote_bytes' bytes on R2, not $$local_bytes; the index stays put"; exit 1; }; \
			echo "verified $$file ($$local_bytes bytes) on R2"; \
		done; \
	}; \
	echo "Uploading indicators/index.json..."; \
	$(S3) cp "$$dir/index.json" "$(INDICATORS_R2)/index.json" --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"

# Manual: run after scripts/indicators_weights.py, only when asked. Each grid
# is a Zarr directory; `cp --recursive`, not `sync`, as for the other stores.
upload-r2-indicators-weights: ## Upload the area-weight stores (manual)
	@set -e; stores=$$(find $(INDICATORS_WEIGHTS_DIR) -maxdepth 1 -name '*.zarr' -type d | sort); \
	[ -n "$$stores" ] || { echo "no weights in $(INDICATORS_WEIGHTS_DIR); run scripts/indicators_weights.py"; exit 1; }; \
	for store in $$stores; do \
		$(S3) cp "$$store" "$(INDICATORS_R2)/weights/$$(basename "$$store")/" --recursive --no-progress $(DRY_RUN) \
			--cache-control "no-cache"; \
	done; \
	[ -n "$(DRY_RUN)" ] || for store in $$stores; do \
		name=$$(basename "$$store"); \
		for file in $$(cd "$$store" && find . -type f | sed 's|^\./||' | sort); do \
			local_bytes=$$(wc -c < "$$store/$$file" | tr -d ' '); \
			remote_bytes=$$($(call r2_size,indicators/weights/$$name/$$file)); \
			[ "$$remote_bytes" = "$$local_bytes" ] || { echo "$$name/$$file is '$$remote_bytes' bytes on R2, not $$local_bytes"; exit 1; }; \
		done; \
		echo "verified $$name on R2"; \
	done
