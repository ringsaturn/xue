"""Surface weather stations: national observation networks, a point
product beside the raster runs, the airports and the soundings.

One product carries many networks (``networks.py``: JMA AMeDAS, DWD's
national SYNOP reports, the Hong Kong Observatory), each read by its own
adapter into one set of elements and units, merged every ten minutes onto
the previous round's 24-hour history and written under the pointer →
immutable directory contract the other point products use
(``docs/synop.md``): per network a ``<network>.jsonl`` with a line per
station and a ``<network>.stations.json`` with each station's current
values and the byte span of its line, so a reader takes one station with
one range request.
"""
