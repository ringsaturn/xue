"""Feed families: how a run of each source is fetched.

A source names its family in :attr:`~xuebuild.sources.SourceSpec.feed`, and
:mod:`xuebuild.fetch` asks the family's :class:`Fetcher` instead of testing
the source's id. A family is an upstream service and the shape of its
objects, not a source: the IFS at both resolutions and the AIFS are one
``ecmwf-open-data`` feed, every geostationary disk is one ``satellite``
feed. What a family knows about one source comes from the source's own
fields (``platform``, ``open_meteo``, ``companion_files``, …) or from a
table keyed by its id in the family's code (``ECMWF_OPEN_DATA_MODELS``).

The families' code stays in :mod:`xuebuild.fetch` beside the shared
request, range and index helpers it is written against; a fetcher here
names which of it serves which question. Every call goes through the
module attribute at call time, so a test that patches
``xuebuild.fetch.<name>`` patches what the feed runs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from . import aurora, cmadust, cmageps, fetch, hkonowcast, silam
from .idx import ByteRange
from .errors import DownloadError
from .model import GfsRun
from .sources import SourceSpec

LOG = logging.getLogger(__name__)

Exists = Callable[[str], bool]
InputIds = tuple[str, ...] | None
BBox = tuple[float, float, float, float] | None


class Fetcher(Protocol):
    """What :mod:`xuebuild.fetch` asks of a feed family."""

    def latest_slot(self, spec: SourceSpec, *, now: datetime | None = None) -> datetime:
        """The newest frame a live observation feed holds."""
        ...

    def window_start(self, spec: SourceSpec, newest: datetime, hours: int, raw_root: Path | None) -> datetime:
        """Where the live window whose newest frame is ``newest`` starts:
        the run ``--run latest`` resolves to."""
        ...

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        """Whether ``run`` has landed through ``hours``."""
        ...

    def object_url(self, spec: SourceSpec, run: GfsRun, forecast_hour: int) -> str:
        """The object one forecast hour is fetched from."""
        ...

    def fetch_run(
        self, spec: SourceSpec, run: GfsRun, hours: int, raw_root: Path, *, force: bool, input_ids: InputIds, bbox: BBox
    ) -> list[Path]:
        """Fetch a run (or an observation window) under ``raw_root``."""
        ...


class Feed:
    """The answers a family without the capability gives."""

    def latest_slot(self, spec: SourceSpec, *, now: datetime | None = None) -> datetime:
        raise DownloadError(f"{spec.manifest_model} is not a live observation source")

    def window_start(self, spec: SourceSpec, newest: datetime, hours: int, raw_root: Path | None) -> datetime:
        # The hour ``hours - 1`` before the newest frame: every feed whose
        # own listing or archive reaches that far back.
        return newest.replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours - 1)

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        raise DownloadError(f"{spec.manifest_model} is read from a local file, not fetched")

    def object_url(self, spec: SourceSpec, run: GfsRun, forecast_hour: int) -> str:
        raise DownloadError(f"{spec.manifest_model} has no object per forecast hour")

    def fetch_run(
        self, spec: SourceSpec, run: GfsRun, hours: int, raw_root: Path, *, force: bool, input_ids: InputIds, bbox: BBox
    ) -> list[Path]:
        raise DownloadError(f"{spec.manifest_model} is read from a local file, not fetched")


# -- one GRIB per forecast hour ------------------------------------------------


class FrameFeed(Feed):
    """A forecast feed whose frame is one GRIB per forecast hour, cut out of
    the upstream objects by :func:`xuebuild.fetch.fetch_frame` and repacked
    there when the source's ``packing`` asks."""

    frame_attempts = 1
    """How often a failed frame is tried before the run fails."""

    def frame_payload(
        self, spec: SourceSpec, run: GfsRun, forecast_hour: int, destination: Path, input_ids: InputIds
    ) -> bytes:
        """One frame's GRIB records, in the source's input order."""
        raise NotImplementedError

    def prepare(self, spec: SourceSpec, run: GfsRun, forecast_hours: list[int], destination: Path, *, force: bool) -> None:
        """Whatever the frames are cut from, fetched before the first frame."""

    def fetch_run(
        self, spec: SourceSpec, run: GfsRun, hours: int, raw_root: Path, *, force: bool, input_ids: InputIds, bbox: BBox
    ) -> list[Path]:
        destination = raw_root / f"{spec.id}.{run.id}"
        forecast_hours = spec.forecast_hours(hours, cycle=run.time.hour)
        self.prepare(spec, run, forecast_hours, destination, force=force)
        return fetch.fetch_frames(
            spec, run, forecast_hours, destination, force=force, input_ids=input_ids, attempts=self.frame_attempts
        )


