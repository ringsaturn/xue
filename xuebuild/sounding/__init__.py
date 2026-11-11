"""Radiosondes: the observed vertical profile, a product beside the runs.

A sounding is a few hundred levels of pressure, height, temperature, dew
point and wind at one point — not a raster, so it is not a `.xue` bundle.
It is its own small pipeline, the shape ``xuebuild/tc`` established: list
the WIS2 Global Cache's GTS→WIS2 gateway directories and national
nodes, and Météo-France's open-data directory, download the TEMP
bulletins and files that have arrived since the last issue, decode each
(eccodes' ``bufr_dump`` for BUFR, ``meteofrance.py`` for Météo-France's
JSON), deduplicate the station-hours the sources and the corrections
deliver more than once, derive the few quantities a
profile is read for, and write one immutable directory per hour under the
same pointer → immutable-directory contract the runs use
(``docs/sounding.md``).

``build.py`` is the only module where the sources meet; ``bufr.py`` knows
BUFR and nothing about the product's files, ``schema.py`` knows the files
and nothing about BUFR.
"""
