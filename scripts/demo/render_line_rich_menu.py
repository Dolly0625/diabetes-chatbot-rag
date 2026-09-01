"""Render the patient-only LINE Rich Menu payload.

This command is intentionally a local artifact generator.  It never calls the
LINE API, creates a menu, uploads an image, or binds a menu to an account.  A
deployer can review the JSON and then create/bind the menu explicitly in LINE
Developers Console (or wire it into a separately approved deployment job).

Demo example::

    python3 -m scripts.demo.render_line_rich_menu \
      --patient-portal-url https://example.ngrok.app/demo/previsit \
      --output /tmp/tfda-patient-rich-menu.json

The Demo URL must be a stable, tokenless HTTPS entry point.  In Demo mode
``/demo/previsit`` creates the short-lived, user-bound token after the user
clicks; do not paste a per-user ``?token=...`` URL into a Rich Menu.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from line_bot.ui import build_rich_menu_payload


def _valid_menu_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except Exception:
        return False
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    # A Rich Menu is shared by users.  A per-user token here would leak one
    # person's room to every account that can tap the menu.
    query = parse_qs(parsed.query, keep_blank_values=True)
    return "token" not in {key.lower() for key in query}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the patient-only TFDA LINE Rich Menu JSON")
    parser.add_argument(
        "--patient-portal-url",
        default=os.getenv("PATIENT_RICH_MENU_URL", "").strip(),
        help="Stable HTTPS entry point, e.g. https://host/demo/previsit",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON output path")
    args = parser.parse_args(argv)

    url = str(args.patient_portal_url or "").strip()
    if not _valid_menu_url(url):
        parser.error("patient portal URL must be HTTPS, include a host, and must not contain a per-user token")

    payload = build_rich_menu_payload(patient_portal_url=url)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"已產生病患 Rich Menu 定義：{args.output}")
    else:
        print(rendered, end="")
    print("僅產生 JSON，未呼叫 LINE API；請人工審核後在 LINE Developers Console 建立並綁定。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

