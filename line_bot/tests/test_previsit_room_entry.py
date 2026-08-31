import json
import logging

from line_bot.ui import (
    PREVISIT_ROOM_ALT_TEXT,
    PREVISIT_ROOM_BUTTON_LABEL,
    PREVISIT_ROOM_NO_URL_HINT,
    PREVISIT_ROOM_TRIGGER_TEXT,
    build_previsit_room_entry_messages,
    build_previsit_room_flex_contents,
    build_previsit_room_flex_message,
    build_previsit_room_trigger_quick_reply,
    is_previsit_room_trigger,
)


def test_trigger_precise_match():
    assert is_previsit_room_trigger("開啟看診前對談室") is True
    assert is_previsit_room_trigger(" 開啟看診前對談室 ") is True
    assert is_previsit_room_trigger("開啟看診前對談室\n") is True
    assert is_previsit_room_trigger("開啟看診前對談室，") is False
    assert is_previsit_room_trigger("我要準備看診") is False
    assert is_previsit_room_trigger("") is False
    assert is_previsit_room_trigger(" 開啟看診前對談室- ") is False


def test_flex_altText_and_button_label_constraints():
    msg = build_previsit_room_flex_message(room_url=None)
    assert msg["type"] == "flex"
    assert isinstance(msg["altText"], str)
    assert 1 <= len(msg["altText"]) <= 400
    assert msg["altText"] == PREVISIT_ROOM_ALT_TEXT
    bubble = msg["contents"]
    assert bubble["type"] == "bubble"
    footer_btn = bubble["footer"]["contents"][0]
    label = footer_btn["action"]["label"]
    assert 1 <= len(label) <= 20
    assert label == PREVISIT_ROOM_BUTTON_LABEL
    assert label == PREVISIT_ROOM_TRIGGER_TEXT


