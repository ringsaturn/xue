# The point products (docs/tc.md, docs/airport.md, docs/sounding.md,
# docs/synop.md): one directory per issue or round, immutable and ?v=<crc32>-addressed, taken live
# by latest-<product>.json. Each build reads the previous live one, so a
# publish pulls it first (live-*-index); a missing pointer is the first publish.

# ISSUE=YYYYMMDDHH for tc and sounding; airport and synop take ROUND=YYYYMMDDHHMM.
ISSUE ?= now
AIRPORT_ROUND = $(if $(ROUND),$(ROUND),now)
SYNOP_ROUND = $(if $(ROUND),$(ROUND),now)
TC_KEEP ?= 168
AIRPORT_KEEP ?= 18
SOUNDING_KEEP ?= 168
SYNOP_KEEP ?= 18
NEXRAD_KEEP ?= 40
NEXRAD_ROUND = $(if $(ROUND),$(ROUND),now)
NEXRAD_SITES ?= all

.PHONY: tc-build live-tc-index upload-r2-tc prune-r2-tc airport-build live-airport-index upload-r2-airport prune-r2-airport sounding-build live-sounding-index upload-r2-sounding prune-r2-sounding synop-build live-synop-index upload-r2-synop prune-r2-synop nexrad-build nexrad-case prune-r2-nexrad

# $(call point_dir,product,id): an issue's directory under the data root.
# The hourly products file it under its day, <product>/YYYY/MM/DD/<product>.<id>,
# so the archive can grow for years; the airport's rounds stay flat.
point_dir = $(if $(filter tc sounding,$(1)),$(1)/$(shell printf '%s' '$(2)' | cut -c1-4)/$(shell printf '%s' '$(2)' | cut -c5-6)/$(shell printf '%s' '$(2)' | cut -c7-8)/$(1).$(2),$(1).$(2))

# $(call point_pointer_check,product,id,noun,pass hint,why no pointer): the
# pointer on disk must name this issue and carry its index's CRC32.
define point_pointer_check
[ "$(2)" != "now" ] || { echo "$(4)"; exit 1; }; \
	rel=$(call point_dir,$(1),$(2)); \
	dir=web/public/data/$$rel; \
	[ -f "$$dir/index.json" ] || { echo "no built $(1) $(3) at $$dir"; exit 1; }; \
	[ -f web/public/data/latest-$(1).json ] || { echo "no latest-$(1).json: the build withheld the pointer ($(5))"; exit 1; }; \
	pointer_path=$$(jq -r .path web/public/data/latest-$(1).json); \
	[ "$$pointer_path" = "$$rel/index.json" ] || { echo "latest-$(1).json names $$pointer_path, not $$rel"; exit 1; }; \
	pointer_crc=$$(jq -r .crc32 web/public/data/latest-$(1).json); \
	index_crc=$$($(CRC32) $$dir/index.json); \
	[ "$$pointer_crc" = "$$index_crc" ] || { echo "latest-$(1).json carries CRC32 $$pointer_crc but $$dir/index.json is $$index_crc"; exit 1; }
endef

# The size R2 reports for a key, or nothing.
r2_size = $(AWS) s3api --endpoint-url $(R2_ENDPOINT) head-object --bucket $(R2_BUCKET) --key "$(R2_PREFIX)/$(1)" --query ContentLength --output text 2>/dev/null || true

