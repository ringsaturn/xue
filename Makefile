PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
# A source id from xuebuild/sources.py (gfs, ecmwf, aifs, ifshres, sflux, hrrr,
# gefsaero, cfs, mrms, jma, cma, himawari, goeseast, goeswest, meteosat, ...).
MODEL ?= gfs
# A forecast cycle YYYYMMDDHH, or an observation window's first hour;
# `latest` resolves the newest (the live rolling window on an observation).
RUN ?= latest
# Last forecast hour to build; empty takes the model's whole axis.
HOURS ?=
FORCE ?=
PROFILE ?= balanced
# One round (HHMM) of a rolling window, built into <model>.<run>/<ROUND>/ so a
# later round never overwrites objects a viewer is still range-reading.
ROUND ?=
RUN_DIR = $(MODEL).$(RUN)$(if $(ROUND),/$(ROUND))

.PHONY: check help install wasm api-worker deploy-api api-dev api-dev-cdn test test-rust test-e2e encoder-rust encoder-rust-test encoder-wheel encoder-py mvp serve clean

check: ## Verify GDAL / zstd / node / wasm-pack versions
	$(PYTHON) scripts/check_dependencies.py

help: ## List the public targets
	@awk -F ':.*## ' '/^[a-z0-9-]+:.*## / { printf "  %-26s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install: ## Install the frontend's npm dependencies
	npm ci

wasm: ## Build the WASM decoder into web/src/wasm/
	cd rust && wasm-pack build xue-wasm --target web --out-dir ../../web/src/wasm --out-name xue

# The data-API Worker (rust/xue-worker). Its R2 binding is `remote: true`, so
# `api-dev` reads the real bucket; `api-dev-cdn` reads the public origin
# instead, for a network that blocks the remote-binding session.
api-worker: ## Build the data-API Worker
	cd rust/xue-worker && worker-build --release

deploy-api: ## Deploy the data-API Worker
	cd rust/xue-worker && npx wrangler deploy

api-dev: ## Run the API Worker locally against the real bucket
	cd rust/xue-worker && npx wrangler dev

api-dev-cdn: ## Run the API Worker locally against the public origin
	cd rust/xue-worker && npx wrangler dev --var DATA_SOURCE:cdn

test: test-rust ## Rust, Python and web unit tests
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
	npm run test:web

# The workspace's default members leave out xue-py, which links GDAL through
# the encoder feature; a decode-only check must not need GDAL.
test-rust: ## Regenerate the golden fixture, then cargo test
	$(PYTHON) tests/prepare_bin_fixture.py
	cd rust && cargo test

test-e2e: ## Playwright end-to-end tests
	npm run test:e2e

# The native encoder links GDAL (pkg-config) and needs libclang for bindgen;
# the `encoder` profile keeps it at opt-level 3 (see rust/Cargo.toml).
ENCODER = cd rust && PKG_CONFIG_PATH="$$(gdal-config --prefix)/lib/pkgconfig" cargo
ENCODER_ARGS = --profile encoder --features encoder

encoder-rust: ## Build the native encoder from source
	$(ENCODER) build $(ENCODER_ARGS) --bin xue-encode

# The golden test demands the exact bytes the reference encoder just wrote.
encoder-rust-test: ## Native encoder unit tests and byte-identity golden test
	$(PYTHON) tests/prepare_bin_fixture.py
	$(ENCODER) test -p xue $(ENCODER_ARGS)

encoder-wheel: ## Build a self-contained xuepy wheel with a minimal GDAL
	./scripts/build-encoder-wheel.sh

# So the parity tests compare this tree's encoders rather than the published
# wheel. Needs a system GDAL and libclang.
encoder-py: ## Install this tree's native encoder into the venv
	uv pip install --reinstall ./rust/xue-py

mvp: check install wasm ## Build one run (MODEL, RUN, HOURS) and the frontend
	$(PYTHON) -m xuebuild build-bin --model $(MODEL) --run $(RUN) $(if $(HOURS),--hours $(HOURS)) --profile $(PROFILE) --zarr $(FORCE)
	npm run build

serve: ## vite preview on 127.0.0.1
	npm run preview -- --host 127.0.0.1

clean:
	@echo "Generated data is retained for reuse. Remove dist manually if required."

include mk/r2.mk mk/cache.mk mk/products.mk mk/showcase.mk mk/deploy.mk mk/extras.mk
