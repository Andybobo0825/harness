# Harness Coding Agent（個人化 Harness-Coding Agent）

這是一個**獨立的 HarnessX-inspired coding agent runtime**。底層 LLM / coding executor 仍是目前的 **Codex agent**，但 harness runtime、candidate、replay、gate、state 都改由本專案自己的 `.harness/` 管理；OMX 只是開發時可用的外部工具或相容背景，不是此 agent 的產品架構。

## 定位

- **Codex backend**：目前 Codex agent 負責語意判斷、coding、產生 candidate response。
- **Harness runtime**：本專案自己的 `.harness/` 負責 candidates、replay、gate result、state、variant 與 co-evolution metadata。
- **OMX**：只作為開發輔助/相容 adapter；不是主 workflow，也不是 runtime state owner。

這份 repo 的目標不是完整複製 HarnessX 論文，而是做出可在本機持續擴充、具備自我除錯與自我演化能力的「harness-coding agent」。

## 目前模組

```text
personal_harness/
  core.py          # lifecycle hooks、Event、Processor、HarnessConfig、hook runner
  replay.py        # JSONL replay store：任務軌跡與 evolution audit
  eval.py          # deterministic gate：manifest / smoke / seesaw 檢查
  evolution.py     # candidate edit loop：接受或拒絕候選 harness
  aegis.py         # Digester / Planner / Evolver / Critic 四階段 pipeline
  variants.py      # variant isolation / ensemble routing
  codex_agent.py   # 目前 Codex agent 作為 LLM/coding backend 的 JSON 檔案協定
  codex_capture.py # Codex session JSONL -> AgentExecution 的真實執行紀錄 adapter
  codex_capture_command.py # script command：Codex session capture -> .harness replay/state
  execution_controller.py # task transcript -> replay -> candidate/gate -> state 的閉環控制器
  coevolution.py   # cross-harness replay buffer 與 mock GRPO trainer seam
  omx_adapter.py   # optional read-only `.omx/state` / `.omx/logs` 相容 snapshot
  harness_state.py # 寫入 `.harness/state/personal-harness-state.json`
  harness_command.py # script command：request -> Codex response -> gate -> result
  launcher.py      # harness-codex 啟動器與 harness-status 狀態輸出
```

## Core：typed hook processor

核心 extension point 是 hook-scoped `Processor`：

```python
from personal_harness import Event, HarnessConfig, Hook, Processor, ProcessorOutcome, run_hook

class AddMarker(Processor):
    singleton_group = "marker"

    def process(self, event):
        return ProcessorOutcome.emit(event.with_payload({**event.payload, "marker": "mine"}))

config = HarnessConfig(version="v1").with_processor(Hook.BEFORE_MODEL, AddMarker())
[result] = run_hook(config, Hook.BEFORE_MODEL, Event(Hook.BEFORE_MODEL, {}))
assert result.payload["marker"] == "mine"
```

已支援：

- processor ordering
- singleton group replacement
- pass-through / transform / split / intercept
- read-only hooks mutation guard

## AEGIS：四階段 harness adaptation

`aegis.py` 實作最小可用的 AEGIS pipeline：

1. **Digester**：從 replay 中抽出未解任務與 failure category。
2. **Planner**：把 failure evidence 轉成 adaptation landscape。
3. **Evolver**：透過 deterministic generator 產生 `CandidateEdit`；之後可替換成 LLM-generated candidates。
4. **Critic**：先擋掉明顯 reward hacking / verifier exploit 風險，再交給 deterministic gate。

目前 LLM candidate generation 明確定義為「目前這個 Codex agent」：`codex_agent.py` 會寫出 `.harness/candidates/codex-candidate-request.json`，由當前 Codex agent 讀取、修改 repo 或撰寫候選方案，再寫回 `.harness/candidates/codex-candidate-response.json`。Request 檔案預設會帶 fresh `request_id`，response 必須原樣 echo；gate 預設只消費同一個 `request_id` 的 candidate，沒有 correlation 的舊 response 不會被接受。程式不呼叫外部 LLM API；Codex agent 就是 generator。`Evolver(generator)` 仍保留 deterministic callable seam，方便測試與批次化。

## Eval gate 與 replay audit

