# LINE → 專用看診前對談室 入口（work tree: line-previsit-room-entry）

## 產品決策
- LINE 留給日常衛教；**不在 LINE 內開始八題問診**。
- 使用者說 `我要準備看診` 時，不直接進入 intake，回覆專用入口 Flex 並引導至網頁對談室。
- 對談室支援暫停續填、最後由病患確認才分享給醫護。

## 契約
- 統一觸發文字：`開啟看診前對談室`（常數 `PREVISIT_ROOM_TRIGGER_TEXT`）。
- 用戶點按時 `send text` 必須精準等於該字串，讓 backend 產生安全 `room_url`。

## UI Helper 位置
`line_bot/ui.py`（純呈現層，**不**生成 token、不推斷病患/草稿、不以中文 substring 猜狀態、不 log URL）

```python
from line_bot.ui import (
    PREVISIT_ROOM_TRIGGER_TEXT,
    is_previsit_room_trigger,
    build_previsit_room_flex_message,
    build_previsit_room_entry_messages,
    build_previsit_room_trigger_quick_reply,
)

# 1) 精準判斷觸發
if is_previsit_room_trigger(event_text):
    # -> 後端產生安全連結後再回入口
    ...

# 2) 入口 Flex（無 URL 時不造假連結，提供可測試的不點擊狀態）
msgs = build_previsit_room_entry_messages(room_url=None)
# -> reply via MessagingApi: messages=msgs

# 3) 有安全連結時（backend 已產生）
secure_url = external_room_service.create_room_url(line_user_id)  # https://...
msgs = build_previsit_room_entry_messages(room_url=secure_url)

# 4) QuickReply fallback
quick = build_previsit_room_trigger_quick_reply()  # [{"label": "開啟看診前對談室", "text": "開啟看診前對談室"}]
```

## Payload 規格（符合 LINE Flex 約束）
- `type: "flex"`, `altText` ≤400 字元（目前 30 字），`contents.type: "bubble"`
- `body`: 說明 LINE 衛教／專用對談室／可暫停／確認才分享／不在 LINE 八題
- `footer.button`：
  - 無/無效 `room_url` → `action: {type:"message", label:"開啟看診前對談室", text:"開啟看診前對談室"}`（可測試的不可點狀態，文字提示「尚未產生專用連結」）
  - 有效 `https://` → `action: {type:"uri", label:"開啟看診前對談室", uri: room_url}`
- `room_url` 驗證：`https://`、含 netloc、長度 ≤2000、無空白；`http://`、`javascript:`、`https://` 等視為無效→安全狀態
- 任何分支都不會將 `room_url`/token 寫入 log

## Backend 需呼叫方式（不改 app.py 範例）
```python
from line_bot.ui import build_previsit_room_entry_messages, is_previsit_room_trigger

# 在 webhook text handler：
if "我要準備看診" in text or text.strip() == "我要準備看診":
    # 回入口（引導去對談室，而非直接問診）
    return reply_text_with_flex(build_previsit_room_entry_messages(room_url=None))

if is_previsit_room_trigger(text):
    room_url = your_secure_service.issue_room_url(line_user_id)  # 後端產生、簽名、短效
    return reply_text_with_flex(build_previsit_room_entry_messages(room_url=room_url))
```

## 測試
`line_bot/tests/test_previsit_room_entry.py` 覆蓋：altText/button label 約束、無 URL 安全態、payload 動作正確、token 不進 log、無效 URL 降級、bubble 結構合法。

## Local demo
```bash
python3 -c "from line_bot.ui import build_previsit_room_flex_message; import json; print(json.dumps(build_previsit_room_flex_message(room_url=None), ensure_ascii=False, indent=2))"
python3 -c "from line_bot.ui import build_previsit_room_flex_message; print(build_previsit_room_flex_message(room_url='https://example.com/previsit?t=sec')['contents']['footer']['contents'][0]['action'])"
pytest line_bot/tests/test_previsit_room_entry.py -q
```
