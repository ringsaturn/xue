"""Air-quality stations: hourly pollutant concentrations from national
monitoring networks, a point product beside the surface stations
(``synop``), the airports and the soundings.

One product carries many networks (``networks.py``), each read by its own
adapter into one set of elements and units, merged every half hour onto
the previous round's 24-hour history and written under the pointer →
immutable directory contract the other point products use
(``docs/aq.md``): per network a ``<network>.jsonl`` with a line per station
and a ``<network>.stations.json`` table holding each station's current
values and the byte span of its line, so a reader takes one station with
one range request. The US EPA's AirNow and Hong Kong's EPD are the first
networks.
"""
