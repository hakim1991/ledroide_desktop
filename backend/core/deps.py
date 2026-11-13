"""Checks whether a module's declared Python requirements are satisfied, without importing them."""
import importlib
import importlib.metadata as md
import logging

try:
    from packaging.requirements import InvalidRequirement, Requirement
except ImportError:  # pragma: no cover - packaging ships with setuptools/pip
    from pip._vendor.packaging.requirements import InvalidRequirement, Requirement

log = logging.getLogger("ledroide.deps")


def missing_requirements(requirements: list[str]) -> list[str]:
    """Requirements that are not installed, or installed in a version outside the specifier.
    Extras are not verified (only the base distribution). Unparsable entries are reported missing."""
    importlib.invalidate_caches()
    missing: list[str] = []
    for raw in requirements:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            log.warning("unparsable requirement %r", raw)
            missing.append(raw)
            continue
        if req.marker is not None and not req.marker.evaluate():
            continue
        try:
            version = md.version(req.name)
        except md.PackageNotFoundError:
            missing.append(raw)
            continue
        if req.specifier and not req.specifier.contains(version, prereleases=True):
            missing.append(raw)
    return missing
