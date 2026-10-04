"""Airports: METAR observations and TAF forecasts, a point product beside
the raster runs and beside the tropical cyclones.

An observation is a line of text, not a raster, so the airports are their
own small pipeline — three cached files from the NOAA Aviation Weather
Center every ten minutes, parsed into SI records, merged onto the previous
round's 24-hour history and written under the same pointer → immutable
directory contract the runs and the storm tracks use (``docs/airport.md``):
one ``history.jsonl`` per round with each station's byte span in the index,
so a reader takes one station with one range request. ``build.py`` is the
only place the three sources meet.
"""
