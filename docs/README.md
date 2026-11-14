# Specifications

The documents in this directory are the normative contract for what Xue
writes and what every reader must accept. They win over the code, over the
developer documentation ([`contribution/`](contribution/README.md)) and
over any comment. They are licensed under [CC BY 4.0](../LICENSE-CC-BY);
the code is MIT / Apache-2.0 and the published data keeps its sources'
terms (see the [README](../README.md#data-and-licensing)).

| Document | Specifies |
|---|---|
| [zarr-profile.md](zarr-profile.md) | The Zarr v3 store a bundle is published as, the series companion, the polar store: what is published today, so read this first |
| [bundle.md](bundle.md) | What every bundle carries: the metadata JSON (`schemaVersion` 1–4), the codebooks and the residual arithmetic |
| [format.md](format.md) | The legacy `.xue` container (v1, v2) that runs published before the stores still carry |
| [schemas/](schemas/) | The JSON Schemas (draft 2020-12) normative for shape: the manifest, bundle metadata v3, a store's group and array documents, the pointer and the point products' files |
| [stac.md](stac.md) | The STAC catalog: layout, run Items, source Collections, point products, the showcase |
| [registry.md](registry.md) | The registry document: the variables, bundles and sources an encoder builds, the closed sets of rules it names |
| [encoder.md](encoder.md) | The native encoder and its parity rule with the reference encoder |
| [sources.md](sources.md) | What each source publishes: variables, grids, axes, caveats |
| [tc.md](tc.md), [airport.md](airport.md), [sounding.md](sounding.md), [synop.md](synop.md), [aq.md](aq.md), [alerts.md](alerts.md), [nexrad.md](nexrad.md) | The point and single-site radar products: pointer, index, files, `schemaVersion` |
| [fire.md](fire.md) | The active fire product: FIRMS detections aggregated hourly, pointer, index, sensor files |
| [river.md](river.md) | River flow in a showcase case: the `river/` directory, the network and gauge kinds |
| [satellite.md](satellite.md) | Geostationary imagery and its composites |
| [indicators.md](indicators.md) | The agriculture indicators: crop-area-weighted daily quantities per forecast run, appended to monthly files read by byte range |
| [metrics.md](metrics.md) | The run metrics: when each forecast run went live, measured from the publish workflows |
| [api.md](api.md) | The data API (experimental) |

## Versions

Every artifact carries its own version, none tied to a code release.

| Artifact | Written today | Read |
|---|---|---|
| Bundle metadata (`schemaVersion`) | 3 | 1 to 3 |
| Container (`FixedHeader.version`) | 2, only when a build asks (`--xue`) | 1 and 2 |
| Manifest | schema 5 | 5; unknown fields ignored |
| Live pointer | schema 1 | 1 |
| Zarr store (`zarr_format`) | 3 | 3 |
| STAC (`stac_version`) | 1.1.0 | — |
| Point products (`schemaVersion`, one per product) | see each document | up to the one a reader implements |
| Indicators (`schemaVersion`) | 2 | 2 |
| Run metrics (`schemaVersion`) | 1 | 1 |

## Compatibility

- A reader keeps every version it has ever read. Published runs, rounds and
  cases are never rebuilt, so a version once published stays readable.
- An optional new field (a widening) does not change a version: readers
  ignore fields they do not know. A widening reaches the readers (the shell,
  the API) before the first data that carries it.
- A change a current reader could misread is a new version number. Readers
  reject a version above the one they implement, so a reader that knows the
  new version is deployed before the first data in it, and the old version
  keeps being read.
- Nothing is removed from a specification. A retired artifact (the
  container) stays specified for the files that exist.

How these are built, tested and released is in
[`contribution/`](contribution/README.md); why the format looks as it does,
in [`contribution/architecture-rationale.md`](contribution/architecture-rationale.md).

Changes to these documents are listed in [`CHANGELOG.md`](../CHANGELOG.md).
