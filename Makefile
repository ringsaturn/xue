PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
# RUN ?= 2026081506
RUN ?= latest
# Last forecast hour to build; empty takes the whole axis the model publishes
# (240 for the global models, 18 for HRRR).
HOURS ?=
FORCE ?=
PROFILE ?= balanced
# Forecast source: gfs (NOAA 0.25°, hourly), ecmwf (IFS open data, 3-hourly),
# sflux (GFS surface flux, native ~13 km, hourly, adds the dswrf layer), or
# hrrr (NOAA HRRR, 3 km over the contiguous US, a cycle every hour, to F18).
MODEL ?= gfs
# Published runs of one model to keep on R2 (`make prune-r2`). One means the
# live run only: the bucket carries no history.
KEEP ?= 1
# `--dryrun` to preview an upload or a prune.
DRY_RUN ?=
# Each model has its own mutable live pointer; GFS uses the bare latest.json,
# the other models use latest-<model>.json.
LATEST_FILE = $(if $(filter gfs,$(MODEL)),latest.json,latest-$(MODEL).json)

# R2 is S3-compatible, so the dataset bucket is managed with the AWS CLI
# rather than anything of ours. Needs an R2 API token's key pair in
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY and the account id in
# CLOUDFLARE_ACCOUNT_ID (it is the endpoint host).
R2_BUCKET ?= dataset
R2_PREFIX ?= xue
R2_ENDPOINT ?= https://$(CLOUDFLARE_ACCOUNT_ID).r2.cloudflarestorage.com
AWS ?= aws
S3 = $(AWS) s3 --endpoint-url $(R2_ENDPOINT)
# R2 has no regions, and AWS CLI v2's default checksum headers are not
# accepted by every S3-compatible backend.
AWS_DEFAULT_REGION ?= auto
AWS_REQUEST_CHECKSUM_CALCULATION ?= when_required
AWS_RESPONSE_CHECKSUM_VALIDATION ?= when_required
export AWS_DEFAULT_REGION AWS_REQUEST_CHECKSUM_CALCULATION AWS_RESPONSE_CHECKSUM_VALIDATION

.PHONY: check install wasm test test-rust test-e2e encoder-rust encoder-rust-test encoder-wheel bench bench-video bench-lossy mvp serve format-pdf deploy-build upload-r2 upload-r2-bundles upload-r2-manifest check-pointer upload-r2-pointer warm-r2 prune-r2 live-run live-manifest deploy-pages deploy showcase showcase-check upload-r2-showcase tc-build live-tc-index upload-r2-tc prune-r2-tc clean

check:
	$(PYTHON) scripts/check_dependencies.py

install:
	npm ci

wasm:
	cd rust && wasm-pack build xue-wasm --target web --out-dir ../../web/src/wasm --out-name xue

test: test-rust
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
	npm run test:web

# The decoder and the wasm binding — the workspace's default members, which
# leave out `xue-py` because it enables the xue crate's `encoder` feature and
# so links GDAL. A decode-only check must not need one; `make encoder-rust-test`
# is where the encoder is tested.
test-rust:
	$(PYTHON) tests/prepare_bin_fixture.py
	cd rust && cargo test

test-e2e:
	npm run test:e2e

# The native encoder, behind the xue crate's off-by-default `encoder` feature.
# It links GDAL, so it needs pkg-config pointed at the GDAL install and
# libclang for gdal-sys' bindgen. The `encoder` cargo profile is what keeps it
# at opt-level 3 while the wasm decoder keeps the size-tuned release profile —
# see the comments in rust/Cargo.toml. Not part of `make test`, which needs no
# GDAL headers: what `make test` does cover is the encoder's *output*, through
# the installed xuepy wheel (tests/test_native.py).
ENCODER = cd rust && PKG_CONFIG_PATH="$$(gdal-config --prefix)/lib/pkgconfig" cargo
ENCODER_ARGS = --profile encoder --features encoder

encoder-rust:
	$(ENCODER) build $(ENCODER_ARGS) --bin xue-encode

# Regenerates the Python-encoded fixture first: the golden test demands the
# exact bytes the reference encoder wrote for it.
encoder-rust-test:
	$(PYTHON) tests/prepare_bin_fixture.py
	$(ENCODER) test -p xue $(ENCODER_ARGS)

