"""Export the named-peak layer the relief view labels (`web/public/peaks.json`).

Protomaps tiles carry OSM peaks only from zoom 11–14, so the mountains a
regional view would name are missing below that. Wikidata (CC0) has the
peaks with a topographic prominence; prominence, not height, is what ranks
them, since by height alone the Himalaya is a carpet of unnamed 7000 m
shoulders. Each peak gets the zoom it first shows at from its prominence.

Run by hand when the list should be refreshed; the output is committed.

    .venv/bin/python scripts/build_peaks.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "xue-peaks/1.0 (https://github.com/ringsaturn/xue)"
OUT = Path(__file__).resolve().parent.parent / "web" / "public" / "peaks.json"

MIN_PROMINENCE_M = 300
# (prominence floor in m, first zoom). The top rows put the handful of
# great massifs on a continental view; the bottom row fills the regional
# zooms until the basemap's own OSM peaks take over.
ZOOM_BY_PROMINENCE = ((4000, 2), (3000, 3), (2000, 4), (1500, 5), (1000, 6), (600, 7), (300, 9))

# Wikidata label languages per basemap `name:*` key, best first. Chinese
# labels are often only under the bare `zh`, in either script.
LANGUAGES = {
    "en": ("en", "mul"),
    "ja": ("ja",),
    "ko": ("ko",),
    "de": ("de",),
    "fr": ("fr",),
    "es": ("es",),
    "pt": ("pt",),
    "tr": ("tr",),
    "ru": ("ru",),
    "zh-Hans": ("zh-hans", "zh-cn", "zh-sg", "zh"),
    "zh-Hant": ("zh-hant", "zh-tw", "zh-hk", "zh"),
}

PEAKS_QUERY = f"""
SELECT ?m (MAX(?elev) AS ?e) (MAX(?prom) AS ?p) (SAMPLE(?coord) AS ?c) WHERE {{
  ?m p:P2660 ?ps . ?ps a wikibase:BestRank ; psn:P2660/wikibase:quantityAmount ?prom .
  FILTER(?prom >= {MIN_PROMINENCE_M})
  ?m p:P2044 ?es . ?es a wikibase:BestRank ; psn:P2044/wikibase:quantityAmount ?elev .
  ?m p:P625 ?cs . ?cs psv:P625 ?cv .
  ?cv wikibase:geoGlobe wd:Q2 ; wikibase:geoLatitude ?lat ; wikibase:geoLongitude ?lon .
  BIND(CONCAT(STR(?lon), " ", STR(?lat)) AS ?coord)
}} GROUP BY ?m
"""


def sparql(query: str) -> list[dict]:
    body = urllib.parse.urlencode({"query": query}).encode()
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)["results"]["bindings"]
        except Exception as error:  # the endpoint throttles with 429/503
            if attempt == 4:
                raise
            print(f"retry after {error}", file=sys.stderr)
            time.sleep(10 * (attempt + 1))
    raise AssertionError


def first_zoom(prominence: float) -> int:
    return next(zoom for floor, zoom in ZOOM_BY_PROMINENCE if prominence >= floor)


def main() -> None:
    peaks: dict[str, dict] = {}
    for row in sparql(PEAKS_QUERY):
        qid = row["m"]["value"].rsplit("/", 1)[1]
        lon, lat = (float(v) for v in row["c"]["value"].split())
        elevation = float(row["e"]["value"])
        prominence = float(row["p"]["value"])
        # Prominence above height is a data-entry error (a foot value read
        # as metres, a swapped pair); the peak is unranked, so leave it out.
        if prominence > elevation + 1 or elevation > 8900:
            continue
        peaks[qid] = {"lon": lon, "lat": lat, "e": elevation, "p": prominence, "labels": {}}
    print(f"{len(peaks)} peaks", file=sys.stderr)

    wanted = sorted({tag for tags in LANGUAGES.values() for tag in tags})
    filter_langs = ", ".join(f'"{tag}"' for tag in wanted)
    ids = sorted(peaks)
    for start in range(0, len(ids), 400):
        values = " ".join(f"wd:{qid}" for qid in ids[start : start + 400])
        query = f"""
        SELECT ?m ?l WHERE {{
          VALUES ?m {{ {values} }}
          ?m rdfs:label ?l . FILTER(LANG(?l) IN ({filter_langs}))
        }}"""
        for row in sparql(query):
            qid = row["m"]["value"].rsplit("/", 1)[1]
            peaks[qid]["labels"][row["l"]["xml:lang"]] = row["l"]["value"]

    features = []
    for peak in peaks.values():
        labels = peak["labels"]
        names = {}
        for key, tags in LANGUAGES.items():
            name = next((labels[tag] for tag in tags if tag in labels), None)
            if name:
                names[key] = name
        default = names.pop("en", None) or next(iter(names.values()), None)
        if default is None:
            continue
        properties: dict[str, object] = {
            "name": default,
            "elevation": round(peak["e"]),
            "prominence": round(peak["p"]),
            "min_zoom": first_zoom(peak["p"]),
        }
        # A language whose label is the default adds nothing to fall back to.
        properties.update({f"name:{key}": name for key, name in sorted(names.items()) if name != default})
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(peak["lon"], 4), round(peak["lat"], 4)]},
                "properties": properties,
            }
        )
    features.sort(key=lambda feature: -feature["properties"]["prominence"])
    collection = {
        "type": "FeatureCollection",
        "attribution": "Wikidata (CC0)",
        "features": features,
    }
    OUT.write_text(json.dumps(collection, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {len(features)} peaks to {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