# $(call point_upload_ndjson,product,id,key,noun,pass hint,why no pointer):
# the .jsonl the index names (under .<key>.path), then the index, then the
# pointer, each object before the one that names it; the pointer moves only
# once R2 reports both sizes.
define point_upload_ndjson
@set -e; \
	$(call point_pointer_check,$(1),$(2),$(4),$(5),$(6)); \
	$(3)=$$(jq -r .$(3).path $$dir/index.json); \
	$(S3) cp $$dir/$$$(3) $(R2_ROOT)/$$rel/$$$(3) --no-progress $(DRY_RUN) \
		--content-type application/x-ndjson --cache-control "public, max-age=31536000, immutable"; \
	$(S3) cp $$dir/index.json $(R2_ROOT)/$$rel/index.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "public, max-age=31536000, immutable"; \
	[ ! -f "$$dir/$(STAC_ITEM)" ] || $(S3) cp $$dir/$(STAC_ITEM) $(R2_ROOT)/$$rel/$(STAC_ITEM) \
		--no-progress $(DRY_RUN) --content-type application/geo+json --cache-control "no-cache"; \
	[ -n "$(DRY_RUN)" ] || { \
		$(3)_bytes=$$(wc -c < "$$dir/$$$(3)" | tr -d ' '); \
		index_bytes=$$(wc -c < "$$dir/index.json" | tr -d ' '); \
		remote_$(3)=$$($(call r2_size,$$rel/$$$(3))); \
		remote_index=$$($(call r2_size,$$rel/index.json)); \
		[ "$$remote_$(3)" = "$$$(3)_bytes" ] || { echo "$$rel/$$$(3) is '$$remote_$(3)' bytes on R2, not $$$(3)_bytes; the pointer stays put"; exit 1; }; \
		[ "$$remote_index" = "$$index_bytes" ] || { echo "$$rel/index.json is '$$remote_index' bytes on R2, not $$index_bytes; the pointer stays put"; exit 1; }; \
		echo "verified $$$(3) ($$$(3)_bytes bytes) and index.json ($$index_bytes bytes) on R2"; \
	}; \
	echo "Uploading latest-$(1).json (takes $(1) $(4) $(2) live)..."; \
	$(S3) cp web/public/data/latest-$(1).json $(R2_ROOT)/latest-$(1).json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"; \
	$(MAKE) --no-print-directory upload-r2-stac-collection STAC_DIR=$(1) DRY_RUN=$(DRY_RUN)
endef

# $(call point_live_ndjson,product,key,noun): the live pointer, index and the
# .jsonl it spans. The .jsonl is the next build's history, so its CRC32 is
# checked rather than trusted; a missing one starts a fresh window instead of
# wedging the pipeline.
define point_live_ndjson
@set -e; mkdir -p web/public/data; \
	pointer=$$($(S3) cp $(R2_ROOT)/latest-$(1).json - --only-show-errors 2>/dev/null || true); \
	[ -n "$$pointer" ] || { echo "no live $(1) pointer"; exit 0; }; \
	path=$$(printf '%s' "$$pointer" | jq -r .path); \
	directory=$$(dirname "$$path"); \
	mkdir -p "web/public/data/$$directory"; \
	$(S3) cp "$(R2_ROOT)/$$path" "web/public/data/$$path" --only-show-errors; \
	printf '%s' "$$pointer" > web/public/data/latest-$(1).json; \
	$(2)=$$(jq -r .$(2).path "web/public/data/$$path"); \
	named=$$(jq -r .$(2).crc32 "web/public/data/$$path"); \
	if $(S3) cp "$(R2_ROOT)/$$directory/$$$(2)" "web/public/data/$$directory/$$$(2)" --no-progress --only-show-errors; then \
		crc=$$($(CRC32) "web/public/data/$$directory/$$$(2)"); \
		[ "$$crc" = "$$named" ] || { echo "the live $$$(2) is CRC32 $$crc but its index says $$named"; exit 1; }; \
		stations=$$(jq -r '.stations | length' "web/public/data/$$path"); \
		bytes=$$(jq -r .$(2).byteLength "web/public/data/$$path"); \
		echo "live $(1) $(3): $$path ($$stations stations, $$bytes bytes of $(2))"; \
	else \
		rm -f "web/public/data/$$directory/$$$(2)"; \
		echo "warning: the live $(3) $$path names $$$(2) but R2 has no such object; carrying on, so the next $(3) starts a fresh window rather than wedging the pipeline"; \
	fi
