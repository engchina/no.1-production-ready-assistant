"""検索・回答プロファイル単位の Approved FAQ(類似問)API。"""

import io

import pytest
from openpyxl import Workbook  # type: ignore[import-untyped]
from pr_system_settings.embedding import EMBEDDING_DIMENSIONS

from app.api.routes import search_answer_profile_knowledge as knowledge_route
from app.main import app
from tests.support import AsgiTestClient
from tests.test_search_answer_profile_domain_keywords import FakeKnowledgeOracle

client = AsgiTestClient(app)
BASE = "/api/search-answer-profiles/bv-1/approved-faq"


@pytest.fixture
def fake_oracle(monkeypatch: pytest.MonkeyPatch) -> FakeKnowledgeOracle:
    fake = FakeKnowledgeOracle()
    monkeypatch.setattr(knowledge_route, "OracleClient", lambda: fake)
    monkeypatch.setattr(knowledge_route, "GenAiClient", StubGenAi)
    return fake


class StubGenAi:
    """意味照合の embedding を決定論化する(外部 OCI を呼ばない)。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def embed(self, texts: list[str], *, input_type: str = "") -> list[list[float]]:
        return [
            [1.0 if "取り消" in text else 0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2)
            for text in texts
        ]


def _excel(rows: list[tuple[str, str]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["QUESTION", "ANSWER"])
    for row in rows:
        sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_add_list_suggest_and_delete_approved_faq(fake_oracle: FakeKnowledgeOracle) -> None:
    added = client.post(
        BASE, json={"question": "受注を取り消すには？", "answer": "受注一覧で取消を押します。"}
    )
    assert added.status_code == 200
    records = added.json()["data"]["records"]
    assert [record["question"] for record in records] == ["受注を取り消すには？"]
    faq_id = records[0]["id"]

    listed = client.get(BASE).json()["data"]["records"]
    assert listed[0]["answer"] == "受注一覧で取消を押します。"

    suggested = client.post(f"{BASE}/suggest", json={"query": "受注を取り消すには？"})
    suggestion = suggested.json()["data"]["suggestions"][0]
    assert suggestion["id"] == faq_id
    assert suggestion["direct"] is True
    unrelated = client.post(f"{BASE}/suggest", json={"query": "在庫の棚卸し手順"})
    assert all(not item["direct"] for item in unrelated.json()["data"]["suggestions"])

    deleted = client.post(f"{BASE}/delete", json={"ids": [faq_id]})
    assert deleted.json()["data"]["deleted_count"] == 1
    assert client.get(BASE).json()["data"]["records"] == []


def test_excel_preview_and_import_modes(fake_oracle: FakeKnowledgeOracle) -> None:
    content = _excel([("受注を登録するには？", "受注入力画面で登録します。"), ("", "")])
    files = {"file": ("faq.xlsx", content, "application/octet-stream")}

    preview = client.post(f"{BASE}/import/preview", files=files)
    assert preview.status_code == 200
    assert preview.json()["data"]["total"] == 1
    assert preview.json()["data"]["rows"][0]["question"] == "受注を登録するには？"

    imported = client.post(f"{BASE}/import", files=files, data={"mode": "INSERT"})
    assert imported.json()["data"]["inserted_count"] == 1
    again = client.post(f"{BASE}/import", files=files, data={"mode": "INSERT"})
    assert again.json()["data"]["inserted_count"] == 0
    assert len(again.json()["data"]["records"]) == 1

    bad = client.post(f"{BASE}/import/preview", files={"file": ("faq.csv", b"a,b", "text/csv")})
    assert bad.status_code == 422


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"question": "  ", "answer": "a"}, "質問を入力してください。"),
        ({"question": "q", "answer": ""}, "回答を入力してください。"),
    ],
    ids=["question", "answer"],
)
def test_add_rejects_blank_fields_like_the_screen(
    fake_oracle: FakeKnowledgeOracle, payload: dict[str, str], message: str
) -> None:
    """FAQ の追加の未入力は、画面の欄の下と同じ文言で欄を指す 422（#541）。"""
    response = client.post(BASE, json=payload)
    assert response.status_code == 422
    assert response.json()["error_messages"] == [message]


