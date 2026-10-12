# The run metrics (docs/metrics.md): one file, metrics/latency.json at the
# data root, rebuilt from the GitHub Actions records of the forecast publish
# workflows. The previous file is the build's state (the workflow runs it
# has already read), so it is pulled first.

# The local mirror of the data root the file builds under.
METRICS_DATA_DIR ?= web/public/data
# GitHub API calls one build may make; GITHUB_TOKEN allows 1,000 an hour.
METRICS_BUDGET ?= 600

.PHONY: live-metrics metrics-latency upload-r2-metrics

live-metrics: ## Pull the live metrics/latency.json the next build extends
	@set -e; mkdir -p $(METRICS_DATA_DIR)/metrics; \
	if $(S3) cp $(R2_ROOT)/metrics/latency.json $(METRICS_DATA_DIR)/metrics/latency.json --only-show-errors 2>/dev/null; then \
		echo "live metrics: $$(wc -c < $(METRICS_DATA_DIR)/metrics/latency.json | tr -d ' ') bytes"; \
	else \
		rm -f $(METRICS_DATA_DIR)/metrics/latency.json; echo "no live metrics; the window starts empty"; \
	fi

metrics-latency: ## Rebuild metrics/latency.json from the Actions API (needs GITHUB_TOKEN)
	$(PYTHON) -m xuebuild metrics-latency --output-dir $(METRICS_DATA_DIR) --budget $(METRICS_BUDGET)

upload-r2-metrics: ## Upload metrics/latency.json
	@set -e; file=$(METRICS_DATA_DIR)/metrics/latency.json; \
	[ -f "$$file" ] || { echo "no built metrics at $$file"; exit 1; }; \
	$(S3) cp "$$file" $(R2_ROOT)/metrics/latency.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"
