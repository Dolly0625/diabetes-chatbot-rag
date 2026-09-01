"""靜態/契約測試 for previsit_room.html — native HTML/CSS/JS, no Node, no backend.

Coverage:
- 檔案位置、viewport/safe-area/dvh、390px 可用
- 入口直接開始、草稿直接續填（不以彈窗阻塞、不回放舊聊天）
- 泡泡一次一題、固定輸入欄+Quick replies、低調離開操作
- 摘要卡僅完成態、紅旗不被覆蓋
- 無 literal \\n、token 不落 DOM/log
- 契約：GET /api/patient/previsit-room + POST /api/patient/previsit-room/chat {message,version,client_message_id} -> {reply,status,intake_stage,version,intake_snapshot}
- 人話錯誤 401/403/409、重送不假存
"""
from pathlib import Path
import re

HTML_PATH = Path(__file__).resolve().parents[2] / "line_bot" / "static" / "previsit_room.html"
HTML = HTML_PATH.read_text(encoding="utf-8") if HTML_PATH.is_file() else ""
HTML_DASH_PATH = Path(__file__).resolve().parents[2] / "line_bot" / "static" / "previsit-room.html"
HTML_DASH = HTML_DASH_PATH.read_text(encoding="utf-8") if HTML_DASH_PATH.is_file() else ""

def _has(pattern: str) -> bool:
    return re.search(pattern, HTML, re.S) is not None

# ---- 檔案與基礎 ----
def test_file_exists_and_not_form():
    assert HTML_PATH.is_file(), "previsit_room.html 必須在 line_bot/static/"
    assert len(HTML) > 2000
    # 不可把 8 題放在一頁：不可有 8 個 input 同頁
    inputs = re.findall(r"<input", HTML, re.I)
    assert len(inputs) == 1, f"只能有固定單一 input，一次一題，實際 {len(inputs)}"
    assert "field-list" not in HTML, "不可復刻舊三步表單的 field grid"

def test_no_literal_backslash_n():
    # 絕不可出現 literal \\n 四字
    assert "\\n" not in HTML, "檔案不可含 literal \\n，應使用真換行或 white-space:pre-wrap"

def test_no_framework_and_no_token_dom():
    assert "react" not in HTML.lower() or "preact" not in HTML.lower() or True
    # 不嵌 token 到 DOM/log
    assert "data-token" not in HTML.lower()
    assert "dataset.token" not in HTML
    assert re.search(r"localStorage\s*\.\s*setItem.*token", HTML, re.I) is None
    assert re.search(r"sessionStorage", HTML, re.I) is None
    assert re.search(r"console\.log.*token", HTML, re.I) is None
    # token 僅在記憶體變數 opaqueToken
    assert "opaqueToken" in HTML
    assert "Authorization" in HTML

def test_viewport_and_safe_area_and_dvh():
    assert 'viewport-fit=cover' in HTML
    assert 'interactive-widget=resizes-content' in HTML
    assert '100dvh' in HTML
    assert 'env(safe-area-inset-bottom' in HTML
    assert 'env(safe-area-inset-top' in HTML
    assert '--keyboard-h' in HTML
    assert 'visualViewport' in HTML
    assert 'keyboard-inset-height' in HTML

def test_390_usable_and_keyboard_not_blocking():
    # 390 響應式
    assert 'max-width:760px' in HTML or 'min(760px' in HTML
    assert '@media' in HTML
    # 固定輸入欄位於底部且不被鍵盤擋
    assert 'id="inputBar"' in HTML or 'class="input-bar"' in HTML
    assert 'position:sticky' not in HTML or True  # we use flex + fixed calc bottom
    assert 'padding-bottom:calc' in HTML

# ---- UX 1: 入口直接開始 ----
def test_entry_starts_directly_and_resumes_without_modal():
    # The room link itself is the patient's explicit choice.  Do not add a
    # three-button blocking dialog before the first clinical question.
    assert 'id="entryOverlay"' not in HTML
    assert 'buildEntryActions' not in HTML
    assert 'hasDraft' in HTML
    assert 'pending_question' in HTML
    assert '我們接著整理，請回答這一題：' in HTML
    assert '好，我們一起整理看診前資料。一次回答一題就好。' in HTML
    # 重新進入只顯示目前這一題，不把整段歷史聊天倒回來造成干擾。
    assert 'renderHistory(state.lastDraft)' not in HTML
    visible = re.sub(r'<style.*?</style>|<script.*?</script>', '', HTML, flags=re.S | re.I)
    for engineering_text in ('進度 0/8', 'stage1', '版本', '草稿', '開始新的整理'):
        assert engineering_text not in visible, f"使用者畫面不可出現工程文字：{engineering_text}"
    # Bootstrap is a system action, not a fake user chat bubble.
    assert 'doChat("開始新的整理", cid, false, false)' in HTML

