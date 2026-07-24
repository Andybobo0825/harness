"""Global Harness install, update, doctor, rollback, and version CLI."""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.request import Request, urlopen

from .deployment import BackupArchive, DeploymentContext, DeploymentError, deploy_release, deployment_lock
from .install_smoke import run_install_smoke_tests
from .omx_overlay import OVERLAY_FILES, OVERLAY_REVISION, PINNED_OMX_VERSION
from .release_contract import (
    INSTALL_MANIFEST_SCHEMA,
    ReleaseContractError,
    ReleaseManifest,
    load_release_manifest,
    minimum_python_version,
    sha256_file,
    verify_release_assets,
)

DEFAULT_RELEASE_REPOSITORY = "Andybobo0825/harness"
DEFAULT_HARNESS_HOME = Path("~/.local/share/harness-codex").expanduser()
DEFAULT_CODEX_HOME = Path("~/.codex").expanduser()
_BACKUP_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")


class DownloadError(RuntimeError):
    """Raised when an immutable GitHub Release cannot be verified."""


UrlOpener = Callable[..., Any]


def download_release(
    requested_version: str | None,
    destination: Path | str,
    *,
    repository: str = DEFAULT_RELEASE_REPOSITORY,
    opener: UrlOpener = urlopen,
) -> ReleaseManifest:
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    if requested_version:
        normalized = requested_version.removeprefix("v")
        api_url = f"https://api.github.com/repos/{repository}/releases/tags/v{normalized}"
    else:
        api_url = f"https://api.github.com/repos/{repository}/releases/latest"
    release = _download_json(api_url, opener)
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise DownloadError("GitHub response is not a stable release")
    tag = str(release.get("tag_name", ""))
    if requested_version and tag != f"v{requested_version.removeprefix('v')}":
        raise DownloadError(f"GitHub release tag mismatch: {tag}")
    assets_raw = release.get("assets")
    if not isinstance(assets_raw, list):
        raise DownloadError("GitHub release has no assets")
    assets = {
        str(asset.get("name")): str(asset.get("browser_download_url"))
        for asset in assets_raw
        if isinstance(asset, Mapping) and asset.get("name") and asset.get("browser_download_url")
    }
    for required in ("harness-release.json", "SHA256SUMS"):
        if required not in assets:
            raise DownloadError(f"GitHub release is missing {required}")
        _download_file(assets[required], root / required, opener)
    try:
        manifest = load_release_manifest(root / "harness-release.json")
    except (OSError, json.JSONDecodeError, ReleaseContractError) as exc:
        raise DownloadError(f"Invalid release manifest: {exc}") from exc
    if manifest.tag != tag:
        raise DownloadError(f"Release manifest tag {manifest.tag} does not match GitHub tag {tag}")
    wheel_url = assets.get(manifest.wheel_filename)
    if not wheel_url:
        raise DownloadError(f"GitHub release is missing {manifest.wheel_filename}")
    _download_file(wheel_url, root / manifest.wheel_filename, opener)
    try:
        verify_release_assets(manifest, root)
    except ReleaseContractError as exc:
        raise DownloadError(f"Release checksum verification failed: {exc}") from exc
    return manifest


def build_staged_install_command(
    wheel_path: Path,
    release_manifest_path: Path,
    *,
    harness_home: Path,
    codex_home: Path,
    project_roots: Sequence[Path] = (),
    previous_source: Path | None = None,
) -> list[str]:
    code = (
        "import sys; "
        "sys.path.insert(0, sys.argv.pop(1)); "
        "from personal_harness.install_command import staged_install_main; "
        "raise SystemExit(staged_install_main(sys.argv[1:]))"
    )
    command = [
        sys.executable,
        "-c",
        code,
        str(wheel_path),
        "--release-manifest",
        str(release_manifest_path),
        "--wheel",
        str(wheel_path),
        "--harness-home",
        str(harness_home),
        "--codex-home",
        str(codex_home),
    ]
    for root in project_roots:
        command.extend(["--project-root", str(root)])
    if previous_source is not None:
        command.extend(["--previous-source", str(previous_source)])
    return command