endef

# $(call point_prune,product,keep,noun): delete directories beyond the newest
# <keep>, never the one the live pointer names; no pointer, nothing to prune.
# The candidates are the flat <product>.<id> directories under the root and
# those under <product>/YYYY/MM/DD/, ranked by their own name. A nested listing
# that fails only hides candidates, and a hidden one is neither deleted nor
# pushes a listed one past <keep>.
define point_prune
@set -e; \
	live=$$($(S3) cp $(R2_ROOT)/latest-$(1).json - --only-show-errors 2>/dev/null | jq -r .path || true); \
	live=$${live%/index.json}; \
	[ -n "$$live" ] || { echo "no live $(1) pointer, nothing to prune"; exit 0; }; \
	echo "live $(1) $(3): $$live"; \
	listing=$$($(S3) ls $(R2_ROOT)/) \
		|| { echo "listing the bucket failed, refusing to prune"; exit 1; }; \
	directories=$$(printf '%s\n' "$$listing" | awk '/ PRE /{print $$2}' | sed 's:/$$::' | grep "^$(1)\." || true); \
	for year in $$($(S3) ls $(R2_ROOT)/$(1)/ 2>/dev/null | awk '/ PRE [0-9][0-9][0-9][0-9]\/$$/{print $$2}' | sed 's:/$$::'); do \
		for month in $$($(S3) ls $(R2_ROOT)/$(1)/$$year/ 2>/dev/null | awk '/ PRE [0-9][0-9]\/$$/{print $$2}' | sed 's:/$$::'); do \
			for day in $$($(S3) ls $(R2_ROOT)/$(1)/$$year/$$month/ 2>/dev/null | awk '/ PRE [0-9][0-9]\/$$/{print $$2}' | sed 's:/$$::'); do \
				directories="$$directories $$($(S3) ls $(R2_ROOT)/$(1)/$$year/$$month/$$day/ 2>/dev/null | awk '/ PRE /{print $$2}' \
					| sed 's:/$$::' | grep "^$(1)\." | sed "s:^:$(1)/$$year/$$month/$$day/:" || true)"; \
			done; \
		done; \
	done; \
	for directory in $$(for candidate in $$directories; do printf '%s %s\n' "$${candidate##*/}" "$$candidate"; done \
		| sort -r | tail -n +$$(($(2) + 1)) | cut -d' ' -f2); do \
		if [ "$$directory" != "$$live" ]; then \
			echo "Deleting $$directory..."; \
			$(S3) rm $(R2_ROOT)/$$directory/ --recursive --only-show-errors $(DRY_RUN); \
		fi; \
	done
endef

tc-build: ## Build the tropical cyclone product for ISSUE
	$(PYTHON) -m xuebuild tc-build --issue $(ISSUE) $(FORCE)

live-tc-index: ## Pull the live tc pointer and index
	@set -e; mkdir -p web/public/data; \
	pointer=$$($(S3) cp $(R2_ROOT)/latest-tc.json - --only-show-errors 2>/dev/null || true); \
	[ -n "$$pointer" ] || { echo "no live tc pointer"; exit 0; }; \
	path=$$(printf '%s' "$$pointer" | jq -r .path); \
	mkdir -p "web/public/data/$$(dirname "$$path")"; \
	if $(S3) cp "$(R2_ROOT)/$$path" "web/public/data/$$path" --only-show-errors; then \
		printf '%s' "$$pointer" > web/public/data/latest-tc.json; \
		echo "live tc issue: $$path"; \
	else \
		echo "warning: the live tc pointer names $$path but R2 has no such object; carrying on, so the next issue starts fresh rather than wedging the pipeline"; \
	fi