# A self-contained wheel: builds a minimal GDAL (GRIB and netCDF drivers only)
# from source on first run, then bundles it, GDAL's and PROJ's data
# directories, and every licence text alongside the extension module.
encoder-wheel:
	./scripts/build-encoder-wheel.sh

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
	$(PYTHON) scripts/prep_webcodecs_spike.py data/raw/gfs.$(RUN) --frames $(or $(HOURS),240)

# Default build: per-variable Xue bundles plus the WebGL2 frontend.
# MODEL=ecmwf builds the ECMWF IFS open data feed instead of GFS;
# MODEL=sflux builds the native-resolution GFS surface flux feed;
# MODEL=hrrr builds the hourly 3 km HRRR feed over the contiguous US.
mvp: check install wasm
	$(PYTHON) -m xuebuild build-bin --model $(MODEL) --run $(RUN) $(if $(HOURS),--hours $(HOURS)) --profile $(PROFILE) $(FORCE)
	npm run build

serve:
	npm run preview -- --host 127.0.0.1

# Build for Cloudflare Pages: dist-deploy/ carries only the static shell.
# The manifest lives on R2 next to the bundles
# (uploaded by `make upload-r2`), so Pages only needs deploying when the code
# changes — publishing a new run is an R2-only operation.
# Typeset the normative format spec as docs/format.pdf.
# Needs pandoc and a TeX Live with xetex; on Debian/Ubuntu:
#   apt-get install pandoc texlive-xetex texlive-fonts-recommended \
#     texlive-latex-recommended lmodern
format-pdf:
	scripts/build_format_pdf.sh

# Everything vite emits except data/ — the shell, its hashed assets, and the
# favicon / social-card images the <head> links by root-absolute path.
deploy-build:
	npm run build -- --mode deploy
	rm -rf dist-deploy
	mkdir -p dist-deploy
	rsync -a --exclude 'data/' dist/ dist-deploy/

# Upload one run to the public R2 dataset bucket that deploy builds read from
# (web/.env.deploy -> VITE_DATA_BASE_URL): the per-variable .xue bundles
# (full and .half.xue resolution tiers), posters, the optional WebCodecs
# video artifacts, and the run's manifest. Run assets are immutable (clients
# address them as ?v=<crc32>); the mutable per-model live pointer is copied
# afterwards, and uploading it is what takes the run live. Between the two,
# `warm-r2` pulls every artifact through the CDN so the first viewer of the
# new run finds it at the edge rather than waiting on the fill from R2; a
# warm-up failure is reported but never holds the pointer back.
# Pass a concrete RUN=YYYYMMDDHH.
#
# This is the whole-run path for a run built in one go. The scheduled publish
# builds a run in pieces — one job per bundle group, each syncing what it
# built with `upload-r2-bundles`, then one job assembling the manifest and
# taking the run live with `upload-r2-manifest` — and the three targets share
# their pieces: a partial manifest (manifest.part.*.json, the assembler's
# input) never reaches the bucket from any of them.
upload-r2:
	@set -e; dir=web/public/data/$(MODEL).$(RUN); \
	[ -d "$$dir" ] || { echo "no built run at $$dir, pass RUN=YYYYMMDDHH"; exit 1; }; \
	$(MAKE) --no-print-directory check-pointer MODEL=$(MODEL) RUN=$(RUN); \
	$(S3) sync $$dir s3://$(R2_BUCKET)/$(R2_PREFIX)/$(MODEL).$(RUN)/ --no-progress $(DRY_RUN) \
		--exclude "manifest.part.*.json" \
		--cache-control "public, max-age=31536000, immutable"; \
	[ -n "$(DRY_RUN)" ] || $(MAKE) --no-print-directory warm-r2 MODEL=$(MODEL) RUN=$(RUN) \
		|| echo "warming the edge cache failed; the run goes live cold"; \
	$(MAKE) --no-print-directory upload-r2-pointer MODEL=$(MODEL) RUN=$(RUN) DRY_RUN=$(DRY_RUN)

