"""Error type for all update failures."""


class UpdateError(Exception):
    """Raised on any failed update step (network, checksum, file swap)."""