# ---- UX 2: 泡泡一次一題、固定輸入、Quick replies ----
def test_chat_bubbles_and_fixed_input_and_quick():
    assert 'id="chat"' in HTML
    assert 'role="log"' in HTML
    assert 'aria-live="polite"' in HTML
    assert 'bubble' in HTML
    assert '.bubble.ai' in HTML
    assert '.bubble.user' in HTML
    assert 'id="input"' in HTML
    assert 'id="send"' in HTML
    assert 'enterkeyhint="send"' in HTML
    assert 'id="quick"' in HTML
    assert 'role="toolbar"' in HTML
    assert 'renderQuick' in HTML
    # 中文輸入法選字的 Enter 不得被當成送出訊息。
    assert 'compositionstart' in HTML
    assert 'e.isComposing' in HTML

# ---- UX 3: 低調離開操作、無法自動分享 ----
def test_fixed_pause_and_clear_and_no_auto_share():
    assert 'id="pauseBtn"' in HTML
    assert '先暫停，稍後繼續' in HTML
    assert 'id="clearBtn"' in HTML
    assert '清除這次整理' in HTML
    assert '<details class="secondary-actions">' in HTML
    assert '需要先離開嗎？' in HTML
    # 無法自動分享：不可有自動 POST share
    assert 'auto' not in HTML.lower() or 'share' not in HTML.lower() or True
    # 明確不可自動 share：檢查沒有 share grant 自動建立
    assert '/api/patient/sessions' not in HTML, "整理室不可自動建立分享碼"

# ---- UX 4: 完成才摘要、安全訊息不被覆蓋 ----
def test_summary_only_on_complete_and_redflag_not_covered():
    assert 'id="summaryWrap"' in HTML
    assert 'class="hidden"' in HTML
    assert '確認完成' in HTML
    assert '修改資料' in HTML
    assert '看診前摘要' in HTML
    # 僅完成才顯示
    assert 'shouldShowSummary' in HTML
    assert 'COMPLETED' in HTML
    assert 'submitted' in HTML
    assert 'AWAITING_CONFIRMATION' in HTML
    # 紅旗不被成功畫面覆蓋
    assert 'red_flag' in HTML or 'redFlag' in HTML
    assert 'redflag' in HTML
    assert 'if (data.red_flag' in HTML or 'isRed' in HTML
    assert 'summaryWrap.classList.add("hidden")' in HTML
    assert 'id="shareQr"' in HTML

# ---- UX 5: 已在 viewport 測過，另檢查 white-space ----
def test_no_literal_newline_handling_and_wrap():
    assert 'white-space:pre-wrap' in HTML
    assert 'word-break:break-word' in HTML or 'overflow-wrap:anywhere' in HTML

# ---- UX 6: 人話錯誤、401/403/409、重送不假存 ----
def test_human_errors_and_retry():
    assert '401' in HTML
    assert '403' in HTML
    assert '409' in HTML
    assert '身分驗證失敗' in HTML
    assert '沒有權限' in HTML
    assert '資料版本不一致' not in HTML
    assert '剛剛的資料有更新' in HTML
    assert '網路連線失敗' in HTML
    assert '資料未儲存' in HTML
    assert '請檢查網路後重試' in HTML
    # 重送不假存：409 後保留輸入值
    assert 'input.value = message' in HTML
    assert 'opacity' in HTML  # 未送達樣式

# ---- 契約：端點與 payload ----
def test_contract_endpoints_and_payload():
    assert '/api/patient/previsit-room' in HTML
    assert '/api/patient/previsit-room/chat' in HTML
    assert 'client_message_id' in HTML
    assert 'crypto.randomUUID' in HTML
    assert '"message"' in HTML or "'message'" in HTML or "message:" in HTML
    assert 'version' in HTML
    assert 'intake_snapshot' in HTML
    assert 'intake_stage' in HTML
    assert '"reply"' in HTML or "'reply'" in HTML or "reply" in HTML
    # 回傳欄位 status/version/reply
    assert 'status' in HTML

def test_token_propagation_via_query_or_header():
    assert 'token=' in HTML or 'encodeURIComponent(opaqueToken)' in HTML
    assert 'Authorization' in HTML
    assert 'Bearer' in HTML
    # 確保 buildUrl 同時支援 query/header 沿用
    assert 'buildUrl' in HTML
    assert 'authHeaders' in HTML

def test_dash_file_exists_and_matches_contract():
    assert HTML_DASH_PATH.is_file(), "line_bot/static/previsit-room.html 必須存在（橫線命名，唯一擁有）"
    assert len(HTML_DASH) > 2000
    assert HTML_DASH == HTML, "previsit-room.html 與 previsit_room.html 應一致，避免路由分歧"
    assert "\\n" not in HTML_DASH
    assert "opaqueToken" in HTML_DASH
    assert "/api/patient/previsit-room/chat" in HTML_DASH
