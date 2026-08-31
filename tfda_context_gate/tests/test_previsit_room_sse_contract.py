"""靜態契約測試 for previsit-room SSE — 驗證 stream_mode=final_only 正確前端協定。

契約：
- POST /api/patient/previsit-room/chat/stream 以 fetch ReadableStream 讀 SSE
- 事件 phase / delta / complete / error
- UX：立即泡泡「正在理解…」、delta 追加同泡泡、autoscroll 不搶閱讀、complete 才 commit、error 標「這次沒有成功儲存」可重試、AbortController 於離開/取消、不重複送出
- 不可假 streaming（無 setInterval 拆字）、fallback 存在且不覆蓋已 complete 內容
"""
from pathlib import Path
import re

HTML_PATH = Path(__file__).resolve().parents[2] / "line_bot" / "static" / "previsit-room.html"
HTML = HTML_PATH.read_text(encoding="utf-8") if HTML_PATH.is_file() else ""
HTML_UNDERSCORE_PATH = Path(__file__).resolve().parents[2] / "line_bot" / "static" / "previsit_room.html"
HTML_UNDERSCORE = HTML_UNDERSCORE_PATH.read_text(encoding="utf-8") if HTML_UNDERSCORE_PATH.is_file() else ""

# 供每個 test 快速判別 SSE 相關是否在 dash 檔
def _has(pat: str) -> bool:
    return re.search(pat, HTML, re.S) is not None

def test_sse_endpoint_and_fetch_stream_reader():
    assert "/api/patient/previsit-room/chat/stream" in HTML, "必須呼叫 SSE 端點 POST /api/patient/previsit-room/chat/stream"
    # fetch ReadableStream 協定
    assert "fetch(" in HTML and "ReadableStream" in HTML or "getReader" in HTML, "必須以 fetch + ReadableStream/getReader 讀流"
    assert "getReader" in HTML, "必須呼叫 response.body.getReader()"
    assert "TextDecoder" in HTML, "必須以 TextDecoder 解碼二進位流"
    assert 'Accept' in HTML and 'text/event-stream' in HTML, "必須帶 Accept: text/event-stream"
    # 同時仍保留非 SSE POST 作 fallback
    assert "/api/patient/previsit-room/chat" in HTML, "必須保留非 SSE POST 作為 fallback"

def test_sse_parser_phase_delta_complete_error():
    # SSE 事件解析：需處理 event: + data: 與四種事件
    assert 'event:' in HTML or 'currentEvent' in HTML, "必須解析 SSE event 欄位"
    assert 'data:' in HTML, "必須解析 SSE data 欄位"
    # 四種事件字面必須出現
    assert '"phase"' in HTML or "'phase'" in HTML or 'phase' in HTML, "必須處理 phase 事件"
    assert 'delta' in HTML, "必須處理 delta 事件"
    assert 'complete' in HTML, "必須處理 complete 事件"
    assert 'error' in HTML, "必須處理 error 事件"
    # 至少有對 complete 的獨立分支
    assert re.search(r'currentEvent\s*===\s*["\']complete["\']', HTML) or re.search(r'event.*complete', HTML, re.I), "complete 必須有獨立分支"
    assert re.search(r'currentEvent\s*===\s*["\']delta["\']', HTML), "delta 必須有獨立分支"
    assert re.search(r'currentEvent\s*===\s*["\']error["\']', HTML), "error 必須有獨立分支"

def test_abort_controller_and_lifecycle():
    assert "AbortController" in HTML, "必須使用 AbortController"
    assert "signal" in HTML, "必須將 signal 傳給 fetch"
    assert ".abort()" in HTML, "必須呼叫 abort()"
    # 頁面離開/取消時 abort
    assert "beforeunload" in HTML, "必須監聽 beforeunload 以 abort"
    assert "pagehide" in HTML, "必須監聽 pagehide 以 abort"
    assert "visibilitychange" in HTML or "document.hidden" in HTML, "應處理 visibilitychange/hidden 以 abort"
    # pause/clear 需Abort 且不重複送出
    assert re.search(r'pauseBtn.*abort|abort.*pause', HTML, re.S | re.I) or ("pauseBtn" in HTML and "abortController" in HTML), "暫停時需 abort 進行中請求"
    assert "abortController" in HTML, "需在 state 儲存 abortController"

def test_complete_only_state_commit():
    # complete 才更新 version/intake/progress/summary
    idx_delta = HTML.find('currentEvent === "delta"')
    idx_complete = HTML.find('currentEvent === "complete"')
    idx_error = HTML.find('currentEvent === "error"')
    assert idx_complete != -1, "須有 complete 分支"
    if idx_delta != -1 and idx_complete != -1:
        assert "state.version" not in HTML[idx_delta:idx_complete], "delta 階段不可更新 version，必須等 complete"
    seg = HTML[idx_complete: idx_error if idx_error != -1 else idx_complete + 3000]
    assert "state.version" in seg, "complete 才可更新 version"
    assert "updateProgress" in seg, "complete 才可更新 progress"
    assert "renderSummary" in seg or "renderQuick" in seg, "complete 才可更新 summary/quick"
    assert "sseCompleted" in HTML, "須以 sseCompleted 旗標避免 fallback 覆蓋已完成內容"