# One piece of a fanned-out publish: sync whatever bundles, variants, posters
# and companions this machine built into the run directory (no manifest —
# the assembler writes that, and a part stays local), then warm exactly
# those artifacts, listed by the partial manifests present. The run is not
# live and nothing references these objects until the pointer flips, so a
# piece that never gets assembled is an orphan the next prune deletes.
upload-r2-bundles:
	@set -e; dir=web/public/data/$(MODEL).$(RUN); \
	[ -d "$$dir" ] || { echo "no built bundles at $$dir, pass RUN=YYYYMMDDHH"; exit 1; }; \
	ls $$dir/manifest.part.*.json > /dev/null 2>&1 || { echo "no manifest.part.*.json in $$dir: build with --bundles"; exit 1; }; \
	$(S3) sync $$dir s3://$(R2_BUCKET)/$(R2_PREFIX)/$(MODEL).$(RUN)/ --no-progress $(DRY_RUN) \
		--exclude "manifest.json" --exclude "manifest.part.*.json" \
		--cache-control "public, max-age=31536000, immutable"; \
	[ -n "$(DRY_RUN)" ] || for part in $$dir/manifest.part.*.json; do \
		scripts/warm_edge_cache.sh $(MODEL) $(RUN) --artifacts-of "$$part" \
			|| echo "warming the edge cache for $$part failed; those artifacts go live cold"; \
	done

# The last piece: the assembled manifest (immutable like the bundles it
# names), warmed on its own, then the pointer that takes the run live.
upload-r2-manifest:
	@set -e; dir=web/public/data/$(MODEL).$(RUN); \
	[ -f "$$dir/manifest.json" ] || { echo "no manifest at $$dir/manifest.json: run assemble-run first"; exit 1; }; \
	$(MAKE) --no-print-directory check-pointer MODEL=$(MODEL) RUN=$(RUN); \
	$(S3) cp $$dir/manifest.json s3://$(R2_BUCKET)/$(R2_PREFIX)/$(MODEL).$(RUN)/manifest.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "public, max-age=31536000, immutable"; \
	[ -n "$(DRY_RUN)" ] || scripts/warm_edge_cache.sh $(MODEL) $(RUN) --manifest-only \
		|| echo "warming the manifest failed; the run goes live cold"; \
	$(MAKE) --no-print-directory upload-r2-pointer MODEL=$(MODEL) RUN=$(RUN) DRY_RUN=$(DRY_RUN)

# The pointer must name the run being uploaded and carry the CRC32 of the
# manifest on disk — a later build, or an assemble of other parts, rewrites
# both, and a pointer that disagrees with its manifest strands every viewer
# on a 404.
check-pointer:
	@set -e; dir=web/public/data/$(MODEL).$(RUN); \
	pointer_run=$$(jq -r .run web/public/data/$(LATEST_FILE)); \
	[ "$$pointer_run" = "$(RUN)" ] || { \
		echo "$(LATEST_FILE) names run $$pointer_run, not $(RUN) — a later build rewrote it;"; \
		echo "rebuild run $(RUN) (or upload run $$pointer_run) so the pointer matches the assets"; \
		exit 1; }; \
	pointer_crc=$$(jq -r .manifestCrc32 web/public/data/$(LATEST_FILE)); \
	manifest_crc=$$($(PYTHON) -c "import sys, zlib; print(f'{zlib.crc32(open(sys.argv[1], \"rb\").read()) & 0xFFFFFFFF:08x}')" $$dir/manifest.json); \
	[ "$$pointer_crc" = "$$manifest_crc" ] || { \
		echo "$(LATEST_FILE) carries manifest CRC32 $$pointer_crc but $$dir/manifest.json is $$manifest_crc;"; \
		echo "rebuild or reassemble run $(RUN) so the pointer matches the manifest"; \
		exit 1; }

upload-r2-pointer:
	@echo "Uploading $(LATEST_FILE) (takes $(MODEL) run $(RUN) live)..."; \
	$(S3) cp web/public/data/$(LATEST_FILE) s3://$(R2_BUCKET)/$(R2_PREFIX)/$(LATEST_FILE) --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"

# GET every artifact of one uploaded run through the public hostname, with
# the site's Origin header, so the edge (and, with tiered cache, the upper
# tier every other data center fills from) holds it before anyone asks.
warm-r2:
	scripts/warm_edge_cache.sh $(MODEL) $(RUN)

