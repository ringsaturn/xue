PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
# RUN ?= 2026081506
RUN ?= latest
# Last forecast hour to build; empty takes the whole axis the model publishes
# (240 for the global models, 18 for HRRR).
HOURS ?=
FORCE ?=
PROFILE ?= balanced
# Forecast source: gfs (NOAA 0.25°, hourly), ecmwf (IFS open data, 3-hourly),
# aifs (ECMWF AIFS Single open data, 6-hourly to F360),
# sflux (GFS surface flux, native ~13 km, hourly, adds the dswrf layer), or
# hrrr (NOAA HRRR, 3 km over the contiguous US, a cycle every hour, to F18);
# mrms (the NOAA radar mosaic, an observation every two minutes), jma
# (the JMA precipitation nowcast over Japan, every five minutes, through the
# jma-radar tool), cma (the CMA radar mosaic over China, every six
# minutes, read out of its private daily Zarr archive), himawari (the
# Himawari-9 10.4 µm infrared window and Dust RGB, every ten minutes, warped
# from NOAA's ISatSS tiles by the fetch stage), goeseast and goeswest (the
# same from GOES-19 and GOES-18, NOAA's CMIPF files) and meteosat (the same
# from Meteosat-12's FCI chunks on the EUMETSAT Data Store, the cycle on
# each hour alone) build a window named by its first hour — `RUN=latest`
# the live rolling window.
MODEL ?= gfs
# One round of a rolling window (`build-bin --round`): the run's artifacts
# and manifest live in <model>.<run>/<ROUND>/ and the pointer names that
# round, so a later round of the same run never overwrites what a viewer is
# still reading (a range request against a rewritten object decodes the
# wrong bytes). Empty for a forecast run, which is built once.
ROUND ?=
RUN_DIR = $(MODEL).$(RUN)$(if $(ROUND),/$(ROUND))
# Published runs of one model to keep on R2 (`make prune-r2`). One means the
# live run only: the bucket carries no history.
KEEP ?= 1
# `--dryrun` to preview an upload or a prune.
DRY_RUN ?=
# WARM=false skips the edge-cache warm-up between the assets and the pointer
# (a rolling window's round is a handful of small objects, replaced minutes
# later; its first viewer fills the edge at little cost).
WARM ?= true
# Each model has its own mutable live pointer; GFS uses the bare latest.json,
# the other models use latest-<model>.json.
LATEST_FILE = $(if $(filter gfs,$(MODEL)),latest.json,latest-$(MODEL).json)
# The STAC catalog derived beside those (docs/stac.md): a root catalog at
# the data root, one Collection per model under <model>/ with the live
# Item beside it, one Item per run beside its manifest. Not read by the
# shell; for STAC clients.
STAC_CATALOG = catalog.json
STAC_COLLECTION = collection.json
STAC_ITEM = item.json
# Which catalog directory `upload-r2-stac-collection` pushes: a model's by
# default, `showcase`, or one of the point products (docs/stac.md
# "Point products"), each of which holds a collection.json and the live
# item.json beside it.
STAC_DIR ?= $(MODEL)

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

