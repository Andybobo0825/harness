"""Immutable GitHub Release and local installation manifest contracts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

RELEASE_MANIFEST_SCHEMA = "harness-release/v1"
INSTALL_MANIFEST_SCHEMA = "harness-install/v1"
REQUIRED_SMOKE_TESTS = ("custom_capture", "tmux_oversized_image", "lifecycle")
_STABLE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PYTHON_REQUIRES = re.compile(r"^>=(?P<major>[0-9]+)\.(?P<minor>[0-9]+)(?:\.(?P<patch>[0-9]+))?$")


class ReleaseContractError(ValueError):
    """Raised when release metadata or assets violate the stable contract."""


@dataclass(frozen=True)
class ReleaseManifest:
    schema_version: str
    version: str
    tag: str
    wheel_filename: str
    wheel_sha256: str
    python_requires: str
    platforms: tuple[str, ...]
    omx_version: str
    omx_tarball_url: str
    omx_integrity: str
    overlay_revision: str
    state_schema: str
    smoke_tests: tuple[str, ...]


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_release_manifest(path: Path | str) -> ReleaseManifest:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ReleaseContractError("Release manifest must be a JSON object")
    if raw.get("schema_version") != RELEASE_MANIFEST_SCHEMA:
        raise ReleaseContractError(f"Unsupported release manifest schema: {raw.get('schema_version')}")
    version = _required_string(raw, "version")
    if not _STABLE_VERSION.fullmatch(version):
        raise ReleaseContractError(f"Release version must be stable semver: {version}")
    tag = _required_string(raw, "tag")
    if tag != f"v{version}":
        raise ReleaseContractError(f"Release tag {tag} does not match version {version}")
    if raw.get("draft") is not False or raw.get("prerelease") is not False:
        raise ReleaseContractError("Draft and prerelease artifacts are not installable")

    wheel = _required_mapping(raw, "wheel")
    wheel_filename = _required_string(wheel, "filename")
    if (
        Path(wheel_filename).name != wheel_filename
        or "/" in wheel_filename
        or "\\" in wheel_filename
        or not wheel_filename.startswith(f"personal_harness-{version}-")
        or not wheel_filename.endswith(".whl")
    ):
        raise ReleaseContractError(
            "Wheel filename must be a path-free personal_harness artifact for the release version"
        )
    wheel_sha256 = _required_string(wheel, "sha256")
    if not _SHA256.fullmatch(wheel_sha256):
        raise ReleaseContractError("Wheel sha256 must be 64 lowercase hexadecimal characters")

    omx = _required_mapping(raw, "omx")
    platforms_raw = raw.get("platforms")
    smoke_raw = raw.get("smoke_tests")
    if not isinstance(platforms_raw, list) or not platforms_raw or not all(isinstance(item, str) for item in platforms_raw):
        raise ReleaseContractError("platforms must be a non-empty string list")
    if not isinstance(smoke_raw, list) or tuple(smoke_raw) != REQUIRED_SMOKE_TESTS:
        raise ReleaseContractError(f"smoke_tests must equal {list(REQUIRED_SMOKE_TESTS)}")

    python_requires = _required_string(raw, "python_requires")
    if _PYTHON_REQUIRES.fullmatch(python_requires) is None:
        raise ReleaseContractError("python_requires must be a single >=major.minor[.patch] constraint")

    return ReleaseManifest(
        schema_version=RELEASE_MANIFEST_SCHEMA,
        version=version,
        tag=tag,
        wheel_filename=wheel_filename,
        wheel_sha256=wheel_sha256,
        python_requires=python_requires,
        platforms=tuple(platforms_raw),
        omx_version=_required_string(omx, "version"),
        omx_tarball_url=_required_string(omx, "tarball_url"),
        omx_integrity=_required_string(omx, "integrity"),
        overlay_revision=_required_string(omx, "overlay_revision"),
        state_schema=_required_string(raw, "state_schema"),
        smoke_tests=tuple(smoke_raw),
    )


def verify_release_assets(manifest: ReleaseManifest, directory: Path | str) -> None:
    root = Path(directory)
    wheel = root / manifest.wheel_filename
    if not wheel.is_file():
        raise ReleaseContractError(f"Missing wheel asset: {wheel.name}")
    actual = sha256_file(wheel)
    if actual != manifest.wheel_sha256:
        raise ReleaseContractError(f"Wheel checksum mismatch: expected {manifest.wheel_sha256}, got {actual}")

    sums_path = root / "SHA256SUMS"
    if not sums_path.is_file():
        raise ReleaseContractError("Missing SHA256SUMS")
    checksums = _read_sha256sums(sums_path)
    manifest_path = root / "harness-release.json"
    manifest_checksum = sha256_file(manifest_path) if manifest_path.is_file() else None
    if checksums.get(manifest_path.name) != manifest_checksum:
        raise ReleaseContractError("SHA256SUMS does not match the release manifest checksum")
    if checksums.get(manifest.wheel_filename) != manifest.wheel_sha256:
        raise ReleaseContractError("SHA256SUMS does not match the release manifest wheel checksum")


def minimum_python_version(requirement: str) -> tuple[int, int, int]:
    match = _PYTHON_REQUIRES.fullmatch(requirement)
    if match is None:
        raise ReleaseContractError(f"Unsupported python_requires constraint: {requirement}")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch") or 0),
    )


def atomic_write_json(path: Path | str, payload: Mapping[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _required_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    result = value.get(key)
    if not isinstance(result, Mapping):
        raise ReleaseContractError(f"{key} must be an object")
    return result


def _required_string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ReleaseContractError(f"{key} must be a non-empty string")
    return result.strip()


def _read_sha256sums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not _SHA256.fullmatch(parts[0]):
            raise ReleaseContractError(f"Malformed SHA256SUMS line {line_number}")
        checksums[parts[1].lstrip("* ")] = parts[0]
    return checksums


__all__ = [
    "INSTALL_MANIFEST_SCHEMA",
    "RELEASE_MANIFEST_SCHEMA",
    "REQUIRED_SMOKE_TESTS",
    "ReleaseContractError",
    "ReleaseManifest",
    "atomic_write_json",
    "load_release_manifest",
    "minimum_python_version",
    "sha256_file",
    "verify_release_assets",
]
