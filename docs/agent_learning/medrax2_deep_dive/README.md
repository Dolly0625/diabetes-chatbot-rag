# MedRAX2 Agent 設計深入導讀

這套文件不是 MedRAX2 README 的翻譯，而是以固定原始碼快照為準，逐層回答「這個 Agent 到底如何運作、控制權在哪裡、哪些能力來自框架、哪些能力來自應用程式」。

## 研究基準

- Repository：[`bowang-lab/MedRAX2`](https://github.com/bowang-lab/MedRAX2)
- 固定 commit：[`dcd6b852f3f9557640159e200fab5f0acdea39ff`](https://github.com/bowang-lab/MedRAX2/tree/dcd6b852f3f9557640159e200fab5f0acdea39ff)
- Commit date：2026-04-03
- 本文件檢查日期：2026-08-23
- 對照系統：本工作區 `tfda_context_gate/`

所有 GitHub 連結都固定到上述 commit，避免 `main` 更新後文件描述和程式碼不一致。程式碼節錄只保留理解控制流程所需的部分；省略號表示原始碼被裁切，不代表可以直接複製執行。

## 建議閱讀順序

| 順序 | 文件 | 核心問題 |
| --- | --- | --- |
| 1 | [01_全貌與原始碼地圖.md](01_全貌與原始碼地圖.md) | MedRAX2 的複雜度分布在哪裡？ |
| 2 | [02_Agent核心Graph逐行解析.md](02_Agent核心Graph逐行解析.md) | 121 行 `agent.py` 如何形成 ReAct loop？ |
| 3 | [03_State_Message與Memory.md](03_State_Message與Memory.md) | state、message、checkpoint、thread 有何差別？ |
| 4 | [04_Tool系統與SelectiveInitialization.md](04_Tool系統與SelectiveInitialization.md) | LLM 如何看見工具？工具如何載入與執行？ |
| 5 | [05_RAG與ModelFactory.md](05_RAG與ModelFactory.md) | RAG 為何是可選 tool？模型供應商如何抽換？ |
| 6 | [06_多模態輸入_API與UI.md](06_多模態輸入_API與UI.md) | 一張影像如何同時交給 LLM 與本地工具？ |
| 7 | [07_Prompt_控制權與安全邊界.md](07_Prompt_控制權與安全邊界.md) | prompt 規則和程式強制有何不同？ |
| 8 | [08_Benchmark_測試與可觀測性.md](08_Benchmark_測試與可觀測性.md) | benchmark 能證明什麼、不能證明什麼？ |
| 9 | [09_對照TFDA與實驗藍圖.md](09_對照TFDA與實驗藍圖.md) | 哪些設計值得搬，哪些必須拒絕？ |
| 10 | [10_動手練習與檢核答案.md](10_動手練習與檢核答案.md) | 如何確認自己真的讀懂？ |
| 11 | [11_原始目標驗收與下一階段缺口.md](11_原始目標驗收與下一階段缺口.md) | 這輪是否真的完成原始要求，還缺哪些 MedRAX2 能力？ |

## 讀完後應能畫出的兩張圖

第一張是 MedRAX2 的內圈：

```text
User messages
      ↓
agent node: LLM
      ├─ no tool_calls ─────────────→ END
      └─ tool_calls
              ↓
          tools node
              ↓ ToolMessage(s)
          agent node: LLM
```

第二張是完整應用外圈：

```text
CLI / Gradio / FastAPI / Web platform
                ↓
        message and file adapters
                ↓
     Agent + checkpointer + tools
          ↓                 ↓
      model provider    model/tool resources
          ↓                 ↓
      final text       images, RAG, search,
                       DICOM, Python sandbox
```

若只能畫出第一張，代表只理解了 Agent loop，還沒有理解 MedRAX2 專案。

## 判讀規則

閱讀每段程式碼時固定問六題：

1. 這一層接收什麼型別？
2. 這一層回傳什麼型別？
3. 誰能決定下一步？
4. 限制是 prompt 建議，還是程式碼強制？
5. 失敗是 raise、回傳 error payload，還是交給 LLM 解讀？
6. 這份 state 是 process memory、conversation memory，還是 durable storage？

## 與既有第八章的關係

既有的 [08_MedRAX2架構對照與討論筆記.md](../08_MedRAX2架構對照與討論筆記.md) 適合會議前快速複習；本資料夾適合逐檔學習與實作。兩者結論應一致，但用途不同。