def test_body_explains_line_vs_room_and_pause_and_confirm():
    contents = build_previsit_room_flex_contents(room_url=None)
    body = contents["body"]
    text_nodes = []

    def collect(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and "text" in node:
                text_nodes.append(node["text"])
            for v in node.values():
                if isinstance(v, list):
                    for item in v:
                        collect(item)
                elif isinstance(v, dict):
                    collect(v)

    collect(body)
    joined = "\n".join(text_nodes)
    assert "LINE" in joined
    assert "衛教" in joined
    assert "專用對談室" in joined
    assert "暫停" in joined
    assert "確認才分享" in joined
    assert "不會在 LINE 內開始八題問診" in joined


def test_no_url_does_not_fabricate_uri_and_is_message_action():
    for bad in [None, "", "   ", "http://example.com/room", "javascript:alert(1)", "https://", "https:// "]:
        msg = build_previsit_room_flex_message(room_url=bad)
        action = msg["contents"]["footer"]["contents"][0]["action"]
        assert action["type"] == "message", f"bad={bad!r} should be message"
        assert action["text"] == PREVISIT_ROOM_TRIGGER_TEXT
        assert "uri" not in action
        # also no URI string hidden elsewhere
        dumped = json.dumps(msg, ensure_ascii=False)
        # should not contain example.com fake
        if isinstance(bad, str) and bad.startswith("http://"):
            assert bad not in dumped

    # valid https should be uri
    good = "https://example.com/room?token=secret123"
    msg2 = build_previsit_room_flex_message(room_url=good)
    action2 = msg2["contents"]["footer"]["contents"][0]["action"]
    assert action2["type"] == "uri"
    assert action2["uri"] == good
    assert action2["label"] == PREVISIT_ROOM_TRIGGER_TEXT


def test_with_url_hint_vs_no_url_hint():
    no_url = build_previsit_room_flex_contents(room_url=None)
    with_url = build_previsit_room_flex_contents(room_url="https://example.com/room/abc")
    # extract hint text
    def find_hint(contents):
        for item in contents["body"]["contents"]:
            if item.get("type") == "text" and "尚未產生" in item.get("text", "") or "連結已就緒" in item.get("text", ""):
                return item["text"]
        return ""

    assert PREVISIT_ROOM_NO_URL_HINT in json.dumps(no_url, ensure_ascii=False)
    assert "連結已就緒" in json.dumps(with_url, ensure_ascii=False)


def test_payload_action_correct_and_secure():
    # without url -> message action exact
    msgs = build_previsit_room_entry_messages(room_url=None)
    assert len(msgs) == 1
    assert msgs[0]["type"] == "flex"
    action = msgs[0]["contents"]["footer"]["contents"][0]["action"]
    assert action == {"type": "message", "label": PREVISIT_ROOM_TRIGGER_TEXT, "text": PREVISIT_ROOM_TRIGGER_TEXT}

    # with valid url -> uri action exact, no token generation
    url = "https://clinic.example.tw/previsit-room?t=abc123&sig=xyz"
    msgs2 = build_previsit_room_entry_messages(room_url=url)
    action2 = msgs2[0]["contents"]["footer"]["contents"][0]["action"]
    assert action2["type"] == "uri"
    assert action2["uri"] == url
    assert action2["label"] == PREVISIT_ROOM_TRIGGER_TEXT


def test_url_not_leaked_to_log(caplog):
    caplog.set_level(logging.INFO)
    token_url = "https://example.com/previsit-room?token=SUPER_SECRET_TOKEN_12345&user=U123"
    # ensure builder does not log token
    with caplog.at_level(logging.DEBUG):
        build_previsit_room_flex_message(room_url=token_url)
        build_previsit_room_entry_messages(room_url=token_url)
        build_previsit_room_flex_contents(room_url=token_url)
    # search all captured records
    all_text = "\n".join([r.getMessage() for r in caplog.records])
    # also check caplog.text
    all_text += caplog.text
    assert "SUPER_SECRET_TOKEN_12345" not in all_text
    # also ensure no logger in ui module leaked via root
    # double-check json dump does not auto-log
    assert token_url not in all_text or "SUPER_SECRET" not in all_text


def test_quick_reply_trigger_contract():
    qr = build_previsit_room_trigger_quick_reply()
    assert len(qr) == 1
    assert qr[0]["label"] == PREVISIT_ROOM_TRIGGER_TEXT
    assert qr[0]["text"] == PREVISIT_ROOM_TRIGGER_TEXT
    assert 1 <= len(qr[0]["label"]) <= 20


def test_no_fake_link_when_url_missing_is_testable_state():
    msg = build_previsit_room_flex_message(room_url=None)
    dumped = json.dumps(msg, ensure_ascii=False)
    # must not contain any http link at all
    assert "https://" not in dumped or PREVISIT_ROOM_NO_URL_HINT in dumped
    # ensure we can programmatically detect non-clickable state
    action = msg["contents"]["footer"]["contents"][0]["action"]
    assert action["type"] != "uri"
    # has explanatory text
    body_text = json.dumps(msg["contents"]["body"], ensure_ascii=False)
    assert "尚未產生" in body_text or PREVISIT_ROOM_NO_URL_HINT in body_text


def test_invalid_urls_are_treated_as_missing():
    invalid = ["https://", "https:// ", " http://example.com ", "ftp://example.com", "https://example com/room"]
    for url in invalid:
        msg = build_previsit_room_flex_message(room_url=url)
        action = msg["contents"]["footer"]["contents"][0]["action"]
        assert action["type"] == "message"
        assert "uri" not in action


def test_contents_bubble_structure_valid_for_line():
    msg = build_previsit_room_flex_message(room_url="https://example.com/room")
    # minimal LINE Flex validation (without calling API)
    assert msg["type"] == "flex"
    assert isinstance(msg["altText"], str) and len(msg["altText"]) > 0
    assert msg["contents"]["type"] == "bubble"
    assert "body" in msg["contents"]
    assert "footer" in msg["contents"]
    assert msg["contents"]["body"]["type"] == "box"
    assert msg["contents"]["footer"]["type"] == "box"
    for btn in msg["contents"]["footer"]["contents"]:
        assert btn["type"] == "button"
        assert btn["action"]["label"] == PREVISIT_ROOM_TRIGGER_TEXT