Candidate 必須通過：

1. manifest 完整；
2. smoke check 通過；
3. 不宣告會 regression 到 replay 中已 solved 的 task。

所有接受/拒絕都會寫入 replay，metadata 會包含：

- `record_type: evolution_decision`
- candidate 名稱
- base harness version
- candidate harness version
- decision
- stable reason codes

## Variant isolation / ensemble routing

`VariantRouter` 讓不同 task 可以被 route 到不同 harness variant。它會根據 replay 中同一 task / variant 的成功率選擇 variant。當 variant pool 滿了，`fork()` 會淘汰歷史成功率最低的 variant。

這對應 HarnessX 論文中「單一 global harness 在 heterogeneous tasks 上容易 fix-one-break-one」的問題。

## Codex agent candidate handoff

`CodexAgentEvolver` 提供檔案協定：

- request：`.harness/candidates/codex-candidate-request.json`
- response：`.harness/candidates/codex-candidate-response.json`
- `llm_owner` 固定為 `current-codex-agent`

這代表候選 edit 的語意生成者就是目前 Codex agent，而不是另一個模型服務。第一版 response 會轉成 auditable `CandidateEdit`，支援 target version、manifest、expected improvements/regressions、smoke flag。


## Script 指令：request → Codex response → gate

本 repo 提供可直接執行的 script：

```bash
scripts/harness-agent --root . --json
```

流程：

1. 讀取 `.harness/candidates/codex-candidate-request.json`，並要求 request 帶 `request_id`。
2. 如果尚未有 response，依目前 request 產生一份保守的 `codex-candidate-response.json`，並標記 `llm_owner=current-codex-agent`。在互動式使用時，目前 Codex agent 也可以先手動/自動覆寫這個 response。
3. 只將同一個 `request_id` 的 response 轉成 `CandidateEdit`。
4. 跑 `EvaluationGate` 與 `EvolutionEngine`。
5. 寫入 `.harness/candidates/codex-gate-result.json`，包含 `request_id` 與 response path 方便 audit。
6. 接受/拒絕結果會寫入 replay audit。

常用參數：

```bash
# 使用預設 replay：.harness/replay/replay.jsonl
scripts/harness-agent --root . --json

# 指定 replay
scripts/harness-agent --root . --replay .harness/replay/replay.jsonl --json

# 強制根據 request 重產 response
scripts/harness-agent --root . --overwrite-response --json
```

exit code：accepted 為 `0`，rejected 為 `2`。

## Agent execution controller：task → replay → candidate/gate → state

`execution_controller.py` 補上 coding agent runtime 的第一個閉環控制面。它不假裝能攔截 Codex 內部私有事件；第一版邊界是明確的 `AgentExecution` transcript，由 Codex frontend 或後續 hook integration 填入：

- model output；
- tool call result；
- verification / test result；
- ordered execution events（可保留 sequence / correlation id）；
- task metadata。

`AgentExecutionController.record_execution()` 會：

1. 將 model/tool/verification 轉成 replay events；
2. 根據 tool/test exit code 判斷 solved / reward；
3. 將失敗分類為 `tool`、`verification`、`verification_missing` 或 `model`；
4. 寫入 `.harness/replay/replay.jsonl`；
5. 對每次失敗任務寫出帶 fresh `request_id` 的 Codex candidate request；
6. 只接受同一個 `request_id` 的 Codex candidate response，避免舊 response 被新 failure 誤用；
7. 寫入 `.harness/state/personal-harness-state.json`，phase 會是 `task_complete`、`candidate_requested`、`candidate_shipped` 或 `candidate_rejected`，並保留 launcher 已寫入的 `launch` / `status` metadata；失敗任務的 replay/state/outcome 會保留同一個 `request_id`。

這讓目前 harness 從「可演化地基」前進到「可記錄並驅動自我修正的 controller」。

## Codex execution capture：session JSONL → harness

`codex_capture.py` 把真實 Codex session JSONL 轉成 controller 可消費的 `AgentExecution`。第一版採用 file-backed capture：讀取 `~/.codex/sessions/**/*.jsonl` 或呼叫端指定的 session 檔，而不是攔截 Codex 私有 runtime hook。

它會正規化：

