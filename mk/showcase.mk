# Historical cases (showcase/cases/*.json → web/public/data/showcase/). CASE
# names one or more; a case is permanent and nothing prunes it.
# CASES_DIR=showcase/cases-local points at scratch definitions.
CASES_DIR ?= showcase/cases

.PHONY: showcase showcase-check showcase-refresh live-showcase-catalog upload-r2-showcase

showcase: ## Build showcase cases (CASE=<id>...)
	$(PYTHON) -m xuebuild showcase build --cases-dir $(CASES_DIR) $(FORCE) $(CASE)

showcase-check: ## Validate case definitions
	$(PYTHON) -m xuebuild showcase check --cases-dir $(CASES_DIR) $(CASE)

# The catalog is collected from what is on disk, so a fresh runner pulls the
# live cases' sidecars first; a one-case catalog would unlist the rest.
live-showcase-catalog: ## Pull the published cases' sidecars and manifests
	@set -e; mkdir -p web/public/data/showcase; \
	$(S3) sync $(R2_ROOT)/showcase/ web/public/data/showcase/ \
		--exclude "*" --include "*/case.json" --include "*/manifest.json" --only-show-errors; \
	echo "live cases: $$(ls web/public/data/showcase/*/case.json 2>/dev/null | wc -l | tr -d ' ')"

showcase-refresh: ## Rewrite built cases' catalog rows from their definitions
	$(PYTHON) -m xuebuild showcase refresh --cases-dir $(CASES_DIR) $(CASE)

# showcase.json is the mutable index and goes last: it makes a new case visible.
upload-r2-showcase: ## Upload built cases, then showcase.json
	@set -e; \
	[ -d web/public/data/showcase ] || { echo "no built cases at web/public/data/showcase"; exit 1; }; \
	$(S3) sync web/public/data/showcase $(R2_ROOT)/showcase/ --no-progress $(DRY_RUN) \
		--exclude "$(STAC_COLLECTION)" --exclude "*/$(STAC_ITEM)" \
		--cache-control "public, max-age=31536000, immutable"; \
	for item in web/public/data/showcase/*/$(STAC_ITEM); do \
		[ -f "$$item" ] || continue; \
		$(S3) cp "$$item" $(R2_ROOT)/showcase/$$(basename $$(dirname "$$item"))/$(STAC_ITEM) --no-progress $(DRY_RUN) \
			--content-type application/geo+json --cache-control "no-cache"; \
	done; \
	echo "Uploading showcase.json (publishes the case list)..."; \
	$(S3) cp web/public/data/showcase.json $(R2_ROOT)/showcase.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"; \
	$(MAKE) --no-print-directory upload-r2-stac-collection MODEL=showcase DRY_RUN=$(DRY_RUN)