class NoaaGrib(FrameFeed):
    """A NOAA bucket object per forecast hour with an ``.idx`` sidecar
    (GFS ``pgrb2`` with its GFS-Wave companion, ``sflux``, GEFS-Aerosols):
    the records come down as byte ranges, and both ends of every family
    prove a run."""

    def __init__(self, url: Callable[[GfsRun, int], str]) -> None:
        self._url = url

    def object_url(self, spec: SourceSpec, run: GfsRun, forecast_hour: int) -> str:
        return self._url(run, forecast_hour)

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        return fetch._grib_run_is_complete(spec, run, hours, exists)

    def frame_payload(
        self, spec: SourceSpec, run: GfsRun, forecast_hour: int, destination: Path, input_ids: InputIds
    ) -> bytes:
        return fetch._download_noaa_payload(run, forecast_hour, spec, input_ids)


class NoaaHrrr(FrameFeed):
    """HRRR on two mirrors whose hours land out of order."""

    def object_url(self, spec: SourceSpec, run: GfsRun, forecast_hour: int) -> str:
        return fetch.hrrr_object_url(run, forecast_hour)

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        return fetch._hrrr_run_is_complete(spec, run, hours, exists)

    def frame_payload(
        self, spec: SourceSpec, run: GfsRun, forecast_hour: int, destination: Path, input_ids: InputIds
    ) -> bytes:
        return fetch._download_hrrr_payload(run, forecast_hour, spec, input_ids)


class NoaaRrfs(NoaaGrib):
    """RRFS on the bucket NODD fills out of order: every hour proves a run."""

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        return fetch._rrfs_run_is_complete(spec, run, hours, exists)


class EcmwfOpenData(FrameFeed):
    """The ECMWF open data service (:data:`xuebuild.fetch.ECMWF_OPEN_DATA_MODELS`):
    ``.index`` sidecars, three mirrors, the ``wave`` stream companion."""

    frame_attempts = fetch.ECMWF_FRAME_ATTEMPTS

    def object_url(self, spec: SourceSpec, run: GfsRun, forecast_hour: int) -> str:
        # On a split source the surface part, which every frame touches.
        part = "sfc" if fetch.ecmwf_open_data(spec.id).split else None
        return fetch.ecmwf_object_url(run, forecast_hour, model=spec.id, part=part)

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        return fetch._ecmwf_run_is_complete(spec, run, hours, exists)

    def frame_payload(
        self, spec: SourceSpec, run: GfsRun, forecast_hour: int, destination: Path, input_ids: InputIds
    ) -> bytes:
        return fetch._download_ecmwf_payload(run, forecast_hour, spec, input_ids)


class DwdIconEu(FrameFeed):
    """DWD open data: one bzip2 object per field and step."""

    def object_url(self, spec: SourceSpec, run: GfsRun, forecast_hour: int) -> str:
        # One object per field: the temperature's names the frame.
        return fetch.icon_eu_object_url(run, forecast_hour, "tmp2m")

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        return fetch._icon_eu_run_is_complete(spec, run, hours, exists)

    def frame_payload(
        self, spec: SourceSpec, run: GfsRun, forecast_hour: int, destination: Path, input_ids: InputIds
    ) -> bytes:
        return fetch._download_icon_eu_payload(run, forecast_hour, spec, input_ids)


