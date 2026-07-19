# Harness 全域 Release 安裝器設計

## 目標

將目前的 editable Harness 與手動 OMX hotfix 轉為可重現、可驗證、可回滾的全域產品安裝流程。所有專案共用同一套已版本化的 Harness CLI 與 OMX 相容層；各專案仍只保存自己的 `.harness/` runtime state。

成功條件：

- GitHub Release 是唯一正式更新來源，不讀取或直接安裝 `main`。
- Release 提供 Python wheel、release manifest 與 SHA-256 checksums。
- `harness install`、`harness update`、`harness doctor`、`harness rollback`、`harness version` 可管理完整生命週期。
- 一次交易完成 Harness wheel、OMX overlay、Codex hooks、state migration 與 post-install smoke tests。
- 任何修改步驟失敗時，自動恢復交易前狀態。

## 發布契約

GitHub tag 使用 `v<version>`，Release 必須包含：

- `personal_harness-<version>-py3-none-any.whl`
- `harness-release.json`
- `SHA256SUMS`

`harness-release.json` schema 為 `harness-release/v1`，包含：

- Harness 版本與 GitHub tag。
- wheel 檔名與 SHA-256。
- 支援的平台與最低 Python 版本。
- 唯一允許的 OMX 版本 `0.20.2`。
- OMX npm tarball URL、integrity 與 overlay revision。
- state schema 目標版本。
- 必須通過的 smoke test 名稱。

Updater 只讀 GitHub Releases API 的正式 release；忽略 draft、prerelease 與 branch archive。使用者可透過 `--version` 安裝指定 release，否則採最新穩定版。

## 安裝架構

新增 `harness` console command：

```text
harness install [--release-manifest PATH] [--wheel PATH]
harness update [--version VERSION]
harness doctor
harness rollback [--backup-id ID]
harness version
```

使用者層級安裝資料位於 `HARNESS_HOME`，預設為：

```text
~/.local/share/harness-codex/
  install/manifest.json
  releases/<version>/
  backups/<backup-id>/
  logs/
```

專案 `.harness/` 不保存安裝器資料，也不持有 OMX patch。

## 更新交易

更新採 staged two-process 流程，避免執行中的舊 Harness 模組覆蓋自己：

1. 目前 CLI 從 GitHub Release 下載 manifest、wheel 與 checksums 到暫存目錄。
2. 驗證 manifest schema、release tag、wheel SHA-256 與 `SHA256SUMS` 一致。
3. 以 wheel 作為 zipimport 路徑啟動「新版本」deployment runner。
4. Runner 建立唯一 backup ID，備份目前 install manifest、已保存的 Harness wheel、全域 OMX package、OMX bin link 與會被更新的 Codex hook/config 檔案。
5. 安裝新 Harness wheel。
6. 確認 npm package `oh-my-codex@0.20.2`；不同版本時先安裝 pinned 版本。
7. 對 OMX source 與 dist 套用可重入 overlay；只接受官方 preimage 或已套用 overlay 的內容，未知內容立即失敗。
8. 執行 OMX user-scope setup，以 merge 方式更新 Codex hooks，保留非 OMX hook entries。
9. 執行已知 project roots 的 state migration；未知專案在下一次讀取 state 時 lazy migration。
10. 執行 post-install smoke tests。
11. 所有 smoke tests 通過後，原子寫入 install manifest 並保留此次 wheel 與 release metadata。

步驟 4 之後任一步驟失敗，都必須反向恢復備份。rollback 本身必須記錄結果，不可覆寫最後一份可用備份。

## OMX Overlay

第一版只支援官方 `oh-my-codex@0.20.2`。Overlay 修正：

- oversized stdin 超過限制後停止累積但持續 drain 到 EOF，不呼叫 `process.stdin.destroy()`。
- oversized `UserPromptSubmit` 回傳 `{}` fail-open。
- oversized `Stop` 仍保留原本 active-workflow safety gate。

Overlay 同時更新：

- `dist/scripts/codex-native-hook.js`
- `src/scripts/codex-native-hook.ts`

Patcher 以明確 preimage/postimage 片段及 SHA-256 驗證，不使用寬鬆正規表示式猜測未知版本。重複執行必須回報 `already_applied`，不可再次修改。

