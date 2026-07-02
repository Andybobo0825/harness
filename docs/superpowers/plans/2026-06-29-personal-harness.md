# 個人化 Harness 實作計畫

> **給 agentic workers：** 以測試先行實作。每個任務都要能獨立驗證，避免修改任何外部 runtime state。

**目標：** 建立獨立的 harness-coding agent runtime；底層 coding/LLM executor 仍是目前 Codex agent，但 typing、eval、replay、AEGIS、variant、co-evolution seam 與 runtime state 都歸屬 `.harness/`。

**架構：** dependency-free Python package。OMX 僅保留 optional read-only adapter 作為既有環境觀察來源；不是主 workflow，也不是 state owner。核心 harness 保持可測試。

**技術棧：** Python 3 標準庫、`unittest`、JSONL。

---

### 任務 1：Typed Core

**檔案：**
- `personal_harness/core.py`
- `tests/test_core.py`

步驟：
- 寫 processor ordering、singleton replacement、intercept、read-only mutation 測試。
- 確認紅測。
- 實作 core types 與 `run_hook()`。
- 確認測試轉綠。

### 任務 2：Replay Store

**檔案：**
- `personal_harness/replay.py`
- `tests/test_replay.py`

步驟：
- 寫 append/read、solved task lookup、malformed JSONL 測試。
- 實作 JSONL replay store。
- 確認測試轉綠。

### 任務 3：Eval Gate 與 Evolution Loop

**檔案：**
- `personal_harness/eval.py`
- `personal_harness/evolution.py`
- `tests/test_eval_evolution.py`

步驟：
- 寫 manifest、smoke、seesaw rejection 測試。
- 寫 candidate accepted/rejected audit 測試。
- 實作 deterministic gate 與 evolution engine。
- 確認測試轉綠。

### 任務 4：AEGIS 四階段 Pipeline

**檔案：**
- `personal_harness/aegis.py`
- `tests/test_aegis.py`

步驟：
- 寫 Digester/Planner/Evolver/Critic pipeline 測試。
- 寫 no-op short circuit 測試。
- 寫 exploit-risk critic rejection 測試。
- 實作四階段 pipeline。

### 任務 5：Variant Isolation

**檔案：**
- `personal_harness/variants.py`
- `tests/test_variants.py`

步驟：
- 寫根據 prior success rate route task 的測試。
- 寫 variant pool 滿時淘汰最低成功率 variant 的測試。
- 實作 `HarnessVariant` 與 `VariantRouter`。

### 任務 6：Co-evolution Seam

**檔案：**
- `personal_harness/coevolution.py`
- `tests/test_coevolution.py`

步驟：
- 寫 bounded replay buffer 測試。
- 寫 group-relative advantage 測試。
- 寫 harness evolution + mock GRPO model update 測試。
- 實作 deterministic trainer seam。

### 任務 7：相容 Adapter 與獨立 Runtime State

**檔案：**
- `personal_harness/omx_adapter.py`
- `personal_harness/harness_state.py`
- `tests/test_omx_adapter.py`
- `tests/test_harness_state.py`

步驟：
- 寫 read-only `.omx` snapshot 測試。
- 寫 `.harness/state/personal-harness-state.json` 寫入/讀取測試。
- 實作 read-only adapter 與獨立 state writer。

### 任務 8：繁體中文文件與驗證

**檔案：**
- `README.md`
- `docs/superpowers/specs/2026-06-29-personal-harness-design.md`
- `docs/superpowers/plans/2026-06-29-personal-harness.md`

步驟：
- 文件改為繁體中文。
- 記錄架構、邊界、目前缺口。
- 執行：`python3 -m unittest discover -s tests -p 'test_*.py' -v`。
- 執行：`python3 -m compileall personal_harness tests`。

### 任務 9：harness-codex 啟動器

**檔案：**
- `personal_harness/launcher.py`
- `scripts/harness-codex`
- `scripts/harness-status`
- `tests/test_launcher.py`

步驟：
- 寫 gpt-5.5/high/YOLO 預設命令測試。
- 寫 session active -> closed state lifecycle 測試。
- 寫一行 harness status 測試。
- 實作啟動器、狀態輸出與 tmux footer 暫時套用/還原。
