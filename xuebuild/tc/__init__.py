"""Tropical cyclones: a product beside the raster runs, not inside them.

The ``.xue`` container is a raster container; a storm track is a few
kilobytes of points. So the tracks are their own small pipeline — fetch the
agency and model products (ATCF text, JTWC's JMV 3.0 warnings, ECMWF's
``tf`` BUFR, IBTrACS CSV), parse each with a parser that imports no other
parser, cross the identities, and write one immutable directory per
aggregation hour under the same pointer → immutable-directory contract the
raster runs use (``docs/tc.md``). ``build.py`` is the only place the
sources meet.
"""
