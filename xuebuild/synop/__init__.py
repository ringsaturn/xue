"""Surface weather stations: national observation networks, a point
product beside the raster runs, the airports and the soundings.

One product carries many networks (``networks.py``), each read by its own
adapter into one set of elements and units, merged every ten minutes onto
the previous round's 24-hour history and written under the pointer →
immutable directory contract the other point products use
(``docs/synop.md``): a ``<network>.jsonl`` per network with each station's
byte span in the index, so a reader takes one station with one range
request. JMA AMeDAS is the first network.
"""