- assistant model message → `ExecutionEvent("model_output", ...)`
- `function_call` → ordered `tool_call` event，保留 `call_id` correlation
- `function_call_output` → paired `tool_result`、exit code、tool metadata
- recognized 測試/型別/lint 類 command，且 tool output 有明確 exit code → `VerificationResult` 與 `verification_result` event

為避免 false-positive solved，capture 不用單純 substring 判斷 verification：像 `rg pytest README.md`、`cat pytest.ini` 這類只是搜尋或讀檔的命令不會被當成驗證。若 recognized verification command 的 output 沒有 Codex shell exit-code marker，該 verification 會被記成 failure，而不是推定成功。

常用入口：

```python
from personal_harness import agent_execution_from_codex_session, record_codex_session

execution = agent_execution_from_codex_session("~/.codex/sessions/.../rollout.jsonl", task_id="my-task")

outcome = record_codex_session(
    ".",
    "~/.codex/sessions/.../rollout.jsonl",
    harness_version="v1",
    model_version="gpt-5.5",
    task_id="my-task",
)
```

`record_codex_session()` 會直接呼叫 `AgentExecutionController.record_execution()`，所以 solved execution 會進 `.harness/replay/replay.jsonl` 與 `.harness/state/`；未驗證或失敗 execution 會自動寫出 Codex candidate request。OMX 仍只是開發工具或相容讀取來源，capture 不會把 `.omx` 當產品 runtime state owner。

可直接用 script 測試：

```bash
# 只解析、不寫 .harness
scripts/harness-capture-codex \
  --session ~/.codex/sessions/.../rollout.jsonl \
  --task-id smoke-capture \
  --harness-version v1 \
  --model-version gpt-5.5 \
  --dry-run --json

# 指定 session，寫入 .harness replay/state
scripts/harness-capture-codex \
  --root . \
  --session ~/.codex/sessions/.../rollout.jsonl \
  --task-id smoke-capture \
  --harness-version v1 \
  --model-version gpt-5.5 \
  --json

# 抓目前 repo cwd 對應的最新 Codex session
scripts/harness-capture-codex \
  --root . \
  --latest \
  --cwd . \
  --task-id latest-capture \
  --harness-version v1 \
  --model-version gpt-5.5 \
  --json

# 抓某個 launch 時間之後、目前 repo cwd 對應的第一個 session，並排除已記錄 id
scripts/harness-capture-codex \
  --root . \
  --latest \
  --cwd . \
  --started-after 1782840000.0 \
  --exclude-session-id sess-already-captured \
  --harness-version v1 \
  --model-version gpt-5.5 \
  --json
```

沒有 `--started-after` 時，`--latest` 仍維持「指定 cwd 的最新 session」語意。帶 `--started-after` 時，selector 會改成「該時間後、指定 cwd 的第一個 session」，並可用 `--exclude-session-id` 避免重複回灌。

`harness-codex` 預設會在 Codex process 結束後自動做同一件事：用目前 repo root 當 `cwd`，再加上本次 launch 寫入的 `metadata.launch.started_at`，只選本次 launch 開始後符合 repo cwd 的 Codex session。選到後會解析 `session_meta.id`，把它寫入 `.harness` 的 `metadata.capture_on_exit.session_id` 與 `metadata.captured_sessions`，後續 capture 會排除已記錄的 session id。capture 會在 `close_harness_session()` 前執行，所以 replay/candidate/state 先完成，最後 session state 仍會標成 `closed`，並保留 `metadata.last_task` 與 `metadata.capture_on_exit`。

如果只想開 Codex、不想回灌這次 session：

```bash
scripts/harness-codex --root . --no-capture-on-exit
```

如果 Codex session root 不是預設的 `~/.codex/sessions`：

```bash
scripts/harness-codex --root . --capture-sessions-root /path/to/sessions
```

capture-on-exit 失敗不會覆蓋 Codex 的 exit code；錯誤會寫入 `.harness/state/personal-harness-state.json` 的 `metadata.capture_on_exit`，方便之後診斷。

## 啟動方式：harness-codex

`harness-codex` 是這套 harness-coding agent 的產品化入口。它不叫 `codex --omx`，因為這不是 OMX workflow；它是用 Codex 當 backend、用 `.harness/` 當 runtime 的獨立啟動器。

