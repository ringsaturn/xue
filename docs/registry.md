# The registry document

What an encoder needs to know about the variables, bundles and sources it
builds, as one JSON document. The Python tables in `xuebuild/` author it
(`xue registry` prints it); the native encoder reads it in place of a
table of its own, so a table change is a document change and needs no
release. The committed snapshot `rust/xue/registry.json` is held to
the tables by a test.

The document is not a published artifact and not part of the format. What
reaches a store is the variable block of `docs/format.md`, which the
`label`, `unit`, `parameter`, `producer`, `aerosol` and `quantization` keys
of a variable entry are, key for key: an encoder copies them into
`attributes.xue`, it never computes them. Everything else in an entry is
how to obtain the field and what to do with it.

## Shape

```jsonc
{
  "registrySchema": 1,
  "variables": { "<variable id>": { … } },
  "bundles":   { "<bundle id>":   { … } },
  "sources":   { "<source id>":   { … } }
}
```

Bundle ids are the `^[a-z][a-z0-9]*$` strings the manifest admits; a
variable that is read and never published may carry `_` (`prate_ave`). Keys are
camelCase. Every key of an entry is written, even when empty, except where
a key is marked *optional* below: an absent optional key means "none".

`registrySchema` is the version of this document's own shape. A reader
implements a set of schemas and refuses a document outside it before any
input is read. Adding a key an existing reader can ignore keeps the number;
changing what a reader must do with an existing key raises it.

## Variables

```jsonc
"tmp2m": {
  "label": "2 meter temperature",
  "unit": "°C",
  "parameter": { "discipline": 0, "parameterCategory": 0, "parameterNumber": 0,
                 "typeOfFirstFixedSurface": 103,
                 "scaleFactorOfFirstFixedSurface": 0, "scaledValueOfFirstFixedSurface": 2 },
  "producer": { "id": "shachen" },                 // optional
  "aerosol": { "aerosolType": 62001, … },          // optional
  "quantization": {                                // optional: absent = input-only
    "quality":  { "type": "linear", "offset": -60.0, "scale": 0.5, "minimumCode": 0, "maximumCode": 220, "nodataCode": 255 },
    "compact":  { … },
    "balanced": { … }
  },
  "valueRange": [-60, 50],
  "fillValues": [],
  "static": false,
  "predictor": "delta",
  "decimation": "sample",
  "companions": ["poster", "video"],
  "conversion": { "rule": "celsius" },
  "match": { … }
}
```

