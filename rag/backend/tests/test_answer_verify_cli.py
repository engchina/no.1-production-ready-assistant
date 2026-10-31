"""回答フローの検証 CLI(回答評価・回帰・CRAG goldset)。API は MockTransport でスタブする。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.rag.answer_verify_cli import judge, main, split_sections

ANSWER = (
    "市区町村長名は基本情報登録で変更します。\n"
    "操作手順（基本情報登録）\n"
    "1. 市区町村長名を入力し、実行を押します。\n"
    "根拠：manual.pdf\n"
    "資料から確認できなかった事項\n"
    "印影の差し替え手順\n"
)


def _envelope(data: Any, status: int = 200, errors: list[str] | None = None) -> httpx.Response:
    return httpx.Response(
        status, json={"data": data, "error_messages": errors or [], "warning_messages": []}
    )


class FakeApi:
    def __init__(self, *, path: str = "grounded", fail_ids: set[str] | None = None) -> None:
        self.path = path
        self.fail_ids = fail_ids or set()
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append((request.url.path, body, dict(request.headers)))
        if request.url.path == "/api/search":
            if body["query"] in self.fail_ids:
                return _envelope(None, 503, ["検索が一時的に利用できません。"])
            return _envelope(
                {
                    "trace_id": f"trace-{len(self.requests)}",
                    "answer": ANSWER,
                    "citations": [],
                    "diagnostics": {
                        "retrieval_strategy": "hybrid",
                        "retrieval_strategy_adapter": self.path,
                        "answer": {"confidence": "high", "needs_human_review": False},
                    },
                }
            )
        return _envelope(None, 404, ["not found"])


def _write(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_split_sections_skips_subquestion_headings() -> None:
    """小問の見出し(#2399。質問の写し)は前の節の本文に入れない。"""
    answer = (
        "要約。\n\n質問①：登録の方法は？\n\n資料により確認できた事項\n\n・登録画面で登録します。\n\n"
        "質問②：削除の方法は？\n\n資料から確認できなかった事項\n\n・『②削除の方法は？』については、取得した資料で確認できませんでした。"
    )

    sections = split_sections(answer)

    assert sections["資料により確認できた事項"].strip() == "・登録画面で登録します。"
    assert "質問②" not in "".join(sections.values())


def test_split_sections_and_judge_follow_rag_poc_rules() -> None:
    sections = split_sections(ANSWER)

    assert sections["summary"] == "市区町村長名は基本情報登録で変更します。"
    assert sections["操作手順"].startswith("基本情報登録")
    assert "manual.pdf" not in "".join(sections.values())
    result = judge(
        ANSWER,
        {
            "applied_all": [["基本情報登録"], ["市区町村長名", "実行"]],
            "applied_excludes": ["印影"],
            "gap_contains": ["印影"],
        },
    )
    assert result["passed"] is True
    # gap の節は「適用」に数えない。
    assert judge(ANSWER, {"applied_all": [["印影"]]})["passed"] is False


def test_individual_answer_evaluation_command_is_removed() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["answers"])
    assert exc.value.code == 2


def test_regression_runs_repeats_resumes_and_summarizes(tmp_path: Path) -> None:
    cases = _write(
        tmp_path / "cases.json",
        {
            "cases": [
                {
                    "id": "q01",
                    "question": "市長名を変更したい",
                    "run_id": "rag_poc の run(読み捨てる)",
                    "expect": {"applied_all": [["基本情報登録"]], "gap_contains": ["印影"]},
                },
                {
                    "id": "q02",
                    "question": "別の質問",
                    "expect": {"applied_all": [["存在しない語"]]},
                },
            ]
        },
    )
    out = tmp_path / "out"
    api = FakeApi()
    args = [
        "regression",
        "--cases",
        str(cases),
        "--search-answer-profile",
        "bv-1",
        "--out",
        str(out),
        "--repeat",
        "2",
    ]

    main([*args, "--only", "q01"], transport=httpx.MockTransport(api))
    assert sorted(path.name for path in out.glob("q*.json")) == ["q01-1.json", "q01-2.json"]
    main(args, transport=httpx.MockTransport(api))

    assert len(api.requests) == 4  # q01 × 2 回 + 再開後の q02 × 2 回
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "| q01 | pass (high) | pass (high) | 2/2 |" in summary
    assert "| q02 | FAIL (high) | FAIL (high) | 0/2 | applied_all:存在しない語 |" in summary

    main([*args, "--summary-only"], transport=httpx.MockTransport(api))
    assert len(api.requests) == 4


def test_crag_goldset_returns_rag_poc_exit_code(tmp_path: Path) -> None:
    def case(case_id: str, crag_answer: str) -> dict[str, Any]:
        return {
            "case_id": case_id,
            "question": "質問",
            "expected_evidence_ids": ["e1"],
            "expected_answer_terms": ["赤枠", "実行"],
            "variants": {
                "standard_rag": {"retrieved_ids": ["e1"], "answer": "わかりません。"},
                "crag": {"retrieved_ids": ["e1"], "answer": crag_answer},
                "crag_rerank": {"retrieved_ids": ["e1"], "answer": crag_answer},
            },
        }

    better = _write(tmp_path / "better.json", {"cases": [case("c1", "赤枠の実行を押します。")]})
    worse = _write(tmp_path / "worse.json", {"cases": [case("c1", "青枠を押します。")]})

    assert main(["crag-goldset", str(better)]) == 0
    assert main(["crag-goldset", str(worse)]) == 1