.PHONY: check install wasm test test-rust test-e2e encoder-rust encoder-rust-test encoder-wheel bench bench-video bench-lossy mvp serve format-pdf deploy-build upload-r2 upload-r2-bundles upload-r2-manifest upload-r2-stac-item check-pointer upload-r2-pointer upload-r2-stac-collection warm-r2 prune-r2 prune-r2-rounds live-run live-manifest live-window pull-r2-frames push-r2-frames prune-r2-frames deploy-pages deploy showcase showcase-check showcase-refresh live-showcase-catalog upload-r2-showcase tc-build live-tc-index upload-r2-tc prune-r2-tc airport-build live-airport-index upload-r2-airport prune-r2-airport sounding-build live-sounding-index upload-r2-sounding prune-r2-sounding clean

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
# MODEL=aifs builds the ECMWF AIFS Single open data feed;
# MODEL=sflux builds the native-resolution GFS surface flux feed;
# MODEL=hrrr builds the hourly 3 km HRRR feed over the contiguous US.
mvp: check install wasm
	$(PYTHON) -m xuebuild build-bin --model $(MODEL) --run $(RUN) $(if $(HOURS),--hours $(HOURS)) --profile $(PROFILE) --zarr $(FORCE)
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
# warm-up failure is reported but never holds the pointer back, and
# WARM=false leaves it out. Pass a concrete RUN=YYYYMMDDHH.
#
# This is the whole-run path for a run built in one go. The scheduled publish
# builds a run in pieces — one job per bundle group, each syncing what it
# built with `upload-r2-bundles`, then one job assembling the manifest and
# taking the run live with `upload-r2-manifest` — and the three targets share
# their pieces: a partial manifest (manifest.part.*.json, the assembler's
# input) never reaches the bucket from any of them.
#
# A rolling window's round (ROUND=HHMM) is the same path one directory
# deeper: the round's artifacts and manifest are synced into
# <model>.<run>/<ROUND>/ and the pointer names the round.
upload-r2:
	@set -e; dir=web/public/data/$(RUN_DIR); \
	[ -d "$$dir" ] || { echo "no built run at $$dir, pass RUN=YYYYMMDDHH"; exit 1; }; \
	$(MAKE) --no-print-directory check-pointer MODEL=$(MODEL) RUN=$(RUN) ROUND=$(ROUND); \
	$(S3) sync $$dir s3://$(R2_BUCKET)/$(R2_PREFIX)/$(RUN_DIR)/ --no-progress $(DRY_RUN) \
		--exclude "manifest.part.*.json" --exclude "$(STAC_ITEM)" \
		--cache-control "public, max-age=31536000, immutable"; \
	$(MAKE) --no-print-directory upload-r2-stac-item MODEL=$(MODEL) RUN=$(RUN) ROUND=$(ROUND) DRY_RUN=$(DRY_RUN); \
	[ -n "$(DRY_RUN)" ] || [ "$(WARM)" != true ] || $(MAKE) --no-print-directory warm-r2 MODEL=$(MODEL) RUN=$(RUN) ROUND=$(ROUND) \
		|| echo "warming the edge cache failed; the run goes live cold"; \
	$(MAKE) --no-print-directory upload-r2-pointer MODEL=$(MODEL) RUN=$(RUN) DRY_RUN=$(DRY_RUN)

# One piece of a fanned-out publish: copy whatever bundles, variants, posters
# and companions this machine built into the run directory (no manifest —
# the assembler writes that, and a part stays local), then warm exactly
# those artifacts, listed by the partial manifests present. The run is not
# live and nothing references these objects until the pointer flips, so a
# piece that never gets assembled is an orphan the next prune deletes.
#
# `cp --recursive`, not `sync`: a sync lists the destination prefix to
# decide what to skip, and with every piece of a run doing that at once
# over a prefix of a few hundred store objects, R2 answered one of them
# `ServiceUnavailable: reduce your concurrent request rate for the same
# object` and the piece failed. A piece has nothing to skip — its objects
# are its own, immutable, and written once — so a plain copy is the same
# upload without the listing. Three attempts, since a transient refusal of
# one object out of hundreds should not cost the whole publish.
upload-r2-bundles:
	@set -e; dir=web/public/data/$(MODEL).$(RUN); \
	[ -d "$$dir" ] || { echo "no built bundles at $$dir, pass RUN=YYYYMMDDHH"; exit 1; }; \
	ls $$dir/manifest.part.*.json > /dev/null 2>&1 || { echo "no manifest.part.*.json in $$dir: build with --bundles"; exit 1; }; \
	for attempt in 1 2 3; do \
		$(S3) cp $$dir s3://$(R2_BUCKET)/$(R2_PREFIX)/$(MODEL).$(RUN)/ --recursive --no-progress $(DRY_RUN) \
			--exclude "manifest.json" --exclude "manifest.part.*.json" \
			--cache-control "public, max-age=31536000, immutable" && break; \
		[ "$$attempt" -lt 3 ] || { echo "uploading the bundles failed three times"; exit 1; }; \
		echo "upload attempt $$attempt failed; retrying in $$((attempt * 20)) s"; sleep $$((attempt * 20)); \
	done; \
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
	$(MAKE) --no-print-directory upload-r2-stac-item MODEL=$(MODEL) RUN=$(RUN) DRY_RUN=$(DRY_RUN); \
	[ -n "$(DRY_RUN)" ] || scripts/warm_edge_cache.sh $(MODEL) $(RUN) --manifest-only \
		|| echo "warming the manifest failed; the run goes live cold"; \
	$(MAKE) --no-print-directory upload-r2-pointer MODEL=$(MODEL) RUN=$(RUN) DRY_RUN=$(DRY_RUN)

# The run's STAC Item (docs/stac.md), beside its manifest. It is not
# ?v=-addressed — a STAC client reads it by its plain name, and a top-up
# rewrites it in place with the manifest — so unlike the artifacts it is
# served revalidated rather than immutable. Written by build-bin and
# assemble-run; a run directory built before them has none, and that is not
# an error.
upload-r2-stac-item:
	@set -e; dir=web/public/data/$(RUN_DIR); \
	[ -f "$$dir/$(STAC_ITEM)" ] || { echo "no $(STAC_ITEM) in $$dir; the run predates the catalog"; exit 0; }; \
	$(S3) cp $$dir/$(STAC_ITEM) s3://$(R2_BUCKET)/$(R2_PREFIX)/$(RUN_DIR)/$(STAC_ITEM) --no-progress $(DRY_RUN) \
		--content-type application/geo+json --cache-control "no-cache"

