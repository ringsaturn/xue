"""Active fires: NASA FIRMS's near-real-time VIIRS and MODIS detections, a
point product beside the raster runs and the station products.

Every hour the last-24-hour FIRMS files are laid over the previous round's
seven-day window and written under the pointer → immutable directory
contract the other point products use (``docs/fire.md``): a
``<sensor>.jsonl`` per sensor with one line per UTC hour, each hour's byte
span in the index, so a reader takes any span of hours of one sensor with
one range request. Xue detects nothing itself; it only carries FIRMS's
detections in one shape.
"""