upload-r2-tc: ## Upload a tc issue, then take it live
	@set -e; \
	$(call point_pointer_check,tc,$(ISSUE),issue,pass ISSUE=YYYYMMDDHH,no source contributed); \
	$(S3) cp $$dir $(R2_ROOT)/$$rel/ --recursive --no-progress $(DRY_RUN) --exclude "$(STAC_ITEM)" \
		--content-type application/json --cache-control "public, max-age=31536000, immutable"; \
	[ ! -f "$$dir/$(STAC_ITEM)" ] || $(S3) cp $$dir/$(STAC_ITEM) $(R2_ROOT)/$$rel/$(STAC_ITEM) \
		--no-progress $(DRY_RUN) --content-type application/geo+json --cache-control "no-cache"; \
	[ -n "$(DRY_RUN)" ] || { \
		index_bytes=$$(wc -c < "$$dir/index.json" | tr -d ' '); \
		remote_index=$$($(call r2_size,$$rel/index.json)); \
		[ "$$remote_index" = "$$index_bytes" ] || { echo "$$rel/index.json is '$$remote_index' bytes on R2, not $$index_bytes; the pointer stays put"; exit 1; }; \
		echo "verified index.json ($$index_bytes bytes) on R2"; \
	}; \
	echo "Uploading latest-tc.json (takes tc issue $(ISSUE) live)..."; \
	$(S3) cp web/public/data/latest-tc.json $(R2_ROOT)/latest-tc.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"; \
	$(MAKE) --no-print-directory upload-r2-stac-collection STAC_DIR=tc DRY_RUN=$(DRY_RUN)

prune-r2-tc: ## Delete tc issues beyond the newest TC_KEEP
	$(call point_prune,tc,$(TC_KEEP),issue)

airport-build: ## Build the airport METAR / TAF product for ROUND
	$(PYTHON) -m xuebuild airport-build --round $(AIRPORT_ROUND) $(FORCE)

live-airport-index: ## Pull the live airport pointer, index and history
	$(call point_live_ndjson,airport,history,round)

upload-r2-airport: ## Upload an airport round, then take it live
	$(call point_upload_ndjson,airport,$(AIRPORT_ROUND),history,round,pass ROUND=YYYYMMDDHHMM,neither observation source arrived)

prune-r2-airport: ## Delete airport rounds beyond the newest AIRPORT_KEEP
	$(call point_prune,airport,$(AIRPORT_KEEP),round)

sounding-build: ## Build the radiosonde sounding product for ISSUE
	$(PYTHON) -m xuebuild sounding-build --issue $(ISSUE) $(FORCE)

live-sounding-index: ## Pull the live sounding pointer, index and soundings
	$(call point_live_ndjson,sounding,soundings,issue)

upload-r2-sounding: ## Upload a sounding issue, then take it live
	$(call point_upload_ndjson,sounding,$(ISSUE),soundings,issue,pass ISSUE=YYYYMMDDHH,nothing contributed)

prune-r2-sounding: ## Delete sounding issues beyond the newest SOUNDING_KEEP
	$(call point_prune,sounding,$(SOUNDING_KEEP),issue)

synop-build: ## Build the surface station product for ROUND
	$(PYTHON) -m xuebuild synop-build --round $(SYNOP_ROUND) $(FORCE)