當官方 OMX release 已包含等價修正時，新的 Harness release manifest 可以將 overlay revision 設為 `none`，並更新 pinned OMX 版本。

## Codex Hooks

安裝器透過 pinned OMX 的 user-scope merge setup 更新 hooks。交易前備份：

- `~/.codex/hooks.json`
- `~/.codex/config.toml`
- `~/.codex/AGENTS.md`
- OMX 管理的 prompts、skills 與 agents 目錄 metadata

驗證時必須確認所有 OMX-managed hook commands 指向實際 patched `dist/scripts/codex-native-hook.js`，並保留非 OMX hook commands。

## State Schema Migration

State schema 升級為 `personal-harness-state/v2`，新增：

- `installation_id`：來自全域 install manifest。
- `state_revision`：單調遞增 migration revision。
- `migrated_at`：最後 migration 時間。

Migration 規則：

- v1 → v2 保留所有 runtime 與 metadata 欄位。
- migration 使用 temporary file 加 `os.replace()` 原子寫入。
- malformed state 不覆寫，回報失敗並觸發整體安裝 rollback。
- v2 重複 migration 是 no-op。
- launcher 每次讀取 state 時可執行相同的 lazy migration，確保未登錄專案仍能升級。

## Install Manifest 與 Checksums

全域 install manifest schema 為 `harness-install/v1`，記錄：

- installation ID、Harness version、release tag 與安裝時間。
- Python executable、wheel path 與 wheel SHA-256。
- OMX version、package root、overlay revision、overlay 前後 checksum。
- hooks path 與 checksum。
- 已完成的 state migration roots。
- smoke test 名稱、結果與執行時間。
- 此次更新的 backup ID 與前一個成功 manifest checksum。

Manifest 使用 temporary file 加 `os.replace()` 原子更新。`harness doctor` 重新計算檔案 checksum 並對照 manifest，差異視為 drift。

## Backup 與 Rollback

Backup manifest 記錄每個路徑原本是否存在、型態、mode 與 SHA-256。檔案與 symlink 原樣備份；OMX package directory 完整複製，避免 rollback 依賴網路。

自動 rollback 順序：

1. 停止後續安裝步驟。
2. 恢復 OMX package 與 bin link。
3. 恢復 Codex hooks/config。
4. 重新安裝備份的 Harness wheel；初次從 editable install 轉換時，保存可重新安裝的 source path。
5. 恢復舊 install manifest。
6. 執行 rollback doctor，將結果寫入獨立 log。

如果 rollback 也失敗，CLI 必須非零退出並輸出 backup 路徑，不得宣稱安裝成功。

## Post-install Smoke Tests

每次 install/update 必須在暫存目錄執行：

1. `custom_capture`：建立包含 `custom_tool_call`、`custom_tool_call_output` 的 synthetic session，確認 tool call 與 verification 被捕捉。
2. `tmux_oversized_image`：在帶有 `TMUX` 環境的 producer 中向 native hook 串流至少 6 MiB `UserPromptSubmit`，確認無 `EPIPE`、exit code 0、stdout `{}`。
3. `lifecycle`：建立 v1 state、migration 到 v2，啟動與關閉 session，確認唯一 session ID、start/complete checkpoint 關聯與 capture failure details 持久化。

任何 smoke test 失敗都視為 deployment failure 並觸發 rollback。

## GitHub Actions

新增 release workflow：

- PR／push 執行 Python tests、wheel build、wheel install smoke 與 artifact checksum verification。
- `v*` tag 驗證 tag 與 package version 一致。
- build wheel、產生 `harness-release.json` 與 `SHA256SUMS`。
- 在乾淨 runner 執行 installer smoke tests。
- 驗證成功後建立 GitHub Release 並上傳三個 artifacts。

Workflow 不從 `main` 自動發布；只有 tag 能產生正式 Release。

## 相容性與停止條件

- Python：3.11 以上。
- 第一版 OMX：僅 `0.20.2`。
- macOS/Linux；Windows 在 manifest 中標示不支援，不執行全域 OMX 安裝。
- 安裝成功的唯一條件是 manifest 原子寫入完成且三組 smoke tests全數通過。
- 不自動修改任何專案原始碼；只 migration `.harness/state`。
