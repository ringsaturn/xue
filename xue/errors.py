class XueError(Exception):
    """An expected, user-actionable pipeline error."""


class DownloadError(XueError):
    """NOAA download or HTTP validation failed."""


class ConversionError(XueError):
    """A GRIB file or external conversion command was invalid."""


class ManifestError(XueError):
    """The public manifest violates the versioned contract."""


class BundleError(XueError):
    """A Xue bundle violates the versioned binary contract."""

