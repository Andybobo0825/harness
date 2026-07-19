# Harness Capture、OMX Oversized Hook 與 Lifecycle 設計

## 目標

- Harness capture 同時理解 Codex 的 `function_call` 與 `custom_tool_call` 事件。
- OMX hook 遇到 oversized stdin 時持續 drain，避免上游寫入端收到 `EPIPE`。
- oversized `UserPromptSubmit` 採 fail-open，避免圖片附件讓 prompt 被阻擋；`Stop` 的安全行為維持不變。
- memory 測試不依賴過期的固定日期。
- lifecycle checkpoint 可由唯一 session ID 串接，並保留 capture failure 的具體錯誤。

## Capture 正規化

兩種 call schema 進入同一條處理流程：

- `function_call` 使用 `arguments`。
- `custom_tool_call` 使用 `input`，並辨識單一 `tools.exec_command({ cmd: ... })` 的命令。
- `custom_tool_call_output` 的文字區塊合併為一份輸出。
- `Script completed` / `Script failed` 視為明確執行結果。

若一個 custom call 內含多個 shell command，仍計入 tool call，但不把它誤認成單一 verification command。

## Oversized Hook

stdin 超過限制後停止累積內容，但持續讀到 EOF。`UserPromptSubmit` 無法解析 oversized 圖片 prompt 時回傳空物件 fail-open；`Stop` 仍依現有 active-state 規則決定是否阻擋。

## Lifecycle 與容量控制

- 每次 harness launch 產生唯一 session ID，start/complete checkpoints 共用。
- capture failure 的 status 與錯誤字串寫入 checkpoint、replay 與 state summary。
- `.harness/state` 只保留最近 50 筆 checkpoint summary。
- `.harness/flow-checkpoints/checkpoints.jsonl` 保留完整歷史，且不會自動注入模型上下文。

因此新增 session ID 不會讓單一 session 的 prompt/context 持續膨脹；主要成本只是少量磁碟 metadata。