class MeteoFranceArpege(FrameFeed):
    """Météo-France open data: whole multi-step packages, downloaded before
    the frames are cut out of them."""

    def object_url(self, spec: SourceSpec, run: GfsRun, forecast_hour: int) -> str:
        return fetch.arpege_package_url(run, fetch.arpege_package(forecast_hour))

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        return fetch._arpege_run_is_complete(spec, run, hours, exists)

    def prepare(self, spec: SourceSpec, run: GfsRun, forecast_hours: list[int], destination: Path, *, force: bool) -> None:
        # The packages the frames not on disk yet reach.
        missing = [
            hour
            for hour in forecast_hours
            if force or not (destination / f"{spec.id}.{run.id}.f{hour:03d}.grib2").exists()
        ]
        if missing:
            fetch._fetch_arpege_packages(run, missing, destination, force=force)

    def frame_payload(
        self, spec: SourceSpec, run: GfsRun, forecast_hour: int, destination: Path, input_ids: InputIds
    ) -> bytes:
        return fetch._download_arpege_payload(run, forecast_hour, spec, destination, input_ids)


class CmaGeps(FrameFeed):
    """CMA GRAPES GEPS's ensemble product files off two WIS2 caches
    (:mod:`xuebuild.cmageps`): one file per lead time with no index, whose
    records are found by walking the message heads over Range against the
    registry's ensemble rules."""

    def __init__(self) -> None:
        # The run's listing, taken once before the frames: every frame
        # reads its file from it.
        self._listings: dict[tuple[str, str], dict[int, list[cmageps.CacheObject]]] = {}

    def object_url(self, spec: SourceSpec, run: GfsRun, forecast_hour: int) -> str:
        return cmageps.object_url(run, forecast_hour)

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        # Neither cache holds every file of a run; their listings together
        # must name every lead time.
        return cmageps.run_is_complete(spec, run, hours, fetch=fetch.fetch_text)

    def prepare(self, spec: SourceSpec, run: GfsRun, forecast_hours: list[int], destination: Path, *, force: bool) -> None:
        objects = cmageps.list_run(run, fetch=fetch.fetch_text)
        missing = [hour for hour in forecast_hours if not objects.get(hour)]
        if missing:
            # A cache read by name may still hold them; a frame no cache
            # serves fails the run.
            LOG.warning(
                "%s run %s: no listing names +%s h; trying the caches read by name",
                spec.manifest_model,
                run.id,
                ", +".join(map(str, missing[:8])) + (" ..." if len(missing) > 8 else ""),
            )
        self._listings[(spec.id, run.id)] = objects

    def frame_payload(
        self, spec: SourceSpec, run: GfsRun, forecast_hour: int, destination: Path, input_ids: InputIds
    ) -> bytes:
        objects = self._listings.get((spec.id, run.id))
        if objects is None:
            objects = cmageps.list_run(run, fetch=fetch.fetch_text)
        return cmageps.frame_records(
            spec,
            run,
            forecast_hour,
            fetch._frame_variable_ids(spec, forecast_hour, input_ids),
            cmageps.candidates(run, forecast_hour, objects),
            fetch.fetch_range,
        )


# -- one series per variable ---------------------------------------------------


class NoaaCfs(Feed):
    """CFSv2's per-variable time series objects."""

    def object_url(self, spec: SourceSpec, run: GfsRun, forecast_hour: int) -> str:
        # No object per forecast hour: a run is one object per variable
        # holding the whole series. The hour is ignored and the run's
        # reference object named — the one a probe would ask for.
        return fetch.cfs_variable_url(run, spec.input_variable_ids[0])

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        return fetch._cfs_run_is_complete(spec, run, hours)

    def fetch_run(
        self, spec: SourceSpec, run: GfsRun, hours: int, raw_root: Path, *, force: bool, input_ids: InputIds, bbox: BBox
    ) -> list[Path]:
        return fetch._fetch_cfs_run(spec, run, hours, raw_root, force=force, input_ids=input_ids)


class OpenMeteo(Feed):
    """The Open-Meteo open data bucket, read through ``om2nc``."""

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        return fetch._open_meteo_run_is_complete(spec, run)

    def fetch_run(
        self, spec: SourceSpec, run: GfsRun, hours: int, raw_root: Path, *, force: bool, input_ids: InputIds, bbox: BBox
    ) -> list[Path]:
        return fetch._fetch_open_meteo_run(spec, run, hours, raw_root, force=force, input_ids=input_ids)


