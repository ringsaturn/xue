"""River flow on a reach network: the network store (the Zarr profile's
third branch, ``docs/zarr-profile.md``), the reaches' GeoParquet, and a
showcase case's ``river/index.json`` (``docs/river.md``) that names them
beside the gauges' observed series and the mapped flood extents.

``python -m xuebuild.river build <definition.json>`` writes one case's
``river/`` directory from a work directory (:mod:`xuebuild.river.build`).
"""
