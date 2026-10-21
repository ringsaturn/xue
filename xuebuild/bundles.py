"""The bundle registry: every published bundle that is more than the
scalar of the variable of the same id (docs/registry.md §Bundles).

A scalar variable ships one single-variable bundle each, named by its id,
and needs no entry here; a scalar the converter *derives* has one, naming
its inputs and the algorithm. A vector field ships its two components
together in one two-variable bundle for the frontend's magnitude shader
and particle layer: the 10 m wind, the wind on each isobaric surface, the
water vapour flux the converter derives on each surface from the specific
humidity and the wind there, and the wave vectors it derives from a
height and a direction. A composite's members were produced in the fetch
stage and are read off the series; a volume's members are read directly,
lowest level first.

``kind`` and ``derivation.kind`` are names out of closed sets the encoders
implement (``docs/registry.md`` §"Closed sets"); the arithmetic each
derivation names lives in :mod:`xuebuild.binconvert` and is mirrored in
``rust/xue/src/encode/convert.rs``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ConversionError
from .satellite.platforms import PLATFORMS
from .satellite.producers import PRODUCERS
from .variables import (
    CAT_LEVELS_HPA,
    CLOUD_WATER_VARIABLE_IDS,
    DUST_CF_BUNDLE_ID,
    DUST_CF_COMPONENT_IDS,
    DUST_RGB_BUNDLE_ID,
    DUST_RGB_COMPONENT_IDS,
    ISOBARIC_LEVELS_HPA,
    REFLECTIVITY_VARIABLE_IDS,
    SWELL_VECTOR_COMPONENT_IDS,
    TRUE_COLOR_BUNDLE_ID,
    TRUE_COLOR_COMPONENT_IDS,
    WAVE_VECTOR_COMPONENT_IDS,
    WIND_WAVE_VECTOR_COMPONENT_IDS,
    ZHOUYE_BUNDLE_ID,
    ZHOUYE_COMPONENT_IDS,
    isobaric_variable,
)

BUNDLE_KINDS = frozenset({"scalar", "vector", "composite", "volume"})
DERIVATIONS = frozenset(
    {
        "vapour-flux",
        "wave-vector",
        "theta-e",
        "precipitation-type",
        "clear-air-turbulence",
        "precipitation-rate",
    }
)


@dataclass(frozen=True)
class Derivation:
    """How a bundle's members are computed in the converter: the named
    algorithm over ``inputs``, on the isobaric surface ``level_hpa`` where
    the algorithm takes one."""

    kind: str
    inputs: tuple[str, ...]
    level_hpa: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in DERIVATIONS:
            raise ValueError(f"unknown derivation: {self.kind}")

    def document(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"kind": self.kind, "inputs": list(self.inputs)}
        if self.level_hpa is not None:
            entry["levelHpa"] = self.level_hpa
        return entry


@dataclass(frozen=True)
class BundleSpec:
    """One bundle that is not a plain scalar: its ``kind``, its ``members``
    in file order (``variableId`` 1…n; a scalar's is itself), the
    ``derivation`` that computes them, and for a composite the producer
    that made them, the channels it reads and the subset of those a
    source may lack (read when the source fetches them, left out
    otherwise)."""

    id: str
    kind: str
    members: tuple[str, ...] = ()
    derivation: Derivation | None = None
    producer: str | None = None
    inputs: tuple[str, ...] = ()
    optional_inputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in BUNDLE_KINDS:
            raise ValueError(f"{self.id}: unknown bundle kind {self.kind}")
        if self.kind == "vector" and len(self.members) != 2:
            raise ValueError(f"{self.id}: a vector bundle has two members")
        if self.kind == "scalar" and (self.members or self.derivation is None):
            raise ValueError(f"{self.id}: a listed scalar is a derived one")

    @property
    def variable_ids(self) -> tuple[str, ...]:
        """The variables the bundle's arrays hold, in bundle order."""
        return self.members if self.kind != "scalar" else (self.id,)

    def document(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"kind": self.kind}
        if self.kind != "scalar":
            entry["members"] = list(self.members)
        if self.kind == "composite":
            entry["producer"] = self.producer
            entry["inputs"] = list(self.inputs)
            entry["optionalInputs"] = list(self.optional_inputs)
        if self.derivation is not None:
            entry["derivation"] = self.derivation.document()
        return entry


WIND_COMPONENT_IDS = ("ugrd10m", "vgrd10m")
WIND_BUNDLE_ID = "wind10m"
WAVE_BUNDLE_ID = "wave"
WIND_WAVE_BUNDLE_ID = "windwave"
SWELL_BUNDLE_ID = "swell"
# The 80 m wind pair HRRR writes for wind power, a vector bundle of its own.
WIND_80M_COMPONENT_IDS = ("ugrd80m", "vgrd80m")
WIND_80M_BUNDLE_ID = "wind80m"
# The 100 m wind pair, the hub height of a modern turbine: the same parameters
# on the 100 m surface, a vector bundle of its own.
WIND_100M_COMPONENT_IDS = ("ugrd100m", "vgrd100m")
WIND_100M_BUNDLE_ID = "wind100m"

# The clear-air turbulence index on each of its surfaces: the deformation is
# taken on the surface itself, the vertical shear across the pair of
# surfaces ``(above, below)`` that brackets it among the eight registered —
# centred at 250 hPa, one-sided at the two ends.
CAT_SHEAR_LAYERS: dict[int, tuple[int, int]] = {300: (250, 300), 250: (200, 300), 200: (200, 250)}


def _cat_input_ids(level: int) -> tuple[str, ...]:
    """The inputs of ``cat<level>``, each once: the wind and height above,
    then below, then the wind on the surface (already listed for 300 and
    200, whose surface is one end of the layer)."""
    above, below = CAT_SHEAR_LAYERS[level]
    return tuple(
        dict.fromkeys(
            (
                f"ugrd{above}",
                f"vgrd{above}",
                f"hgt{above}",
                f"ugrd{below}",
                f"vgrd{below}",
                f"hgt{below}",
                f"ugrd{level}",
                f"vgrd{level}",
            )
        )
    )


def _composite(bundle_id: str, members: tuple[str, ...]) -> BundleSpec:
    """A composite bundle: its members in file order, the producer that
    made them in the fetch stage (xuebuild/satellite/producers.py) and the
    channels it reads. An input the recipe has a stand-in for on an imager
    that lacks it is optional: read when the source fetches it, left out
    when it does not. A platform the recipe cannot run on at all drops
    nothing here."""
    producer = PRODUCERS[bundle_id]
    optional: set[str] = set()
    for platform in PLATFORMS.values():
        try:
            optional.update(set(producer.inputs) - set(producer.inputs_for(platform)))
        except ConversionError:
            continue
    return BundleSpec(
        bundle_id,
        "composite",
        members=members,
        producer=producer.id,
        inputs=producer.inputs,
        optional_inputs=tuple(input_id for input_id in producer.inputs if input_id in optional),
    )


def _wave_vector(bundle_id: str, members: tuple[str, str], height_id: str, direction_id: str) -> BundleSpec:
    return BundleSpec(bundle_id, "vector", members=members, derivation=Derivation("wave-vector", (height_id, direction_id)))


BUNDLES: dict[str, BundleSpec] = {
    spec.id: spec
    for spec in (
        BundleSpec(WIND_BUNDLE_ID, "vector", members=WIND_COMPONENT_IDS),
        BundleSpec(WIND_80M_BUNDLE_ID, "vector", members=WIND_80M_COMPONENT_IDS),
        BundleSpec(WIND_100M_BUNDLE_ID, "vector", members=WIND_100M_COMPONENT_IDS),
        *(BundleSpec(f"wind{level}", "vector", members=(f"ugrd{level}", f"vgrd{level}")) for level in ISOBARIC_LEVELS_HPA),
        # A vapour flux pair is derived from the specific humidity and both
        # wind components on its surface.
        *(
            BundleSpec(
                f"qflux{level}",
                "vector",
                members=(f"uqflx{level}", f"vqflx{level}"),
                derivation=Derivation("vapour-flux", (f"spfh{level}", f"ugrd{level}", f"vgrd{level}")),
            )
            for level in ISOBARIC_LEVELS_HPA
        ),
        # Each wave vector from a height and a direction: the combined sea's,
        # the wind sea's and the first swell partition's.
        _wave_vector(WAVE_BUNDLE_ID, WAVE_VECTOR_COMPONENT_IDS, "htsgw", "dirpw"),
        _wave_vector(WIND_WAVE_BUNDLE_ID, WIND_WAVE_VECTOR_COMPONENT_IDS, "wvhgt", "wvdir"),
        _wave_vector(SWELL_BUNDLE_ID, SWELL_VECTOR_COMPONENT_IDS, "swhgt", "swdir"),
        # The composites, in manifest order: the Dust RGB's three guns, then
        # the DEBRA confidence and the ZHOUYE confidence, two produced
        # bundles of one variable each, then the true colour composite's
        # three guns.
        _composite(DUST_RGB_BUNDLE_ID, DUST_RGB_COMPONENT_IDS),
        _composite(DUST_CF_BUNDLE_ID, DUST_CF_COMPONENT_IDS),
        _composite(ZHOUYE_BUNDLE_ID, ZHOUYE_COMPONENT_IDS),
        _composite(TRUE_COLOR_BUNDLE_ID, TRUE_COLOR_COMPONENT_IDS),
        # The volumes: the MRMS reflectivity mosaic's 33 constant-altitude
        # levels and a model's cloud water on 24, lowest first, so a cell's
        # column is one tile's adjacent chunks. Nothing is derived; the
        # bundle is a packaging of its members.
        BundleSpec("refl3d", "volume", members=REFLECTIVITY_VARIABLE_IDS),
        BundleSpec("cloud3d", "volume", members=CLOUD_WATER_VARIABLE_IDS),
        # The derived scalars: the equivalent potential temperature on each
        # isobaric surface from the temperature and the specific humidity
        # there; the categorical precipitation type from the four NCEP
        # flags, in the order derive_ptype combines them; the clear-air
        # turbulence index on each of its surfaces. Like a vapour flux
        # bundle, listing one in a source's scalars publishes it only when
        # every input is fetched, and an input that serves only the
        # derivation is released once it is done.
        *(
            BundleSpec(
                f"thetae{level}",
                "scalar",
                derivation=Derivation("theta-e", (f"tmp{level}", f"spfh{level}"), level_hpa=level),
            )
            for level in ISOBARIC_LEVELS_HPA
        ),
        BundleSpec("ptype", "scalar", derivation=Derivation("precipitation-type", ("crain", "cfrzr", "cicep", "csnow"))),
        *(
            BundleSpec(
                f"cat{level}",
                "scalar",
                derivation=Derivation("clear-air-turbulence", _cat_input_ids(level), level_hpa=level),
            )
            for level in CAT_LEVELS_HPA
        ),
        # The precipitation rate: every source names its precipitation
        # input differently (GFS prate, ECMWF tp, sflux prate_ave,
        # Open-Meteo apcp), so the bundle's input is the first of these the
        # source lists, de-accumulated, de-averaged or taken as is by the
        # source's precipitation flags.
        BundleSpec("prate", "scalar", derivation=Derivation("precipitation-rate", ("prate", "tp", "prate_ave", "apcp"))),
    )
}


#: The one bundle whose derivation is the precipitation rate.
PRECIPITATION_RATE_BUNDLE_ID = next(
    spec.id for spec in BUNDLES.values() if spec.derivation is not None and spec.derivation.kind == "precipitation-rate"
)


def bundle(bundle_id: str) -> BundleSpec | None:
    """The bundle's entry, or None for the plain scalar bundle of the
    variable of the same id."""
    return BUNDLES.get(bundle_id)


def bundle_kind(bundle_id: str) -> str:
    spec = BUNDLES.get(bundle_id)
    return spec.kind if spec is not None else "scalar"


def derivation(bundle_id: str) -> Derivation | None:
    spec = BUNDLES.get(bundle_id)
    return spec.derivation if spec is not None else None


def is_derived_scalar(bundle_id: str) -> bool:
    """Whether the bundle is a scalar the converter computes from other
    planes after they are read — the equivalent potential temperature, the
    precipitation type, the turbulence index. The precipitation rate is
    derived too, but in the precipitation step, from one input the source
    chooses, and is not one of these."""
    derived = derivation(bundle_id)
    return bundle_kind(bundle_id) == "scalar" and derived is not None and derived.kind != "precipitation-rate"


def bundle_variable_ids(bundle_id: str) -> tuple[str, ...]:
    """The variables one bundle carries, in bundle order: a scalar's own,
    a vector's pair, a composite's components, a volume's levels."""
    spec = BUNDLES.get(bundle_id)
    return spec.variable_ids if spec is not None else (bundle_id,)


def vector_input_ids(bundle_id: str) -> tuple[str, ...]:
    """The source inputs one vector bundle is built from: a wind pair is its
    own two components; a vapour flux pair is derived from the specific
    humidity and both wind components on the same surface; the wave vector
    from the significant wave height and the primary wave direction."""
    spec = BUNDLES[bundle_id]
    return spec.derivation.inputs if spec.derivation is not None else spec.members


def _derivation_level(bundle_id: str, kind: str) -> int | None:
    derived = derivation(bundle_id)
    if derived is None or derived.kind != kind:
        return None
    return derived.level_hpa


def theta_e_level(bundle_id: str) -> int | None:
    """The isobaric surface of a ``thetae<level>`` bundle, or None."""
    return _derivation_level(bundle_id, "theta-e")


def cat_level(bundle_id: str) -> int | None:
    """The isobaric surface of a ``cat<level>`` bundle, or None."""
    return _derivation_level(bundle_id, "clear-air-turbulence")


def vapour_flux_level(bundle_id: str) -> int | None:
    """The isobaric surface of a ``qflux<level>`` bundle, or None: the
    surface its specific humidity input is on."""
    derived = derivation(bundle_id)
    if derived is None or derived.kind != "vapour-flux":
        return None
    isobaric = isobaric_variable(derived.inputs[0])
    return isobaric[1] if isobaric is not None else None


# The tables by kind, for readers that walk one kind: a vector bundle's
# members, a derived vector's inputs, a composite's members, a volume's
# levels, a derived scalar's inputs.
VECTOR_BUNDLES: dict[str, tuple[str, str]] = {
    spec.id: (spec.members[0], spec.members[1]) for spec in BUNDLES.values() if spec.kind == "vector"
}
DERIVED_VECTORS: dict[str, tuple[str, ...]] = {
    spec.id: spec.derivation.inputs for spec in BUNDLES.values() if spec.kind == "vector" and spec.derivation is not None
}
COMPOSITE_BUNDLES: dict[str, tuple[str, ...]] = {spec.id: spec.members for spec in BUNDLES.values() if spec.kind == "composite"}
VOLUME_BUNDLES: dict[str, tuple[str, ...]] = {spec.id: spec.members for spec in BUNDLES.values() if spec.kind == "volume"}
DERIVED_SCALARS: dict[str, tuple[str, ...]] = {
    spec.id: spec.derivation.inputs
    for spec in BUNDLES.values()
    if is_derived_scalar(spec.id) and spec.derivation is not None
}