# The pointer must name the run being uploaded and carry the CRC32 of the
# manifest on disk — a later build, or an assemble of other parts, rewrites
# both, and a pointer that disagrees with its manifest strands every viewer
# on a 404.
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
	manifest_crc=$$($(PYTHON) -c "import sys, zlib; print(f'{zlib.crc32(open(sys.argv[1], \"rb\").read()) & 0xFFFFFFFF:08x}')" $$dir/manifest.json); \
	[ "$$pointer_crc" = "$$manifest_crc" ] || { \
		echo "$(LATEST_FILE) carries manifest CRC32 $$pointer_crc but $$dir/manifest.json is $$manifest_crc;"; \
		echo "rebuild or reassemble run $(RUN) so the pointer matches the manifest"; \
		exit 1; }

upload-r2-pointer:
	@echo "Uploading $(LATEST_FILE) (takes $(MODEL) run $(RUN) live)..."; \
	$(S3) cp web/public/data/$(LATEST_FILE) s3://$(R2_BUCKET)/$(R2_PREFIX)/$(LATEST_FILE) --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache" \
	&& $(MAKE) --no-print-directory upload-r2-stac-collection MODEL=$(MODEL) DRY_RUN=$(DRY_RUN)

# The pointer's STAC face, for a source, the showcase or a point product
# (STAC_DIR): the live Item at the directory's stable path
# (the run's Item relocated, so a bookmark outlives the run), the source's
# Collection, whose `item` and `latest-version` links name it, and the root
# catalog listing every source. All mutable like the pointer, so all
# no-cache; uploaded right after it, since a Collection that points at a
# run the pointer does not is the one inconsistency a client could see.
# The Item goes first: a Collection must never name a live Item that is
# not there yet.
upload-r2-stac-collection:
	@set -e; \
	[ -f web/public/data/$(STAC_DIR)/$(STAC_COLLECTION) ] || { echo "no $(STAC_DIR)/$(STAC_COLLECTION); nothing built the catalog"; exit 0; }; \
	echo "Uploading $(STAC_DIR)/$(STAC_ITEM), $(STAC_DIR)/$(STAC_COLLECTION) and $(STAC_CATALOG)..."; \
	[ ! -f web/public/data/$(STAC_DIR)/$(STAC_ITEM) ] || \
	$(S3) cp web/public/data/$(STAC_DIR)/$(STAC_ITEM) s3://$(R2_BUCKET)/$(R2_PREFIX)/$(STAC_DIR)/$(STAC_ITEM) --no-progress $(DRY_RUN) \
		--content-type application/geo+json --cache-control "no-cache"; \
	$(S3) cp web/public/data/$(STAC_DIR)/$(STAC_COLLECTION) s3://$(R2_BUCKET)/$(R2_PREFIX)/$(STAC_DIR)/$(STAC_COLLECTION) --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"; \
	$(S3) cp web/public/data/$(STAC_CATALOG) s3://$(R2_BUCKET)/$(R2_PREFIX)/$(STAC_CATALOG) --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"

# GET every artifact of one uploaded run through the public hostname, with
# the site's Origin header, so the edge (and, with tiered cache, the upper
# tier every other data center fills from) holds it before anyone asks.
warm-r2:
	ROUND=$(ROUND) scripts/warm_edge_cache.sh $(MODEL) $(RUN)

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

# The published cases' sidecars and manifests (two small objects each), so a
# build on a fresh runner regenerates showcase.json from every case that is
# live rather than from the one it just built: the catalog is collected
# from what is on disk (xuebuild/showcase.py::collect_catalog), and an
# upload of a one-case catalog would unlist the rest.
live-showcase-catalog:
	@set -e; mkdir -p web/public/data/showcase; \
	$(S3) sync s3://$(R2_BUCKET)/$(R2_PREFIX)/showcase/ web/public/data/showcase/ \
		--exclude "*" --include "*/case.json" --include "*/manifest.json" --only-show-errors; \
	echo "live cases: $$(ls web/public/data/showcase/*/case.json 2>/dev/null | wc -l | tr -d ' ')"

# A built case's catalog row (title, summary, tags, credit) rewritten from
# its definition without a rebuild — a translation or a corrected summary
# reaches showcase.json without refetching the run.
showcase-refresh:
	$(PYTHON) -m xuebuild showcase refresh --cases-dir $(CASES_DIR) $(CASE)

