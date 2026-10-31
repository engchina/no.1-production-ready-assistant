"""異機能合成の防止と最終監査からの一度限りの回復を検証する。"""

from test_crag_context_recovery import record

from rag_engine.generation.answering import AnswerContext, AnswerResponse
from rag_engine.retrieval.evidence_scope import evidence_scope_error, unsupported_heading_navigation
from rag_engine.retrieval.task_contract import task_contract


def span(name, text="", **extra):
    return dict(section_path=["管理-(1)" + name], text=text, **extra)


def test_adjacent_functions_are_not_one_operation_but_explicit_transition_is_allowed():
    a, b = span("登録"), span("照会")
    assert evidence_scope_error([a, b])
    assert not evidence_scope_error([a, span("登録", "次ページの確定操作")])
    assert not evidence_scope_error([a, span("照会", "登録から照会へ移動します。")])
    assert evidence_scope_error([a, span("照会", "登録から照会へ移動できません。")])


def test_document_version_and_business_are_separate():
    assert evidence_scope_error([span("登録", document_scope=["doc", "1"]), span("登録", document_scope=["doc", "2"])])
    assert evidence_scope_error([span("登録", business_scope="給与"), span("登録", business_scope="倉庫")])
    assert not evidence_scope_error([span("登録"), span("登録")])


def test_heading_is_not_navigation_without_explicit_operation():
    assert unsupported_heading_navigation("D【一覧帳票】を開きます。", [span("登録", "D【一覧帳票】\n帳票名と説明")])
    assert not unsupported_heading_navigation("D【一覧帳票】を開きます。", [span("登録", "D【一覧帳票】を選択して開きます。")])
    assert unsupported_heading_navigation("D【一覧帳票】画面から出力できます。", [span("登録", "D【一覧帳票】\n帳票名と説明")])
    assert not unsupported_heading_navigation(
        "D【一覧帳票】画面から出力できます。", [span("登録", "D【一覧帳票】画面からPDFを出力できます。")]
    )


def test_members_and_count_are_independent():
    assert task_contract("集計に計上されている明細を確認したい")["requested_granularity"] == ["members"]
    assert task_contract("何人かと算定根拠を確認したい")["requested_granularity"] == ["count", "calculation_basis"]


def response(status="partial", supported=()):
    return AnswerResponse(
        answer_text="支持された部分回答",
        generation_trace={
            "reviews": [
                {
                    "status": "completed",
                    "complete": True,
                    "reviews": [],
                    "request_reviews": [dict(request_id="Q1", status=status), *[dict(request_id=i, status="addressed") for i in supported]],
                }
            ]
        },
    )


def context():
    a = record("p1", 1, "画面を開く。", "parent", "")
    b = record("p2", 2, "条件を指定して実行する。", "parent", "")
    return AnswerContext(records=[a], text=a.text, expansion_records=(a, b))
