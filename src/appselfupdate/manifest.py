"""The update manifest: one JSON document describing the latest release."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import UpdateError


@dataclass
class UpdateInfo:
    """One entry from the update manifest."""

    version: str
    url: str
    sha256: str
    notes: str = ""
    min_supported: str = ""
    force: bool = False

    @classmethod
    def from_manifest(cls, data: dict) -> "UpdateInfo":
        try:
            version = str(data["version"])
            url = str(data["url"])
            sha256 = str(data["sha256"]).lower()
        except (KeyError, TypeError) as e:
            raise UpdateError(f"manifest is missing required field: {e}") from e
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise UpdateError("manifest sha256 must be 64 lowercase hex chars")
        return cls(
            version=version,
            url=url,
            sha256=sha256,
            notes=str(data.get("notes", "")),
            min_supported=str(data.get("min_supported", "")),
            force=bool(data.get("force", False)),
        )