# Push the built cases and the catalog to R2. Cases are immutable and
# ?v=<crc32>-addressed like run assets; showcase.json is the mutable index and
# is copied last, which is what makes a new case visible.
upload-r2-showcase:
	@set -e; \
	[ -d web/public/data/showcase ] || { echo "no built cases at web/public/data/showcase"; exit 1; }; \
	$(S3) sync web/public/data/showcase s3://$(R2_BUCKET)/$(R2_PREFIX)/showcase/ --no-progress $(DRY_RUN) \
		--exclude "$(STAC_COLLECTION)" --exclude "*/$(STAC_ITEM)" \
		--cache-control "public, max-age=31536000, immutable"; \
	for item in web/public/data/showcase/*/$(STAC_ITEM); do \
		[ -f "$$item" ] || continue; \
		$(S3) cp "$$item" s3://$(R2_BUCKET)/$(R2_PREFIX)/showcase/$$(basename $$(dirname "$$item"))/$(STAC_ITEM) --no-progress $(DRY_RUN) \
			--content-type application/geo+json --cache-control "no-cache"; \
	done; \
	echo "Uploading showcase.json (publishes the case list)..."; \
	$(S3) cp web/public/data/showcase.json s3://$(R2_BUCKET)/$(R2_PREFIX)/showcase.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"; \
	$(MAKE) --no-print-directory upload-r2-stac-collection MODEL=showcase DRY_RUN=$(DRY_RUN)

# The tropical cyclone product (docs/tc.md): agency and model tracks
# aggregated once an hour into web/public/data/tc.<issue>/ and the mutable
# latest-tc.json beside the run pointers. ISSUE=YYYYMMDDHH names the hour
# (default: this one). The previous hour's index is what keeps an
# unnumbered system's id stable, so a publish fetches the live one first.
ISSUE ?= now
TC_KEEP ?= 48

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
	$(S3) sync $$dir s3://$(R2_BUCKET)/$(R2_PREFIX)/tc.$(ISSUE)/ --no-progress $(DRY_RUN) --exclude "$(STAC_ITEM)" \
		--content-type application/json --cache-control "public, max-age=31536000, immutable"; \
	[ ! -f "$$dir/$(STAC_ITEM)" ] || $(S3) cp $$dir/$(STAC_ITEM) s3://$(R2_BUCKET)/$(R2_PREFIX)/tc.$(ISSUE)/$(STAC_ITEM) \
		--no-progress $(DRY_RUN) --content-type application/geo+json --cache-control "no-cache"; \
	echo "Uploading latest-tc.json (takes tc issue $(ISSUE) live)..."; \
	$(S3) cp web/public/data/latest-tc.json s3://$(R2_BUCKET)/$(R2_PREFIX)/latest-tc.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"; \
	$(MAKE) --no-print-directory upload-r2-stac-collection STAC_DIR=tc DRY_RUN=$(DRY_RUN)

# Delete tc issue directories beyond the newest TC_KEEP (48 hours = two
# days; nothing reads an older issue — the shell and the next build both
# start from the live pointer) and never the one the live pointer names.
# No pointer yet (before the first publish, or a dry run of it) means
# nothing is live to protect and nothing to prune, not a reason to fail.
prune-r2-tc:
	@set -e; \
	live=$$($(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/latest-tc.json - --only-show-errors 2>/dev/null | jq -r .path | cut -d/ -f1 || true); \
	[ -n "$$live" ] || { echo "no live tc pointer, nothing to prune"; exit 0; }; \
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

# The airport METAR / TAF product (docs/airport.md): the Aviation Weather
# Center's decoded observations and forecasts, merged every ten minutes into
# web/public/data/airport.<round>/ — the index and the history file it spans
# — and taken live by the mutable latest-airport.json beside the run
# pointers. The round is a whole YYYYMMDDHHMM; the rolling window's ROUND
# above is an HHMM inside a run, and the two share the variable, so an empty
# ROUND here means the current round.
AIRPORT_ROUND = $(if $(ROUND),$(ROUND),now)
AIRPORT_KEEP ?= 18

airport-build:
	$(PYTHON) -m xuebuild airport-build --round $(AIRPORT_ROUND) $(FORCE)

# The live product's pointer, index and history file, into web/public/data/
# where airport-build looks for the previous round: the history file is this
# round's history, so a publish without it would start every station's 24
# hours again — and a truncated one would publish a hole, which is why the
# CRC32 the index carries is checked here rather than trusted. Three
# objects. Prints nothing and writes nothing when there is no live product
# yet (the first publish).
live-airport-index:
	@set -e; mkdir -p web/public/data; \
	pointer=$$($(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/latest-airport.json - --only-show-errors 2>/dev/null || true); \
	[ -n "$$pointer" ] || { echo "no live airport pointer"; exit 0; }; \
	path=$$(printf '%s' "$$pointer" | jq -r .path); \
	directory=$$(dirname "$$path"); \
	mkdir -p "web/public/data/$$directory"; \
	$(S3) cp "s3://$(R2_BUCKET)/$(R2_PREFIX)/$$path" "web/public/data/$$path" --only-show-errors; \
	printf '%s' "$$pointer" > web/public/data/latest-airport.json; \
	history=$$(jq -r .history.path "web/public/data/$$path"); \
	$(S3) cp "s3://$(R2_BUCKET)/$(R2_PREFIX)/$$directory/$$history" "web/public/data/$$directory/$$history" \
		--no-progress --only-show-errors; \
	crc=$$($(PYTHON) -c "import sys, zlib; print(f'{zlib.crc32(open(sys.argv[1], \"rb\").read()) & 0xFFFFFFFF:08x}')" "web/public/data/$$directory/$$history"); \
	named=$$(jq -r .history.crc32 "web/public/data/$$path"); \
	[ "$$crc" = "$$named" ] || { echo "the live $$history is CRC32 $$crc but its index says $$named"; exit 1; }; \
	stations=$$(jq -r '.stations | length' "web/public/data/$$path"); \
	bytes=$$(jq -r .history.byteLength "web/public/data/$$path"); \
	echo "live airport round: $$path ($$stations stations, $$bytes bytes of history)"

# Push the round's history file, then the index that spans it, then the
# pointer that takes the round live — each object before the one that names
# it, all immutable and ?v=<crc32>-addressed like a run's artifacts. The
# pointer on disk must name the round being uploaded and carry its index's
# CRC32, the way check-pointer holds a run's.
upload-r2-airport:
	@set -e; \
	[ "$(AIRPORT_ROUND)" != "now" ] || { echo "pass ROUND=YYYYMMDDHHMM"; exit 1; }; \
	dir=web/public/data/airport.$(AIRPORT_ROUND); \
	[ -f "$$dir/index.json" ] || { echo "no built airport round at $$dir"; exit 1; }; \
	[ -f web/public/data/latest-airport.json ] || { echo "no latest-airport.json: the build withheld the pointer (neither observation source arrived)"; exit 1; }; \
	pointer_path=$$(jq -r .path web/public/data/latest-airport.json); \
	[ "$$pointer_path" = "airport.$(AIRPORT_ROUND)/index.json" ] || { echo "latest-airport.json names $$pointer_path, not airport.$(AIRPORT_ROUND)"; exit 1; }; \
	pointer_crc=$$(jq -r .crc32 web/public/data/latest-airport.json); \
	index_crc=$$($(PYTHON) -c "import sys, zlib; print(f'{zlib.crc32(open(sys.argv[1], \"rb\").read()) & 0xFFFFFFFF:08x}')" $$dir/index.json); \
	[ "$$pointer_crc" = "$$index_crc" ] || { echo "latest-airport.json carries CRC32 $$pointer_crc but $$dir/index.json is $$index_crc"; exit 1; }; \
	history=$$(jq -r .history.path $$dir/index.json); \
	$(S3) cp $$dir/$$history s3://$(R2_BUCKET)/$(R2_PREFIX)/airport.$(AIRPORT_ROUND)/$$history --no-progress $(DRY_RUN) \
		--content-type application/x-ndjson --cache-control "public, max-age=31536000, immutable"; \
	$(S3) cp $$dir/index.json s3://$(R2_BUCKET)/$(R2_PREFIX)/airport.$(AIRPORT_ROUND)/index.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "public, max-age=31536000, immutable"; \
	[ ! -f "$$dir/$(STAC_ITEM)" ] || $(S3) cp $$dir/$(STAC_ITEM) s3://$(R2_BUCKET)/$(R2_PREFIX)/airport.$(AIRPORT_ROUND)/$(STAC_ITEM) \
		--no-progress $(DRY_RUN) --content-type application/geo+json --cache-control "no-cache"; \
	echo "Uploading latest-airport.json (takes airport round $(AIRPORT_ROUND) live)..."; \
	$(S3) cp web/public/data/latest-airport.json s3://$(R2_BUCKET)/$(R2_PREFIX)/latest-airport.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"; \
	$(MAKE) --no-print-directory upload-r2-stac-collection STAC_DIR=airport DRY_RUN=$(DRY_RUN)

# Delete round directories beyond the newest AIRPORT_KEEP (three hours;
# nothing reads an older round — the shell and the next build both start
# from the live pointer) and never the one the live pointer names.
prune-r2-airport:
	@set -e; \
	live=$$($(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/latest-airport.json - --only-show-errors 2>/dev/null | jq -r .path | cut -d/ -f1 || true); \
	[ -n "$$live" ] || { echo "no live airport pointer, nothing to prune"; exit 0; }; \
	echo "live airport round: $$live"; \
	listing=$$($(S3) ls s3://$(R2_BUCKET)/$(R2_PREFIX)/) \
		|| { echo "listing the bucket failed, refusing to prune"; exit 1; }; \
	for round in $$(printf '%s\n' "$$listing" | awk '/ PRE /{print $$2}' \
		| sed 's:/$$::' | grep "^airport\." | sort -r | tail -n +$$(($(AIRPORT_KEEP) + 1))); do \
		if [ "$$round" != "$$live" ]; then \
			echo "Deleting $$round..."; \
			$(S3) rm s3://$(R2_BUCKET)/$(R2_PREFIX)/$$round/ --recursive --only-show-errors $(DRY_RUN); \
		fi; \
	done

# The radiosonde sounding product (docs/sounding.md): the GTS→WIS2
# gateways' TEMP bulletins aggregated once an hour into
# web/public/data/sounding.<issue>/ — the index and the one soundings file
# it spans — and taken live by the mutable latest-sounding.json beside the
# run pointers. ISSUE is shared with the tc targets.
SOUNDING_KEEP ?= 48

sounding-build:
	$(PYTHON) -m xuebuild sounding-build --issue $(ISSUE) $(FORCE)

# The live product's pointer, index and soundings file, into
# web/public/data/ where sounding-build looks for the previous hour: the
# soundings file holds every station's older nominal times, so a publish
# without it would start each station's window again — and a truncated one
# would publish a hole, which is why the CRC32 the index carries is checked
# here rather than trusted. Three objects. Prints nothing and writes
# nothing when there is no live product yet (the first publish).
live-sounding-index:
	@set -e; mkdir -p web/public/data; \
	pointer=$$($(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/latest-sounding.json - --only-show-errors 2>/dev/null || true); \
	[ -n "$$pointer" ] || { echo "no live sounding pointer"; exit 0; }; \
	path=$$(printf '%s' "$$pointer" | jq -r .path); \
	directory=$$(dirname "$$path"); \
	mkdir -p "web/public/data/$$directory"; \
	$(S3) cp "s3://$(R2_BUCKET)/$(R2_PREFIX)/$$path" "web/public/data/$$path" --only-show-errors; \
	printf '%s' "$$pointer" > web/public/data/latest-sounding.json; \
	soundings=$$(jq -r .soundings.path "web/public/data/$$path"); \
	$(S3) cp "s3://$(R2_BUCKET)/$(R2_PREFIX)/$$directory/$$soundings" "web/public/data/$$directory/$$soundings" \
		--no-progress --only-show-errors; \
	crc=$$($(PYTHON) -c "import sys, zlib; print(f'{zlib.crc32(open(sys.argv[1], \"rb\").read()) & 0xFFFFFFFF:08x}')" "web/public/data/$$directory/$$soundings"); \
	named=$$(jq -r .soundings.crc32 "web/public/data/$$path"); \
	[ "$$crc" = "$$named" ] || { echo "the live $$soundings is CRC32 $$crc but its index says $$named"; exit 1; }; \
	stations=$$(jq -r '.stations | length' "web/public/data/$$path"); \
	bytes=$$(jq -r .soundings.byteLength "web/public/data/$$path"); \
	echo "live sounding issue: $$path ($$stations stations, $$bytes bytes of soundings)"

# Push the issue's soundings file, then the index that spans it, then the
# pointer that takes the issue live — each object before the one that names
# it, all immutable and ?v=<crc32>-addressed like a run's artifacts. The
# pointer on disk must name the issue being uploaded and carry its index's
# CRC32, the way check-pointer holds a run's.
upload-r2-sounding:
	@set -e; \
	[ "$(ISSUE)" != "now" ] || { echo "pass ISSUE=YYYYMMDDHH"; exit 1; }; \
	dir=web/public/data/sounding.$(ISSUE); \
	[ -f "$$dir/index.json" ] || { echo "no built sounding issue at $$dir"; exit 1; }; \
	[ -f web/public/data/latest-sounding.json ] || { echo "no latest-sounding.json: the build withheld the pointer (nothing contributed)"; exit 1; }; \
	pointer_path=$$(jq -r .path web/public/data/latest-sounding.json); \
	[ "$$pointer_path" = "sounding.$(ISSUE)/index.json" ] || { echo "latest-sounding.json names $$pointer_path, not sounding.$(ISSUE)"; exit 1; }; \
	pointer_crc=$$(jq -r .crc32 web/public/data/latest-sounding.json); \
	index_crc=$$($(PYTHON) -c "import sys, zlib; print(f'{zlib.crc32(open(sys.argv[1], \"rb\").read()) & 0xFFFFFFFF:08x}')" $$dir/index.json); \
	[ "$$pointer_crc" = "$$index_crc" ] || { echo "latest-sounding.json carries CRC32 $$pointer_crc but $$dir/index.json is $$index_crc"; exit 1; }; \
	soundings=$$(jq -r .soundings.path $$dir/index.json); \
	$(S3) cp $$dir/$$soundings s3://$(R2_BUCKET)/$(R2_PREFIX)/sounding.$(ISSUE)/$$soundings --no-progress $(DRY_RUN) \
		--content-type application/x-ndjson --cache-control "public, max-age=31536000, immutable"; \
	$(S3) cp $$dir/index.json s3://$(R2_BUCKET)/$(R2_PREFIX)/sounding.$(ISSUE)/index.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "public, max-age=31536000, immutable"; \
	[ ! -f "$$dir/$(STAC_ITEM)" ] || $(S3) cp $$dir/$(STAC_ITEM) s3://$(R2_BUCKET)/$(R2_PREFIX)/sounding.$(ISSUE)/$(STAC_ITEM) \
		--no-progress $(DRY_RUN) --content-type application/geo+json --cache-control "no-cache"; \
	echo "Uploading latest-sounding.json (takes sounding issue $(ISSUE) live)..."; \
	$(S3) cp web/public/data/latest-sounding.json s3://$(R2_BUCKET)/$(R2_PREFIX)/latest-sounding.json --no-progress $(DRY_RUN) \
		--content-type application/json --cache-control "no-cache"; \
	$(MAKE) --no-print-directory upload-r2-stac-collection STAC_DIR=sounding DRY_RUN=$(DRY_RUN)

# Delete sounding issue directories beyond the newest SOUNDING_KEEP (48
# hours = two days, the window a station's four nominal times cover) and
# never the one the live pointer names. No pointer yet means nothing is
# live to protect and nothing to prune, not a reason to fail.
prune-r2-sounding:
	@set -e; \
	live=$$($(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/latest-sounding.json - --only-show-errors 2>/dev/null | jq -r .path | cut -d/ -f1 || true); \
	[ -n "$$live" ] || { echo "no live sounding pointer, nothing to prune"; exit 0; }; \
	echo "live sounding issue: $$live"; \
	listing=$$($(S3) ls s3://$(R2_BUCKET)/$(R2_PREFIX)/) \
		|| { echo "listing the bucket failed, refusing to prune"; exit 1; }; \
	for issue in $$(printf '%s\n' "$$listing" | awk '/ PRE /{print $$2}' \
		| sed 's:/$$::' | grep "^sounding\." | sort -r | tail -n +$$(($(SOUNDING_KEEP) + 1))); do \
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

# A rolling window keeps its rounds beside one another inside the run
# directory. After the pointer has moved on to a new round, the rounds
# before the newest ROUNDS_KEEP are deleted — never the one the pointer
# names, whatever its name sorts as. A viewer on a replaced round has the
# next round plus a poll interval to be brought forward before its objects
# go. The runs the top-level prune (`prune-r2`, KEEP=2 for these sources)
# still keeps beside the live one are trimmed to their newest round: their
# viewers have long moved on, and a satellite round is most of a gigabyte.
# Rounds are named by the clock minute they were built at (HHMM), so a run
# whose rounds straddle midnight sorts wrong lexically; `round_order`
# folds a round more than twelve hours before the newest into the next day.
ROUNDS_KEEP ?= 2
define round_order
from sys import argv
rounds = argv[1].split()
newest = max(rounds)
def key(r): return int(r) + (2400 if int(newest) - int(r) > 1200 else 0)
print(' '.join(sorted(rounds, key=key, reverse=True)))
endef
export round_order
prune-r2-rounds:
	@set -e; \
	pointer=$$($(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/$(LATEST_FILE) - --only-show-errors 2>/dev/null || true); \
	[ -n "$$pointer" ] || { echo "no live pointer for $(MODEL), refusing to prune"; exit 1; }; \
	live=$$(printf '%s' "$$pointer" | jq -r .manifestPath | xargs dirname); \
	run=$$(printf '%s' "$$pointer" | jq -r .run); \
	echo "live $(MODEL) round: $$live"; \
	runs=$$($(S3) ls s3://$(R2_BUCKET)/$(R2_PREFIX)/ | awk '/ PRE /{print $$2}' | sed 's:/$$::' | grep "^$(MODEL)\." || true); \
	for dir in $$runs; do \
		listing=$$($(S3) ls s3://$(R2_BUCKET)/$(R2_PREFIX)/$$dir/) \
			|| { echo "listing $$dir failed, refusing to prune"; exit 1; }; \
		rounds=$$(printf '%s\n' "$$listing" | awk '/ PRE /{print $$2}' | sed 's:/$$::' | grep -E '^[0-9]{4}$$' || true); \
		[ -n "$$rounds" ] || continue; \
		if [ "$$dir" = "$(MODEL).$$run" ]; then keep=$(ROUNDS_KEEP); else keep=1; fi; \
		for round in $$($(PYTHON) -c "$$round_order" "$$rounds" | tr ' ' '\n' | tail -n +$$((keep + 1))); do \
			if [ "$$dir/$$round" != "$$live" ]; then \
				echo "Deleting $$dir/$$round..."; \
				$(S3) rm s3://$(R2_BUCKET)/$(R2_PREFIX)/$$dir/$$round/ --recursive --only-show-errors $(DRY_RUN); \
			fi; \
		done; \
	done

# Print the run the live pointer names, or nothing when there is no pointer.
live-run:
	@$(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/$(LATEST_FILE) - --only-show-errors | jq -r .run || true

# The manifest of the run the pointer names, as the bucket holds it — the
# base a top-up merges new bundles onto (`xuebuild assemble-run
# --base-manifest`). Prints nothing when there is no live run.
live-manifest:
	@set -e; path=$$($(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/$(LATEST_FILE) - --only-show-errors 2>/dev/null | jq -r .manifestPath || true); \
	[ -n "$$path" ] || exit 0; \
	$(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/$$path - --only-show-errors

# The window.json beside the live manifest of a rolling window (what the
# round holds: frame count, first and newest slot), as the bucket holds it.
# Prints nothing when there is no live run or the run has no window record.
live-window:
	@set -e; path=$$($(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/$(LATEST_FILE) - --only-show-errors 2>/dev/null | jq -r .manifestPath || true); \
	[ -n "$$path" ] || exit 0; \
	$(S3) cp s3://$(R2_BUCKET)/$(R2_PREFIX)/$$(dirname $$path)/window.json - --only-show-errors 2>/dev/null || true

# The decoded-frame cache of a source whose frames are decoded from a
# tile service (JMA, through the jma-radar tool: one NetCDF per frame under
# data/raw/<model>-frames/<grid>/) or warped from an agency's tiles (the
# satellite sources, xuebuild/satellite/: one GeoTIFF per frame and per
# variable, channel or composed gun, under data/raw/<model>-frames/<variable>/),
# mirrored under <prefix>/<model>-frames/
# on the bucket so every runner shares one copy and the agency serves each
# frame once. Frames are immutable and named by their time
# (`hrpns_<YYYYMMDDHHMMSS>.nc`, `ir104_<YYYYMMDDHHMMSS>.tif`), so a pull
# takes the hours of the window about to be built (the last HOURS + 1
# hours), a push sends what is new, and a prune drops the hours older than
# FRAMES_KEEP_HOURS. The default keeps a week, for a feed whose tiles expire
# at the agency (JMA: a case can only be cut from what is kept); a satellite
# source keeps its window and a little slack, since NOAA's buckets hold the
# scans for years and a six-hour window of one imager is two gigabytes of
# frames a day. None of this is served to the viewer.
FRAMES_DIR = data/raw/$(MODEL)-frames
FRAMES_PREFIX = s3://$(R2_BUCKET)/$(R2_PREFIX)/$(MODEL)-frames
FRAMES_KEEP_HOURS ?= 168
pull-r2-frames:
	@set -e; mkdir -p $(FRAMES_DIR); \
	includes=$$($(PYTHON) -c "from datetime import datetime, timedelta, UTC; now = datetime.now(UTC); \
	print(' '.join('--include */*_' + (now - timedelta(hours=h)).strftime('%Y%m%d%H') + '*' for h in range(int('$(if $(HOURS),$(HOURS),3)') + 1, -1, -1)))"); \
	$(S3) sync $(FRAMES_PREFIX)/ $(FRAMES_DIR)/ --exclude "*" --include "*/grid.json" $$includes --only-show-errors; \
	echo "frame cache: $$(find $(FRAMES_DIR) \( -name '*.nc' -o -name '*.tif' \) | wc -l | tr -d ' ') frames on disk"

push-r2-frames:
	@set -e; [ -d $(FRAMES_DIR) ] || { echo "no frame cache at $(FRAMES_DIR)"; exit 0; }; \
	$(S3) sync $(FRAMES_DIR)/ $(FRAMES_PREFIX)/ --size-only --only-show-errors $(DRY_RUN)

# A stale hour is deleted by its `YYYYMMDDHH` stamp (one rm per hour, the
# sidecars beside the frames share the stamp); whole stale days go in one
# rm each, so a cache that has never been pruned costs a listing per day
# rather than per hour.
prune-r2-frames:
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
