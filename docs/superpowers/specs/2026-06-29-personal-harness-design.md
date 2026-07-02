# 個人化 Harness 設計規格

## 目標

建立一套獨立的 HarnessX-inspired harness-coding agent。底層 LLM/coding executor 仍是目前 Codex agent，但 runtime、replay、candidate、gate 與 state 由 `.harness/` 自主管理，不以 OMX workflow 命名或作為主架構。

## 範圍

本階段實作 dependency-free Python package，方便本地測試與後續接到獨立 CLI 或任意 agent frontend。

納入範圍：

- lifecycle hooks 與 processors；
- deterministic processor composition；
- JSONL replay storage；
- deterministic eval gate；
- Digester / Planner / Evolver / Critic 四階段 AEGIS pipeline；
- LLM-generated candidates 由目前 Codex agent 透過 JSON 檔案協定產生；
- variant isolation / ensemble routing；
- bounded replay buffer 與 mock cross-harness GRPO seam；
- optional read-only OMX snapshot（相容/觀察用途）；
- 寫入 `.harness/state/personal-harness-state.json`。

不納入範圍：

- 真實模型 fine-tuning；
- 真實 GRPO training；
- 自動修改 OMX 既有 state；OMX 僅作相容觀察，不是主 runtime；
- tmux/team runtime 控制；
- 完整產品化 CLI。

## 架構

`personal_harness` 分成以下模組：

- `core.py`：Event、Hook、Processor、HarnessConfig、composition contract。
- `replay.py`：JSONL replay store，儲存 trajectory 與 audit record。
- `eval.py`：deterministic candidate gate。
- `evolution.py`：candidate edit accept/reject loop。
- `aegis.py`：Digester、Planner、Evolver、Critic、AEGISPipeline。
- `variants.py`：HarnessVariant、VariantRouter。
- `codex_agent.py`：Codex agent candidate request/response 檔案協定。
- `coevolution.py`：CrossHarnessReplayBuffer、GRPOTrainer seam、CoEvolutionEngine。
- `omx_adapter.py`：optional read-only `.omx` compatibility snapshot。
- `harness_state.py`：standalone `.harness` runtime state writer/reader。
- `launcher.py`：`harness-codex` 啟動命令、session lifecycle state、`harness-status` 狀態輸出。

## 資料流

1. 任務執行結果寫入 replay。
2. Digester 從 replay 中提取失敗任務與 failure category。
3. Planner 產生 adaptation landscape。
4. Evolver 產生 candidate harness edits；LLM/程式生成者就是目前 Codex agent，可透過 `.harness/candidates/codex-candidate-request.json` / `codex-candidate-response.json` 交接。
5. Critic 擋掉 reward hacking / verifier exploit 風險。
6. EvaluationGate 做 manifest、smoke、seesaw 檢查。
7. EvolutionEngine ship 第一個通過 gate 的 candidate，或記錄 rejection。
8. VariantRouter 可根據 replay 成功率 route task 到最適 variant。
9. CoEvolutionEngine 使用同一 replay buffer 驅動 harness evolution 與 mock model update seam。
10. Runtime state 可寫入 `.harness/state/personal-harness-state.json`。
11. `harness-codex` 啟動 Codex backend，預設 gpt-5.5/high/YOLO，並在 session 開始/結束更新 `.harness` state；tmux 內會暫時顯示 harness footer。

## 安全與錯誤處理

- read-only hooks 被 mutation 時 raise `HarnessContractError`。
- malformed replay JSONL 會附 line number。
- gate rejection 用 stable reason code 表示。
- Critic 會拒絕含 exploit / hardcode / verifier hack 等明顯風險的 candidate。
- personal harness state 寫入自己的 `.harness` state 檔，不覆寫外部 runtime state。

## 測試策略

使用 Python `unittest`，不新增 dependency。測試涵蓋：

- core processor composition；
- replay persistence；
- eval/evolution gate；
- AEGIS pipeline；
- variant routing；
- co-evolution seam；
- optional OMX compatibility adapter；
- standalone `.harness` personal state writer。

## 個人化原則

這不是通用 framework，也不是 OMX workflow；它是我的 harness-coding agent runtime。優先順序是：可審計、可測試、可局部替換、與外部 orchestration 低耦合。