# The live pointer, index and every network file it spans: the files are the
# next build's history, so each CRC32 is checked rather than trusted, and a
# missing one only restarts that network's window.
live-synop-index: ## Pull the live synop pointer, index and network files
	@set -e; mkdir -p web/public/data; \
	pointer=$$($(S3) cp $(R2_ROOT)/latest-synop.json - --only-show-errors 2>/dev/null || true); \
	[ -n "$$pointer" ] || { echo "no live synop pointer"; exit 0; }; \
	path=$$(printf '%s' "$$pointer" | jq -r .path); \
	directory=$$(dirname "$$path"); \
	mkdir -p "web/public/data/$$directory"; \
	$(S3) cp "$(R2_ROOT)/$$path" "web/public/data/$$path" --only-show-errors; \
	printf '%s' "$$pointer" > web/public/data/latest-synop.json; \
	for file in $$(jq -r '.networks[].file.path' "web/public/data/$$path"); do \
		named=$$(jq -r --arg file "$$file" '.networks[] | select(.file.path == $$file) | .file.crc32' "web/public/data/$$path"); \
		if $(S3) cp "$(R2_ROOT)/$$directory/$$file" "web/public/data/$$directory/$$file" --only-show-errors; then \
			crc=$$($(CRC32) "web/public/data/$$directory/$$file"); \
			[ "$$crc" = "$$named" ] || { echo "the live $$file is CRC32 $$crc but its index says $$named"; exit 1; }; \
			echo "live synop round: $$directory/$$file ($$(wc -c < "web/public/data/$$directory/$$file" | tr -d ' ') bytes)"; \
		else \
			rm -f "web/public/data/$$directory/$$file"; \
			echo "warning: $$path names $$file but R2 has no such object; that network's window starts again"; \
		fi; \
	done

# Every network file, then the index, then the pointer: each object before
# the one that names it, and the pointer moves only once R2 reports every
# size.
upload-r2-synop: ## Upload a synop round, then take it live
	@set -e; \
	$(call point_pointer_check,synop,$(SYNOP_ROUND),round,pass ROUND=YYYYMMDDHHMM,no network's observations arrived); \
	files=$$(jq -r '.networks[].file.path' $$dir/index.json); \
	for file in $$files; do \
		$(S3) cp $$dir/$$file $(R2_ROOT)/$$rel/$$file --no-progress $(DRY_RUN) \
			--content-type application/x-ndjson --cache-control "public, max-age=31536000, immutable"; \
	done; \
	$(S3) cp $$dir/index.json $(R2_ROOT)/$$rel/index.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "public, max-age=31536000, immutable"; \
	[ ! -f "$$dir/$(STAC_ITEM)" ] || $(S3) cp $$dir/$(STAC_ITEM) $(R2_ROOT)/$$rel/$(STAC_ITEM) \
		--no-progress $(DRY_RUN) --content-type application/geo+json --cache-control "no-cache"; \
	[ -n "$(DRY_RUN)" ] || { \
		for file in $$files index.json; do \
			local_bytes=$$(wc -c < "$$dir/$$file" | tr -d ' '); \
			remote_bytes=$$($(call r2_size,$$rel/$$file)); \
			[ "$$remote_bytes" = "$$local_bytes" ] || { echo "$$rel/$$file is '$$remote_bytes' bytes on R2, not $$local_bytes; the pointer stays put"; exit 1; }; \
			echo "verified $$file ($$local_bytes bytes) on R2"; \
		done; \
	}; \
	echo "Uploading latest-synop.json (takes synop round $(SYNOP_ROUND) live)..."; \
	$(S3) cp web/public/data/latest-synop.json $(R2_ROOT)/latest-synop.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"; \
	$(MAKE) --no-print-directory upload-r2-stac-collection STAC_DIR=synop DRY_RUN=$(DRY_RUN)

prune-r2-synop: ## Delete synop rounds beyond the newest SYNOP_KEEP
	$(call point_prune,synop,$(SYNOP_KEEP),round)

nexrad-build: ## Build the nexrad rounds up to ROUND for NEXRAD_SITES (docs/nexrad.md)
	$(PYTHON) -m xuebuild nexrad-build --round $(NEXRAD_ROUND) --sites $(NEXRAD_SITES)

nexrad-case: ## Replay a nexrad showcase case, CASE=showcase/nexrad-cases/<id>.json
	@[ -n "$(CASE)" ] || { echo "pass CASE=showcase/nexrad-cases/<id>.json"; exit 1; }
	$(PYTHON) -m xuebuild nexrad-case $(CASE)

prune-r2-nexrad: ## Delete nexrad rounds beyond the newest NEXRAD_KEEP
	$(call point_prune,nexrad,$(NEXRAD_KEEP),round)
