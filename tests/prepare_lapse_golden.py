"""Regenerate the terrain-correction golden the frontend is held to.

Twenty-one synthetic cells — a summit three kilometres above a
quarter-degree model ground by day and by night, a night inversion over
the model ground, a valley, a slope just above the ground, a two-surface
column, a summit above 500 hPa, a column handed over out of height order —
each with the temperature at the site that the method evaluated against
mountain stations gives: the isobaric temperatures interpolated linearly in
their heights (sorted, the standard 6.5 K/km past either end), and above
the model ground

    T = free(site) + exp(-dz / H) * (tmp2m - free(model ground))

with H = 2500 m where the model ground is warmer than the free atmosphere
and 800 m where it is colder, and below it ``tmp2m - 0.0065 * dz``. ``tests/web/lapse.test.ts`` holds
``web/src/lapse.ts`` to it.

Run it after a deliberate change to the method, and commit the diff with
the change::

    .venv/bin/python tests/prepare_lapse_golden.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "lapse-golden.json"
LAPSE_RATE = 0.0065
WARM_DECAY_M = 2500.0
COLD_DECAY_M = 800.0


def free_atmosphere(z: float, levels: list[tuple[float, float]]) -> float:
    """Linear in height between the surfaces, the standard rate past either
    end — the evaluation's ``interp_profile`` for one cell."""
    heights = np.array([height for height, _ in levels])
    temperatures = np.array([temperature for _, temperature in levels])
    order = np.argsort(heights)
    heights, temperatures = heights[order], temperatures[order]
    if z < heights[0]:
        return float(temperatures[0] - LAPSE_RATE * (z - heights[0]))
    if z > heights[-1]:
        return float(temperatures[-1] - LAPSE_RATE * (z - heights[-1]))
    return float(np.interp(z, heights, temperatures))


def site_temperature(t2m: float, model: float, site: float, levels: list[tuple[float, float]]) -> float:
    dz = site - model
    if dz <= 0 or len(levels) < 2:
        return t2m - LAPSE_RATE * dz
    anomaly = t2m - free_atmosphere(model, levels)
    return free_atmosphere(site, levels) + math.exp(-dz / (WARM_DECAY_M if anomaly > 0 else COLD_DECAY_M)) * anomaly


def standard_column(surface: float, lapse: float) -> list[tuple[float, float]]:
    """(height, temperature) on 1000/925/850/700/500 hPa: standard heights,
    one lapse rate in K/m from ``surface`` °C at 110 m."""
    return [(z, surface - lapse * (z - 110.0)) for z in (110.0, 760.0, 1460.0, 3010.0, 5570.0)]


# A night over a plain: 6 K warmer at 925 than at 1000, then the free
# atmosphere's 6 K/km.
INVERSION = [(110.0, 2.0), (760.0, 8.0), (1460.0, 3.8), (3010.0, -5.5), (5570.0, -21.0)]
# GFS 2026100518 F000 over Mount Fuji (35.25 N, 138.75 E), straight from the
# pgrb2 records.
FUJI = [(66.63, 21.716), (734.01, 18.832), (1457.14, 15.916), (3090.22, 9.014), (5807.00, -5.390)]

CASES: list[dict] = [
    {"name": "fuji gfs", "t2m": 16.891, "model": 647.74, "site": 3684.0, "levels": FUJI},
    {"name": "summit, moist column", "t2m": 17.0, "model": 643.0, "site": 3748.0, "levels": standard_column(24.0, 0.0046)},
    {"name": "summit by day", "t2m": 26.0, "model": 643.0, "site": 3748.0, "levels": standard_column(24.0, 0.0065)},
    {"name": "summit at night", "t2m": 9.0, "model": 643.0, "site": 3748.0, "levels": INVERSION},
    {"name": "slope just above", "t2m": 12.0, "model": 900.0, "site": 1000.0, "levels": INVERSION},
    {"name": "valley", "t2m": 15.0, "model": 1400.0, "site": 600.0, "levels": standard_column(20.0, 0.0065)},
    {"name": "valley at night", "t2m": -3.0, "model": 900.0, "site": 400.0, "levels": INVERSION},
    {"name": "on the model ground", "t2m": 5.0, "model": 800.0, "site": 800.0, "levels": INVERSION},
    {"name": "above 500 hPa", "t2m": -8.0, "model": 4500.0, "site": 8000.0, "levels": standard_column(28.0, 0.006)},
    {"name": "below 1000 hPa", "t2m": 30.0, "model": -20.0, "site": 300.0, "levels": standard_column(30.0, 0.0098)},
    {"name": "two surfaces", "t2m": 11.0, "model": 700.0, "site": 2600.0, "levels": [INVERSION[2], INVERSION[4]]},
    {"name": "out of height order", "t2m": 4.0, "model": 1200.0, "site": 2900.0, "levels": INVERSION[::-1]},
    {"name": "no column", "t2m": 4.0, "model": 1200.0, "site": 2900.0, "levels": [INVERSION[2]]},
]
_rng = np.random.default_rng(33)
for _index in range(8):
    _ground = float(_rng.uniform(0.0, 2500.0))
    CASES.append(
        {
            "name": f"random {_index}",
            "t2m": float(_rng.uniform(-30.0, 35.0)),
            "model": _ground,
            "site": _ground + float(_rng.uniform(-800.0, 3500.0)),
            "levels": standard_column(float(_rng.uniform(-25.0, 35.0)), float(_rng.uniform(-0.003, 0.0095))),
        }
    )


def golden() -> list[dict]:
    return [
        {
            "name": case["name"],
            "t2m": case["t2m"],
            "model": case["model"],
            "site": case["site"],
            "levels": [{"height": height, "temperature": temperature} for height, temperature in case["levels"]],
            "siteTemperature": site_temperature(case["t2m"], case["model"], case["site"], case["levels"]),
        }
        for case in CASES
    ]


if __name__ == "__main__":
    GOLDEN.write_text(json.dumps(golden(), indent=2) + "\n")
    print(f"wrote {GOLDEN}")
