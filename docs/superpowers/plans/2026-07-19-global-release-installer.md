# Global Harness Release Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a versioned global Harness CLI that installs only immutable GitHub Release artifacts and transactionally manages the pinned OMX overlay, Codex hooks, state migrations, backups, rollback, and post-install smoke tests.

**Architecture:** Keep immutable release parsing/checksums, OMX patching, filesystem transactions, smoke tests, and CLI orchestration in separate modules. The current CLI downloads a release into staging, then launches the downloaded wheel through zipimport so the new version owns the install transaction and can roll back every mutable surface.

**Tech Stack:** Python 3.11+ standard library, setuptools wheel, npm global OMX package, GitHub Releases API, GitHub Actions, unittest.

---

## File Structure

- `personal_harness/release_contract.py`: release/install manifest validation, SHA-256, atomic JSON.
- `personal_harness/omx_overlay.py`: pinned OMX discovery, preimage validation, idempotent overlay.
- `personal_harness/deployment.py`: backup archive, restore, transactional install steps, hook setup.
- `personal_harness/install_smoke.py`: three post-install smoke tests.
- `personal_harness/install_command.py`: `harness install/update/doctor/rollback/version` CLI and staged wheel runner.
- `personal_harness/harness_state.py`: v1→v2 migration and lazy migration support.
- `personal_harness/release_builder.py`: wheel-adjacent release manifest and checksum generation.
- `scripts/harness`: repository wrapper.
- `scripts/build-harness-release`: release artifact builder wrapper.
- `.github/workflows/ci.yml`: tests and wheel verification for branches/PRs.
- `.github/workflows/release.yml`: tag-only GitHub Release publication.
- `tests/test_release_contract.py`, `tests/test_omx_overlay.py`, `tests/test_deployment.py`, `tests/test_install_smoke.py`, `tests/test_install_command.py`, `tests/test_release_builder.py`: focused TDD coverage.

### Task 1: Commit the verified capture/lifecycle prerequisite

**Files:**
- Modify: `personal_harness/codex_capture.py`
- Modify: `personal_harness/flow_checkpoint.py`
- Modify: `personal_harness/launcher.py`
- Modify: `tests/test_codex_capture.py`
- Modify: `tests/test_flow_checkpoint.py`
- Modify: `tests/test_launcher.py`
- Add: `docs/superpowers/specs/2026-07-18-harness-capture-hook-lifecycle-design.md`
- Add: `docs/superpowers/plans/2026-07-18-harness-capture-hook-lifecycle.md`

- [ ] Run the existing regression tests and confirm custom capture, lifecycle IDs, failure details, and retention pass.
- [ ] Run the full suite and confirm all tests pass.
- [ ] Commit only the prerequisite diff with Lore trailers.

### Task 2: Release and install manifest contracts

**Files:**
- Create: `personal_harness/release_contract.py`
- Create: `tests/test_release_contract.py`

- [ ] Write failing tests for strict `harness-release/v1` validation, stable-release rejection of draft/prerelease metadata, SHA-256 verification, install-manifest atomic writes, and checksum mismatch failure.
- [ ] Run `python3 -m unittest tests.test_release_contract -v`; expect missing-module failure.
- [ ] Implement typed `ReleaseManifest`, `InstallManifest`, `sha256_file()`, `load_release_manifest()`, `verify_release_assets()`, and `atomic_write_json()` using only the standard library.

Core API:

```python
@dataclass(frozen=True)
class ReleaseManifest:
    version: str
    tag: str
    wheel: str
    wheel_sha256: str
    omx_version: str
    omx_integrity: str
    overlay_revision: str
    state_schema: str
    smoke_tests: tuple[str, ...]

def verify_release_assets(manifest: ReleaseManifest, directory: Path) -> None: ...
def write_install_manifest(path: Path, payload: Mapping[str, Any]) -> None: ...
```

- [ ] Re-run the focused tests and confirm green.

### Task 3: State v2 migration

**Files:**
- Modify: `personal_harness/harness_state.py`
- Modify: `tests/test_harness_state.py`
- Modify: callers that construct state when required by the new fields.

- [ ] Write failing tests for v1→v2 preservation, v2 no-op, malformed-state preservation, installation ID injection, and lazy migration during read.
- [ ] Run `python3 -m unittest tests.test_harness_state -v`; expect schema failures.
- [ ] Implement `LEGACY_SCHEMA_VERSION`, v2 `SCHEMA_VERSION`, `migrate_personal_harness_state(root, installation_id, now=None)`, and a read path that migrates only valid v1 data atomically.

Migration result:

```python
{
  "schema_version": "personal-harness-state/v2",
  "installation_id": "...",
  "state_revision": 2,
  "migrated_at": 1784390400.0,
  "active": False,
  "metadata": {...}
}
```

- [ ] Re-run state plus launcher/controller tests and confirm green.

### Task 4: Pinned OMX overlay

**Files:**
- Create: `personal_harness/omx_overlay.py`
- Create: `tests/test_omx_overlay.py`

- [ ] Write failing fixture-based tests for official `0.20.2` preimage patching, source/dist updates, `already_applied`, unknown content rejection, wrong OMX version rejection, and postimage checksums.
- [ ] Run `python3 -m unittest tests.test_omx_overlay -v`; expect missing-module failure.
- [ ] Implement exact fragment replacement for the stdin drain and UserPromptSubmit fail-open changes. Require all old fragments or all new fragments; mixed/unknown files fail without partial writes.