def test_suggestion_toggle_is_kept_across_faq_edits_and_stops_suggestions(
    fake_oracle: FakeKnowledgeOracle,
) -> None:
    """類似問の提示は検索・回答プロファイルごとにオン / オフでき、未設定はオン(#684)。"""
    assert client.get(BASE).json()["data"]["enabled"] is True
    client.post(BASE, json={"question": "受注を取り消すには？", "answer": "取消を押します。"})

    off = client.put(f"{BASE}/settings", json={"enabled": False})
    assert off.status_code == 200
    assert off.json()["data"]["enabled"] is False
    # FAQ を追加・削除しても設定は消えない。
    client.post(BASE, json={"question": "受注を登録するには？", "answer": "登録を押します。"})
    assert client.get(BASE).json()["data"]["enabled"] is False
    suggested = client.post(f"{BASE}/suggest", json={"query": "受注を取り消すには？"})
    assert suggested.json()["data"]["suggestions"] == []

    client.put(f"{BASE}/settings", json={"enabled": True})
    suggested = client.post(f"{BASE}/suggest", json={"query": "受注を取り消すには？"})
    assert suggested.json()["data"]["suggestions"]


def test_chat_suggestions_use_the_chat_min_score_and_at_most_three(
    fake_oracle: FakeKnowledgeOracle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """チャットは共通の一致度の下限以上を最大 3 件だけ出す。"""
    for index in range(5):
        client.post(
            BASE, json={"question": f"受注 {index} を取り消すには？", "answer": f"回答 {index}"}
        )
    search = client.post(f"{BASE}/suggest", json={"query": "受注を取り消すには？", "limit": 5})
    assert len(search.json()["data"]["suggestions"]) == 5
    chat = client.post(
        f"{BASE}/suggest", json={"query": "受注を取り消すには？", "limit": 5, "purpose": "chat"}
    )
    assert 0 < len(chat.json()["data"]["suggestions"]) <= 3

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "rag_approved_faq_suggestion_min_score", 1.0)
    # 下限を上げると、近い候補でも提示しない。
    weak = client.post(f"{BASE}/suggest", json={"query": "受注を取り消すには？", "purpose": "chat"})
    assert weak.json()["data"]["suggestions"] == []


def test_chat_suggestions_drop_candidates_far_from_the_top(
    fake_oracle: FakeKnowledgeOracle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """チャットは 1 位から RAG_APPROVED_FAQ_SUGGESTION_MAX_GAP より離れた候補を出さない(#709)。"""
    from app.config import get_settings

    client.post(BASE, json={"question": "受注を取り消すには？", "answer": "取消ボタンを押す。"})
    client.post(BASE, json={"question": "受注 1 を取り消すには？", "answer": "回答 1"})
    monkeypatch.setattr(get_settings(), "rag_approved_faq_suggestion_min_score", 0.5)

    monkeypatch.setattr(get_settings(), "rag_approved_faq_suggestion_max_gap", 1.0)
    both = client.post(f"{BASE}/suggest", json={"query": "受注を取り消すには？", "purpose": "chat"})
    assert len(both.json()["data"]["suggestions"]) == 2

    monkeypatch.setattr(get_settings(), "rag_approved_faq_suggestion_max_gap", 0.0)
    top = client.post(f"{BASE}/suggest", json={"query": "受注を取り消すには？", "purpose": "chat"})
    assert [item["question"] for item in top.json()["data"]["suggestions"]] == [
        "受注を取り消すには？"
    ]
    # 検索の画面も同じ提示条件で絞る。
    search = client.post(f"{BASE}/suggest", json={"query": "受注を取り消すには？"})
    assert len(search.json()["data"]["suggestions"]) == 1


@pytest.mark.parametrize("purpose", ["chat", "search"])
def test_suggestion_minimum_applies_to_both_answer_entry_points(
    fake_oracle: FakeKnowledgeOracle, monkeypatch: pytest.MonkeyPatch, purpose: str
) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "rag_approved_faq_semantic_enabled", False)
    client.post(BASE, json={"question": "合成の設定 A はどこですか？", "answer": "画面 A です。"})
    monkeypatch.setattr(get_settings(), "rag_approved_faq_suggestion_min_score", 1.0)
    weak = client.post(f"{BASE}/suggest", json={"query": "別の設定 B", "purpose": purpose})
    assert weak.status_code == 200
    assert weak.json()["data"]["suggestions"] == []
    exact = client.post(
        f"{BASE}/suggest", json={"query": "合成の設定 A はどこですか？", "purpose": purpose}
    )
    assert len(exact.json()["data"]["suggestions"]) == 1
