"""What the point products share: the tropical cyclone product
(``xuebuild/tc``), the soundings and the airports are each a small
pipeline of their own, but they publish through one contract (``docs/tc.md``
§1) — a mutable ``latest-<product>.json`` pointer naming an immutable
directory's ``index.json`` by path, byte length and CRC32, every file
served under ``?v=<crc32>``. The serialisation, the pointer shape and the
validators' primitives live here so the three cannot drift; each product
keeps its own validators and raises its own error class through them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .common import crc32_hex, iso_z, write_bytes_atomic  # noqa: F401 — re-exported for the products

POINTER_SCHEMA_VERSION = 1
CRC32 = re.compile(r"^[0-9a-f]{8}$")
ISSUE_HOUR = re.compile(r"^\d{10}$")
ROUND = re.compile(r"^\d{12}$")

ProductError = type[Exception]
"""A product's own error class (``TcProductError`` …): every check below
raises the one it is handed."""


def encode_json(payload: dict[str, Any]) -> bytes:
    """The one serialisation — compact separators, keys as built, ASCII
    escaped — so a file's CRC32 is a function of its content alone."""
    return json.dumps(
        payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


BUILD_REVISION_ENV = "XUE_BUILD_REVISION"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{7,40}$")


def sha256_hex(*parts: bytes) -> str:
    """A source's ``sha256``: the digest of the bytes it was read from, in
    the order they were fetched, so a round can be traced to its input."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.hexdigest()


def builder_block() -> dict[str, Any]:
    """The index's optional ``builder``: the commit the round was built
    from, taken from ``XUE_BUILD_REVISION`` (the workflows and the host set
    it), ``null`` when unset — so offline and golden builds stay
    byte-for-byte reproducible."""
    revision = os.environ.get(BUILD_REVISION_ENV, "").strip().lower() or None
    if revision is not None and not REVISION.fullmatch(revision):
        revision = None
    return {"name": "xuebuild", "revision": revision}


def check_builder(error: ProductError, value: object, label: str) -> None:
    """Validate an optional ``builder`` block (absent is fine)."""
    if value is None:
        return
    if not isinstance(value, dict) or value.get("name") != "xuebuild":
        raise error(f"{label} must be an object naming xuebuild")
    revision = value.get("revision")
    if revision is not None and (not isinstance(revision, str) or not REVISION.fullmatch(revision)):
        raise error(f"{label}.revision must be a commit hash or null")


def check_sha256(error: ProductError, value: object, label: str) -> None:
    """Validate an optional source ``sha256`` (absent is fine)."""
    if value is not None and (not isinstance(value, str) or not SHA256.fullmatch(value)):
        raise error(f"{label} must be 64 lowercase hex digits")


def published_source(status: Any) -> dict[str, Any]:
    """A source's status (``SourceStatus``) as the index publishes it:
    where the bytes came from, not where this build happened to put them."""
    return {key: value for key, value in status.to_json().items() if key != "file"}


def pointer_payload(
    product: str, issued: datetime, index_path: str, index_bytes: bytes
) -> dict[str, Any]:
    """The pointer's fields; the product's own validator checks them."""
    return {
        "schemaVersion": POINTER_SCHEMA_VERSION,
        "product": product,
        "issued": iso_z(issued),
        "path": index_path,
        "byteLength": len(index_bytes),
        "crc32": crc32_hex(index_bytes),
    }


def pointer_shape_error(payload: object, product: str) -> str | None:
    """The product-independent checks of a pointer: None when they pass,
    else the message. A product adds its own path rule on top."""
    if not isinstance(payload, dict):
        return f"{product} pointer must be an object"
    if payload.get("schemaVersion") != POINTER_SCHEMA_VERSION:
        return f"{product} pointer schemaVersion must be {POINTER_SCHEMA_VERSION}"
    if payload.get("product") != product:
        return f"{product} pointer product must be {product!r}"
    path = payload.get("path")
    if (
        not isinstance(path, str)
        or not path.endswith("/index.json")
        or path.startswith(("/", "http:", "https:"))
        or ".." in Path(path).parts
    ):
        return f"{product} pointer path must be a relative <issue directory>/index.json path"
    byte_length = payload.get("byteLength")
    if (
        isinstance(byte_length, bool)
        or not isinstance(byte_length, int)
        or byte_length <= 0
    ):
        return f"{product} pointer byteLength must be a positive integer"
    if not isinstance(payload.get("crc32"), str) or not CRC32.match(payload["crc32"]):
        return f"{product} pointer crc32 must be 8 lowercase hex characters"
    return None


def archive_directory(product: str, issue: datetime) -> str:
    """``sounding/2026/09/14/sounding.2026091402``: an hourly issue's
    directory under its product's year, month and day, so the archive can
    grow for years with every listing level a few dozen prefixes wide. The leaf keeps the
    product and the hour, so a directory copied anywhere still says what
    it is."""
    hour = issue.astimezone(UTC)
    return f"{product}/{hour:%Y}/{hour:%m}/{hour:%d}/{product}.{hour:%Y%m%d%H}"


def flat_directory(product: str, issue: datetime) -> str:
    """``sounding.2026091402``: the layout an hourly issue was published in
    before the archive tree. Pointers and Items naming it stay readable."""
    return f"{product}.{issue.astimezone(UTC):%Y%m%d%H}"


def archive_round_directory(product: str, moment: datetime) -> str:
    """``alerts/2026/11/01/alerts.202611010005``: a minute round's
    directory under its product's UTC year, month and day, the minute
    sibling of :func:`archive_directory`."""
    minute = moment.astimezone(UTC)
    return f"{product}/{minute:%Y}/{minute:%m}/{minute:%d}/{product}.{minute:%Y%m%d%H%M}"


def flat_round_directory(product: str, moment: datetime) -> str:
    """``alerts.202611010005``: the layout a minute round was published in
    before the archive tree. Pointers and Items naming it stay readable."""
    return f"{product}.{moment.astimezone(UTC):%Y%m%d%H%M}"


def round_directories(product: str, moment: datetime) -> tuple[str, str]:
    """Where a minute round may live: the archive tree, then flat."""
    return (archive_round_directory(product, moment), flat_round_directory(product, moment))


def existing_round_directory(output_root: Path, product: str, moment: datetime) -> Path | None:
    """The round's directory under ``output_root`` in whichever layout it
    was written (or pulled) in, the archive tree first; None when neither
    holds it."""
    for name in round_directories(product, moment):
        directory = output_root / name
        if directory.is_dir():
            return directory
    return None


def local_round_directories(output_root: Path, product: str) -> list[Path]:
    """Every ``<product>.<12 digits>`` directory under ``output_root``, flat
    or filed under ``<product>/YYYY/MM/DD/``. Only four-digit years are
    walked, so ``<product>/archive/`` and the Collection beside the days are
    never taken for rounds."""
    patterns = (f"{product}.*", f"{product}/[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/{product}.*")
    found = []
    for pattern in patterns:
        for path in output_root.glob(pattern):
            digits = path.name.split(".", 1)[1]
            if path.is_dir() and len(digits) == 12 and digits.isdigit():
                found.append(path)
    return found


def parse_issue_hour(error: ProductError, value: str) -> datetime:
    """``2026091206`` → that UTC hour: the issue id of the hourly products."""
    if not ISSUE_HOUR.match(value):
        raise error("issue must be a UTC hour, YYYYMMDDHH")
    try:
        return datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError as exc:
        raise error(f"issue is not a valid hour: {value}") from exc


def check_time(error: ProductError, value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise error(f"{label} must be an ISO 8601 UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise error(f"{label} is not a valid timestamp") from exc


def check_number(
    error: ProductError, value: object, label: str, *, minimum: float | None = None, maximum: float | None = None
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error(f"{label} must be a number")
    if math.isnan(value):
        raise error(f"{label} must not be NaN")
    if minimum is not None and value < minimum:
        raise error(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise error(f"{label} must be at most {maximum}")


def check_integer(
    error: ProductError,
    value: object,
    label: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise error(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise error(f"{label} must be at most {maximum}")
    return value


def check_optional_string(error: ProductError, value: object, label: str) -> None:
    if value is not None and not isinstance(value, str):
        raise error(f"{label} must be a string or null")


def check_crc_file(error: ProductError, value: object, label: str, path: str) -> int:
    """A ``{path, byteLength, crc32}`` entry naming a file beside the
    index; returns its byte length."""
    if not isinstance(value, dict):
        raise error(f"{label} must be an object")
    if value.get("path") != path:
        raise error(f"{label}.path must be {path!r}, the file beside the index")
    byte_length = value.get("byteLength")
    if isinstance(byte_length, bool) or not isinstance(byte_length, int) or byte_length < 0:
        raise error(f"{label}.byteLength must be a non-negative integer")
    if not isinstance(value.get("crc32"), str) or not CRC32.match(value["crc32"]):
        raise error(f"{label}.crc32 must be 8 lowercase hex characters")
    return byte_length


def check_sources(
    error: ProductError,
    sources: object,
    label: str,
    source_key: re.Pattern[str],
    extra: Callable[[dict[str, Any], str], None],
) -> None:
    """The ``sources[]`` list every index and storm file carries: objects
    with a unique, well-formed ``id``, a boolean ``ok`` and an ``error``
    where not ok. ``extra(status, where)`` checks a product's own fields of
    each entry, in turn, before the next entry is looked at."""
    if not isinstance(sources, list):
        raise error(f"{label} must be a list")
    seen: set[str] = set()
    for index, status in enumerate(sources):
        where = f"{label}[{index}]"
        if not isinstance(status, dict):
            raise error(f"{where} must be an object")
        source_id = status.get("id")
        if not isinstance(source_id, str) or not source_key.match(source_id):
            raise error(f"{where}.id must be a source id")
        if source_id in seen:
            raise error(f"{label} lists {source_id} twice")
        seen.add(source_id)
        if not isinstance(status.get("ok"), bool):
            raise error(f"{where}.ok must be a boolean")
        if not status["ok"] and not isinstance(status.get("error"), str):
            raise error(f"{where} failed without an error message")
        extra(status, where)


def check_pointer(
    error: ProductError,
    payload: object,
    product: str,
    directories: Callable[[datetime], tuple[str, ...]],
    mismatch: str,
) -> None:
    """The shared pointer checks, then the product's path rule: the path
    names one of the directories the issue it says it is may live in
    (``mismatch`` if not)."""
    message = pointer_shape_error(payload, product)
    if message is not None:
        raise error(message)
    assert isinstance(payload, dict)
    issued = check_time(error, payload.get("issued"), "pointer.issued")
    if posixpath.dirname(payload["path"]) not in directories(issued):
        raise error(mismatch)


def read_index_file(
    error: ProductError, path: Path, product: str, validate: Callable[[object], None]
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise error(f"cannot read {product} index {path}: {exc}") from exc
    validate(payload)
    return payload


def load_previous_index(
    path: Path | None,
    output_root: Path,
    *,
    product: str,
    pointer_filename: str,
    read_index: Callable[[Path], dict[str, Any]],
    error: ProductError,
    log: logging.Logger,
) -> dict[str, Any] | None:
    """The previous issue's index: the one given, else the one the local
    pointer names, else nothing (a first build, or a fresh checkout). An
    explicitly given index must be valid; one found through the pointer
    that is not is skipped with a warning."""
    if path is not None:
        return read_index(path)
    pointer = output_root / pointer_filename
    if not pointer.exists():
        return None
    try:
        named = json.loads(pointer.read_text(encoding="utf-8")).get("path")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(named, str):
        return None
    index_path = output_root / named
    if not index_path.exists():
        return None
    try:
        return read_index(index_path)
    except error as exc:
        log.warning("%s: ignoring the previous index at %s: %s", product, index_path, exc)
        return None


Bounds = tuple[float | None, float | None]


@dataclass(frozen=True)
class RoundProduct:
    """The skeleton every minute-round product (airport, synop, aq,
    alerts, fire, nexrad) repeated: round parsing and flooring, the
    directory a round lives in, the pointer, and the primitives its
    validators are built from. A product binds one of these to its name,
    its cadence and its own error class, and keeps only its line
    validators.

    ``archived`` rounds are filed under their UTC day
    (:func:`archive_round_directory`) and a pointer may also name the flat
    layout; an unarchived product (a round carries the whole window and is
    short-lived) is flat only."""

    product: str
    error: ProductError
    cadence_minutes: int = 10
    archived: bool = True

    def parse_round(self, value: str) -> datetime:
        """``202610050010`` → the round's UTC minute. A minute that is not
        a multiple of the cadence names no round."""
        if not ROUND.match(value):
            raise self.error("round must be a UTC minute, YYYYMMDDHHMM")
        try:
            moment = datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=UTC)
        except ValueError as exc:
            raise self.error(f"round is not a valid minute: {value}") from exc
        if self.cadence_minutes >= 60:
            if moment.minute:
                raise self.error(f"round must be a whole hour: {value}")
        elif moment.minute % self.cadence_minutes:
            raise self.error(f"round minute must be a multiple of {self.cadence_minutes}: {value}")
        return moment

    def floor_round(self, moment: datetime) -> datetime:
        """The round a moment falls in: the minute floored to the cadence.
        The publisher's clock is never exactly on it (a cron drifts by a
        minute or three), and the round name must not drift with it."""
        moment = moment.astimezone(UTC)
        if self.cadence_minutes >= 60:
            return moment.replace(minute=0, second=0, microsecond=0)
        minute = (moment.minute // self.cadence_minutes) * self.cadence_minutes
        return moment.replace(minute=minute, second=0, microsecond=0)

    @staticmethod
    def round_name(moment: datetime) -> str:
        return moment.astimezone(UTC).strftime("%Y%m%d%H%M")

    def round_directory(self, moment: datetime) -> str:
        """The round's immutable directory: ``synop/2026/10/05/synop.202610050010``
        archived, ``fire.202610120500`` flat."""
        if self.archived:
            return archive_round_directory(self.product, moment)
        return flat_round_directory(self.product, moment)

    def raw_directory(self, moment: datetime) -> str:
        """Where a round's fetched files sit under the raw root, flat: a
        raw round is read once, by its own build."""
        return flat_round_directory(self.product, moment)

    def pointer_directories(self, moment: datetime) -> tuple[str, ...]:
        """Where a pointer may name the round."""
        if self.archived:
            return round_directories(self.product, moment)
        return (flat_round_directory(self.product, moment),)

    def local_round_directory(self, output_root: Path, moment: datetime) -> Path:
        """The round's directory under ``output_root`` in whichever layout
        it was written or pulled in; where this build would write it when
        neither holds it."""
        return existing_round_directory(output_root, self.product, moment) or output_root / self.round_directory(moment)

    def build_pointer(self, issued: datetime, index_path: str, index_bytes: bytes) -> dict[str, Any]:
        payload = pointer_payload(self.product, issued, index_path, index_bytes)
        self.validate_pointer(payload)
        return payload

    def validate_pointer(self, payload: object) -> None:
        check_pointer(
            self.error,
            payload,
            self.product,
            self.pointer_directories,
            f"{self.product} pointer path does not name the issued round's directory",
        )

    def read_index(self, path: Path, validate: Callable[[object], None]) -> dict[str, Any]:
        return read_index_file(self.error, path, self.product, validate)

    # Primitives: the bounds are one (minimum, maximum) pair.

    def time(self, value: object, label: str) -> datetime:
        return check_time(self.error, value, label)

    def number(self, value: object, label: str, bounds: Bounds | None = None) -> None:
        minimum, maximum = bounds or (None, None)
        check_number(self.error, value, label, minimum=minimum, maximum=maximum)

    def optional_number(self, value: object, label: str, bounds: Bounds | None = None) -> None:
        if value is not None:
            self.number(value, label, bounds)

    def integer(self, value: object, label: str, bounds: Bounds | None = None) -> None:
        minimum, maximum = bounds or (None, None)
        check_integer(self.error, value, label, minimum, maximum)

    def optional_integer(self, value: object, label: str, bounds: Bounds | None = None) -> None:
        if value is not None:
            self.integer(value, label, bounds)

    def optional_string(self, value: object, label: str) -> None:
        check_optional_string(self.error, value, label)

    def crc_file(self, value: object, label: str, path: str) -> int:
        return check_crc_file(self.error, value, label, path)


@dataclass(frozen=True)
class IssueProduct:
    """The hourly-issue twin (tc, sounding): issues are filed under their
    UTC day and a pointer may also name the flat layout."""

    product: str
    error: ProductError

    def issue_directory(self, issue: datetime) -> str:
        return archive_directory(self.product, issue)

    def pointer_directories(self, issue: datetime) -> tuple[str, ...]:
        return (self.issue_directory(issue), flat_directory(self.product, issue))

    def parse_issue(self, value: str) -> datetime:
        return parse_issue_hour(self.error, value)

    def build_pointer(self, issue: datetime, index_path: str, index_bytes: bytes) -> dict[str, Any]:
        payload = pointer_payload(self.product, issue, index_path, index_bytes)
        self.validate_pointer(payload)
        return payload

    def validate_pointer(self, payload: object) -> None:
        check_pointer(
            self.error,
            payload,
            self.product,
            self.pointer_directories,
            f"{self.product} pointer path does not name the issued hour's directory",
        )

    def read_index(self, path: Path, validate: Callable[[object], None]) -> dict[str, Any]:
        return read_index_file(self.error, path, self.product, validate)

    def time(self, value: object, label: str) -> datetime:
        return check_time(self.error, value, label)