Core API:

```python
@dataclass(frozen=True)
class OmxOverlayResult:
    version: str
    revision: str
    status: str
    before: Mapping[str, str]
    after: Mapping[str, str]

def apply_omx_overlay(package_root: Path, expected_version: str = "0.20.2") -> OmxOverlayResult: ...
```

- [ ] Re-run focused tests and confirm green.

### Task 5: Backup, install transaction, hooks, and rollback

**Files:**
- Create: `personal_harness/deployment.py`
- Create: `tests/test_deployment.py`

- [ ] Write failing tests for file/directory/symlink backups, unique backup IDs, restore of deleted and overwritten paths, automatic rollback after injected hook/smoke failure, preservation of non-OMX hooks, and install manifest written only after success.
- [ ] Run `python3 -m unittest tests.test_deployment -v`; expect missing-module failure.
- [ ] Implement `BackupArchive`, `DeploymentContext`, `DeploymentTransaction`, injected command runner, pinned npm install, `omx setup --scope user --merge-agents`, and hook target validation.

Transaction boundary:

```python
with DeploymentTransaction(context) as transaction:
    transaction.backup_targets()
    transaction.install_harness_wheel()
    transaction.ensure_pinned_omx()
    transaction.apply_overlay()
    transaction.refresh_hooks()
    transaction.migrate_states()
    transaction.run_smoke_tests()
    transaction.commit_manifest()
```

- [ ] Re-run focused tests and confirm both success and injected-failure rollback paths.

### Task 6: Post-install smoke tests

**Files:**
- Create: `personal_harness/install_smoke.py`
- Create: `tests/test_install_smoke.py`

- [ ] Write failing tests for the `custom_capture`, `tmux_oversized_image`, and `lifecycle` smoke functions, including producer-side EPIPE capture and `{}` hook output.
- [ ] Run `python3 -m unittest tests.test_install_smoke -v`; expect missing-module failure.
- [ ] Implement synthetic custom session capture, a 6 MiB chunked producer with `TMUX` set, and a temporary lifecycle/migration/capture-failure run.

Result contract:

```python
SmokeResult(name="tmux_oversized_image", passed=True, details={"hook_exit_code": 0, "producer_error": None})
```

- [ ] Re-run focused smoke tests against both fixture hook and installed patched OMX hook.

### Task 7: Global CLI and GitHub Release updater

**Files:**
- Create: `personal_harness/install_command.py`
- Create: `tests/test_install_command.py`
- Create: `scripts/harness`
- Modify: `pyproject.toml`
- Modify: `tests/test_packaging.py`
- Modify: `README.md`

- [ ] Write failing tests for all five subcommands, GitHub stable-release selection, draft/prerelease rejection, explicit version selection, staged wheel zipimport command, download checksum failure, and non-zero exit propagation.
- [ ] Run `python3 -m unittest tests.test_install_command tests.test_packaging -v`; expect missing CLI and console-script failures.
- [ ] Implement the CLI with `urllib.request`, `HARNESS_RELEASE_REPOSITORY` override, `HARNESS_HOME` override, and no branch/tarball install path.
- [ ] Add `harness = "personal_harness.install_command:main"`, bump package version to `1.1.0`, and document global lifecycle commands.
- [ ] Re-run focused tests and wrapper `--help` smoke.

### Task 8: Release artifact builder and GitHub workflows

**Files:**
- Create: `personal_harness/release_builder.py`
- Create: `tests/test_release_builder.py`
- Create: `scripts/build-harness-release`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/release.yml`

- [ ] Write failing tests that build a release directory from a wheel, verify tag/version agreement, deterministic `SHA256SUMS`, release manifest contents, and rejection of non-wheel/mismatched artifacts.
- [ ] Run `python3 -m unittest tests.test_release_builder -v`; expect missing-module failure.
- [ ] Implement the builder and wrappers.
- [ ] Add CI for tests/build/install smoke and a `v*` tag-only release workflow using `gh release create` or `softprops/action-gh-release` with the three verified artifacts.
- [ ] Build `dist/release/v1.1.0`, install the wheel into a temporary virtual environment, and run all smoke tests.

### Task 9: Real upgrade transaction and completion verification

**Files:**
- Runtime-only: user-level Harness install, global OMX, Codex hooks, install manifest/backups.

- [ ] Run all 1.1.0 unit tests and `python3 -m compileall`.
- [ ] Build the wheel and release metadata, then run the staged installer against the real user environment.
- [ ] Confirm OMX is pinned at `0.20.2`, overlay is applied, hooks point at patched dist, state migration succeeds, and three smoke tests pass.
- [ ] Run `harness doctor` and verify all recorded checksums.
- [ ] Perform a controlled injected-failure transaction in a temporary isolated environment and verify automatic rollback.
- [ ] Record a Harness flow checkpoint containing test, release-build, doctor, and smoke evidence.

### Task 10: Review, publish branch, and open draft PR

**Files:**
- All scoped source, tests, docs, scripts, and workflows.

- [ ] Review `git diff --check`, status, release artifacts exclusion, and secret scan.
- [ ] Commit implementation using Lore trailers and explicit file staging.
- [ ] Push `agent/global-release-installer` to `origin`.
- [ ] Open a draft PR against the GitHub default branch summarizing deployment behavior, compatibility boundary, rollback, and verification.
- [ ] Do not create a production GitHub Release until the PR is merged and a `v1.1.0` tag is intentionally pushed.
