# Implementation Plan

1. 以回歸測試覆蓋 custom tool capture 與動態 memory 日期。
2. 正規化 custom call/output，並解析明確的 custom exec status。
3. 以回歸測試覆蓋 checkpoint session ID、capture error 與 state retention。
4. 將唯一 session ID 與 capture failure details 串入 lifecycle checkpoints。
5. 以 OMX hook 測試重現 oversized `EPIPE` 與圖片 prompt 阻擋。
6. 修改 source 與 installed dist：oversized stdin drain、`UserPromptSubmit` fail-open。
7. 執行 Harness 完整測試、OMX hook 測試、實際 POS capture dry-run 與 checkpoint 驗證。