class CmaDust(Feed):
    """CMA's dust forecast off two WIS2 caches (:mod:`xuebuild.cmadust`)."""

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        # Neither cache holds every file of a run; their listings together
        # must name every lead time.
        return cmadust.run_is_complete(spec, run, hours, fetch=fetch.fetch_text)

    def fetch_run(
        self, spec: SourceSpec, run: GfsRun, hours: int, raw_root: Path, *, force: bool, input_ids: InputIds, bbox: BBox
    ) -> list[Path]:
        return cmadust.fetch_run(
            spec,
            run,
            hours,
            raw_root,
            force=force,
            input_ids=input_ids,
            fetch_text=fetch.fetch_text,
            read_range=fetch.fetch_range,
        )


class FmiSilam(Feed):
    """FMI's SILAM surface fields on AWS open data (:mod:`xuebuild.silam`)."""

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        # The whole run lands at once; the folder must hold every field's
        # day files.
        return silam.run_is_complete(spec, run, hours, fetch=fetch.fetch_text)

    def fetch_run(
        self, spec: SourceSpec, run: GfsRun, hours: int, raw_root: Path, *, force: bool, input_ids: InputIds, bbox: BBox
    ) -> list[Path]:
        return silam.fetch_run(
            spec,
            run,
            hours,
            raw_root,
            force=force,
            input_ids=input_ids,
            fetch_text=fetch.fetch_text,
            fetch_bytes=fetch_silam_file,
        )


def fetch_silam_file(url: str) -> bytes:
    """One SILAM day file, whole: some 35 MB, so a longer budget than a
    GRIB range's."""
    return fetch.get_ok(url, timeout=silam.FILE_TIMEOUT, max_elapsed=silam.FILE_MAX_ELAPSED)


# -- observation windows -------------------------------------------------------


class NoaaMrms(Feed):
    """The MRMS bucket: one gzipped GRIB per product and scan."""

    def latest_slot(self, spec: SourceSpec, *, now: datetime | None = None) -> datetime:
        return fetch.latest_mrms_slot(spec, now=now)

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        return fetch._mrms_run_is_complete(spec, run, hours)

    def fetch_run(
        self, spec: SourceSpec, run: GfsRun, hours: int, raw_root: Path, *, force: bool, input_ids: InputIds, bbox: BBox
    ) -> list[Path]:
        return fetch._fetch_mrms_run(spec, run, hours, raw_root, force=force, input_ids=input_ids)


class JmaNowcast(Feed):
    """JMA's nowcast tiles, assembled by ``jma-radar``."""

    def latest_slot(self, spec: SourceSpec, *, now: datetime | None = None) -> datetime:
        return fetch.latest_jma_slot(spec, now=now)

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        return fetch._jma_run_is_complete(spec, run, hours)

    def fetch_run(
        self, spec: SourceSpec, run: GfsRun, hours: int, raw_root: Path, *, force: bool, input_ids: InputIds, bbox: BBox
    ) -> list[Path]:
        return fetch._fetch_jma_run(spec, run, hours, raw_root, force=force, input_ids=input_ids)


class CmaArchive(Feed):
    """The CMA radar mosaic read out of its archive (``XUE_CMA_ARCHIVE``)."""

    def latest_slot(self, spec: SourceSpec, *, now: datetime | None = None) -> datetime:
        return fetch.latest_cma_slot(spec, now=now)

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        return fetch._cma_run_is_complete(spec, run, hours)

    def fetch_run(
        self, spec: SourceSpec, run: GfsRun, hours: int, raw_root: Path, *, force: bool, input_ids: InputIds, bbox: BBox
    ) -> list[Path]:
        return fetch._fetch_cma_run(spec, run, hours, raw_root, force=force, input_ids=input_ids)


class Ovation(Feed):
    """SWPC's OVATION aurora nowcast (:mod:`xuebuild.aurora`)."""

    def latest_slot(self, spec: SourceSpec, *, now: datetime | None = None) -> datetime:
        return fetch.latest_aurora_slot(spec, now=now)

    def window_start(self, spec: SourceSpec, newest: datetime, hours: int, raw_root: Path | None) -> datetime:
        # The live service keeps only the newest grid, so the window grows
        # from the frame cache and may not yet reach back a whole window:
        # it starts at the hour of the earliest cached frame, so the run id
        # and the manifest's run time (the first frame's hour, the
        # observation rule) agree from the first round. An empty cache
        # starts at the newest frame's own hour.
        start = super().window_start(spec, newest, hours, raw_root)
        if raw_root is None:
            return start
        frames = aurora.cached_frames(raw_root / aurora.FRAMES_DIRNAME)
        if not frames:
            return newest.replace(minute=0, second=0, microsecond=0)
        return max(start, frames[0][0].replace(minute=0, second=0, microsecond=0))

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        return fetch._aurora_run_is_complete(spec, run, hours)

    def fetch_run(
        self, spec: SourceSpec, run: GfsRun, hours: int, raw_root: Path, *, force: bool, input_ids: InputIds, bbox: BBox
    ) -> list[Path]:
        return fetch._fetch_aurora_run(spec, run, hours, raw_root, force=force, input_ids=input_ids)