def staged_install_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a staged immutable Harness release deployment.")
    parser.add_argument("--release-manifest", required=True)
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--harness-home", required=True)
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--project-root", action="append", default=[])
    parser.add_argument("--previous-source")
    args = parser.parse_args(argv)

    release_path = Path(args.release_manifest).resolve()
    release = load_release_manifest(release_path)
    verify_release_assets(release, release_path.parent)
    minimum_python = minimum_python_version(release.python_requires)
    if sys.version_info[:3] < minimum_python:
        raise DeploymentError(
            f"Release {release.version} requires Python {release.python_requires}; found {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )
    platform_name = "macos" if sys.platform == "darwin" else "linux" if sys.platform.startswith("linux") else sys.platform
    if platform_name not in release.platforms:
        raise DeploymentError(f"Release {release.version} does not support platform {platform_name}")
    npm_root = _run_stdout(["npm", "root", "-g"])
    npm_prefix = _run_stdout(["npm", "prefix", "-g"])
    omx_root = Path(npm_root) / "oh-my-codex"
    omx_executable = Path(npm_prefix) / "bin" / "omx"
    project_roots = _known_project_roots(Path(args.harness_home), args.project_root)
    context = DeploymentContext(
        harness_home=Path(args.harness_home).expanduser().resolve(),
        codex_home=Path(args.codex_home).expanduser().resolve(),
        omx_package_root=omx_root,
        omx_executable=omx_executable,
        wheel_path=Path(args.wheel).resolve(),
        release=release,
        project_roots=project_roots,
        python_executable=sys.executable,
        smoke_runner=run_install_smoke_tests,
        previous_harness_source=Path(args.previous_source).resolve() if args.previous_source else None,
    )
    result = deploy_release(context)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def doctor_installation(
    harness_home: Path | str,
    *,
    allow_transaction_journal: bool = False,
) -> Mapping[str, Any]:
    home = Path(harness_home).expanduser()
    manifest_path = home / "install" / "manifest.json"
    allow_transaction_journal = allow_transaction_journal or os.environ.get("HARNESS_ROLLBACK_VERIFY") == "1"
    checks: list[dict[str, Any]] = []
    journal_path = home / "install" / "transaction.json"
    journal = _optional_json(journal_path)
    checks.append(
        {
            "name": "transaction_journal",
            "passed": not journal_path.exists() or allow_transaction_journal,
            "detail": (
                f"recovery required: harness recover --backup-id {journal.get('backup_id', '<unknown>')}"
                if journal_path.exists()
                else "none"
            ),
        }
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        checks.append({"name": "install_manifest", "passed": False, "detail": str(exc)})
        return {"ok": False, "checks": checks}
    if not isinstance(manifest, Mapping):
        checks.append({"name": "install_manifest", "passed": False, "detail": "manifest is not an object"})
        return {"ok": False, "checks": checks}
    schema_ok = manifest.get("schema_version") == INSTALL_MANIFEST_SCHEMA
    checks.append({"name": "install_manifest", "passed": schema_ok, "detail": str(manifest_path)})

    required_shapes_ok = (
        isinstance(manifest.get("harness_version"), str)
        and isinstance(manifest.get("python_executable"), str)
        and isinstance(manifest.get("wheel_path"), str)
        and isinstance(manifest.get("wheel_sha256"), str)
        and isinstance(manifest.get("omx"), Mapping)
        and isinstance(manifest.get("hooks"), Mapping)
    )
    checks.append({"name": "install_manifest_fields", "passed": required_shapes_ok, "detail": "required fields"})
    if not required_shapes_ok:
        return {"ok": False, "checks": checks, "manifest": dict(manifest)}

    active_version = _package_version()
    checks.append(
        {
            "name": "installed_distribution",
            "passed": active_version == manifest.get("harness_version"),
            "detail": active_version,
        }
    )
    recorded_python = Path(str(manifest.get("python_executable", ""))).expanduser()
    active_python = Path(sys.executable).resolve()
    checks.append(
        {
            "name": "python_interpreter",
            "passed": recorded_python.is_file() and recorded_python.resolve() == active_python,
            "detail": {"active": str(active_python), "recorded": str(recorded_python)},
        }
    )

    wheel = Path(str(manifest.get("wheel_path", "")))
    expected_wheel = str(manifest.get("wheel_sha256", ""))
    actual_wheel = sha256_file(wheel) if wheel.is_file() else None
    checks.append({"name": "wheel", "passed": actual_wheel == expected_wheel, "detail": actual_wheel})

    omx = manifest.get("omx", {}) if isinstance(manifest.get("omx"), Mapping) else {}
    omx_root = Path(str(omx.get("package_root", "")))
    after = omx.get("after", {}) if isinstance(omx.get("after"), Mapping) else {}
    package = _optional_json(omx_root / "package.json")
    omx_identity_ok = (
        package.get("name") == "oh-my-codex"
        and package.get("version") == PINNED_OMX_VERSION
        and omx.get("version") == PINNED_OMX_VERSION
        and omx.get("overlay_revision") == OVERLAY_REVISION
    )
    checks.append(
        {
            "name": "omx_identity",
            "passed": omx_identity_ok,
            "detail": {
                "name": package.get("name"),
                "version": package.get("version"),
                "overlay_revision": omx.get("overlay_revision"),
            },
        }
    )
    exact_overlay_shape = set(after) == set(OVERLAY_FILES)
    checks.append({"name": "omx_overlay_manifest", "passed": exact_overlay_shape, "detail": sorted(after)})
    for relative in OVERLAY_FILES:
        expected = after.get(relative)
        target = omx_root / str(relative)
        actual = sha256_file(target) if target.is_file() else None
        checks.append(
            {
                "name": f"omx:{relative}",
                "passed": isinstance(expected, str) and actual == expected,
                "detail": actual,
            }
        )

    hooks = manifest.get("hooks", {}) if isinstance(manifest.get("hooks"), Mapping) else {}
    hooks_path = Path(str(hooks.get("path", "")))
    actual_hooks = sha256_file(hooks_path) if hooks_path.is_file() else None
    expected_hooks = hooks.get("sha256")
    checks.append(
        {
            "name": "codex_hooks",
            "passed": isinstance(expected_hooks, str) and actual_hooks == expected_hooks,
            "detail": actual_hooks,
        }
    )
    return {"ok": all(check["passed"] for check in checks), "checks": checks, "manifest": manifest}


def rollback_installation(
    harness_home: Path | str,
    backup_id: str | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Mapping[str, Any]:
    home = Path(harness_home).expanduser()
    with deployment_lock(home):
        return _rollback_installation_locked(home, backup_id, runner=runner)


def _rollback_installation_locked(
    home: Path,
    backup_id: str | None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> Mapping[str, Any]:
    backups_root = home / "backups"
    journal_path = home / "install" / "transaction.json"
    journal = _optional_json(journal_path)
    if backup_id is None and isinstance(journal.get("backup_id"), str):
        backup_id = str(journal["backup_id"])
    if backup_id:
        if not _BACKUP_ID.fullmatch(backup_id):
            raise DeploymentError(f"Invalid Harness backup id: {backup_id}")
        backup_root = backups_root / backup_id
    else:
        candidates = sorted((path for path in backups_root.iterdir() if path.is_dir()), reverse=True)
        if not candidates:
            raise DeploymentError(f"No Harness backups found under {backups_root}")
        backup_root = candidates[0]
    archive = BackupArchive.load(backup_root)
    if journal and journal.get("backup_id") != archive.backup_id:
        raise DeploymentError(
            f"Incomplete transaction belongs to backup {journal.get('backup_id')}, not {archive.backup_id}"
        )
    backup_payload = _optional_json(archive.manifest_path)
    deployment_metadata = (
        backup_payload.get("deployment") if isinstance(backup_payload.get("deployment"), Mapping) else {}
    )
    archive.restore()
    archive.verify_restored()
    manifest = _optional_json(home / "install" / "manifest.json")
    wheel_path = manifest.get("wheel_path")
    if isinstance(wheel_path, str) and Path(wheel_path).is_file():
        rollback_python = manifest.get("python_executable")
        python_executable = (
            str(rollback_python)
            if isinstance(rollback_python, str) and Path(rollback_python).is_file()
            else sys.executable
        )
        completed = runner(
            [python_executable, "-m", "pip", "install", "--user", "--force-reinstall", wheel_path],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise DeploymentError(f"Filesystem restored but Harness wheel rollback failed: {completed.stderr}")
    elif isinstance(deployment_metadata.get("previous_harness_source"), str):
        previous_source = Path(str(deployment_metadata["previous_harness_source"]))
        if previous_source.is_dir():
            completed = runner(
                [sys.executable, "-m", "pip", "install", "--user", "--editable", str(previous_source)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if completed.returncode != 0:
                raise DeploymentError(f"Filesystem restored but editable Harness rollback failed: {completed.stderr}")
    verification = (
        _run_restored_doctor(home, manifest, runner=runner)
        if manifest
        else {"ok": not (home / "install" / "manifest.json").exists(), "state": "uninstalled"}
    )
    if not verification.get("ok"):
        raise DeploymentError(f"Rollback restored files but post-rollback doctor failed: {verification}")
    journal_path.unlink(missing_ok=True)
    log_path = home / "logs" / "transactions.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "schema_version": "harness-transaction-log/v1",
                    "backup_id": archive.backup_id,
                    "status": "manual_rollback_verified",
                    "timestamp": time.time(),
                },
                sort_keys=True,
            )
            + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
    return {
        "backup_id": archive.backup_id,
        "restored": True,
        "manifest": dict(manifest),
        "verification": verification,
    }


def recover_installation(
    harness_home: Path | str,
    backup_id: str | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Mapping[str, Any]:
    home = Path(harness_home).expanduser()
    with deployment_lock(home):
        journal_path = home / "install" / "transaction.json"
        journal = _optional_json(journal_path)
        if not journal_path.exists() or not journal:
            raise DeploymentError(f"No incomplete Harness transaction found at {journal_path}")
        journal_backup = journal.get("backup_id")
        selected = backup_id or (str(journal_backup) if isinstance(journal_backup, str) else None)
        if selected is None:
            raise DeploymentError(f"Incomplete transaction has no valid backup id: {journal_path}")
        return _rollback_installation_locked(home, selected, runner=runner)


def _run_restored_doctor(
    home: Path,
    manifest: Mapping[str, Any],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> Mapping[str, Any]:
    recorded_python = manifest.get("python_executable")
    if not isinstance(recorded_python, str) or not Path(recorded_python).is_file():
        raise DeploymentError("Restored manifest has no usable Python interpreter for doctor")
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["HARNESS_ROLLBACK_VERIFY"] = "1"
    with tempfile.TemporaryDirectory(prefix="harness-rollback-doctor-") as directory:
        completed = runner(
            [
                recorded_python,
                "-m",
                "personal_harness.install_command",
                "--harness-home",
                str(home),
                "doctor",
                "--json",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            cwd=directory,
            env=environment,
        )
    if completed.returncode != 0:
        raise DeploymentError(f"Restored Harness doctor failed ({completed.returncode}): {completed.stderr.strip()}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DeploymentError(f"Restored Harness doctor returned invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise DeploymentError("Restored Harness doctor did not return a JSON object")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install and maintain the global Harness runtime from GitHub Releases.")
    parser.add_argument("--harness-home", default=os.environ.get("HARNESS_HOME", str(DEFAULT_HARNESS_HOME)))
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", str(DEFAULT_CODEX_HOME)))
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "update"):
        command = subparsers.add_parser(name)
        command.add_argument("--version")
        command.add_argument("--release-manifest")
        command.add_argument("--wheel")
        command.add_argument("--project-root", action="append", default=[])
    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument("--allow-transaction-journal", action="store_true", help=argparse.SUPPRESS)
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--backup-id")
    rollback_parser.add_argument("--json", action="store_true")
    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--backup-id")
    recover_parser.add_argument("--json", action="store_true")
    version_parser = subparsers.add_parser("version")
    version_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    harness_home = Path(args.harness_home).expanduser().resolve()

    if args.command == "version":
        payload = {
            "package_version": _package_version(),
            "installed_version": _optional_json(harness_home / "install" / "manifest.json").get("harness_version"),
        }
        _print_result(payload, args.json)
        return 0
    if args.command == "doctor":
        payload = doctor_installation(
            harness_home,
            allow_transaction_journal=args.allow_transaction_journal,
        )
        _print_result(payload, args.json)
        return 0 if payload["ok"] else 1
    if args.command == "rollback":
        payload = rollback_installation(harness_home, args.backup_id)
        _print_result(payload, args.json)
        return 0
    if args.command == "recover":
        payload = recover_installation(harness_home, args.backup_id)
        _print_result(payload, args.json)
        return 0

    return _run_install_or_update(args, harness_home, Path(args.codex_home).expanduser().resolve())


def _run_install_or_update(args: argparse.Namespace, harness_home: Path, codex_home: Path) -> int:
    _assert_global_python_boundary()
    local_manifest = getattr(args, "release_manifest", None)
    local_wheel = getattr(args, "wheel", None)
    if bool(local_manifest) != bool(local_wheel):
        raise DownloadError("--release-manifest and --wheel must be provided together")
    repository = os.environ.get("HARNESS_RELEASE_REPOSITORY", DEFAULT_RELEASE_REPOSITORY)
    with tempfile.TemporaryDirectory(prefix="harness-release-") as d:
        staging = Path(d)
        if local_manifest:
            manifest_source = Path(local_manifest).resolve()
            release_source = manifest_source.parent
            for source in (manifest_source, release_source / "SHA256SUMS", Path(local_wheel).resolve()):
                shutil.copy2(source, staging / source.name)
            release = load_release_manifest(staging / "harness-release.json")
            verify_release_assets(release, staging)
        else:
            release = download_release(getattr(args, "version", None), staging, repository=repository)
        wheel = staging / release.wheel_filename
        previous_source = _editable_source_root()
        command = build_staged_install_command(
            wheel,
            staging / "harness-release.json",
            harness_home=harness_home,
            codex_home=codex_home,
            project_roots=tuple(Path(root).resolve() for root in args.project_root),
            previous_source=previous_source,
        )
        completed = subprocess.run(command, check=False)
        return int(completed.returncode)


def _assert_global_python_boundary() -> None:
    if sys.prefix != sys.base_prefix or os.environ.get("VIRTUAL_ENV"):
        raise DeploymentError(
            "Global Harness install/update cannot run from a virtual environment. "
            "Invoke the base Python interpreter so one recorded interpreter owns every project installation."
        )


def _download_json(url: str, opener: UrlOpener) -> Mapping[str, Any]:
    try:
        request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "harness-codex"})
        with opener(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise DownloadError(f"Failed to download GitHub release metadata: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise DownloadError("GitHub release metadata must be an object")
    return payload


def _download_file(url: str, path: Path, opener: UrlOpener) -> None:
    try:
        request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "harness-codex"})
        with opener(request, timeout=60) as response:
            path.write_bytes(response.read())
    except Exception as exc:
        raise DownloadError(f"Failed to download release asset {path.name}: {exc}") from exc


def _run_stdout(command: list[str]) -> str:
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise DeploymentError(f"Command failed: {' '.join(command)}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _known_project_roots(harness_home: Path, explicit: Sequence[str]) -> tuple[Path, ...]:
    roots = {Path(root).expanduser().resolve() for root in explicit}
    manifest = _optional_json(harness_home / "install" / "manifest.json")
    migrations = manifest.get("state_migrations")
    if isinstance(migrations, list):
        for item in migrations:
            if isinstance(item, Mapping) and isinstance(item.get("root"), str):
                roots.add(Path(str(item["root"])).expanduser().resolve())
    cwd = Path.cwd().resolve()
    if (cwd / ".harness").exists():
        roots.add(cwd)
    return tuple(sorted(roots, key=str))


def _editable_source_root() -> Path | None:
    package_root = Path(__file__).resolve().parents[1]
    return package_root if (package_root / "pyproject.toml").is_file() else None


def _optional_json(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, Mapping) else {}


def _package_version() -> str:
    try:
        return package_version("personal-harness")
    except PackageNotFoundError:
        return "source"


def _print_result(payload: Mapping[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
