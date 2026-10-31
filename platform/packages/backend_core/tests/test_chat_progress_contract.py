"""チャットの処理の段階のイベントの契約と実装の一致（#1359）。

契約（`platform/contracts/chat-progress/chat-progress-events.json`）は 3 製品の backend と共有 UI
（`@production-ready/ui` の `chat-progress-events.ts`）が従うイベントの形の正本。形を変えたら
`UPDATE_CHAT_PROGRESS_CONTRACT=1 uv run pytest tests/test_chat_progress_contract.py` で書き直し、
共有 UI のテスト（`tests/chat-progress-events.test.ts`）も通す。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pr_backend_core.chat_progress import chat_progress_contract

CONTRACT = Path(__file__).resolve().parents[3] / "contracts/chat-progress/chat-progress-events.json"


def test_chat_progress_events_match_platform_contract() -> None:
    actual = chat_progress_contract()
    if os.environ.get("UPDATE_CHAT_PROGRESS_CONTRACT") == "1":
        CONTRACT.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(actual, ensure_ascii=False, indent=2) + "\n"
        CONTRACT.write_text(text, encoding="utf-8")
    assert json.loads(CONTRACT.read_text(encoding="utf-8")) == actual, (
        "チャットの処理の段階のイベントの形が契約と違います。"
        "意図した変更なら UPDATE_CHAT_PROGRESS_CONTRACT=1 で契約を更新してください。"
    )
