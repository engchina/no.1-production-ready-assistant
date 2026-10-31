"""操作の禁止（実行・提案）の制約と段落が同じ対象を名指す手がかりが無いとき、両立の比較の矛盾を採らない（#3800。匿名の合成応答）。

実モデルの校正（合成 10 件・各 1 回）で、対象を書かない確認の答え（「今はやめておきたい」）に対しても、モデルは
答えに無い対象を補って scope / mode / fit を決め、unknown を返さなかった。採否はモデルの申告に頼らず、
制約の原文と段落が対象を名指す字を共有するかをコードで確かめる。
"""

from unittest.mock import patch

import pytest

from rag_engine.evaluation.answer_validation import PremiseConsistencyOutput, _target_units, premise_conflicts

QUESTION = "設定 A の値をどうすればよいか教えてください。"
EXECUTION_ONLY = ("今回、設定の変更を実行してよいですか。", "今回は変更を実行しない。変更の案だけを示してほしい。")
VALUE_LIMIT = ("設定 A の値に上限はありますか。", "設定 A の値は 50 以下にしたい。")
NEXT_STEP = "次の手順について、どうしますか。"


def _run(question, passage, premise, check):
    diagnostics = []
    output = PremiseConsistencyOutput(
        checks=[{"answer_passage_id": "A1", "constraints": "条件", "recommendation": "案", "passage_quote": passage, **check}]
    )
    with patch("rag_engine.evaluation.answer_validation._parse_checked", return_value=output):
        conflicts = premise_conflicts(question, [{"id": "A1", "text": passage}], [premise], None, diagnostics=diagnostics)
    [row] = diagnostics
    assert row["adopted"] is ("A1" in conflicts)
    return row