| Key | Meaning |
|---|---|
| `label`, `unit`, `parameter`, `producer`, `aerosol` | Exactly the metadata blocks of `docs/format.md`. `producer` carries the `id` alone; the version is stamped on the series the producer wrote and read off it at build time. |
| `quantization` | One codebook block per profile, each exactly as the metadata writes it. The `type` is one of the closed set below. A variable without this key is read and never published (`tp`, the categorical precipitation flags). |
| `valueRange` | `[low, high]` in `unit`: the range the pipeline treats as the field's own. |
| `fillValues` | Values GDAL hands back where the record carries no data; folded to `valueRange[0]` before anything else touches the plane. |
| `static` | True for a field read at the analysis alone and published as one frame. |
| `predictor` | `delta` (chunks chain against the previous frame) or `raw`. |
| `decimation` | How reduced tiers sample the full tier: `sample` (the site's own code) or `peak` (the warmest code around the site). |
| `companions` | The subset of `poster`, `video` this variable's bundle ships beside the store. `video` is further subject to the source's `video` flag. |
| `conversion` | The unit rule: see below. |
| `match` | How the record is found: see below. |

### `conversion`

`{"rule": <name>, …}`. The rule names the arithmetic that takes a record's
values to `unit` and the unit spellings it accepts a record under; a
record under any other spelling is an error. Spellings are compared after
compaction: lower-cased, whitespace and `*()[]` removed, `²` written `^2`.

| Rule | Accepts | Arithmetic |
|---|---|---|
| `none` | the entry's `acceptedUnits` list, compacted; no list = any unit | identity |
| `celsius` | `k`, `kelvin`; `c`, `celsius`, `degc`; `f`, `fahrenheit`, `degf` (after dropping `°`, `degrees`, `degree`) | K − 273.15; (F − 32) × 5⁄9 |
| `soil-temperature` | as `celsius` | as `celsius`, after the soil read-through: a plane GDAL labels `[C]` whose values are kelvins is kelvins |
| `precipitation-rate` | `kg/m^2s`, `kg/m2s`, `kgm^-2s^-1`, `kgm-2s-1`; `mm/h`, `mm/hr` | × 3600; identity for mm/h |
| `precipitation-accumulation` | `-`, `m`, empty; `kg/m^2`, `kg/m2`, `kgm^-2`, `kgm-2`, `mm`, `kg/m^2s`, `kg/m^2*s`, `kg/m2s`, `kg/m2*s` | × 1000 for metres; identity for the millimetre spellings |
| `pascal-to-hectopascal` | `pa` | ÷ 100 |
| `metre-to-kilometre` | `m`, `metre`, `meter`, `metres`, `meters` | ÷ 1000 |
| `mass-ratio-to-g-per-kg` | `kg/kg` | × 1000 |
| `proportion-to-percent` | `proportion`, `fraction`, `1`, `-`, empty | × 100 |
| `cloud-cover` | `%`; `-`, `1`, `fraction`, `proportion`, `0-1` | identity for `%`; × 100 otherwise |
| `degrees-true` | `degreetrue`, `degtrue`, `degrees`, `degree`, `deg`, `degreestrue` | value mod 360 |
| `geopotential-to-metres` | `gpm`, `m`; `m2/s2`, `m2s-2` (compacted further: `^` dropped too) | identity; ÷ 9.80665 for the geopotential |
| `code-table` | `1`, `-`, empty, the entry's `table`, any spelling containing it or `0=no` (after dropping `codetable`) | identity |

A GRIB record is accepted under the listed spellings alone. A series
variable read out of a NetCDF file is accepted, in addition, under the
variable's own `unit` and its udunits spelling, which take the identity:
the scaling rules scale only under their listed input spellings, so a
series already in the published unit is left as it is.

The raster expression the two inspection paths compare (`gdal.py`,
`inspect.rs`) is derived from the rule and the unit alone and is never
written anywhere. A variable whose `gdal` list is empty matches no band;
that is not an error.

### `match`

```jsonc
"match": {
  "gribElement": "TMP",
  "indexField": ":TMP:2 m above ground:",
  "excludedIndexPhrases": [],
  "indexQualifier": "",
  "alternateIndexFields": [],
  "ecmwfParam": "2t",
  "ecmwfAlternateParams": [],
  "openMeteo": "temperature_2m",
  "dwdParam": "T_2M",
  "mrmsProduct": "",
  "gdalUnit": "C",
  "statistical": null,
  "aliases": [[0, 3, 0]],
  "alternates": [ { "discipline": 0, "category": 2, "number": 22, "levelType": 103,
                    "levelValue": 10.0, "statistical": 2, "gdalUnit": "" } ],
  "gdal": [ { "rule": "height", "element": "TMP", "metres": 2 } ]
}
```

The first eleven keys are the fetch-side spellings of `VariableSpec`
(`xuebuild/variables.py`), one for one. `statistical` is the code table
4.10 process the record must carry (`null`: instantaneous). `aliases` are
other parameter triples on the same surface, `alternates` whole other
record identities, both accepted when matching and never written.

`gdal` is how the variable's record is recognised among a GRIB's bands as
GDAL describes them: a list of rules, the band is the record when any one
accepts it. Empty for a field no GRIB band carries (a derived field, a
series-file variable). Each rule reads the band's `GRIB_ELEMENT`,
`GRIB_SHORT_NAME`, `GRIB_COMMENT`, `GRIB_LEVEL`, description and, for the
template rules, `GRIB_PDS_PDTN`, `GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES` and
`GRIB_DISCIPLINE`; the "text" is the short name, comment, level and
description joined and lower-cased, except that `isobaric`, `phrase`,
`local-parameter` and `total-precipitation` search the comment, level and
description without the short name. Elements compare upper-cased; the
`template-mrms` element compares as written.

| Rule | Parameters | Accepts the band when |
|---|---|---|
| `surface` | `elements` | the element is one of `elements`, and the short name ends in `-SFC`, or the text contains `sfc="` or `ground or water surface` |
| `height` | `element`, `metres` | the element matches, and the short name is `<metres>-HTGL` or `<metres>-M-HTGL`, or the text matches `(^|[^0-9])<metres>(\.0+)?\s*m(eter)?s?\s+above\s+ground` |
| `entire-atmosphere` | `element` | the element matches, and the short name is `0-EATM` or the text contains `entire atmosphere` |
| `mean-sea-level` | `elements` | the element is one of `elements`, and the short name is `0-MSL` or the text contains `mean sea level` |
| `isobaric` | `element`, `hpa` | the element matches, and the short name is `<hpa>-ISBL` or `<hpa×100>-ISBL`, or the text names this level as `<hpa> mb`/`hPa` or `<hpa×100> Pa` and no other |
| `cloud-layer` | `element`, `shortName`, `phrases` | the element matches, and the short name is `shortName` or the text contains one of `phrases` |
| `template` | — | `GRIB_PDS_PDTN` is 0 and the assembled values' parameter category and number, surface type and surface value equal the entry's `parameter` or one of its `alternates` (a `null` level value accepts any) |
| `template-aerosol` | — | `GRIB_PDS_PDTN` is 48 and the assembled values equal the entry's `parameter` and `aerosol` blocks whole |
| `template-rank` | — | the element is `match.gribElement`, then as `template` against the entry's own identity, the surface value being the rank; without an assembled template, the short name is `<rank>-RESERVED(<surface type>)` |
| `template-mrms` | — | the element is the entry's `mrmsProduct` up to its first `_`, the discipline is 209, and the assembled template 4.0 values equal the entry's discipline-209 alternate (or its own identity); without an assembled template, a surface of type 102 must spell `<value>-GPML` in the short name |
| `total-precipitation` | — | the element is `unknown`, `tp`, `apcp` or `tprate`; the short name is `0-SFC` or the text contains `surface`; and the comment contains `cat 1, subcat 193` or the text `total precipitation` |
| `local-parameter` | `category`, `number` | the element is `unknown`, the short name is `0-SFC` or the text contains `ground or water surface`, and the comment contains `cat <category>, subcat <number>` |
| `phrase` | `element`, `phrases` | the element matches and the text contains one of `phrases` |
| `geopotential-surface` | `elements` | the element is one of `elements`, the short name ends in `-SFC` or the text contains `ground or water surface`, and the text contains `geopotential` |

## Bundles

A bundle id absent from `bundles` is the scalar bundle of the variable of
the same id. Every other bundle is listed:

```jsonc
"wind10m":   { "kind": "vector", "members": ["ugrd10m", "vgrd10m"] },
"qflux850":  { "kind": "vector", "members": ["uqflx850", "vqflx850"],
               "derivation": { "kind": "vapour-flux", "inputs": ["spfh850", "ugrd850", "vgrd850"] } },
"thetae850": { "kind": "scalar",
               "derivation": { "kind": "theta-e", "inputs": ["tmp850", "spfh850"], "levelHpa": 850 } },
"prate":     { "kind": "scalar",
               "derivation": { "kind": "precipitation-rate", "inputs": ["prate", "tp", "prate_ave", "apcp"] } },
"dustrgb":   { "kind": "composite", "members": ["dustr", "dustg", "dustb"], "producer": "shachen",
               "inputs": ["ir086", "ir104", "ir112", "ir123"], "optionalInputs": ["ir112"] },
"refl3d":    { "kind": "volume", "members": ["refl0500", …] }
```

| Key | Meaning |
|---|---|
| `kind` | `scalar`, `vector` (two members, u then v), `composite` (members an algorithm produced in the fetch stage, read off the series), `volume` (members read directly, lowest level first). |
| `members` | The variables the bundle's arrays hold, in file order (`variableId` 1…n). Absent for a scalar. |
| `derivation` | *optional*. The members are computed in the converter from `inputs`, by the named algorithm. `levelHpa` is written where the algorithm takes the surface. |
| `producer`, `inputs`, `optionalInputs` | A composite's producer id, the channels it reads, and the subset of those a source may lack (read when the source fetches them, left out otherwise). |

`derivation.kind` is one of the closed set: `vapour-flux` (q·V/g from a
specific humidity and a wind pair), `wave-vector` (a height laid along a
direction), `theta-e` (Bolton's equivalent potential temperature at
`levelHpa`), `precipitation-type` (the four categorical flags combined),
`clear-air-turbulence` (Ellrod at `levelHpa`, calibrated by the source's
`catCalibration`), `precipitation-rate` (the first of `inputs` the source
lists, de-accumulated, de-averaged or taken as is by the source's
precipitation flags).

## Sources

One entry per source, every field of `xuebuild/sources.py`'s `SourceSpec`
under its camelCase name, nested records as objects, tuples as arrays:

```jsonc
"gfs": {
  "id": "gfs", "manifestModel": "GFS", "product": "pgrb2-0p25", "latestFilename": "latest.json",
  "steps": [[120, 1], [384, 3]],
  "inputVariableIds": ["tmp2m", "prate", …],
  "accumulatedPrecipitation": false, "averagedPrecipitation": false, "intervalPrecipitation": false,
  "averageWindowHours": 6, "firstHour": 0,
  "companionFiles": [{ "id": "wave", "variableIds": ["htsgw", …], "repack": true, "productionGrid": null, "tile": null }],
  "optionalAtAnalysis": ["prate", …],
  "statisticalProcesses": [["gust", 2]],
  "bands": [["ir039", { "satelliteSeries": 0, "satelliteNumber": 174, "instrumentType": 297, "centralWavenumber": 257380 }]],
  "platform": null, "openMeteo": null, "gridStep": null, "region": null,
  "bundleScalarIds": [...], "coreBundleIds": [...], "bundleVectorIds": [...], "bundleCompositeIds": [], "bundleVolumeIds": [],
  "productionGrid": [1440, 721], "tile": [48, 52], "variantFactors": [2], "seriesBundleIds": [...],
  "fetchConcurrency": 4, "observation": false, "windowHours": null, "cadenceSeconds": null, "objectCadenceSeconds": null,
  "seriesFile": false, "downsample": null, "cycleHours": 6, "longCycles": [], "longCycleSteps": [],
  "regrid": null, "video": true, "catCalibration": [[300, 0.0, 0.0], …]
}
```

`downsample` is `{"factor": n}` or `null`; `regrid` is `{"step": degrees}`
or `null`. The meaning of each field is `SourceSpec`'s docstring and
`docs/contribution/sources.md`.

## Closed sets

A reader implements these names and refuses a document that names any
other, as it refuses a `registrySchema` it does not implement. Adding a
name is a behaviour change and needs the readers released; using an
existing name on a new entry does not.

| Field | Names |
|---|---|
| `quantization.<profile>.type` | `linear`, `log1p` |
| `conversion.rule` | the thirteen rules above |
| `match.gdal[].rule` | the fourteen rules above |
| `predictor` | `delta`, `raw` |
| `decimation` | `sample`, `peak` |
| `companions[]` | `poster`, `video` |
| `bundles.<id>.kind` | `scalar`, `vector`, `composite`, `volume` |
| `derivation.kind` | the six algorithms above |
| `sources.<id>.downsample`, `.regrid` | the one shape each |

## Validation

Before reading any input, a reader checks: the schema; every name above;
that every `members`, `inputs` and `derivation.inputs` id is a variable;
that every bundle id a source lists is a variable or a `bundles` entry;
that a `vector` has two members; that a published variable (one with
`quantization`) has every profile the reader is asked for. Any failure
refuses the whole build.

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).
