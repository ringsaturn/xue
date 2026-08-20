PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
# RUN ?= 2026081506
RUN ?= latest
HOURS ?= 120
FORCE ?=
PROFILE ?= balanced
# Forecast source: gfs (NOAA 0.25°, hourly), ecmwf (IFS open data, 3-hourly),
# or sflux (GFS surface flux, native ~13 km, hourly, adds the dswrf layer).
MODEL ?= gfs
# Each model has its own mutable live pointer; GFS uses the bare
# latest.json, the other models use latest-<model>.json.
LATEST_FILE = $(if $(filter gfs,$(MODEL)),latest.json,latest-$(MODEL).json)

.PHONY: check install wasm test test-rust test-e2e bench bench-video bench-lossy mvp serve deploy-build upload-r2 deploy-pages deploy clean

check:
	$(PYTHON) scripts/check_dependencies.py

install:
	npm ci

wasm:
	cd rust && wasm-pack build xue-wasm --target web --out-dir ../../web/src/wasm --out-name xue

test: test-rust
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
	npm run test:web

test-rust:
	$(PYTHON) tests/prepare_bin_fixture.py
	cd rust && cargo test

test-e2e:
	npm run test:e2e

bench:
	$(PYTHON) scripts/bench_bin.py data/raw/gfs.$(RUN) --output data/work/bench_bin.json

bench-video:
	$(PYTHON) scripts/bench_video.py data/raw/gfs.$(RUN) --output data/work/bench_video.json

# Lossy tier experiment: scientific-metric acceptance
# of an x264 QP / SVT-AV1 CRF ladder over the quantized code planes.
bench-lossy:
	$(PYTHON) scripts/bench_lossy.py data/raw/gfs.$(RUN) --output data/work/bench_lossy.json

# Builds data/work/webcodecs_spike/ (GOP-6 lossless H.264 stream + demux
# index + browser harness); serve it and open index.html to run the
# WebCodecs random-access spike: can the browser's native VideoDecoder
# random-access the lossless H.264 stream byte-exactly?
spike-webcodecs:
	$(PYTHON) scripts/prep_webcodecs_spike.py data/raw/gfs.$(RUN) --frames $(HOURS)

# Default build: per-variable Xue bundles plus the WebGL2 frontend.
# MODEL=ecmwf builds the ECMWF IFS open data feed instead of GFS;
# MODEL=sflux builds the native-resolution GFS surface flux feed.
mvp: check install wasm
	$(PYTHON) -m xue build-bin --model $(MODEL) --run $(RUN) --hours $(HOURS) --profile $(PROFILE) $(FORCE)
	npm run build

serve:
	npm run preview -- --host 127.0.0.1

# Build for Cloudflare Pages: dist-deploy/ carries only the static shell.
# The manifest lives on R2 next to the bundles
# (uploaded by `make upload-r2`), so Pages only needs deploying when the code
# changes — publishing a new run is an R2-only operation.
deploy-build:
	npm run build -- --mode deploy
	rm -rf dist-deploy
	mkdir -p dist-deploy
	cp dist/index.html dist-deploy/index.html
	cp -R dist/assets dist-deploy/assets

# Upload one run to the public R2 dataset bucket that deploy builds read from
# (web/.env.deploy -> VITE_DATA_BASE_URL): the per-variable .xue bundles
# (full and .half.xue resolution tiers), posters, the optional per-variable
# WebCodecs video artifacts (.h264 + index) and their debug .m3u8, plus the
# run's immutable manifest. All run assets are uploaded
# with immutable cache metadata (they are ?v=<crc32>-addressed); the mutable
# latest.json live pointer goes last — uploading it is what takes the run live.
upload-r2:
	@for f in web/public/data/$(MODEL).$(RUN)/*.xue web/public/data/$(MODEL).$(RUN)/*.poster.bin web/public/data/$(MODEL).$(RUN)/*.h264 web/public/data/$(MODEL).$(RUN)/*.h264.index.json web/public/data/$(MODEL).$(RUN)/*.m3u8 web/public/data/$(MODEL).$(RUN)/manifest.json; do \
		[ -e "$$f" ] || continue; \
		name=$$(basename $$f); \
		case "$$name" in \
			*.json) ct="application/json";; \
			*.m3u8) ct="application/vnd.apple.mpegurl";; \
			*) ct="application/octet-stream";; \
		esac; \
		echo "Uploading $(MODEL).$(RUN)/$$name to R2..."; \
		npx wrangler r2 object put dataset/xue/$(MODEL).$(RUN)/$$name --file $$f --remote \
			--content-type $$ct --cache-control "public, max-age=31536000, immutable" || exit 1; \
	done
	@echo "Uploading $(LATEST_FILE) to R2 (takes $(MODEL) run $(RUN) live)..."
	@npx wrangler r2 object put dataset/xue/$(LATEST_FILE) --file web/public/data/$(LATEST_FILE) --remote \
		--content-type application/json --cache-control "no-cache" || exit 1

# Publish dist-deploy/ (built via deploy-build) to the Cloudflare Pages project.
deploy-pages:
	npx wrangler pages deploy --branch main dist-deploy

# Full deploy: publish the static shell first, then push this run's data (and
# the live pointer) to R2. Frontend-first ordering matters when the manifest
# schema widens (e.g. the optional wind10m bundle): the
# new shell accepts old manifests, but an old cached shell rejects new ones.
# For a data-only refresh, `make upload-r2` alone is enough.
deploy: deploy-build deploy-pages upload-r2

clean:
	@echo "Generated data is retained for reuse. Remove dist manually if required."