# 校正の 10 件のモデルの出力（scope・mode・fit・制約の引用）と、新しい規則の後の判定。前の判定は
# r1-C5・r2-c2・r2-c3 が execution_prohibition_does_not_prohibit_proposal、r2-c1 が conflict_adopted（誤り）。
RECORDED = [
    ("r1-C1", EXECUTION_ONLY, "設定 A の値を 10 から 20 に変える案があります。", "execution_only", "proposal", "fits",
     "利用者の答え: 今回は変更を実行しない。変更の案だけを示してほしい。", "execution_prohibition_does_not_prohibit_proposal"),
    ("r1-C2", ("変更の案を示してよいですか。", "変更の案は示さないでほしい。今の値の意味だけを知りたい。"),
     "設定 A の値を 10 から 20 に変えることを提案します。", "proposal", "proposal", "conflicts",
     "変更の案は示さないでほしい。今の値の意味だけを知りたい。", "conflict_adopted"),
    ("r1-C3", VALUE_LIMIT, "設定 A の値を 30 に変えて保存してください。", "content", "proposal", "fits",
     "設定 A の値は 50 以下にしたい。", "not_conflicting"),
    ("r1-C4", VALUE_LIMIT, "設定 A の値を 80 に変えて保存してください。", "content", "proposal", "conflicts",
     "設定 A の値は 50 以下にしたい。", "conflict_adopted"),
    ("r1-C5", (NEXT_STEP, "今は控えたい。"), "設定 A の値を 20 にします。", "execution_only", "proposal", "conflicts",
     "利用者の答え: 今は控えたい。", "scope_unverified"),
    ("r2-a", EXECUTION_ONLY, "今すぐ設定 A の値を 20 に変えて保存してください。", "execution_only", "execution", "conflicts",
     "利用者の答え: 今回は変更を実行しない。変更の案だけを示してほしい。", "conflict_adopted"),
    ("r2-b", EXECUTION_ONLY, "設定 A の値を 20 に変えて保存しました。", "execution_only", "execution", "conflicts",
     "利用者の答え: 今回は変更を実行しない。変更の案だけを示してほしい。", "conflict_adopted"),
    ("r2-c1", (NEXT_STEP, "今はやめておきたい。"), "設定 A の値を 20 にします。", "execution_only", "execution", "conflicts",
     "利用者の答え: 今はやめておきたい。", "scope_unverified"),
    ("r2-c2", (NEXT_STEP, "それは後にしたい。"), "設定 A の値を 20 にします。", "execution_only", "proposal", "conflicts",
     "利用者の答え: それは後にしたい。", "scope_unverified"),
    ("r2-c3", (NEXT_STEP, "少し様子を見たい。"), "設定 A の値を 20 にします。", "execution_only", "proposal", "fits",
     "利用者の答え: 少し様子を見たい。", "scope_unverified"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("case", "premise", "passage", "scope", "mode", "fit", "quote", "decision"), RECORDED, ids=[row[0] for row in RECORDED]
)
def test_recorded_calibration_outputs(case, premise, passage, scope, mode, fit, quote, decision):
    row = _run(
        QUESTION,
        passage,
        {"question": premise[0], "answer": premise[1]},
        {"fit": fit, "constraint_scope": scope, "passage_mode": mode, "constraint_quote": quote},
    )
    assert row["decision"] == decision
    assert row["adopted"] is (decision == "conflict_adopted")


def test_confirmation_question_names_the_target_the_answer_omits():
    # 答え（「はい、実行しないでください」）は対象を省き、確認の質問が名指す。
    row = _run(
        "手順を教えてください。",
        "設定 B を保存しました。",
        {"question": "設定 B の保存を実行してよいですか。", "answer": "いいえ、しないでください。"},
        {
            "fit": "conflicts",
            "constraint_scope": "execution_only",
            "passage_mode": "execution",
            "constraint_quote": "いいえ、しないでください。",
        },
    )
    assert row["decision"] == "conflict_adopted"


def test_correct_ban_without_any_named_target_is_not_adopted():
    # 規則の代償: 正しい禁止でも、答えと確認の質問のどちらも対象を名指さないなら矛盾を採らない（除かずに残す側）。
    row = _run(
        "手順を教えてください。",
        "設定 B を保存しました。",
        {"question": "進めてよいですか。", "answer": "今回は実行しない。"},
        {"fit": "conflicts", "constraint_scope": "execution_only", "passage_mode": "execution", "constraint_quote": "今回は実行しない。"},
    )
    assert row["decision"] == "scope_unverified"


def test_system_labels_of_the_premise_do_not_count_as_a_target():
    # モデルに渡す本文の「確認の質問:」「利用者の答え:」はシステムの文。段落の「確認」と共有しても結び付きにしない。
    row = _run(
        "手順を教えてください。",
        "設定 B を確認して保存しました。",
        {"question": NEXT_STEP, "answer": "今はやめておきたい。"},
        {"fit": "conflicts", "constraint_scope": "execution_only", "passage_mode": "execution",
         "constraint_quote": "確認の質問: 次の手順について、どうしますか。\n利用者の答え: 今はやめておきたい。"},
    )  # fmt: skip
    assert row["decision"] == "scope_unverified"


def test_question_constraint_uses_only_the_quoted_text():
    # 原質問から写した制約は、写した原文だけを見る（原質問の全体は回答と同じ話題の語を含む）。
    question = "設定 B を見直したい。今は実行しないでください。"
    check = {"fit": "conflicts", "constraint_scope": "execution_only", "passage_mode": "execution"}
    unshared = _run(question, "設定 B を保存しました。", {"answer": "はい"}, {**check, "constraint_quote": "今は実行しないでください。"})
    assert unshared["decision"] == "scope_unverified"
    shared = _run(question, "設定 B を保存しました。", {"answer": "はい"}, {**check, "constraint_quote": question})
    assert shared["decision"] == "conflict_adopted"


def test_content_constraint_on_another_target_is_still_adopted():
    # 対象・値の条件（content）では、段落が別の対象に及ぶこと自体が矛盾なので、対象の共有を求めない。
    row = _run(
        "処理 C はまだ実行していません。選択を外す方法を教えてください。",
        "別の画面で値を 5 に変えます。",
        {"answer": "はい"},
        {"fit": "conflicts", "constraint_scope": "content", "passage_mode": "execution",
         "constraint_quote": "処理 C はまだ実行していません。"},
    )  # fmt: skip
    assert row["decision"] == "conflict_adopted"


def test_target_units():
    assert _target_units("設定 A の値を変える") == {"設", "定", "a", "値", "変"}
    assert _target_units("ＡＢＣ のログ出力") == {"abc", "ログ", "出", "力"}
    assert _target_units("それはあとにしたい。") == set()