```bash
scripts/harness-codex --root .
```

預設會啟動：

```bash
codex --model gpt-5.5 -c 'model_reasoning_effort="high"' -C <root> --dangerously-bypass-approvals-and-sandbox
```

也就是：Codex agent、`gpt-5.5`、high reasoning、YOLO 模式。若要先確認實際命令，不啟動 Codex：

```bash
scripts/harness-codex --root . --dry-run
```

啟動器會在真正進入 Codex 前先印出一行 harness 狀態：

```text
[harness] active gpt-5.5 high YOLO session
```

並且在 `.harness` state 裡記錄 status mode：

- 非 tmux 預設：先啟動 tmux，再進入 `metadata.status.mode: tmux-hud-pane`
- 已在 tmux 內啟動：`metadata.status.mode: tmux-hud-pane`
- 非 tmux 但加 `--no-auto-tmux`：不自動啟動 tmux，維持 `metadata.status.mode: inline`

Codex 內建底部 status line 目前只支援固定項目，例如 model、git branch、context、usage；自訂 harness 狀態不會直接塞進 Codex 原生 footer。`harness-codex` 預設會在非 tmux 環境下自動開 tmux 包住整個 Codex session，讓 harness HUD pane 一起啟動。

如果你本來就在 tmux 裡啟動，HUD 會預設出現；如果你不在 tmux 裡，`harness-codex` 也會預設先開 tmux。HUD 會在 Codex 下方放一個 1 行 tmux pane，執行：

```bash
env HARNESS_TMUX_HUD_OWNER=1 HARNESS_TMUX_HUD_LEADER_PANE=%<codex-pane> harness-status --root <root> --compact --watch --color always
```

`HARNESS_TMUX_HUD_OWNER=1` 表示這個 pane 是本次 `harness-codex` launch 建立的；`HARNESS_TMUX_HUD_LEADER_PANE` 記錄原本的 Codex pane id。建立 HUD pane 後，啟動器會明確選回原本 Codex pane，避免鍵盤焦點停在 HUD pane。

如果你原本不在 tmux 裡，預設會自動開一個 tmux session，再在裡面啟動 Codex 與 harness HUD。若不想自動 tmux 包裝：

```bash
scripts/harness-codex --root . --no-auto-tmux
```

HUD 啟動時畫面會有兩個狀態來源：

- Codex TUI 內建 footer：保留 model/context/usage。
- harness HUD pane：顯示 `[harness] active gpt-5.5 high YOLO session | git:<branch> clean`，或在 worktree 有變更時顯示 `dirty:N` / `untracked:M`。

為了避免 tmux/cmux server 週期性執行 `#(harness-status ...)` 而反覆觸發 macOS 檔案存取權限提示，`harness-codex` 預設不改 tmux `status-left`；狀態只放在 harness HUD pane。HUD 與 Codex process 也會帶上 `GIT_CEILING_DIRECTORIES=<project-parent>`，避免 `git status` 往上爬到使用者 home 目錄的上層 repo，進而掃到 `~/Library`、`.Trash` 或其他 App 資料。若你已經授權 tmux/cmux 且想把狀態也放進 tmux footer，可以明確 opt-in：

```bash
HARNESS_TMUX_STATUS_LEFT=1 scripts/harness-codex --root .
```

`harness-status` 可手動輸出，HUD 顯示時也會上色：

- `[harness]`：cyan
- `active` / `clean`：green
- `YOLO`：magenta
- `dirty` / `untracked` / `stale`：yellow
- inactive/closed/no-repo：dim

手動輸出可控制顏色：

```bash
scripts/harness-status --root . --compact --color auto    # TTY 自動上色
scripts/harness-status --root . --compact --color always  # 強制 ANSI color
scripts/harness-status --root . --compact --color never   # 純文字，適合腳本
```

tmux HUD pane 本身也會套預設 pane style：

```text
fg=colour81,bg=colour234
```

可用環境變數覆蓋；若不想套 pane style，設成 `none`：

```bash
HARNESS_HUD_PANE_STYLE='fg=colour10,bg=colour235' scripts/harness-codex --root .
HARNESS_HUD_PANE_STYLE=none scripts/harness-codex --root .
```

