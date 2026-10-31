"""Weather warnings in force: national warning services, a point product
beside the raster runs and the station products.

One product carries several networks (``networks.py``), each read by its
own adapter into one shape — a closed category list, a five-level scale,
the upstream's own codes and every language it publishes — and written
every five minutes as a snapshot of what is in force under the pointer →
immutable directory contract the other point products use
(``docs/alerts.md``): a ``<network>.jsonl`` per network with each alert's
byte span in the index, and, for networks that name areas rather than
draw them, a versioned PMTiles boundary set the area codes resolve
against (``areas.py``).
"""
