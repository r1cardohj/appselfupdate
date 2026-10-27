"""Loose semver-ish version parsing and comparison."""


def version_tuple(v: str) -> tuple[int, ...]:
    """Parse '1.2.3' / 'v1.2.3' into (1, 2, 3). Non-numeric parts are ignored."""
    parts = []
    for token in v.lstrip("vV").split("."):
        digits = "".join(c for c in token if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    """True if candidate version is strictly newer than current."""
    return version_tuple(candidate) > version_tuple(current)
