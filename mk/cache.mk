# Fetch-stage caches mirrored on the bucket so every runner shares one copy.
# None of it is served to the viewer.

# Decoded or warped frames of the window sources (JMA, the satellites, aurora)
# under data/raw/<model>-frames/<dir>/<name>_<YYYYMMDDHHMMSS>.<ext>. A pull
# takes the window's hours and the same span ahead, since a feed's valid times
# may lead the clock (the OVATION aurora grids by some ninety minutes).
# FRAMES_KEEP_HOURS: a week by default, because JMA's tiles expire at the
# agency and a case can only be cut from what is kept.
FRAMES_DIR = data/raw/$(MODEL)-frames
FRAMES_PREFIX = $(R2_ROOT)/$(MODEL)-frames
FRAMES_KEEP_HOURS ?= 168

.PHONY: pull-r2-frames push-r2-frames prune-r2-frames pull-r2-ancillary stage-ancillary push-r2-ancillary

pull-r2-frames: ## Pull the frame cache for the window about to be built
	@set -e; mkdir -p $(FRAMES_DIR); \
	includes=$$($(PYTHON) -c "from datetime import datetime, timedelta, UTC; now = datetime.now(UTC); \
	print(' '.join('--include */*_' + (now - timedelta(hours=h)).strftime('%Y%m%d%H') + '*' for h in range(int('$(if $(HOURS),$(HOURS),3)') + 1, -int('$(if $(HOURS),$(HOURS),3)') - 2, -1)))"); \
	$(S3) sync $(FRAMES_PREFIX)/ $(FRAMES_DIR)/ --exclude "*" --include "*/grid.json" $$includes --only-show-errors; \
	echo "frame cache: $$(find $(FRAMES_DIR) \( -name '*.nc' -o -name '*.tif' \) | wc -l | tr -d ' ') frames on disk"

push-r2-frames: ## Push new frames to the bucket's cache
	@set -e; [ -d $(FRAMES_DIR) ] || { echo "no frame cache at $(FRAMES_DIR)"; exit 0; }; \
	$(S3) sync $(FRAMES_DIR)/ $(FRAMES_PREFIX)/ --size-only --only-show-errors $(DRY_RUN)

# Whole stale days go in one rm each and the cutoff day hour by hour, so a
# never-pruned cache costs a listing per day rather than per hour.
prune-r2-frames: ## Delete cached frames older than FRAMES_KEEP_HOURS
	@set -e; \
	cutoff=$$($(PYTHON) -c "from datetime import datetime, timedelta, UTC; print((datetime.now(UTC) - timedelta(hours=$(FRAMES_KEEP_HOURS))).strftime('%Y%m%d%H'))"); \
	listing=$$($(S3) ls $(FRAMES_PREFIX)/ --recursive) || { echo "listing the frame cache failed, refusing to prune"; exit 1; }; \
	hours=$$(printf '%s\n' "$$listing" | awk '{print $$4}' | sed -n -E 's:.*/[a-z0-9]*_([0-9]{10})[0-9]{4}\.(nc|tif)$$:\1:p' | sort -u); \
	for day in $$(printf '%s\n' "$$hours" | cut -c1-8 | sort -u); do \
		if [ "$$day" \< "$$(printf '%s' "$$cutoff" | cut -c1-8)" ]; then \
			echo "Deleting frames of $$day..."; \
			$(S3) rm $(FRAMES_PREFIX)/ --recursive --exclude "*" --include "*/*_$$day*" --only-show-errors $(DRY_RUN); \
		fi; \
	done; \
	for hour in $$hours; do \
		if [ "$$(printf '%s' "$$hour" | cut -c1-8)" = "$$(printf '%s' "$$cutoff" | cut -c1-8)" ] && [ "$$hour" \< "$$cutoff" ]; then \
			echo "Deleting frames of $$hour..."; \
			$(S3) rm $(FRAMES_PREFIX)/ --recursive --exclude "*" --include "*/*_$$hour*" --only-show-errors $(DRY_RUN); \
		fi; \
	done

# The CAMEL emissivity months the DEBRA producer reads: one NetCDF per region
# and month under <prefix>/<region>/<YYYYMM>.nc, written by `stage-ancillary`
# (xuebuild/satellite/staging.py, Earthdata credentials in ~/.netrc) and put on
# the bucket by `push-r2-ancillary`, which stage-ancillary.yml runs before each
# month's first slot. The rounds only pull. MONTHS (YYYY-MM) and REGIONS are
# space-separated and optional: the default is this month and the next, every
# region.
ANCILLARY_DIR = data/raw/ancillary
CAMEL_PREFIX ?= s3://$(R2_BUCKET)/shachen-ops/ancillary/camel
MONTHS ?=
REGIONS ?=
pull-r2-ancillary: ## Pull the staged CAMEL months for DEBRA
	@set -e; mkdir -p $(ANCILLARY_DIR)/camel; \
	$(S3) sync $(CAMEL_PREFIX)/ $(ANCILLARY_DIR)/camel/ --exclude "*" --include "*/*.nc" --only-show-errors; \
	echo "ancillary: $$(find $(ANCILLARY_DIR)/camel -name '*.nc' | wc -l | tr -d ' ') staged CAMEL months on disk"

stage-ancillary: ## Stage the CAMEL months for DEBRA (MONTHS=, REGIONS= optional)
	$(PYTHON) -m xuebuild.satellite.staging --root $(ANCILLARY_DIR) \
		$(foreach month,$(MONTHS),--month $(month)) $(foreach region,$(REGIONS),--region $(region))

push-r2-ancillary: ## Push the staged CAMEL months to the bucket
	@set -e; [ -d $(ANCILLARY_DIR)/camel ] || { echo "no staged months at $(ANCILLARY_DIR)/camel"; exit 0; }; \
	$(S3) sync $(ANCILLARY_DIR)/camel/ $(CAMEL_PREFIX)/ --exclude "*" --include "*/*.nc" --size-only --only-show-errors $(DRY_RUN)
