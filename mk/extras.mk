# One-off tools: the format spec as PDF, benchmarks and the WebCodecs spike.

.PHONY: format-pdf bench bench-video bench-lossy spike-webcodecs

# Needs pandoc and a TeX Live with xetex.
format-pdf: ## Typeset docs/format.md as docs/format.pdf
	scripts/build_format_pdf.sh

bench:
	$(PYTHON) scripts/bench_bin.py data/raw/gfs.$(RUN) --output data/work/bench_bin.json

bench-video:
	$(PYTHON) scripts/bench_video.py data/raw/gfs.$(RUN) --output data/work/bench_video.json

bench-lossy:
	$(PYTHON) scripts/bench_lossy.py data/raw/gfs.$(RUN) --output data/work/bench_lossy.json

spike-webcodecs:
	$(PYTHON) scripts/prep_webcodecs_spike.py data/raw/gfs.$(RUN) --frames $(or $(HOURS),240)
