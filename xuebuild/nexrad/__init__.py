"""Single-site weather radar: the WSR-88D network's lowest-sweep
reflectivity and radial velocity (NEXRAD Level 3 N0B / N0G), published as a
rolling window of polar stores (``docs/nexrad.md``, the polar branch of
``docs/zarr-profile.md``): one immutable Zarr store per product per
five-minute round, and a window manifest that spans them.
"""
