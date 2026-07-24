"""Transactional global Harness deployment with filesystem rollback."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import hmac
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4
from urllib.request import Request, urlopen

from .codex_hooks_migration import HookTrustMigrationError, unfence_legacy_omx_hook_trust_state
from .harness_state import SCHEMA_VERSION as STATE_SCHEMA_VERSION
from .harness_state import STATE_RELATIVE_PATH, migrate_personal_harness_state
from .omx_overlay import (
    OMX_TARBALL_INTEGRITY,
    OMX_TARBALL_URL,
    OVERLAY_REVISION,
    PINNED_OMX_VERSION,
    apply_omx_overlay,
)
from .release_contract import (
    INSTALL_MANIFEST_SCHEMA,
    ReleaseManifest,
    atomic_write_json,
    sha256_file,
)


class DeploymentError(RuntimeError):
    """Raised when install/update cannot commit a verified deployment."""


@dataclass(frozen=True)
class BackupArchive:
    root: Path
    manifest_path: Path
    entries: tuple[Mapping[str, Any], ...]

    @classmethod
    def load(cls, root: Path | str) -> "BackupArchive":
        archive_root = Path(root)
        manifest_path = archive_root / "backup-manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or payload.get("schema_version") != "harness-backup/v1":
            raise DeploymentError(f"Unsupported backup manifest: {manifest_path}")
        if payload.get("backup_id") != archive_root.name:
            raise DeploymentError(f"Backup manifest id does not match directory: {manifest_path}")
        entries = payload.get("entries")
        if not isinstance(entries, list) or not all(isinstance(entry, Mapping) for entry in entries):
            raise DeploymentError(f"Malformed backup entries: {manifest_path}")
        return cls(archive_root, manifest_path, tuple(entries))

    @classmethod
    def create(cls, harness_home: Path | str, targets: Sequence[Path | str]) -> "BackupArchive":
        backup_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid4().hex[:12]}"
        root = Path(harness_home) / "backups" / backup_id
        data_root = root / "data"
        data_root.mkdir(parents=True, exist_ok=False)
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw_target in enumerate(targets):
            target = Path(raw_target)
            key = str(target.absolute())
            if key in seen:
                continue
            seen.add(key)
            entry: dict[str, Any] = {"path": key, "slot": str(index)}
            slot = data_root / str(index)
            if target.is_symlink():
                entry.update({"kind": "symlink", "target": os.readlink(target), "mode": stat.S_IMODE(target.lstat().st_mode)})
            elif target.is_file():
                shutil.copy2(target, slot)
                entry.update({"kind": "file", "mode": stat.S_IMODE(target.stat().st_mode), "sha256": sha256_file(target)})
            elif target.is_dir():
                shutil.copytree(target, slot, symlinks=True)
                entry.update({"kind": "directory", "mode": stat.S_IMODE(target.stat().st_mode), "sha256": _tree_checksum(target)})
            else:
                entry.update({"kind": "missing"})
            entries.append(entry)
        manifest_path = root / "backup-manifest.json"
        atomic_write_json(
            manifest_path,
            {
                "schema_version": "harness-backup/v1",
                "backup_id": backup_id,
                "created_at": time.time(),
                "entries": entries,
            },
        )
        return cls(root, manifest_path, tuple(entries))

    @property
    def backup_id(self) -> str:
        return self.root.name

    def restore(self) -> None:
        data_root = self.root / "data"
        for entry in self.entries:
            kind = entry["kind"]
            if kind not in {"file", "directory"}:
                continue
            slot = data_root / str(entry["slot"])
            try:
                actual = sha256_file(slot) if kind == "file" else _tree_checksum(slot)
            except OSError as exc:
                raise DeploymentError(f"Backup data is missing or unreadable: {slot}: {exc}") from exc
            if actual != entry.get("sha256"):
                raise DeploymentError(f"Backup checksum mismatch for {entry['path']}: {actual}")
        for entry in reversed(self.entries):
            target = Path(str(entry["path"]))
            _remove_path(target)
            kind = entry["kind"]
            if kind == "missing":
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if kind == "symlink":
                target.symlink_to(str(entry["target"]))
            elif kind == "file":
                shutil.copy2(data_root / str(entry["slot"]), target)
                os.chmod(target, int(entry["mode"]))
            elif kind == "directory":
                shutil.copytree(data_root / str(entry["slot"]), target, symlinks=True)
                os.chmod(target, int(entry["mode"]))
            else:
                raise DeploymentError(f"Unsupported backup entry kind: {kind}")

    def verify_restored(self) -> None:
        for entry in self.entries:
            target = Path(str(entry["path"]))
            kind = entry["kind"]
            if kind == "missing":
                if target.exists() or target.is_symlink():
                    raise DeploymentError(f"Rollback verification expected missing path: {target}")
            elif kind == "symlink":
                if not target.is_symlink() or os.readlink(target) != entry.get("target"):
                    raise DeploymentError(f"Rollback verification failed for symlink: {target}")
            elif kind == "file":
                if not target.is_file() or sha256_file(target) != entry.get("sha256"):
                    raise DeploymentError(f"Rollback verification failed for file: {target}")
            elif kind == "directory":
                if not target.is_dir() or _tree_checksum(target) != entry.get("sha256"):
                    raise DeploymentError(f"Rollback verification failed for directory: {target}")
            else:
                raise DeploymentError(f"Unsupported backup entry kind: {kind}")


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
SmokeRunner = Callable[["DeploymentContext"], Sequence[Mapping[str, Any]]]
OmxInstaller = Callable[["DeploymentContext"], None]


@dataclass(frozen=True)
class DeploymentContext:
    harness_home: Path
    codex_home: Path
    omx_package_root: Path
    omx_executable: Path
    wheel_path: Path
    release: ReleaseManifest
    project_roots: tuple[Path, ...] = ()
    python_executable: str = "python3"
    npm_executable: str = "npm"
    runner: CommandRunner = subprocess.run
    smoke_runner: SmokeRunner | None = None
    accepted_omx_preimage_checksums: Mapping[str, str] | None = None
    previous_harness_source: Path | None = None
    omx_installer: OmxInstaller | None = None


@contextmanager
def deployment_lock(harness_home: Path | str, *, timeout: float = 30.0):
    lock_path = Path(harness_home) / "install.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DeploymentError(f"Another Harness install or rollback is already in progress: {lock_path}")
                time.sleep(0.02)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} acquired_at={time.time()}\n")
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def deploy_release(context: DeploymentContext) -> Mapping[str, Any]:
    with deployment_lock(context.harness_home):
        return _deploy_release_locked(context)


def _deploy_release_locked(context: DeploymentContext) -> Mapping[str, Any]:
    _validate_release_compatibility(context.release)
    if sha256_file(context.wheel_path) != context.release.wheel_sha256:
        raise DeploymentError("Staged wheel checksum does not match release manifest")
    manifest_path = context.harness_home / "install" / "manifest.json"
    journal_path = context.harness_home / "install" / "transaction.json"
    if journal_path.exists():
        journal = _read_optional_json(journal_path)
        backup_id = journal.get("backup_id", "<unknown>")
        raise DeploymentError(
            f"Incomplete Harness transaction journal requires recovery before a new install: {journal_path}; "
            f"run `harness recover --backup-id {backup_id}`"
        )
    previous_manifest = _read_optional_json(manifest_path)
    previous_python = previous_manifest.get("python_executable")
    if isinstance(previous_python, str) and previous_python.strip():
        if Path(previous_python).expanduser().resolve() != Path(context.python_executable).expanduser().resolve():
            raise DeploymentError(
                "Harness update interpreter does not match the committed global installation: "
                f"recorded={previous_python}, active={context.python_executable}"
            )
    previous_installation_id = previous_manifest.get("installation_id")
    installation_id = (
        previous_installation_id
        if isinstance(previous_installation_id, str) and previous_installation_id.strip()
        else uuid4().hex
    )
    deployment_id = uuid4().hex
    previous_manifest_checksum = sha256_file(manifest_path) if manifest_path.exists() else None
    release_directory = context.harness_home / "releases" / context.release.version
    hooks_path = context.codex_home / "hooks.json"
    preserved_foreign_hooks = _foreign_hooks_snapshot(_read_optional_json(hooks_path))
    state_paths = [root / STATE_RELATIVE_PATH for root in context.project_roots]
    backup_targets = [
        manifest_path,
        context.omx_package_root,
        context.omx_executable,
        hooks_path,
        context.codex_home / "config.toml",
        context.codex_home / "AGENTS.md",
        context.codex_home / "prompts",
        context.codex_home / "skills",
        context.codex_home / "agents",
        release_directory,
        *state_paths,
    ]
    archive = BackupArchive.create(context.harness_home, backup_targets)
    _annotate_backup(
        archive,
        {
            "previous_harness_source": str(context.previous_harness_source) if context.previous_harness_source else None,
            "previous_install_manifest": dict(previous_manifest),
        },
    )
    transaction = {
        "schema_version": "harness-transaction/v1",
        "deployment_id": deployment_id,
        "installation_id": installation_id,
        "backup_id": archive.backup_id,
        "previous_manifest": dict(previous_manifest),
        "started_at": time.time(),
        "status": "prepared",
    }
    _write_transaction_state(journal_path, transaction, "prepared")
    _append_transaction_log(context.harness_home, transaction, "prepared")

    try:
        _write_transaction_state(journal_path, transaction, "installing_harness")
        _run_checked(
            context,
            [
                context.python_executable,
                "-m",
                "pip",
                "install",
                "--user",
                "--upgrade",
                "--force-reinstall",
                str(context.wheel_path),
            ],
            "Harness wheel installation",
        )
        _write_transaction_state(journal_path, transaction, "installing_omx")
        _ensure_pinned_omx(context)
        overlay = apply_omx_overlay(
            context.omx_package_root,
            expected_version=context.release.omx_version,
            accepted_preimage_checksums=context.accepted_omx_preimage_checksums,
        )
        hook_trust_migration = _prepare_codex_hook_trust_refresh(context.codex_home / "config.toml")
        _write_transaction_state(journal_path, transaction, "installing_hooks")
        with tempfile.TemporaryDirectory(prefix="harness-omx-setup-") as setup_directory:
            (Path(setup_directory) / ".omx").mkdir()
            _run_checked(
                context,
                [
                    str(context.omx_executable),
                    "setup",
                    "--scope",
                    "user",
                    "--merge-agents",
                    "--legacy",
                    "--mcp",
                    "none",
                    "--force",
                ],
                "OMX user-scope hook setup",
                env={**os.environ, "CODEX_HOME": str(context.codex_home)},
                cwd=Path(setup_directory),
            )
        hooks_checksum = _validate_hooks(hooks_path, context.omx_package_root, preserved_foreign_hooks)

        migrations = []
        _write_transaction_state(journal_path, transaction, "migrating_state")
        for project_root, state_path in zip(context.project_roots, state_paths):
            if not state_path.exists():
                continue
            result = migrate_personal_harness_state(project_root, installation_id=installation_id)
            migrations.append({"root": str(project_root), "migrated": result.migrated, "schema": result.to_schema})

        if context.smoke_runner is None:
            raise DeploymentError("Deployment context has no post-install smoke runner")
        _write_transaction_state(journal_path, transaction, "running_smoke_tests")
        smoke_results = [dict(item) for item in context.smoke_runner(context)]
        _validate_smoke_results(context.release, smoke_results)

        release_directory.mkdir(parents=True, exist_ok=True)
        saved_wheel = release_directory / context.wheel_path.name
        shutil.copy2(context.wheel_path, saved_wheel)
        for artifact_name in ("harness-release.json", "SHA256SUMS"):
            artifact = context.wheel_path.parent / artifact_name
            if artifact.is_file():
                shutil.copy2(artifact, release_directory / artifact_name)
        install_manifest = {
            "schema_version": INSTALL_MANIFEST_SCHEMA,
            "installation_id": installation_id,
            "deployment_id": deployment_id,
            "harness_version": context.release.version,
            "release_tag": context.release.tag,
            "installed_at": time.time(),
            "python_executable": context.python_executable,
            "wheel_path": str(saved_wheel),
            "wheel_sha256": context.release.wheel_sha256,
            "omx": {
                "version": overlay.version,
                "package_root": str(context.omx_package_root),
                "overlay_revision": overlay.revision,
                "overlay_status": overlay.status,
                "before": dict(overlay.before),
                "after": dict(overlay.after),
            },
            "hooks": {"path": str(hooks_path), "sha256": hooks_checksum},
            "codex_hook_trust_migration": hook_trust_migration,
            "state_migrations": migrations,
            "smoke_tests": smoke_results,
            "backup_id": archive.backup_id,
            "previous_manifest_sha256": previous_manifest_checksum,
        }
        atomic_write_json(manifest_path, install_manifest)
        _write_transaction_state(journal_path, transaction, "committed")
        _append_transaction_log(context.harness_home, transaction, "committed")
        journal_path.unlink(missing_ok=True)
        return install_manifest
    except Exception as exc:
        rollback_error = None
        try:
            archive.restore()
            _restore_previous_harness(context, previous_manifest)
            archive.verify_restored()
            _verify_automatic_rollback(context, previous_manifest)
        except Exception as restore_exc:  # preserve both failure causes for operators
            rollback_error = restore_exc
        if rollback_error is not None:
            try:
                _write_transaction_state(journal_path, transaction, "rollback_failed", error=str(rollback_error))
                _append_transaction_log(context.harness_home, transaction, "rollback_failed", error=str(rollback_error))
            except Exception:
                pass
            raise DeploymentError(
                f"Deployment failed: {exc}; rollback also failed: {rollback_error}; backup={archive.root}"
            ) from exc
        try:
            _write_transaction_state(journal_path, transaction, "rolled_back", error=str(exc))
            _append_transaction_log(context.harness_home, transaction, "rolled_back", error=str(exc))
            journal_path.unlink(missing_ok=True)
        except Exception as journal_exc:
            raise DeploymentError(
                f"Deployment failed and filesystem rollback succeeded, but transaction logging failed: {journal_exc}; "
                f"original={exc}; backup={archive.root}"
            ) from exc
        raise DeploymentError(f"Deployment failed and was rolled back: {exc}; backup={archive.root}") from exc


def _ensure_pinned_omx(context: DeploymentContext) -> None:
    if context.omx_installer is not None:
        context.omx_installer(context)
    else:
        _install_verified_omx_tarball(context)
    version = _read_optional_json(context.omx_package_root / "package.json").get("version")
    if version != context.release.omx_version:
        raise DeploymentError(f"npm did not install OMX {context.release.omx_version}; found {version}")


def _install_verified_omx_tarball(context: DeploymentContext) -> None:
    with tempfile.TemporaryDirectory(prefix="harness-omx-") as directory:
        packed = _run_checked(
            context,
            [
                context.npm_executable,
                "pack",
                context.release.omx_tarball_url,
                "--silent",
                "--pack-destination",
                directory,
            ],
            "Pinned OMX tarball download",
            env={**os.environ, "CODEX_HOME": str(context.codex_home)},
            cwd=Path(directory),
        )
        packed_name = packed.stdout.strip().splitlines()[-1] if packed.stdout.strip() else ""
        if not packed_name or Path(packed_name).name != packed_name:
            raise DeploymentError(f"npm pack returned an unsafe tarball name: {packed_name!r}")
        tarball = Path(directory) / packed_name
        _verify_omx_tarball_file(tarball, context.release.omx_integrity)
        _run_checked(
            context,
            [context.npm_executable, "install", "-g", "--ignore-scripts", str(tarball)],
            "Pinned verified OMX installation",
            env={**os.environ, "CODEX_HOME": str(context.codex_home)},
            cwd=Path(directory),
        )


def _download_verified_omx_tarball(
    url: str,
    integrity: str,
    destination: Path,
    *,
    opener: Callable[..., Any] = urlopen,
) -> Path:
    if not integrity.startswith("sha512-"):
        raise DeploymentError("OMX release integrity must use sha512")
    try:
        expected = base64.b64decode(integrity.removeprefix("sha512-"), validate=True)
        request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "harness-codex"})
        with opener(request, timeout=60) as response:
            payload = response.read(128 * 1024 * 1024 + 1)
    except Exception as exc:
        raise DeploymentError(f"Failed to download verified OMX tarball: {exc}") from exc
    if len(payload) > 128 * 1024 * 1024:
        raise DeploymentError("OMX tarball exceeds the 128 MiB safety limit")
    actual = hashlib.sha512(payload).digest()
    if not hmac.compare_digest(actual, expected):
        raise DeploymentError("OMX tarball integrity does not match the release manifest")
    destination.write_bytes(payload)
    return destination


def _verify_omx_tarball_file(path: Path, integrity: str) -> None:
    if not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
        raise DeploymentError(f"OMX tarball is missing or exceeds the 128 MiB safety limit: {path}")
    if not integrity.startswith("sha512-"):
        raise DeploymentError("OMX release integrity must use sha512")
    try:
        expected = base64.b64decode(integrity.removeprefix("sha512-"), validate=True)
    except ValueError as exc:
        raise DeploymentError(f"Malformed OMX release integrity: {exc}") from exc
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if not hmac.compare_digest(digest.digest(), expected):
        raise DeploymentError("OMX tarball integrity does not match the release manifest")


def _validate_release_compatibility(release: ReleaseManifest) -> None:
    actual = {
        "omx_version": release.omx_version,
        "omx_tarball_url": release.omx_tarball_url,
        "omx_integrity": release.omx_integrity,
        "overlay_revision": release.overlay_revision,
        "state_schema": release.state_schema,
    }
    expected = {
        "omx_version": PINNED_OMX_VERSION,
        "omx_tarball_url": OMX_TARBALL_URL,
        "omx_integrity": OMX_TARBALL_INTEGRITY,
        "overlay_revision": OVERLAY_REVISION,
        "state_schema": STATE_SCHEMA_VERSION,
    }
    mismatches = [key for key, expected_value in expected.items() if actual[key] != expected_value]
    if mismatches:
        raise DeploymentError(f"Release is outside the verified compatibility boundary: {mismatches}")


def _validate_hooks(
    hooks_path: Path,
    omx_root: Path,
    preserved_foreign_hooks: Mapping[str, Any],
) -> str:
    payload = _read_optional_json(hooks_path)
    commands = _all_commands(payload)
    expected_hook = str(omx_root / "dist" / "scripts" / "codex-native-hook.js")
    if not any(expected_hook in command for command in commands):
        raise DeploymentError(f"Codex hooks do not target patched OMX hook: {expected_hook}")
    stale_omx = [
        command for command in commands if "codex-native-hook.js" in command and expected_hook not in command
    ]
    if stale_omx:
        raise DeploymentError(f"Codex hooks still contain stale OMX commands: {stale_omx}")
    current_foreign_hooks = _foreign_hooks_snapshot(payload)
    if current_foreign_hooks != preserved_foreign_hooks:
        raise DeploymentError("OMX setup changed user-managed hook definitions")
    return sha256_file(hooks_path)


def _prepare_codex_hook_trust_refresh(config_path: Path) -> Mapping[str, Any]:
    if not config_path.exists():
        return {"status": "not_present", "path": str(config_path)}
    original = config_path.read_text(encoding="utf-8")
    try:
        result = unfence_legacy_omx_hook_trust_state(original)
    except HookTrustMigrationError as exc:
        raise DeploymentError(f"Codex hook trust-state migration failed closed: {exc}") from exc
    if not result.migrated:
        return {"status": "not_needed", "path": str(config_path)}
    temporary = config_path.with_name(f".{config_path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(result.content, encoding="utf-8")
        os.chmod(temporary, stat.S_IMODE(config_path.stat().st_mode))
        os.replace(temporary, config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "status": "unfenced",
        "path": str(config_path),
        "removed_legacy_keys": list(result.removed_legacy_keys),
    }


def _validate_smoke_results(release: ReleaseManifest, results: Sequence[Mapping[str, Any]]) -> None:
    names = [str(result.get("name")) for result in results]
    if tuple(names) != release.smoke_tests:
        raise DeploymentError(f"Smoke result names {names} do not match release contract {list(release.smoke_tests)}")
    failed = [dict(result) for result in results if result.get("passed") is not True]
    if failed:
        raise DeploymentError(
            "Post-install smoke tests failed: "
            + json.dumps(failed, ensure_ascii=False, sort_keys=True, default=str)
        )


def _restore_previous_harness(context: DeploymentContext, previous_manifest: Mapping[str, Any]) -> None:
    previous_python = previous_manifest.get("python_executable")
    python_executable = (
        str(previous_python)
        if isinstance(previous_python, str) and Path(previous_python).is_file()
        else context.python_executable
    )
    wheel_path = previous_manifest.get("wheel_path")
    if isinstance(wheel_path, str) and Path(wheel_path).is_file():
        _run_checked(
            context,
            [python_executable, "-m", "pip", "install", "--user", "--force-reinstall", wheel_path],
            "Harness wheel rollback",
        )
    elif context.previous_harness_source is not None:
        _run_checked(
            context,
            [
                context.python_executable,
                "-m",
                "pip",
                "install",
                "--user",
                "--editable",
                str(context.previous_harness_source),
            ],
            "Harness editable rollback",
        )


def _verify_automatic_rollback(context: DeploymentContext, previous_manifest: Mapping[str, Any]) -> None:
    manifest_path = context.harness_home / "install" / "manifest.json"
    if not previous_manifest:
        if manifest_path.exists():
            raise DeploymentError(f"Rollback verification expected no install manifest: {manifest_path}")
        return
    previous_python = previous_manifest.get("python_executable")
    if not isinstance(previous_python, str) or not Path(previous_python).is_file():
        raise DeploymentError("Rollback verification cannot run the recorded Harness interpreter")
    completed = _run_checked(
        context,
        [
            previous_python,
            "-m",
            "personal_harness.install_command",
            "--harness-home",
            str(context.harness_home),
            "doctor",
            "--json",
        ],
        "Harness post-rollback doctor",
        env={
            **{key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
            "HARNESS_ROLLBACK_VERIFY": "1",
        },
        cwd=context.harness_home,
    )
    try:
        verification = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DeploymentError(f"Harness post-rollback doctor returned invalid JSON: {exc}") from exc
    if not isinstance(verification, Mapping) or verification.get("ok") is not True:
        raise DeploymentError(f"Harness post-rollback doctor failed: {verification}")


def _run_checked(
    context: DeploymentContext,
    command: list[str],
    label: str,
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = context.runner(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        **({"env": dict(env)} if env is not None else {}),
        **({"cwd": str(cwd)} if cwd is not None else {}),
    )
    if completed.returncode != 0:
        raise DeploymentError(f"{label} failed ({completed.returncode}): {completed.stderr.strip()}")
    return completed


def _read_optional_json(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise DeploymentError(f"Expected JSON object at {path}")
    return payload


def _all_commands(value: Any) -> list[str]:
    commands: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "command" and isinstance(item, str):
                commands.append(item)
            else:
                commands.extend(_all_commands(item))
    elif isinstance(value, list):
        for item in value:
            commands.extend(_all_commands(item))
    return commands


_REMOVE_HOOK_NODE = object()


def _foreign_hooks_snapshot(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    hooks = payload.get("hooks")
    if not isinstance(hooks, Mapping):
        return {}
    snapshot: dict[str, Any] = {}
    for event, groups in hooks.items():
        filtered = _without_omx_hook_nodes(groups)
        if filtered is not _REMOVE_HOOK_NODE and filtered not in ([], {}):
            snapshot[str(event)] = filtered
    return snapshot


def _without_omx_hook_nodes(value: Any) -> Any:
    if isinstance(value, Mapping):
        command = value.get("command")
        if isinstance(command, str) and "codex-native-hook.js" in command:
            return _REMOVE_HOOK_NODE
        result: dict[str, Any] = {}
        for key, item in value.items():
            filtered = _without_omx_hook_nodes(item)
            if filtered is not _REMOVE_HOOK_NODE:
                result[str(key)] = filtered
        if isinstance(value.get("hooks"), list) and not result.get("hooks"):
            return _REMOVE_HOOK_NODE
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            filtered = _without_omx_hook_nodes(item)
            if filtered is not _REMOVE_HOOK_NODE:
                result.append(filtered)
        return result
    return value


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _tree_checksum(root: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        relative = str(path.relative_to(root)).encode()
        digest.update(relative)
        if path.is_symlink():
            digest.update(b"L")
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(b"F")
            digest.update(sha256_file(path).encode())
        else:
            digest.update(b"D")
    return digest.hexdigest()


def _annotate_backup(archive: BackupArchive, deployment: Mapping[str, Any]) -> None:
    payload = json.loads(archive.manifest_path.read_text(encoding="utf-8"))
    payload["deployment"] = dict(deployment)
    atomic_write_json(archive.manifest_path, payload)


def _write_transaction_state(
    path: Path,
    transaction: Mapping[str, Any],
    status: str,
    *,
    error: str | None = None,
) -> None:
    payload = dict(transaction)
    payload.update({"status": status, "updated_at": time.time()})
    if error is not None:
        payload["error"] = error
    atomic_write_json(path, payload)


def _append_transaction_log(
    harness_home: Path,
    transaction: Mapping[str, Any],
    status: str,
    *,
    error: str | None = None,
) -> None:
    path = harness_home / "logs" / "transactions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": "harness-transaction-log/v1",
        "deployment_id": transaction["deployment_id"],
        "installation_id": transaction["installation_id"],
        "backup_id": transaction["backup_id"],
        "status": status,
        "timestamp": time.time(),
    }
    if error is not None:
        record["error"] = error
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


__all__ = [
    "BackupArchive",
    "DeploymentContext",
    "DeploymentError",
    "deployment_lock",
    "deploy_release",
]