class HkoNowcast(Feed):
    """The Hong Kong Observatory's rainfall nowcast CSV on DATA.GOV.HK
    (:mod:`xuebuild.hkonowcast`): the newest issue alone."""

    @staticmethod
    def _head(length: int) -> bytes:
        return fetch.fetch_range(hkonowcast.URL, ByteRange(0, length - 1))

    def latest_slot(self, spec: SourceSpec, *, now: datetime | None = None) -> datetime:
        return hkonowcast.latest_slot(fetch_head=self._head)

    def window_start(self, spec: SourceSpec, newest: datetime, hours: int, raw_root: Path | None) -> datetime:
        # A window is one issue, every frame of it ahead of the clock: it
        # starts at the hour its first frame falls in, which the plain rule
        # misses whenever that frame is in the first half of an hour.
        return hkonowcast.window_start(newest)

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        return hkonowcast.run_is_complete(run, fetch_head=self._head)

    def fetch_run(
        self, spec: SourceSpec, run: GfsRun, hours: int, raw_root: Path, *, force: bool, input_ids: InputIds, bbox: BBox
    ) -> list[Path]:
        return hkonowcast.fetch_run(
            spec, run, hours, raw_root, force=force, input_ids=input_ids, fetch_bytes=lambda url: fetch.get_ok(url)
        )


class Satellite(Feed):
    """A geostationary disk, read through its platform's reader
    (:attr:`~xuebuild.sources.SourceSpec.platform`)."""

    def latest_slot(self, spec: SourceSpec, *, now: datetime | None = None) -> datetime:
        return fetch.latest_satellite_slot(spec, now=now)

    def is_complete(
        self, spec: SourceSpec, run: GfsRun, hours: int, exists: Exists, *, now: datetime | None = None
    ) -> bool:
        return fetch._satellite_run_is_complete(spec, run, hours, now=now)

    def fetch_run(
        self, spec: SourceSpec, run: GfsRun, hours: int, raw_root: Path, *, force: bool, input_ids: InputIds, bbox: BBox
    ) -> list[Path]:
        return fetch._fetch_satellite_run(spec, run, hours, raw_root, force=force, input_ids=input_ids, bbox=bbox)


#: Every feed family, by the name a source's ``feed`` gives.
FEEDS: dict[str, Fetcher] = {
    "noaa-gfs": NoaaGrib(fetch.object_url),
    "noaa-sflux": NoaaGrib(fetch.sflux_object_url),
    "noaa-gefs-aerosols": NoaaGrib(fetch.gefsaero_object_url),
    "noaa-hrrr": NoaaHrrr(),
    "noaa-rrfs": NoaaRrfs(fetch.rrfs_object_url),
    "noaa-cfs": NoaaCfs(),
    "noaa-mrms": NoaaMrms(),
    "ecmwf-open-data": EcmwfOpenData(),
    "dwd-icon-eu": DwdIconEu(),
    "meteofrance-arpege": MeteoFranceArpege(),
    "cmageps": CmaGeps(),
    "open-meteo": OpenMeteo(),
    "cmadust": CmaDust(),
    "fmi-silam": FmiSilam(),
    "jma-nowcast": JmaNowcast(),
    "cma-archive": CmaArchive(),
    "ovation": Ovation(),
    "hko-nowcast": HkoNowcast(),
    "satellite": Satellite(),
    # Read from a file or directory a tool wrote: nothing to fetch.
    "local": Feed(),
}


def fetcher(spec: SourceSpec) -> Fetcher:
    """The fetcher of ``spec``'s feed family."""
    try:
        return FEEDS[spec.feed]
    except KeyError:
        raise DownloadError(f"{spec.manifest_model} names an unknown feed: {spec.feed}") from None
