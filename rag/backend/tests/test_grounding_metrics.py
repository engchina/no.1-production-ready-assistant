"""評価専用の根拠との重なりの計測。"""

from app.rag.grounding_metrics import evaluate_groundedness


def test_evaluate_groundedness_returns_score_and_overlap_counts() -> None:
    """評価 runner でも使える groundedness 診断値を返す。"""
    result = evaluate_groundedness(
        "承認条件は 120000 円です。",
        "[policy.txt#doc-1:0]\n承認条件: 120000 円。クラウド利用料。",
    )

    assert result.grounded is True
    assert 0.0 < result.score < 1.0
    assert result.overlap_count >= 1
    assert result.answer_feature_count >= 1
    assert result.high_signal_overlap is True


def test_evaluate_groundedness_fails_unrelated_answer_with_context() -> None:
    """引用と無関係な回答は groundedness gate で検出できる。"""
    result = evaluate_groundedness(
        "明日の天気は晴れです。",
        "[policy.txt#doc-1:0]\n経費申請は部門長の承認が必要です。",
    )

    assert result.grounded is False
    assert result.score == 0.0
    assert result.overlap_count == 0


def test_evaluate_groundedness_treats_full_and_half_width_as_the_same_token() -> None:
    """根拠の全角の ＨＲＭ と回答の半角の HRM を同じ語として重なりに数える (#1350)。"""
    context = "[plan.pdf#doc-1:0]\nＨＲＭ：第２水曜 ２２：００ ＯＭＳ：第３木曜"
    answer = "HRM 第2水曜 OMS 第3木曜"
    folded = evaluate_groundedness(answer, context)
    plain = evaluate_groundedness(answer, "[plan.pdf#doc-1:0]\nHRM:第2水曜 22:00 OMS:第3木曜")

    assert folded.grounded is True
    assert (folded.overlap_count, folded.score) == (plain.overlap_count, plain.score)
