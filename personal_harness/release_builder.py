"""Build immutable GitHub Release metadata beside a Harness wheel."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
from typing import Mapping, Sequence

from .omx_overlay import (
    OMX_TARBALL_INTEGRITY,
    OMX_TARBALL_URL,
    OVERLAY_REVISION,
    PINNED_OMX_VERSION,
)
from .release_contract import RELEASE_MANIFEST_SCHEMA, REQUIRED_SMOKE_TESTS, atomic_write_json, sha256_file

_WHEEL_NAME = re.compile(r"^personal_harness-(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-.+\.whl$")


class ReleaseBuildError(ValueError):
    """Raised when release artifacts cannot satisfy the immutable contract."""


def build_release_artifacts(wheel_path: Path | str, output_directory: Path | str, *, tag: str) -> Mapping[str, str]:
    wheel_source = Path(wheel_path)
    match = _WHEEL_NAME.fullmatch(wheel_source.name)
    if not wheel_source.is_file() or match is None:
        raise ReleaseBuildError(f"Expected a personal_harness stable wheel, got {wheel_source}")
    version = match.group("version")
    if tag != f"v{version}":
        raise ReleaseBuildError(f"Release tag {tag} does not match wheel version {version}")
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    wheel_target = output / wheel_source.name
    if wheel_source.resolve() != wheel_target.resolve():
        shutil.copy2(wheel_source, wheel_target)
    wheel_sha256 = sha256_file(wheel_target)
    manifest_payload = {
        "schema_version": RELEASE_MANIFEST_SCHEMA,
        "version": version,
        "tag": tag,
        "draft": False,
        "prerelease": False,
        "python_requires": ">=3.11",
        "platforms": ["macos", "linux"],
        "wheel": {"filename": wheel_target.name, "sha256": wheel_sha256},
        "omx": {
            "version": PINNED_OMX_VERSION,
            "tarball_url": OMX_TARBALL_URL,
            "integrity": OMX_TARBALL_INTEGRITY,
            "overlay_revision": OVERLAY_REVISION,
        },
        "state_schema": "personal-harness-state/v2",
        "smoke_tests": list(REQUIRED_SMOKE_TESTS),
    }
    manifest_path = atomic_write_json(output / "harness-release.json", manifest_payload)
    assets = sorted((manifest_path, wheel_target), key=lambda path: path.name)
    sums_path = output / "SHA256SUMS"
    sums_path.write_text("".join(f"{sha256_file(path)}  {path.name}\n" for path in assets), encoding="utf-8")
    return {
        "version": version,
        "tag": tag,
        "wheel": str(wheel_target),
        "manifest": str(manifest_path),
        "checksums": str(sums_path),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Harness GitHub Release metadata from a wheel.")
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args(argv)
    result = build_release_artifacts(args.wheel, args.output, tag=args.tag)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "OMX_TARBALL_INTEGRITY",
    "OMX_TARBALL_URL",
    "ReleaseBuildError",
    "build_release_artifacts",
    "main",
]