# Historical showcase cases: past runs cropped to one weather event, defined
# in showcase/cases/*.json and built into web/public/data/showcase/. Pass
# CASE=<id> (or a space-separated list) to build only some; the whole set is
# a lot of archived GRIB to pull, so building one at a time is the norm.
# Unlike a run, a case is permanent — nothing prunes it.
# CASES_DIR=showcase/cases-local points at scratch definitions kept out of
# the curated, checked-in set.
CASES_DIR ?= showcase/cases

showcase:
	$(PYTHON) -m xuebuild showcase build --cases-dir $(CASES_DIR) $(FORCE) $(CASE)

showcase-check:
	$(PYTHON) -m xuebuild showcase check --cases-dir $(CASES_DIR) $(CASE)

# Push the built cases and the catalog to R2. Cases are immutable and
# ?v=<crc32>-addressed like run assets; showcase.json is the mutable index and
# is copied last, which is what makes a new case visible.
upload-r2-showcase:
	@set -e; \
	[ -d web/public/data/showcase ] || { echo "no built cases at web/public/data/showcase"; exit 1; }; \
	$(S3) sync web/public/data/showcase s3://$(R2_BUCKET)/$(R2_PREFIX)/showcase/ --no-progress $(DRY_RUN) \
		--cache-control "public, max-age=31536000, immutable"; \
	echo "Uploading showcase.json (publishes the case list)..."; \
	$(S3) cp web/public/data/showcase.json s3://$(R2_BUCKET)/$(R2_PREFIX)/showcase.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"

# The tropical cyclone product (docs/tc.md): agency and model tracks
# aggregated once an hour into web/public/data/tc.<issue>/ and the mutable
# latest-tc.json beside the run pointers. ISSUE=YYYYMMDDHH names the hour
# (default: this one). The previous hour's index is what keeps an
# unnumbered system's id stable, so a publish fetches the live one first.
ISSUE ?= now
TC_KEEP ?= 720

tc-build:
	$(PYTHON) -m xuebuild tc-build --issue $(ISSUE) $(FORCE)