git 欄位由 `git status --short --branch --untracked-files=normal` 產生，只做摘要，不讀 diff 內容：

```text
git:main clean
git:main dirty:2 untracked:1
git:no-repo
```

Codex 結束後，`harness-codex` 會 kill HUD pane，並還原 tmux window 設定；若有設定 `HARNESS_TMUX_STATUS_LEFT=1`，也會還原原本的 tmux footer。

Codex 結束後，`harness-codex` 也會預設 capture 這次 session 的 execution transcript，寫入 `.harness/replay/replay.jsonl` 與 `.harness/state/`。這個 capture 會被綁在本次 launch 的 `started_at` 與 repo cwd，不會跨專案吃到其他 cwd 的 session log；成功後會在 state 裡保留解析出的 Codex `session_id`。若不需要這次回灌：

```bash
scripts/harness-codex --root . --no-capture-on-exit
```

如果只想啟動 Codex、不想印出啟動狀態行：

```bash
scripts/harness-codex --root . --quiet-status
```

啟動時會寫入：

```text
.harness/state/personal-harness-state.json
```

並標記：

- `active: true`
- `phase: session`
- `model_version: gpt-5.5`
- `metadata.launch.reasoning: high`
- `metadata.launch.yolo: true`

Codex session 結束時，wrapper 會把同一份 state 改成：

- `active: false`
- `phase: closed`
- `metadata.exit_code: <codex exit code>`

手動狀態檢查仍可用：

```bash
scripts/harness-status --root .
scripts/harness-status --root . --compact
```

如果上一個 Codex process 已經不存在，但 state 還停在 `phase: session`，狀態會顯示 `stale`，避免把舊 session 誤判成目前正在 harness 模式。

如果你是在 tmux 裡啟動，`harness-codex` 會暫時新增 harness HUD pane，類似：

```text
[harness] active gpt-5.5 high YOLO session
```

離開 Codex 後會還原原本 tmux footer/window。若不想啟動 tmux HUD：

```bash
scripts/harness-codex --root . --no-tmux-status
```

## Co-evolution seam

`coevolution.py` 目前不做真實 fine-tuning，但提供：

- `CrossHarnessReplayBuffer`：bounded FIFO replay buffer；
- task-level group-relative advantage；
- `GRPOTrainer`：deterministic mock trainer，回傳 `model-v1+grpo1` 形式的新 model version；
- `CoEvolutionEngine`：同一批 replay 同時驅動 harness evolution 與 model update seam。

這是未來接真實 GRPO / RL training 的界面，不會假裝已經訓練模型。

## Harness runtime state 寫入

`harness_state.py` 會寫入：

```text
.harness/state/personal-harness-state.json
```

schema：

```json
{
  "schema_version": "personal-harness-state/v1",
  "active": true,
  "harness_version": "v2",
  "model_version": "model-v1",
  "variant_id": "default",
  "phase": "aegis"
}
```

此檔案是 personal harness 自己的 runtime state，不覆寫 OMX 既有 state 檔。

## 驗證

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall personal_harness tests
```

目前測試涵蓋：

- typed core processor composition
- replay JSONL persistence
- eval gate accept/reject
- evolution audit record
- AEGIS 四階段 pipeline
- agent execution controller：ordered task transcript、failure classification、request/response correlation、candidate request、gate/state update
- variant routing / fork-retire
- cross-harness replay buffer / mock GRPO update
- `.omx` read-only snapshot
- `.harness/state/personal-harness-state.json` 寫入/讀取

## 仍刻意保留的缺口

- execution controller 已支援 transcript-based 閉環；尚未接入真實 Codex model/tool event hook 自動捕捉。
- LLM candidate generator 已定義為目前 Codex agent 的檔案 handoff；尚未做自動互動式觸發。
- 尚未做真正 GRPO / model fine-tuning。
- 尚未包裝成正式套件或全域安裝型 CLI；目前入口是 `scripts/harness-codex`、`scripts/harness-agent`、`scripts/harness-status`。
- variant routing 尚未和 AEGIS candidate fork 自動連動。

這些會是下一階段擴充點。
