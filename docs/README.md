# Specifications

The documents in this directory are the normative contract for what Xue
writes and what every reader must accept. They win over the code, over the
developer documentation ([`contribution/`](contribution/README.md)) and
over any comment. They are licensed under [CC BY 4.0](../LICENSE-CC-BY);
the code is MIT / Apache-2.0 and the published data keeps its sources'
terms (see the [README](../README.md#data-and-licensing)).

| Document | Specifies |
|---|---|
| [format.md](format.md) | The `.xue` container (v1, v2), the bundle metadata JSON (`schemaVersion` 1–3), the codebooks and the residual arithmetic |
| [zarr-profile.md](zarr-profile.md) | The Zarr v3 store a bundle is published as, the series companion, the polar store |
| [stac.md](stac.md) | The STAC catalog: layout, run Items, source Collections, point products, the showcase |
| [encoder.md](encoder.md) | The native encoder and its byte-for-byte parity rule with the reference encoder |
| [sources.md](sources.md) | What each source publishes: variables, grids, axes, caveats |
| [tc.md](tc.md), [airport.md](airport.md), [sounding.md](sounding.md), [synop.md](synop.md), [nexrad.md](nexrad.md) | The point and single-site radar products: pointer, index, files, `schemaVersion` |
| [satellite.md](satellite.md) | Geostationary imagery and its composites |
| [indicators.md](indicators.md) | The agriculture indicators: crop-area-weighted daily quantities per forecast run, appended to monthly files read by byte range |
| [api.md](api.md) | The data API (experimental) |

## Versions

Every artifact carries its own version, none tied to a code release.

| Artifact | Written today | Read |
|---|---|---|
| Container (`FixedHeader.version`) | 2, as a build intermediate only | 1 and 2 |
| Bundle metadata (`schemaVersion`) | 3 | 1 to 3 |
| Manifest | schema 5 | 5; unknown fields ignored |
| Live pointer | schema 1 | 1 |
| Zarr store (`zarr_format`) | 3 | 3 |
| STAC (`stac_version`) | 1.1.0 | — |
| Point products (`schemaVersion`, one per product) | see each document | up to the one a reader implements |
| Indicators (`schemaVersion`) | 1 | 1 |

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

Changes to these documents are listed in [`CHANGELOG.md`](../CHANGELOG.md).