# The live product's pointer and index, into web/public/data/ where
# tc-build looks for the previous hour. Prints nothing and writes nothing
# when there is no live product yet (the first publish).
live-tc-index:
	@set -e; mkdir -p web/public/data; \
	pointer=$$($(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/latest-tc.json - --only-show-errors 2>/dev/null || true); \
	[ -n "$$pointer" ] || { echo "no live tc pointer"; exit 0; }; \
	path=$$(printf '%s' "$$pointer" | jq -r .path); \
	mkdir -p "web/public/data/$$(dirname "$$path")"; \
	$(S3) cp "s3://$(R2_BUCKET)/$(R2_PREFIX)/$$path" "web/public/data/$$path" --only-show-errors; \
	printf '%s' "$$pointer" > web/public/data/latest-tc.json; \
	echo "live tc issue: $$path"

# Push one issue's directory (immutable, ?v=<crc32>-addressed like a run)
# and then the pointer that takes it live. The pointer on disk must name
# the issue being uploaded and carry its index's CRC32, the way
# check-pointer holds a run's.
upload-r2-tc:
	@set -e; \
	[ "$(ISSUE)" != "now" ] || { echo "pass ISSUE=YYYYMMDDHH"; exit 1; }; \
	dir=web/public/data/tc.$(ISSUE); \
	[ -f "$$dir/index.json" ] || { echo "no built tc issue at $$dir"; exit 1; }; \
	[ -f web/public/data/latest-tc.json ] || { echo "no latest-tc.json: the build withheld the pointer (no source contributed)"; exit 1; }; \
	pointer_path=$$(jq -r .path web/public/data/latest-tc.json); \
	[ "$$pointer_path" = "tc.$(ISSUE)/index.json" ] || { echo "latest-tc.json names $$pointer_path, not tc.$(ISSUE)"; exit 1; }; \
	pointer_crc=$$(jq -r .crc32 web/public/data/latest-tc.json); \
	index_crc=$$($(PYTHON) -c "import sys, zlib; print(f'{zlib.crc32(open(sys.argv[1], \"rb\").read()) & 0xFFFFFFFF:08x}')" $$dir/index.json); \
	[ "$$pointer_crc" = "$$index_crc" ] || { echo "latest-tc.json carries CRC32 $$pointer_crc but $$dir/index.json is $$index_crc"; exit 1; }; \
	$(S3) sync $$dir s3://$(R2_BUCKET)/$(R2_PREFIX)/tc.$(ISSUE)/ --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "public, max-age=31536000, immutable"; \
	echo "Uploading latest-tc.json (takes tc issue $(ISSUE) live)..."; \
	$(S3) cp web/public/data/latest-tc.json s3://$(R2_BUCKET)/$(R2_PREFIX)/latest-tc.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"

# Delete tc issue directories beyond the newest TC_KEEP (720 hours = 30
# days) and never the one the live pointer names.
prune-r2-tc:
	@set -e; \
	live=$$($(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/latest-tc.json - --only-show-errors | jq -r .path | cut -d/ -f1); \
	[ -n "$$live" ] || { echo "no live tc pointer, refusing to prune"; exit 1; }; \
	echo "live tc issue: $$live"; \
	listing=$$($(S3) ls s3://$(R2_BUCKET)/$(R2_PREFIX)/) \
		|| { echo "listing the bucket failed, refusing to prune"; exit 1; }; \
	for issue in $$(printf '%s\n' "$$listing" | awk '/ PRE /{print $$2}' \
		| sed 's:/$$::' | grep "^tc\." | sort -r | tail -n +$$(($(TC_KEEP) + 1))); do \
		if [ "$$issue" != "$$live" ]; then \
			echo "Deleting $$issue..."; \
			$(S3) rm s3://$(R2_BUCKET)/$(R2_PREFIX)/$$issue/ --recursive --only-show-errors $(DRY_RUN); \
		fi; \
	done

# Delete every published run of one model except the newest KEEP and the one
# the live pointer names, so a new run retires the one it replaces. Listing
# the bucket also catches leftovers from an interrupted upload.
# DRY_RUN=--dryrun previews the deletions.
prune-r2:
	@set -e; \
	live=$$($(MAKE) -s --no-print-directory live-run MODEL=$(MODEL)); \
	[ -n "$$live" ] || { echo "no live pointer for $(MODEL), refusing to prune"; exit 1; }; \
	echo "live $(MODEL) run: $$live"; \
	listing=$$($(S3) ls s3://$(R2_BUCKET)/$(R2_PREFIX)/) \
		|| { echo "listing the bucket failed, refusing to prune"; exit 1; }; \
	for run in $$(printf '%s\n' "$$listing" | awk '/ PRE /{print $$2}' \
		| sed 's:/$$::' | grep "^$(MODEL)\." | sort -r | tail -n +$$(($(KEEP) + 1))); do \
		if [ "$$run" != "$(MODEL).$$live" ]; then \
			echo "Deleting $$run..."; \
			$(S3) rm s3://$(R2_BUCKET)/$(R2_PREFIX)/$$run/ --recursive --only-show-errors $(DRY_RUN); \
		fi; \
	done

# Print the run the live pointer names, or nothing when there is no pointer.
live-run:
	@$(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/$(LATEST_FILE) - --only-show-errors | jq -r .run || true

# The manifest of the run the pointer names, as the bucket holds it — the
# base a top-up merges new bundles onto (`xuebuild assemble-run
# --base-manifest`). Prints nothing when there is no live run.
live-manifest:
	@set -e; live=$$($(MAKE) -s --no-print-directory live-run MODEL=$(MODEL)); \
	[ -n "$$live" ] || exit 0; \
	$(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/$(MODEL).$$live/manifest.json - --only-show-errors

# Publish dist-deploy/ (built via deploy-build) to the Cloudflare Pages project.
deploy-pages:
	npx wrangler pages deploy --branch main dist-deploy

# Deploy the static shell. Data publishing is separate — the scheduled
# workflows own it, and a manual push is `make upload-r2 RUN=YYYYMMDDHH`.
# When the manifest schema widens (e.g. the optional wind10m bundle), deploy
# the shell before publishing data in the new schema: the new shell accepts
# old manifests, but an old cached shell rejects new ones.
deploy: deploy-build deploy-pages

clean:
	@echo "Generated data is retained for reuse. Remove dist manually if required."
