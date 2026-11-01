"""工程の span の表示用のデータ（処理の経過は trace の投影。#3226）。

処理の経過の行は span（`pr_backend_core.observability.tracing`）で、工程を測る span に
表示用のデータ（`SpanDisplay`: 種類 `kind`・名前の値 `params`・開いて見る詳細
`details`）を付ける。種類と値は、画面の工程の定義（`frontend/src/lib/chat-progress.ts`
の `CHAT_PROGRESS_DEFINITIONS`）が名前・計測値を付けるときの key で、次の 2 つにする。

- 回答フローの工程（`rag_engine` の `_execution_step` の span `rag.engine.stage`、
  資料との照合・承認済み FAQ からの回答）: 種類 ``answer_step``。値は工程名 ``name``・
  LLM の回数 ``llm_calls``（送り直しで送信が多ければ ``llm_sends``）・結果の説明
  ``note``（「該当なし」など）。
- 回答フローの外の工程（会話を踏まえた質問の書き換え・質問から条件の読み取り・
  回答の記録の保存）: 工程の名前（``history_rewrite`` など）を
  種類にする。

全体を包む工程（`answer` / `evidence` / `retrieval`）と技術の span（モデルの呼び出し・埋め込み・
並べ替え）は種類を付けず、画面が共通の規則（段の名前・技術の span の名前）で名前を付ける。

工程の利用者向けの名前（名詞。RAG の画面と同じ文言）は、共通の span の表の書き手が
`span_display_name` で span の名前の値 ``display_name`` に書く（#3272）。別の製品の画面
（Agent の実行の詳細）は RAG の工程の定義を持たず、この名前をそのまま出す。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pr_backend_core.chat_progress import ChatProgressStepDetails, chat_progress_details
from pr_backend_core.observability.tracing import SpanUpdate

from app.rag.step_details import step_details_model

# 回答フローの工程の種類（値の ``name`` が工程名）。
ANSWER_STEP_KIND = "answer_step"

# 回答フローの外の工程のうち、種類を付けるもの（工程の名前を種類にする）。
PIPELINE_STEP_KINDS: frozenset[str] = frozenset(
    {"history_rewrite", "field_filter", "answer_record_save"}
)

# 工程の失敗の詳細の行（段を開いて見る。利用者向けの短い要約とコード。#2342）。
_STEP_FAILURE_SUMMARY: Mapping[str, str] = {
    "answer_record_save": "回答の記録をデータベースに保存できませんでした。回答は表示しています。",
}
_STEP_FAILURE_DEFAULT = "この工程でエラーが発生しました。"

# 回答の記録の工程の状態のうち、結果の説明ではないもの（完了・例外で中断）。
_PLAIN_STEP_STATUSES: frozenset[str] = frozenset({"", "complete", "エラーで中断"})
# 例外で止まった工程の状態（`rag_engine` の `_execution_step`）。
ANSWER_STEP_FAILED_STATUS = "エラーで中断"

type DisplayParams = dict[str, str | int | float | bool]

# 進捗の工程（SSE の `stage`。span の属性 `pr.stage`）の利用者向けの名前。時間切れの文言（ERROR
# として保存する回答・SSE の error event・MCP の 504）と、span に書く工程の名前（#3272）に使う。
# 画面の進捗の表示名は frontend の i18n（`search.stage.*`。`src/lib/answer-progress.ts`）と同じ
# 名前にする（frontend の `answer-process.test.ts` が照合する）。
ANSWER_STAGE_LABELS: dict[str, str] = {
    "retrieval": "文書から根拠の候補を検索する",
    "history_rewrite": "前の会話を踏まえて質問を補う",
    "attachment_evidence": "会話の添付から根拠を選ぶ",
    "field_filter": "質問から検索の絞り込み条件を読み取る",
    "answer": "質問の条件を整理して回答を作る",
    "evidence": "質問の条件を整理して根拠を探す",
    "answer_record_save": "質問と回答と根拠の記録を保存する",
    "chat_message_save": "回答を会話に保存する",
}
# 回答フローの工程名（`rag_engine` の `_execution_step`）→ 利用者向けの名前（#2096）。画面の
# `frontend/src/lib/answer-process.ts` の `answerStepDisplayName`（i18n の `answer.step.*`）と
# 同じ名前にする（frontend の `answer-process.test.ts` が照合する）。工程名はログ・記録の識別に
# 使うので変えない。
ANSWER_STEP_DISPLAY_NAMES: dict[str, str] = {
    "質問の理解": "質問から対象と条件を読み取る",
    "用語・同義語の確認": "一致する用語の別名を検索文に加える",
    "回答ルールの確認": "確認と回答に使うルールを選ぶ",
    "質問拡張戦略": "資料を探す検索表現を作る",
    "検索文と検索語の確定": "検索文と全文検索の語を決める",
    "回答生成フロー": "根拠を検索して回答を作る",
    "根拠検索フロー": "回答に使う根拠を検索する",
    "検索語の再確定（書き換え後）": "探し直しに使う検索語を決める",
    "文書検索": "検索文から根拠の候補を集める",
    "Rerank": "質問との関連度で候補を並べ替える",
    "文書の選択": "回答の根拠を探す文書を選ぶ",
    "文書の選択の解除": "後回しにした文書を検索対象へ戻す",
    "根拠確認": "要求に答える根拠があるか判定する",
    "根拠確認の結論": "根拠の判定結果をまとめる",
    "補正検索の準備": "不足を補う検索文を作り直す",
    "文脈の追加": "取得済みの根拠に文脈を加える",
    "画面の選択": "質問に関係する画面を目録から選ぶ",
    "章節の拡張": "関係する章節の原文を根拠に加える",
    "要求ごとの根拠の確認": "要求ごとに根拠の対応を確認する",
    "回答に添付する画像": "回答モデルへ渡す根拠画像を準備する",
    "回答文の生成と根拠確認": "回答文を作って根拠と照合する",
    "是正": "指摘された箇所の回答を直す",
    "是正の打ち切り": "回答の修正を打ち切る",
    "文書ごとの起点の上限": "同じ文書の超過候補を後ろへ回す",
    "未回答の要求の探し直し": "未回答の要求の根拠を探し直す",
    "ベクトル検索": "質問に意味が近い文書の箇所を探す",
    "全文検索": "検索語を含む文書の箇所を探す",
    "検索対象のチャンクの読み込み": "検索対象の文書片を読み込む",
    "要約・抽出値の原文の読み込み": "要約と抽出値の出典の原文を読む",
    "前後の文脈の読み込み": "根拠の前後の文脈を読み込む",
    "参照先の根拠の追加": "文書が参照する箇所の根拠を加える",
    "実体・別名による検索拡張": "対象の名前と別名で関連する根拠を探す",
    "文書構造による検索拡張": "文書の章と表のつながりから根拠を加える",
    "根拠の図の切り出し": "根拠の位置から図を切り出す",
    "文書の分類と背景の読み込み": "文書の分類と背景を読み込む",
}
_STEP_ATTEMPT = re.compile(r"（(\d+)回目）$")
ANSWER_STAGE_BEFORE_START_LABEL = "検索の準備"
ANSWER_STAGE_UNKNOWN_LABEL = "処理"


def answer_step_display_name(name: str) -> str:
    """回答フローの工程名の利用者向けの名前（回数つきは「文書の検索（2 回目）」の形）。"""
    value = name.strip()
    if not value:
        return ANSWER_STAGE_UNKNOWN_LABEL
    if value in ANSWER_STEP_DISPLAY_NAMES:
        return ANSWER_STEP_DISPLAY_NAMES[value]
    attempt = _STEP_ATTEMPT.search(value)
    base = value[: attempt.start()] if attempt else value
    if attempt is None or base not in ANSWER_STEP_DISPLAY_NAMES:
        return value
    return f"{ANSWER_STEP_DISPLAY_NAMES[base]}（{attempt.group(1)} 回目）"


def span_display_name(update: SpanUpdate) -> str | None:
    """RAG の span の利用者向けの名前（名詞。RAG の画面の工程の名前と同じ。#3272）。

    回答フローの工程（種類 ``answer_step``）は工程名から、回答フローの外の工程と全体を包む工程は
    段（``pr.stage``）から決める。技術の span・知らない段は None（画面が共通の規則で名前を付ける）。
    """
    display = update.display
    if display is not None and display.kind == ANSWER_STEP_KIND:
        name = (display.params or {}).get("name")
        return answer_step_display_name(name) if isinstance(name, str) and name.strip() else None
    stage = update.attributes.get("pr.stage")
    return ANSWER_STAGE_LABELS.get(stage) if isinstance(stage, str) else None


def answer_step_params(values: Mapping[str, object]) -> DisplayParams:
    """回答フローの工程の記録の値（工程名・LLM の回数・送信の回数・状態）を表示の値にする。

    LLM の回数は 1 回以上のときだけ、送信の回数は呼び出しより多いときだけ（#2966）、
    結果の説明は完了・中断以外のときだけ入れる。
    """
    params: DisplayParams = {}
    name = values.get("name")
    if isinstance(name, str) and name.strip():
        params["name"] = name.strip()
    llm_calls = values.get("llm_calls")
    if isinstance(llm_calls, int) and not isinstance(llm_calls, bool) and llm_calls > 0:
        params["llm_calls"] = llm_calls
        llm_sends = values.get("llm_sends")
        if isinstance(llm_sends, int) and not isinstance(llm_sends, bool) and llm_sends > llm_calls:
            params["llm_sends"] = llm_sends
    note = str(values.get("status") or "").strip()
    if note not in _PLAIN_STEP_STATUSES:
        params["note"] = note
    return params


def failure_details(
    kind: str | None,
    attributes: Mapping[str, object] | None = None,
    *,
    base: ChatProgressStepDetails | None = None,
) -> ChatProgressStepDetails | None:
    """失敗した工程の詳細（短い要約とエラーの種類・コード。#2342）。

    例外の本文は入れない（ORA のメッセージ・業務データを出さない）。工程の詳細（``base``）
    があれば、見出しと値はそのまま残し、行の先頭に要約を足す。
    """
    lines = [_STEP_FAILURE_SUMMARY.get(kind or "", _STEP_FAILURE_DEFAULT)]
    values = attributes or {}
    error_type = values.get("error_type")
    error_code = values.get("error_code")
    if isinstance(error_type, str) and error_type:
        lines.append(f"エラーの種類: {error_type}")
    if isinstance(error_code, str | int) and not isinstance(error_code, bool) and str(error_code):
        lines.append(f"エラーコード: {error_code}")
    if base is None:
        return chat_progress_details(lines=lines)
    return chat_progress_details(
        fields=[(field.label, field.value, bool(field.mono)) for field in base.fields],
        lines=[*lines, *base.lines],
        code=[(block.label, block.text, block.language) for block in base.code],
        truncated=bool(base.truncated),
    )


def answer_step_details(details: object, *, failed: bool = False) -> ChatProgressStepDetails | None:
    """回答フローの工程の詳細（`rag_engine` の構造化した詳細）。

    失敗した工程は要約の行を先頭に足す。
    """
    model = step_details_model(details)
    return failure_details(ANSWER_STEP_KIND, base=model) if failed else model