def test_no_fake_typewriter():
    # 不可以 setInterval 假 streaming 拆字
    assert "setInterval" not in HTML, "不得以 setInterval 假造 token streaming"
    # 不可以 setTimeout 逐字追加（允許 requestAnimationFrame，但不允許逐字 timer）
    # 粗略：若有 setTimeout 必須不是用於 typewriter；我們直接禁止存在字串 slice + setTimeout 的組合
    has_fake = re.search(r'setTimeout.*slice|slice.*setTimeout|for.*setTimeout.*char', HTML, re.S | re.I)
    assert has_fake is None, "不得以 setTimeout + slice 假造逐字"
    # 確保是以 SSE delta 追加，而非本地拆 complete 字串
    assert "正在理解" in HTML, "等待期應顯示「正在理解…」而非假拆字"

def test_fallback_preserves_complete_content():
    # fallback 存在
    assert "doChatFallback" in HTML or "fallback" in HTML.lower(), "必須保留 fallback 邏輯"
    # fallback 前需檢查 sseCompleted 已完成則不覆蓋
    assert re.search(r'sseCompleted.*return|if\s*\(.*sseCompleted', HTML, re.S), "fallback 不得覆蓋已 complete 內容（需有 sseCompleted 守衛）"
    # 404/405/501 時 fallback
    assert "404" in HTML and "405" in HTML, "404/405/501 時應 fallback 到非 SSE POST"
    # fallback 呼叫非 stream 端點
    assert re.search(r'fetchJson.*\/api\/patient\/previsit-room\/chat', HTML), "fallback 應呼叫原 POST /api/patient/previsit-room/chat"

def test_ux_immediate_bubble_and_delta_append():
    assert "正在理解" in HTML, "送出後須立即顯示「正在理解…」泡泡"
    # 每個 delta 即時追加到同一個泡泡
    assert "pendingAiBubbleRef" in HTML or "pendingAi" in HTML, "需以單一泡泡引用追加 delta"
    assert re.search(r'pendingAiBubbleRef\.textContent\s*\+=', HTML) or re.search(r'textContent\s*\+=\s*delta', HTML, re.I), "每個 delta 應追加到同一個泡泡"
    # autoscroll 但不得搶走向上閱讀
    assert "isNearBottom" in HTML, "需實作 isNearBottom 判斷"
    assert "smartScroll" in HTML, "需實作 smartScroll 且僅底部附近才滾動"
    assert re.search(r'scrollHeight\s*-\s*80|scrollTop.*clientHeight', HTML), "autoscroll 需判斷距底部閾值"

def test_error_shows_retry_and_not_fake_saved():
    assert "這次沒有成功儲存" in HTML, "error 需標「這次沒有成功儲存」"
    assert "input.value = message" in HTML, "error 時需保留輸入值讓使用者重試"
    assert 'opacity' in HTML and '0.7' in HTML, "未送達需有透明度提示"
    # error 分支應顯示該字句
    assert re.search(r'currentEvent\s*===\s*["\']error["\'].*這次沒有成功儲存', HTML, re.S), "error 事件分支須顯示「這次沒有成功儲存」"

def test_no_duplicate_send_on_pause_or_clear():
    # sending 旗標防重複
    assert "state.sending" in HTML, "需以 state.sending 防重複送出"
    assert re.search(r'if\s*\(\s*state\.sending', HTML), "sendMessage/doChat 入口需檢查 sending"
    # pause/clear 不自動再發送（僅 abort + UI 提示，不呼叫 doChat 發新請求）
    # 至少 pauseHandler 內不應直接呼叫 doChat
    # 粗略檢查 pauseBtn 監聽內含 abort 但不含 doChat("...") 的第二次送出（clear 的 confirm 後僅本地清除）
    pause_block = re.search(r'pauseBtn\.addEventListener.*?};', HTML, re.S)
    if pause_block:
        assert "abort" in pause_block.group(0).lower(), "pause 應先 abort"
    # clear 需 confirm
    assert "window.confirm" in HTML or "confirm(" in HTML, "結束並清除需 confirm"
    assert "結束並清除" in HTML

def test_token_not_in_dom_or_log():
    assert "localStorage" not in HTML, "token 不得落 localStorage"
    assert "sessionStorage" not in HTML, "token 不得落 sessionStorage"
    assert "data-token" not in HTML.lower(), "token 不得寫入 data-token"
    assert re.search(r'console\.log.*token', HTML, re.I) is None, "不得 console.log token"

def test_no_literal_backslash_n_and_dash_underscore_sync():
    assert "\\n" not in HTML, "不得含 literal \\n"
    assert HTML_UNDERSCORE_PATH.is_file(), "previsit_room.html 需同步存在"
    assert "\\n" not in HTML_UNDERSCORE
    # 兩檔應一致（橫線為權威副本）
    assert HTML == HTML_UNDERSCORE, "previsit-room.html 與 previsit_room.html 需保持一致"
