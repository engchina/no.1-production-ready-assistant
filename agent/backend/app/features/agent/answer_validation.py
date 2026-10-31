"""回答の最終の検証（#1246・#1277）。

組み込み Runtime は回答を保存する前に（`agent_final_validation_enabled`。既定 on）、その Run で
RAG が返した根拠（`rag_search` / `rag_retrieve_evidence` の `document_id`・`chunk_id`）で、
RAG の MCP `rag_validate_answer` を呼ぶ。呼ぶのはモデルではなく Control Plane で、ツールの境界
（`tool_registry.invoke` のポリシー・監査、Run の利用者のサービストークン、Run の step）を通す。

- 根拠の id は返した RAG のものなので、根拠を返した MCP 接続ごとに、その接続の根拠で 1 回ずつ
  呼び、段落（`answer_quote`）ごとに判定をまとめる。判定は RAG が返す段落の位置（`answer_range`）で
  この回答の段落に結び付ける（`align_claims`。RAG の段落の分け方を写さない。#3240）。
- 結果は Run の成果物（kind=`answer_validation`・「資料との照合」）に残す（接続ごとの結果は
  `connections`）。状態は `completed`（判定が出た）/ `unvalidated`（確かめられなかった）/
  `skipped`（確かめる対象が無い）。
- 接続ごとに、その接続の最も新しい `rag_search` の `requests`・`gaps`・`guide`（無ければ
  `rag_lookup_guides` の最上位の業務ガイド）も渡し、RAG の決定的な検査（要求の充足・手順の順序と
  分岐・影響範囲・承認。#1276）を動かす。その接続の `rag_search` の `trace_id`
  （`answer_trace_ids`）も渡し、RAG の回答の監査を通った説明と本文・根拠が同じ段落は、RAG が
  その判定を使ってモデルに渡さない（#1527。根拠は RAG が今の権限と版で読み直す。すべての段落が
  当たればモデルを呼ばない）。
- 判定が valid でなければ、段落の印（platform/contracts/answer-passages/passage-marks.json。#2738）
  を付ける。本文から外すのは根拠と矛盾する段落（`contradicted`）だけで、外した段落と理由を節
  「根拠と矛盾し除いた内容」に示す。根拠で確かめられない・出典を確かめられない・確かめが終わら
  ない・図の画像を読めていない段落は本文に残して未確認（`unverified`）の印を、RAG の規則の検査
  （別の対象・表の行の対応）は警告（`warning`）の印を付ける。番号つきの手順で、外した操作・未確認
  の操作に続く操作は本文に残して警告の印を付ける（#2604・#2735 の続く手順）。外した操作を名前だけで
  示す番号つきの項目と、中身をすべて外した見出しは外し、残った項目の番号を詰める（#2692）。
  決定的な検査の error（要求・業務ガイドの手順・影響範囲・版）・本文の検査で不足を確認した
  業務ガイドの必須の手順・根拠を 1 件も読めない（`no_evidence`。すべての段落を未確認にする）は本文を
  置き換えず、回答全体の警告（`answer_warnings`）にする。印と回答の警告は節「確認が必要な点」に
  並べる（本文だけを読む MCP の利用者にも分かるように）。warning の指摘は出さない。
- 検証そのもの（呼び出し・接続・応答）が失敗したら `unvalidated` にし、回答は消さずに
  「この回答は資料との照合を終えられず、本質問の条件への該当は確認できていません。」を足す
  （基盤の障害で内容を落とさない）。RAG が時間の上限までにどの段落も確かめ終わらなかった
  （`timed_out`）ときも同じ（理由 `validation_timed_out`）。
- RAG が時間の上限までに一部の段落だけを確かめた（`partial`。#2700）ときは、確かめ終わった
  段落の判定で回答を組み立て、確かめ終わらなかった段落（`unassessed`）は本文に残して未確認の
  印を付ける（時間切れで内容を落とさない）。
- RAG の根拠を使っていない Run は、RAG の根拠のツールを持つ Agent なら `unvalidated`
  （`no_rag_evidence`）にして「資料と照らし合わせて確かめていない」と足し、持たない Agent は
  `skipped`（回答はそのまま）。利用者への確認の質問だけの回答は、資料の主張を含まないので
  `skipped`（`clarification_only`）にして注記しない（#1306）。資料に無い・答えられないことだけを
  述べる回答（拒答と「ご了承ください」などの結びだけ）も同じく `skipped`（`absence_only`）にする
  （#1879）。
- 会話の続きで、今のターンが資料の根拠を得ずに答えた（前の回答の整形・言い換え。検索を呼んで
  0 件だったときも同じ。#3314）ときは、前のターンの検証で回答の段落を裏付けた根拠
  （`carried_evidence`。#1837）だけで検証する（`carried_evidence_groups`。#1879）。前のターンの事実を言い直した段落は裏付けられ、
  新しい主張は確かめられない段落として印が付く。
- 図の見た目だけで決まる段落（RAG の判定 `figure_only`。配置・矢印・つながり・分岐への所属
  など、図の本文〔図の説明・OCR の文字〕が明示しない内容。#2678）は、その Run（と前のターンの
  検証）で図の画像がモデルへ届いたかで扱いを決める（`figure_readings`。Run の step の
  `rag_read_source` の `content_images` の `model_input=attached` と由来の document_id・
  chunk_id）。届いていれば画像で確かめた段落として `supported` にし（`figure_check=image_read`・
  `figure_image` に画像の参照と版・領域）、回答に画像で確かめたことの注記を足す。届いて
  いなければ（画像に対応しないモデル・画像の取得の失敗・画像を読んでいない）`unsupported`
  （`figure_check=image_not_read`）にして、本文に残して未確認の印（`figure_not_read`）を付ける。
  RAG の判定は `rag_status` に残す。
- 主張ではない段落（見出し・出典の行・表の区切りと見出しの行・利用者への質問・資料に記載が
  無いことを述べる文と拒答の文・最終の検証が足した節）は、RAG の判定にかかわらず外さず、印も
  付けない（#1306・#1317。判定は `answer_passages`。成果物の段落には `non_claim` を付ける）。
  利用者に現場のデータ・記録の確認を求める段落には `environment_check` を付ける（#1317。主張の
  まま確かめる。回答の対応が「現場の確認が要る」の印に使う）。

ここは根拠の参照の集め方・判定のまとめ方・成果物の内容・回答の組み立てだけを持つ（呼び出しは
`builtin_runtime`）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.features.agent.answer_passages import (
    answer_passage_positions,
    environment_check_passages,
    is_heading,
    is_lookup_note,
    is_operation_step,
    is_structure,
    label_operation_end,
    list_heading_lines,
    list_item_label,
    non_claim_passages,
    passage_spans,
    table_header_lines,
)
from app.features.agent.evidence_match import (
    EvidenceText,
    answer_features,
    matched_features,
    order_weights,
)
from app.features.agent.support_task import (
    RAG_READ_DOCUMENT,
    RAG_READ_SECTION,
    RAG_READ_SOURCE,
    RAG_RETRIEVE_EVIDENCE,
    RAG_SEARCH,
)
from app.features.agent.tool_media import ATTACHED, CONTENT_IMAGES_KEY
from app.features.agent.tools import MCP_TOOL_SEPARATOR, mcp_base_tool_name

if TYPE_CHECKING:
    from app.features.agent.runtime import RunStep

JsonObject = dict[str, Any]

ANSWER_VALIDATION_KIND = "answer_validation"
ANSWER_VALIDATION_NAME = "資料との照合"
VALIDATE_ANSWER_TOOL = "rag_validate_answer"
# 検証に渡す根拠の参照の上限（`rag_validate_answer` の契約の maxItems）。
MAX_VALIDATION_EVIDENCE = 30
# 同じ会話の前のターンから引き継ぐ根拠の参照の上限（接続ごと。#1837）。
MAX_CARRIED_EVIDENCE = 10
# 契約の上限（質問は切り詰めて渡す。回答は切り詰めると別の文の検証になるため、超えたら検証しない）。
MAX_QUERY_CHARS = 8000
MAX_ANSWER_CHARS = 20000

# 判定が出た。
STATUS_COMPLETED = "completed"
# 確かめる対象が無い（回答が空・RAG の根拠のツールを持たない Agent の根拠の無い回答）。
STATUS_SKIPPED = "skipped"
# 確かめられなかった（検証の失敗・RAG の根拠を使わなかった回答）。回答に注記を足す。
STATUS_UNVALIDATED = "unvalidated"

# 決定的な検査の入力（`requests`・`gaps`・`guide.conditions`）の件数の上限（契約の maxItems）。
MAX_CHECK_ITEMS = 30
# 監査の判定を再利用する `rag_search` の回答の記録（`answer_trace_ids`）の上限と、
# trace_id の長さ（契約）。
MAX_ANSWER_TRACE_IDS = 10
MAX_TRACE_ID_CHARS = 64
RAG_LOOKUP_GUIDES = "rag_lookup_guides"
REQUEST_STATUSES = frozenset({"addressed", "partial", "missing", "unknown"})
# 回答に手順を書く業務ガイドの判断（手順・影響範囲を確かめる）。
GUIDE_DECISIONS = frozenset({"answer", "branch"})

REASON_NO_RAG_EVIDENCE = "no_rag_evidence"
# 資料（RAG）の根拠ではなく、利用者が添付したファイルを読んで答えた（#2517）。資料と照らし合わせる
# 対象が無いので注記しない。
REASON_ATTACHMENT_EVIDENCE = "attachment_evidence"
REASON_VALIDATOR_UNAVAILABLE = "validator_unavailable"
REASON_EMPTY_ANSWER = "empty_answer"
# 利用者への確認の質問だけの回答（資料の主張を含まないので、根拠が無くても注記しない。#1306）。
REASON_CLARIFICATION_ONLY = "clarification_only"
# 利用者が「資料の検索は不要」と確認を取り消した Run（#1445。資料を使わずに答えるので注記しない）。
REASON_RAG_DECLINED = "rag_declined"
# 資料に無い・答えられないことだけを述べる回答（拒答。資料の主張を含まないので、根拠が無くても
# 注記しない。#1879）。
REASON_ABSENCE_ONLY = "absence_only"
REASON_ANSWER_TOO_LONG = "answer_too_long"
REASON_CONNECTION_NOT_FOUND = "connection_not_found"
REASON_UNUSABLE_RESULT = "unusable_result"
# RAG が時間の上限までにどの段落も確かめ終わらなかった（`rag_validate_answer` の
# status=timed_out。#2700）。
REASON_VALIDATION_TIMED_OUT = "validation_timed_out"
REASON_VALIDATION_ERROR = "validation_error"

# RAG の「参考情報」の節の説明（該当は確認できていない）と同じ言い方で、照合できなかったことを示す。
UNVERIFIED_NOTICE = "この回答は資料との照合を終えられず、本質問の条件への該当は確認できていません。"
NO_EVIDENCE_NOTICE = "この回答は資料の根拠を使っておらず、資料と照らし合わせて確かめていません。"
# 回答の末尾に足す 2 つの節（#2738）。本文だけを読む MCP の利用者にも、外した段落と確かめが要る
# 段落が分かるようにする。RAG の品質評価は、この見出しの節を faithfulness の本文から除く
# （platform/contracts/answer-passages/unverified-section.json の `headings`）。
REMOVED_HEADING = "根拠と矛盾し除いた内容"
REMOVED_INTRO = "次の内容は根拠と矛盾するため、回答から除きました。"
REVIEW_HEADING = "確認が必要な点"
REVIEW_INTRO = "次の点は根拠で確かめきれていないか、注意が必要です（内容は回答に載せています）。"
SECTION_HEADINGS = (REMOVED_HEADING, REVIEW_HEADING)
# 本文の段落がすべて根拠と矛盾して外れたときの本文。
ALL_REMOVED = "回答の内容はすべて根拠と矛盾したため、本文を載せていません。"
EVIDENCE_TOOLS = frozenset({RAG_SEARCH, RAG_RETRIEVE_EVIDENCE})
# 本文を読むツール（#1330・#1332）。多段の質問では段の事実を読んで確かめるため、読んだ chunk も
# 最終の検証の根拠に入れる（#1345）。
READ_EVIDENCE_TOOLS = frozenset({RAG_READ_SOURCE, RAG_READ_DOCUMENT, RAG_READ_SECTION})
# 文書・章節をまとめて読むツール。回答に当たらない chunk は検索の根拠の後ろに置く（#1451）。
BULK_READ_TOOLS = frozenset({RAG_READ_DOCUMENT, RAG_READ_SECTION})
# 図の見た目だけで決まる段落の RAG の判定（#2678）。`apply_figure_readings` で `supported`
# （図の画像で確かめた）か `unsupported`（図の画像を読めていない）にする。
FIGURE_ONLY = "figure_only"
# 図の見た目だけで決まる段落を、図の画像で確かめたか（判定の `figure_check`）。
FIGURE_IMAGE_READ = "image_read"
FIGURE_IMAGE_NOT_READ = "image_not_read"
# 図の画像で確かめた段落が回答に残ったときに、回答の本文の後に足す注記。
FIGURE_READ_NOTE = (
    "図の見た目（配置・矢印・つながりなど）に関する内容は、資料の図の画像を読んで確かめたもので、"
    "図の説明や図の中の文字では確かめていません。"
)
# 検証に通さない（`valid` にしない）段落の判定（RAG の `is_valid` と同じ）。本文から外すのは
# 根拠と矛盾する段落（`contradicted`）だけで、ほかは本文に残して印を付ける（#2738）。
INVALID_CLAIM_STATUSES = frozenset(
    {"unsupported", "contradicted", "citation_error", "unassessed", FIGURE_ONLY}
)

# 段落の印（platform/contracts/answer-passages/passage-marks.json。#2738）。正本は構造化した記録で、
# 本文の文字列に印の記号を埋め込まない。1 段落に複数の理由があれば並べ、状態は未確認を優先する。
MARK_UNVERIFIED = "unverified"
MARK_WARNING = "warning"
# 本文に残す段落の理由（code → 状態・利用者に見せる短い文）。
PASSAGE_REASONS: dict[str, tuple[str, str]] = {
    "unsupported": (MARK_UNVERIFIED, "根拠で確かめられない"),
    "citation_error": (MARK_UNVERIFIED, "出典を確かめられない"),
    "quote_unmatched": (MARK_UNVERIFIED, "引用の原文が根拠に見当たらない"),
    "unassessed": (MARK_UNVERIFIED, "確かめが終わっていない"),
    "figure_not_read": (MARK_UNVERIFIED, "図の画像を読めておらず確かめられない"),
    "target_mismatch": (MARK_WARNING, "根拠は別の対象についての記載"),
    "value_unbound": (MARK_WARNING, "根拠の表の対応と違う可能性"),
    "table_corrected": (MARK_WARNING, "根拠の表の値に直した"),
    "after_contradicted_step": (MARK_WARNING, "前の手順が根拠と矛盾"),
    "after_unverified_step": (MARK_WARNING, "前提の手順が未確認"),
}
# 本文から外した段落の理由。
REMOVED_REASONS = {"contradicted": "根拠と矛盾"}
# 回答全体の警告の理由（決定的な検査の error・説明を確かめられない業務ガイドの手順・
# 根拠を読めない）。
ANSWER_WARNING_REASONS = {
    "guide_check": "業務ガイドの手順の確認で問題が見つかった",
    "no_evidence": "根拠を読めず、すべての段落が未確認",
    "requests": "質問の要求の一部に答えていない",
}
# RAG が監査の裏付け（supported）を採らなかった理由（`support_check`。#2659・#2653）のうち、印の
# 理由にするもの（それ以外は判定〔`status`〕で決める）。
_SUPPORT_CHECK_REASONS = frozenset({"target_mismatch", "value_unbound", "quote_unmatched"})
# 判定（`status`）ごとの印の理由（`supported`・`data_confirmation` など、ここに無い判定は
# 印にしない）。
_STATUS_REASONS = {
    "unsupported": "unsupported",
    "citation_error": "citation_error",
    "unassessed": "unassessed",
    FIGURE_ONLY: "figure_not_read",
}
# 業務ガイドの手順の説明の充足（#2708）。`rag_validate_answer` の `guide_steps` の状態の強さの順
# （接続をまたいでは最も良い状態を採る）と、説明の無い必須の手順の finding（warning。文は RAG の
# 回答の不足の行と同じ）。
GUIDE_STEP_STATUS_ORDER = (
    "explained",
    "partial",
    "reference_only",
    "unused",
    "unknown",
    "not_searched",
    "no_evidence",
)
GUIDE_STEP_UNEXPLAINED = "guide_step_unexplained"
# 業務ガイドの手順・影響範囲・版の検査（error は回答全体の警告 `guide_check`。回答の対応は
# 人の確認）。
GUIDE_CHECKS = frozenset({"guide", "guide_steps", "impact"})
# RAG が時間の上限までに一部の段落だけを確かめた結果の status（#2700。確かめ終わらなかった
# 段落は `unassessed`）と、どの段落も確かめ終わらなかった結果の status（判定として使えない）。
RESULT_PARTIAL = "partial"
RESULT_TIMED_OUT = "timed_out"
RESULT_NO_EVIDENCE = "no_evidence"
# 判定として使える `rag_validate_answer` の結果の status（それ以外は検証の失敗として扱う）。
# 接続ごとの結果をまとめるときは、前のものを全体の status にする（どれかが partial なら partial）。
_USABLE_RESULT_STATUSES = (RESULT_PARTIAL, "completed", RESULT_NO_EVIDENCE, "no_claims")
# 節の項目は 1 行に縮める（段落・指摘の全文は成果物の判定に残る）。項目の数は切らない（#2361）。
_MAX_QUOTE_CHARS = 80
_MAX_FINDING_CHARS = 120
# 印の理由の詳しい説明（監査・規則の理由）の上限。
_MAX_DETAIL_CHARS = 400


def is_contradicted(claim: JsonObject) -> bool:
    """根拠と矛盾する判定か（本文から外す。#2738）。

    RAG が根拠の ID を照合できずに判定（`status`）を変えても、監査モデルが返した元の判定
    （`audit_status`）が矛盾なら矛盾として扱う（#2622。引用の照合の誤りで矛盾の判定が消え、
    矛盾した本文を載せていた）。
    """
    return "contradicted" in {claim.get("status"), claim.get("audit_status")}


def _short(value: object, limit: int) -> str:
    """1 行にして `limit` 文字まで（超えたら末尾を「…」にする）。"""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _connection_prefix(function_name: str) -> str | None:
    """`<接続>__<ツール>` の接続の部分（MCP 接続のツールでなければ None）。"""
    if MCP_TOOL_SEPARATOR not in function_name:
        return None
    return function_name[: -len(mcp_base_tool_name(function_name)) - len(MCP_TOOL_SEPARATOR)]


def locator_labels(item: JsonObject) -> list[object]:
    """根拠の見出し（章節の列・章節の名前・シート名）。回答と照らすのに使う（#1451）。"""
    labels: list[object] = []
    locator = item.get("locator")
    if isinstance(locator, dict):
        path = locator.get("section_path")
        labels.extend(path if isinstance(path, list) else [])
        labels.append(locator.get("sheet_name"))
    section = item.get("section")
    if isinstance(section, dict):
        labels.append(section.get("title"))
    path = item.get("section_path")
    labels.extend(path if isinstance(path, list) else [])
    # 読み取りのツールの chunk は表計算のシート名を chunk に持つ（#1471）。
    labels.append(item.get("sheet_name"))
    return labels


_EvidenceRef = tuple[tuple[str, str], object, list[object], list[object]]


def _evidence_refs(
    tool: str, output: JsonObject, *, original_only: bool = False
) -> list[_EvidenceRef]:
    """根拠を集める・読むツールの出力の（参照, 本文, 見出し, 文書のファイル名）（出てきた順）。"""
    items: list[tuple[object, object, list[object], list[object]]]
    if tool in EVIDENCE_TOOLS:
        evidence = output.get("evidence")
        items = [
            (item, item.get("excerpt"), locator_labels(item), [item.get("file_name")])
            for item in (evidence if isinstance(evidence, list) else [])
            if isinstance(item, dict)
            and (not original_only or item.get("origin", "source") == "source")
        ]
    elif tool == RAG_READ_SOURCE:
        items = [(output, output.get("text"), locator_labels(output), [output.get("file_name")])]
    elif tool in {RAG_READ_DOCUMENT, RAG_READ_SECTION}:
        chunks = output.get("chunks")
        document_id = output.get("document_id")
        body = output.get("text")
        body = body if isinstance(body, str) else ""
        section_labels = locator_labels(output)
        names = [output.get("file_name")]
        items = []
        for chunk in chunks if isinstance(chunks, list) else []:
            if not isinstance(chunk, dict):
                continue
            start, end = chunk.get("start"), chunk.get("end")
            chunk_text = (
                body[start:end]
                if isinstance(start, int) and isinstance(end, int) and start < end
                else ""
            )
            ref = {"document_id": document_id, "chunk_id": chunk.get("chunk_id")}
            items.append((ref, chunk_text, [*locator_labels(chunk), *section_labels], names))
    else:
        return []
    refs: list[_EvidenceRef] = []
    for item, text, labels, file_names in items:
        if not isinstance(item, dict):
            continue
        document_id, chunk_id = item.get("document_id"), item.get("chunk_id")
        if isinstance(document_id, str) and isinstance(chunk_id, str) and document_id and chunk_id:
            refs.append(((document_id, chunk_id), text, labels, file_names))
    return refs


def evidence_source_texts(steps: Sequence[RunStep]) -> list[str]:
    """モデルの生成した回答を含めず、根拠を取得・読むツールが返した原文を集める。"""
    texts: list[str] = []
    for step in steps:
        call, result = step.tool_call, step.tool_result
        if call is None or result is None or not result.success or step.status != "completed":
            continue
        if not isinstance(result.output, dict):
            continue
        for _ref, text, _labels, _names in _evidence_refs(
            mcp_base_tool_name(call.name), result.output, original_only=True
        ):
            if isinstance(text, str):
                texts.append(text)
    return texts


def _cited_in(answer: str, chunk_id: str) -> bool:
    """回答が根拠の id（MCP の `evidence_id` は `chunk_id` と同じ）を書いているか。

    `…:1` が `…:19` の先頭に一致しないよう、後ろが数字で続くものは数えない。
    """
    return bool(re.search(re.escape(chunk_id) + r"(?!\d)", answer))


# 回答が書いた根拠の id（途中を「…」で省いた・先頭の部分を落とした・区切りを横線で書いた形）。
# 根拠の id は契約では不透明な文字列（rag-tools.json の `chunk_id`）なので、id の形（区切り・桁）を
# 写さず、渡された根拠の id の値から区切りの文字を決めて照合する（#3240）。
_ID_DASHES = "‐‑‒–—―-"
_ID_ELLIPSIS = re.compile(r"…+|‥+|\.{2,}")
# 省いて書いた ID の残した先頭の最短の長さ（これより短いと別の根拠と区別しにくい）。
_MIN_ABBREVIATED_ID_CHARS = 7
# 候補の語の先頭の英数字の最短の長さ（「第3章」「A-1」のような ID でない語を数えない）。
_MIN_ID_HEAD_CHARS = 6


def _id_separators(chunk_ids: Sequence[str]) -> str:
    """根拠の id の区切りの文字（英数字・省略の印の「.」以外の文字）と横線。"""
    found = {
        char
        for chunk_id in chunk_ids
        for char in chunk_id
        if not (char.isascii() and char.isalnum()) and char != "."
    }
    return "".join(sorted(found | set(_ID_DASHES)))


def _cited_id_pattern(separators: str) -> re.Pattern[str]:
    """回答の中の、根拠の id を書いた候補の語（英数字で始まり、区切りか省略の印を挟んで英数字で
    終わる並び）。"""
    seps = re.escape(separators)
    return re.compile(
        rf"(?<![0-9A-Za-z])[0-9A-Za-z]{{{_MIN_ID_HEAD_CHARS},}}"
        rf"(?:[0-9A-Za-z…‥{seps}]|\.{{2,}})*[{seps}][0-9A-Za-z]+(?![0-9A-Za-z])"
    )


def _abbreviated_part(written: str, actual: str) -> bool:
    """ID の部分を、後ろを切って・途中を「…」で省いて書いたか。"""
    pieces = _ID_ELLIPSIS.split(written)
    if len(pieces[0]) < _MIN_ABBREVIATED_ID_CHARS:
        return False
    if len(pieces) == 1:
        return len(written) < len(actual) and actual.startswith(written)
    return re.fullmatch(".+".join(re.escape(piece) for piece in pieces), actual) is not None


def _written_id_matches(written: str, actual: str, separators: str) -> bool:
    """回答が省いて書いた根拠の id（`written`）が `actual` を指すか（最後の部分はそのまま一致）。"""
    # 区切り（横線を含む）は「:」にそろえ、省略の印の直前の区切りは除く。
    unify = re.compile(f"[{re.escape(separators)}]")
    written = re.sub(r":+(?=…|‥|\.{2,})", "", unify.sub(":", written)).lower()
    actual = unify.sub(":", actual).lower()
    if written == actual or (_ID_ELLIPSIS.search(written) and _abbreviated_part(written, actual)):
        return True
    written_parts, actual_parts = written.split(":"), actual.split(":")
    if 2 <= len(written_parts) < len(actual_parts):
        actual_parts = actual_parts[-len(written_parts) :]
    if len(written_parts) != len(actual_parts) or written_parts[-1] != actual_parts[-1]:
        return False
    return all(
        w == a or _abbreviated_part(w, a)
        for w, a in zip(written_parts[:-1], actual_parts[:-1], strict=True)
    )


def cited_chunk_ids(answer: str, chunk_ids: Sequence[str]) -> set[str]:
    """回答が書いた根拠の id（そのままの形と、省いて書いて 1 つの根拠に決まる形）。"""
    cited = {chunk_id for chunk_id in chunk_ids if _cited_in(answer, chunk_id)}
    if not chunk_ids:
        return cited
    separators = _id_separators(chunk_ids)
    for match in dict.fromkeys(
        item.group(0) for item in _cited_id_pattern(separators).finditer(answer)
    ):
        matches = {
            chunk_id for chunk_id in chunk_ids if _written_id_matches(match, chunk_id, separators)
        }
        if len(matches) == 1:
            cited |= matches
    return cited


@dataclass
class _GroupRef:
    """接続の中の根拠 1 件（並べ替えの材料）。"""

    ref: tuple[str, str]
    order: int
    evidence: EvidenceText = field(default_factory=EvidenceText)
    # 文書・章節をまとめて読んだ（rag_read_document / rag_read_section）だけの根拠か。
    bulk_read: bool = True
    # 同じ会話の前のターンの最終の検証で、回答の段落を裏付けた根拠か（#1837）。
    carried: bool = False
    # その Run で図の画像がモデルへ届いた図の根拠か（#2678。図の見た目の段落を図に結び付ける）。
    figure: bool = False


def _ordered(refs: list[_GroupRef], answer: str) -> list[tuple[str, str]]:
    """回答が使った根拠を先にした並び（#1364・#1451）。

    1. 回答が `chunk_id` を書いた根拠（省いて書いて 1 つに決まる形を含む。新しい呼び出しから順）と、
       図の画像がモデルへ届いた図の根拠（#2678）
    2. 回答に固有の特徴（ID・コード・引用・見出し・同じ並び・「文書名＋第 N 章」の章節の参照）で
       当たった根拠（重みの大きい順）
    3. 同じ会話の前のターンで回答の段落を裏付けた根拠（#1837。新しいターンから順）
    4. 残りの検索・本文の読み取り（rag_read_source）の根拠（当たりの重み、新しい呼び出しの順）
    5. 残りの、文書・章節をまとめて読んだ chunk（全文を読んだ台帳の行が上限を埋めない）
    """
    if not answer:
        return [item.ref for item in refs]
    features = answer_features(answer)
    weights = order_weights([matched_features(features, item.evidence) for item in refs])
    cited = cited_chunk_ids(answer, [item.ref[1] for item in refs])

    def key(index: int) -> tuple[int, float, int]:
        item = refs[index]
        if item.ref[1] in cited or item.figure:
            return (0, 0.0, item.order)
        distinct, weight = weights[index]
        if distinct:
            return (1, -weight, item.order)
        if item.carried:
            return (2, 0.0, item.order)
        return (4 if item.bulk_read else 3, -weight, item.order)

    return [refs[index].ref for index in sorted(range(len(refs)), key=key)]


def carried_evidence(validations: list[JsonObject]) -> dict[str, list[JsonObject]]:
    """同じ会話の前のターンの最終の検証で、回答の段落を裏付けた（`supported`）根拠の参照（#1837）。

    `validations` は前のターンの最終の検証の成果物の内容（古い順。`thread_answer_validations`）。
    返すのは MCP 接続（function tool の名前の接続の部分）ごとの参照で、新しいターンから順、接続の
    中で重複なし、最大 `MAX_CARRIED_EVIDENCE` 件。会話の続きでモデルが前のターンの事実を言い直すと、
    今のターンの根拠だけでは裏付けが無く外れるため、前のターンで実際に確かめた根拠も検証に渡す
    （本文は RAG が今の利用者の権限・データの範囲・版で読み直す。削除・改版されていれば
    `missing_evidence` / `stale_evidence` になる）。判定が出なかったターン・接続は使わない。
    """
    carried: dict[str, list[JsonObject]] = {}
    for content in reversed(validations):
        if content.get("status") != STATUS_COMPLETED:
            continue
        connections = content.get("connections")
        for connection in connections if isinstance(connections, list) else []:
            if not isinstance(connection, dict) or connection.get("status") != STATUS_COMPLETED:
                continue
            prefix = _connection_prefix(str(connection.get("tool_name") or ""))
            result = connection.get("result")
            evidence = connection.get("evidence")
            if prefix is None or not isinstance(result, dict) or not isinstance(evidence, list):
                continue
            # 判定の chunk_id は検証に渡した根拠のもの（文書の id は渡した参照から引く）。
            documents = {
                ref.get("chunk_id"): ref.get("document_id")
                for ref in evidence
                if isinstance(ref, dict)
            }
            refs = carried.setdefault(prefix, [])
            claims = result.get("claims")
            for claim in claims if isinstance(claims, list) else []:
                if not isinstance(claim, dict) or claim.get("status") != "supported":
                    continue
                chunk_id = claim.get("chunk_id")
                document_id = documents.get(chunk_id)
                if not isinstance(chunk_id, str) or not isinstance(document_id, str):
                    continue
                ref = {"document_id": document_id, "chunk_id": chunk_id}
                if ref not in refs and len(refs) < MAX_CARRIED_EVIDENCE:
                    refs.append(ref)
    return {prefix: refs for prefix, refs in carried.items() if refs}


def _figure_image(ref: JsonObject, step_id: str) -> JsonObject | None:
    """`rag_read_source` の結果の画像の参照（`content_images` の 1 件）の、図の画像の読み取り。

    由来（`source`）に文書と chunk が無い画像は図の根拠に結び付けられないので None。
    """
    source = ref.get("source")
    if not isinstance(source, dict):
        return None
    document_id, chunk_id = source.get("document_id"), source.get("chunk_id")
    if not isinstance(document_id, str) or not isinstance(chunk_id, str):
        return None
    if not document_id or not chunk_id:
        return None
    reading: JsonObject = {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "model_input": str(ref.get("model_input") or ""),
        "step_id": step_id,
    }
    if isinstance(ref.get("image_id"), str):
        reading["image_id"] = ref["image_id"]
    if isinstance(source.get("chunk_set_id"), str):
        reading["chunk_set_id"] = source["chunk_set_id"]
    if isinstance(source.get("region"), dict):
        reading["region"] = dict(source["region"])
    return reading


def figure_readings(steps: list[RunStep]) -> dict[str, dict[str, JsonObject]]:
    """その Run で `rag_read_source` が返した図の画像の読み取り（MCP 接続 → chunk → 記録。#2678）。

    記録は画像の参照（`image_id`・版の `chunk_set_id`・領域 `region`）と、モデルへ届いたか
    （`model_input`。`attached` が届いた）。同じ chunk の画像が何度か返れば、届いた記録を優先し、
    その中では最も新しい呼び出しのもの。失敗した呼び出し（chunk の id の誤りなど）は画像が無いので
    記録しない（届いていないものとして扱う）。
    """
    readings: dict[str, dict[str, JsonObject]] = {}
    for step in steps:
        call, result = step.tool_call, step.tool_result
        if call is None or result is None or not result.success or step.status != "completed":
            continue
        if mcp_base_tool_name(call.name) != RAG_READ_SOURCE or not isinstance(result.output, dict):
            continue
        prefix = _connection_prefix(call.name)
        refs = result.output.get(CONTENT_IMAGES_KEY)
        if prefix is None or not isinstance(refs, list):
            continue
        found = readings.setdefault(prefix, {})
        for ref in refs:
            reading = _figure_image(ref, step.id) if isinstance(ref, dict) else None
            if reading is None:
                continue
            known = found.get(reading["chunk_id"])
            if (
                known is None
                or reading["model_input"] == ATTACHED
                or known.get("model_input") != ATTACHED
            ):
                found[reading["chunk_id"]] = reading
    return {prefix: found for prefix, found in readings.items() if found}


def carried_figure_readings(validations: list[JsonObject]) -> dict[str, dict[str, JsonObject]]:
    """同じ会話の前のターンの検証で、図の画像で確かめた段落の図の読み取り（#2678）。

    `carried_evidence` で引き継ぐ根拠のうち図の根拠は、前のターンでモデルへ届いた画像で確かめた
    もの。前の回答の整形・言い換えで同じ図の見た目を述べた段落を、今のターンで画像を読み直して
    いないことだけで外さない。返す形は `figure_readings` と同じ（新しいターンのものを優先）。
    """
    readings: dict[str, dict[str, JsonObject]] = {}
    for content in reversed(validations):
        if content.get("status") != STATUS_COMPLETED:
            continue
        connections = content.get("connections")
        for connection in connections if isinstance(connections, list) else []:
            if not isinstance(connection, dict) or connection.get("status") != STATUS_COMPLETED:
                continue
            prefix = _connection_prefix(str(connection.get("tool_name") or ""))
            result = connection.get("result")
            if prefix is None or not isinstance(result, dict):
                continue
            claims = result.get("claims")
            for claim in claims if isinstance(claims, list) else []:
                if not isinstance(claim, dict) or claim.get("figure_check") != FIGURE_IMAGE_READ:
                    continue
                image = claim.get("figure_image")
                chunk_id = claim.get("chunk_id")
                if isinstance(image, dict) and isinstance(chunk_id, str):
                    readings.setdefault(prefix, {}).setdefault(
                        chunk_id, {**image, "model_input": ATTACHED, "carried": True}
                    )
    return readings


def validation_figure_readings(
    steps: list[RunStep], validations: list[JsonObject]
) -> dict[str, dict[str, JsonObject]]:
    """最終の検証に使う図の画像の読み取り（その Run の `figure_readings` と、前のターンの検証で
    図の画像で確かめた図の `carried_figure_readings`。#2678）。

    同じ図はモデルへ届いた記録を優先し、どちらも届いていればその Run のものを使う。
    """
    readings = {prefix: dict(found) for prefix, found in figure_readings(steps).items()}
    for prefix, found in carried_figure_readings(validations).items():
        current = readings.setdefault(prefix, {})
        for chunk_id, reading in found.items():
            known = current.get(chunk_id)
            if known is None or known.get("model_input") != ATTACHED:
                current[chunk_id] = reading
    return readings


def apply_figure_readings(result: JsonObject, readings: dict[str, JsonObject]) -> JsonObject:
    """`figure_only` の段落を、図の画像がモデルへ届いたかで `supported` / `unsupported` にする。

    `readings` は検証した接続の図の読み取り（chunk → 記録。`figure_readings`）。段落の根拠の図
    （`chunk_id`）の画像が届いていれば、画像で確かめた段落として `supported` にし、画像の参照を
    `figure_image` に残す。届いていなければ `unsupported` にする（図の画像を読めていない段落は、図の
    本文〔OCR の文字の並び〕から推した断定なので未確認の印を付ける）。RAG の判定は `rag_status` に
    残し、件数と `valid` を数え直す。`figure_only` の無い結果はそのまま返す。
    """
    raw = result.get("claims")
    claims = raw if isinstance(raw, list) else []
    if not any(isinstance(item, dict) and item.get("status") == FIGURE_ONLY for item in claims):
        return result
    updated: list[JsonObject] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        if claim.get("status") != FIGURE_ONLY:
            updated.append(claim)
            continue
        chunk_id = claim.get("chunk_id")
        reading = readings.get(chunk_id) if isinstance(chunk_id, str) else None
        if reading is not None and reading.get("model_input") == ATTACHED:
            image = {key: value for key, value in reading.items() if key != "model_input"}
            updated.append(
                {
                    **claim,
                    "status": "supported",
                    "rag_status": FIGURE_ONLY,
                    "figure_check": FIGURE_IMAGE_READ,
                    "figure_image": image,
                }
            )
            continue
        not_read: JsonObject = {
            **claim,
            "status": "unsupported",
            "rag_status": FIGURE_ONLY,
            "figure_check": FIGURE_IMAGE_NOT_READ,
        }
        if reading is not None and reading.get("model_input"):
            # 画像を返したが届かなかった理由（画像に対応しないモデル・上限など）。
            not_read["figure_image"] = {"model_input": reading["model_input"]}
        updated.append(not_read)
    return {**result, **_judged(result, updated)}


def _judged(result: JsonObject, claims: list[JsonObject]) -> JsonObject:
    """判定の一覧から数え直した `claims`・`counts`・`valid`（RAG の `is_valid` と同じ規則）。"""
    counts: dict[str, int] = {}
    for claim in claims:
        key = str(claim.get("status"))
        counts[key] = counts.get(key, 0) + 1
    valid = (
        result.get("status") == "completed"
        and not _refs_count(result.get("stale_evidence"))
        and not _refs_count(result.get("missing_evidence"))
        and not any(claim.get("status") in INVALID_CLAIM_STATUSES for claim in claims)
        and not error_findings(result)
        and counts.get("supported", 0) > 0
    )
    return {"claims": claims, "counts": counts, "valid": valid}


def figure_read_claims(result: JsonObject) -> list[JsonObject]:
    """図の画像で確かめた段落の判定（`figure_check=image_read`。主張ではない段落は除く）。"""
    claims = result.get("claims")
    return [
        claim
        for claim in (claims if isinstance(claims, list) else [])
        if isinstance(claim, dict)
        and claim.get("figure_check") == FIGURE_IMAGE_READ
        and not claim.get("non_claim")
    ]


def run_evidence_groups(
    steps: list[RunStep],
    answer: str = "",
    carried: dict[str, list[JsonObject]] | None = None,
) -> list[tuple[str, list[JsonObject]]]:
    """その Run で RAG が返した根拠の参照を、MCP 接続ごとにまとめる（#1277）。

    根拠の id は返した RAG のものなので、接続ごとに分けて検証する。返すのは、接続ごとの
    （その接続の最も新しい根拠のツールの function tool の名前〔接続を決める〕, 参照の一覧）で、
    最も新しく根拠を返した接続から順。参照は新しい呼び出しから順、接続の中で重複なし、最大 30 件。
    `rag_read_source`・`rag_read_document`・`rag_read_section`（#1438）で読んだ chunk も根拠に
    入れる（#1345。多段の質問は段の事実を読んで確かめる）。
    `answer` を渡すと、回答が使った根拠を接続の中で先にする（#1364・#1451。多段の質問は前の段の
    根拠が古い呼び出しにあり、上限で切ると回答が使った根拠が検証に入らず、正しい結論が「確かめ
    られない」になる）。回答が `chunk_id` を書いた根拠、回答の本文と当たった根拠（`evidence_match`）
    の順に先にし、文書・章節をまとめて読んだ chunk で回答に当たらないものは検索の根拠の後ろに置く。
    `carried`（`carried_evidence`。#1837）を渡すと、その Run で根拠を返した接続には、同じ会話の前の
    ターンで回答の段落を裏付けた根拠も入れる（回答に当たる根拠の後ろ、残りの検索の根拠の前）。
    その Run で根拠を返さなかった接続には入れない（前のターンの根拠だけで検証するのは、その Run が
    どの接続からも資料の根拠を得なかったときの `carried_evidence_groups`。#1879・#3314）。
    図の画像がモデルへ届いた図の根拠（`figure_readings`。#2678）は、回答が書いた根拠と同じく先にする
    （図の見た目の段落を、上限で切った後の根拠でも図に結び付けられるようにする）。
    """
    attached = {
        (prefix, chunk_id)
        for prefix, found in figure_readings(steps).items()
        for chunk_id, reading in found.items()
        if reading.get("model_input") == ATTACHED
    }
    groups: dict[str, tuple[str, dict[tuple[str, str], _GroupRef]]] = {}
    for step in reversed(steps):
        call, result = step.tool_call, step.tool_result
        if call is None or result is None or not result.success or step.status != "completed":
            continue
        tool = mcp_base_tool_name(call.name)
        if tool not in EVIDENCE_TOOLS and tool not in READ_EVIDENCE_TOOLS:
            continue
        if not isinstance(result.output, dict):
            continue
        refs_found = _evidence_refs(tool, result.output)
        prefix = _connection_prefix(call.name)
        if prefix is None or not refs_found:
            continue
        _tool_name, refs = groups.setdefault(prefix, (call.name, {}))
        for ref, text, labels, file_names in refs_found:
            item = refs.get(ref)
            if item is None:
                item = refs[ref] = _GroupRef(ref=ref, order=len(refs))
            item.evidence.add(text, labels, file_names)
            if tool not in BULK_READ_TOOLS:
                item.bulk_read = False
            if (prefix, ref[1]) in attached:
                item.figure = True
    grouped: list[tuple[str, list[JsonObject]]] = []
    for prefix, (tool_name, refs) in groups.items():
        if not refs:
            continue
        for carried_ref in (carried or {}).get(prefix, []):
            key = (str(carried_ref["document_id"]), str(carried_ref["chunk_id"]))
            item = refs.get(key)
            if item is None:
                item = refs[key] = _GroupRef(ref=key, order=len(refs))
            item.carried = True
        ordered = _ordered(list(refs.values()), answer)[:MAX_VALIDATION_EVIDENCE]
        grouped.append(
            (tool_name, [{"document_id": ref[0], "chunk_id": ref[1]} for ref in ordered])
        )
    return grouped


def carried_evidence_groups(
    carried: dict[str, list[JsonObject]], answer: str = ""
) -> list[tuple[str, list[JsonObject]]]:
    """前のターンの検証で裏付けた根拠だけの、接続ごとの検証の入力（#1879）。

    会話の続きで、今のターンがツールで根拠を得ずに前の回答を整形・言い換えた（「前の回答を表にして」）
    ときに使う（呼ぶかは `builtin_runtime` が決める）。形は `run_evidence_groups` と同じで、接続を
    決める function tool の名前は、前のターンの検証の接続の部分（`carried_evidence` の key）に
    `rag_search` を付けたもの（`builtin_runtime._validator_connection` がその名前で接続を引く）。
    接続の並びは新しいターンの接続から順、参照は `carried_evidence` の順（新しいターンから順）。
    """
    grouped: list[tuple[str, list[JsonObject]]] = []
    for prefix, carried_refs in carried.items():
        refs: dict[tuple[str, str], _GroupRef] = {}
        for carried_ref in carried_refs:
            key = (str(carried_ref["document_id"]), str(carried_ref["chunk_id"]))
            if key not in refs:
                refs[key] = _GroupRef(ref=key, order=len(refs), carried=True)
        if not refs:
            continue
        ordered = _ordered(list(refs.values()), answer)[:MAX_VALIDATION_EVIDENCE]
        grouped.append(
            (
                f"{prefix}{MCP_TOOL_SEPARATOR}{RAG_SEARCH}",
                [{"document_id": ref[0], "chunk_id": ref[1]} for ref in ordered],
            )
        )
    return grouped


def _text_value(value: object, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _completed_outputs(
    steps: list[RunStep], prefix: str, tool: str
) -> list[tuple[JsonObject, JsonObject]]:
    """接続 `prefix` の、成功した `tool` の呼び出しの（引数, 出力）（新しい順）。"""
    found: list[tuple[JsonObject, JsonObject]] = []
    for step in reversed(steps):
        call, result = step.tool_call, step.tool_result
        if call is None or result is None or not result.success or step.status != "completed":
            continue
        if not isinstance(result.output, dict) or _connection_prefix(call.name) != prefix:
            continue
        if mcp_base_tool_name(call.name) == tool:
            found.append((call.arguments, result.output))
    return found


# 業務データの照会の結果（#1950）を最終の検証に渡す上限（RAG の契約の maxItems・maxLength）。
MAX_DATA_RESULTS = 10
_MAX_DATA_QUESTION_CHARS = 1000
_MAX_DATA_SQL_CHARS = 4000
_MAX_DATA_RESULT_CHARS = 4000
# 結果に入れる先頭の行の数（件数の大きい一覧は先頭だけ。全体の件数は total で示す）。
_MAX_DATA_ROWS = 50
_DATA_QUERY_TOOLS = frozenset({"nl2sql_query", "nl2sql_get_job"})


def _data_result_body(output: JsonObject) -> str | None:
    """照会の結果の文（行を返した結果・SQL を作れないと返した結果。それ以外は None）。"""
    status = output.get("status")
    if status == "done":
        rows = output.get("rows")
        if not isinstance(rows, list):
            return None
        payload: JsonObject = {
            "columns": output.get("columns") or [],
            "rows": rows[:_MAX_DATA_ROWS],
            "total": output.get("total"),
            "truncated": bool(output.get("truncated") or output.get("has_more")),
        }
    elif status == "unanswerable":
        requirement = output.get("requirement")
        message = output.get("error_message") or (
            requirement.get("message") if isinstance(requirement, dict) else None
        )
        if not isinstance(message, str) or not message.strip():
            return None
        payload = {"status": "unanswerable", "message": message.strip()}
    else:
        return None
    return json.dumps(payload, ensure_ascii=False, default=str)[:_MAX_DATA_RESULT_CHARS]


def validation_data_results(steps: list[RunStep]) -> list[JsonObject]:
    """この Run で成功した業務データ（NL2SQL）の照会の結果を、検証の `data_results` にする。

    回答の件数・金額・一覧の段落を、資料の根拠だけで「確かめられない」として外さないようにする（#1950）。
    SQL を実行して行を返したもの（`status=done`。結果は列・先頭の行・件数の JSON の文）と、この業務
    プロファイルでは SQL を作れないと返したもの（`status=unanswerable`。結果は状態と理由。「要る列が
    無いので集計できない」の結論の根拠。#2232）。実行の失敗（`error`）・まだ SQL を作っていない確認
    （`needs_clarification`）は入れない。Control Plane が自分で呼んだもの（確認の取り消しなど）は
    入れない。
    """
    results: list[JsonObject] = []
    seen: set[str] = set()
    for step in steps:
        call, result = step.tool_call, step.tool_result
        if call is None or result is None or not result.success or step.status != "completed":
            continue
        if MCP_TOOL_SEPARATOR not in call.name:
            continue
        if mcp_base_tool_name(call.name) not in _DATA_QUERY_TOOLS:
            continue
        output = result.output
        if not isinstance(output, dict):
            continue
        body = _data_result_body(output)
        if body is None:
            continue
        sql = _text_value(
            output.get("executable_sql") or output.get("generated_sql"), _MAX_DATA_SQL_CHARS
        )
        key = f"{sql}\n{body}"
        if key in seen:
            continue
        seen.add(key)
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        results.append(
            {
                "source": "データ問い合わせ（NL2SQL）",
                "question": _text_value(arguments.get("question"), _MAX_DATA_QUESTION_CHARS) or "",
                "sql": sql or "",
                "result": body,
            }
        )
    return results[-MAX_DATA_RESULTS:]


def _guide_input(guide: object, profile_id: object, conditions: object) -> JsonObject | None:
    """`rag_validate_answer` の `guide`（手順を示す判断の業務ガイドで、プロファイルが分かるとき）。

    確かめる質問・担当への引き継ぎの判断（`clarify` / `handoff`）では回答に手順が無いので渡さない
    （影響範囲・承認の検査が、手順を書かない回答を誤って止めるため）。
    """
    if not isinstance(guide, dict) or guide.get("decision", "answer") not in GUIDE_DECISIONS:
        return None
    guide_id = _text_value(guide.get("guide_id"), 64)
    revision = guide.get("revision")
    profile = _text_value(profile_id, 128)
    if guide_id is None or profile is None or not isinstance(revision, int) or revision < 1:
        return None
    known: dict[str, str] = {}
    if isinstance(conditions, dict):
        known.update(
            {
                key: value
                for key, value in conditions.items()
                if isinstance(key, str) and isinstance(value, str)
            }
        )
    for item in guide.get("known_conditions") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            value = item.get("value")
            if isinstance(value, str) and value:
                known[item["id"]] = value
    content: JsonObject = {
        "search_answer_profile_id": profile,
        "guide_id": guide_id,
        "revision": revision,
    }
    if known:
        content["conditions"] = dict(list(known.items())[:MAX_CHECK_ITEMS])
    return content


def connection_check_inputs(steps: list[RunStep], evidence_tool: str) -> JsonObject:
    """`rag_validate_answer` の決定的な検査の入力（`requests`・`gaps`・`guide`。#1276・#1277）と、
    監査の判定を再利用する回答の記録（`answer_trace_ids`。#1527）。

    根拠を返した接続の最も新しい `rag_search` の要求ごとの充足・不足・業務ガイドを渡す。
    `answer_trace_ids` はその接続の `rag_search` の `trace_id`（新しい順、最大 10 件）。
    `rag_search` が業務ガイドを返さなければ、同じ接続の最も新しい `rag_lookup_guides` の最上位の
    業務ガイドを使う。分からない項目は渡さない（その検査は RAG が行わない）。
    """
    prefix = _connection_prefix(evidence_tool)
    if prefix is None:
        return {}
    inputs: JsonObject = {}
    guide: JsonObject | None = None
    searches = _completed_outputs(steps, prefix, RAG_SEARCH)
    # RAG の回答の監査を通った説明と本文・根拠が同じ段落は、RAG がその判定を使いモデルに
    # 渡さない（#1527）。
    # 記録は RAG が Run の利用者の範囲で読み直す（ここは trace_id を渡すだけ）。
    trace_ids = [
        trace_id
        for _arguments, output in searches
        if (trace_id := _text_value(output.get("trace_id"), MAX_TRACE_ID_CHARS))
    ]
    if trace_ids:
        inputs["answer_trace_ids"] = list(dict.fromkeys(trace_ids))[:MAX_ANSWER_TRACE_IDS]
    if searches:
        arguments, output = searches[0]
        requests = [
            {
                "id": request_id,
                "text": _text_value(item.get("text"), 2000) or "",
                "status": item["status"],
            }
            for item in output.get("requests") or []
            if isinstance(item, dict)
            and (request_id := _text_value(item.get("id"), 64))
            and item.get("status") in REQUEST_STATUSES
        ]
        if requests:
            inputs["requests"] = requests[:MAX_CHECK_ITEMS]
        gaps = [gap for item in output.get("gaps") or [] if (gap := _text_value(item, 2000))]
        if gaps:
            inputs["gaps"] = gaps[:MAX_CHECK_ITEMS]
        provenance = output.get("provenance")
        profile = (
            provenance.get("search_answer_profile_id") if isinstance(provenance, dict) else None
        ) or arguments.get("search_answer_profile_id")
        guide = _guide_input(output.get("guide"), profile, arguments.get("conditions"))
    if guide is None:
        lookups = _completed_outputs(steps, prefix, RAG_LOOKUP_GUIDES)
        if lookups:
            arguments, output = lookups[0]
            guides = output.get("guides")
            if isinstance(guides, list) and guides:
                guide = _guide_input(
                    guides[0],
                    arguments.get("search_answer_profile_id"),
                    arguments.get("conditions"),
                )
    if guide is not None:
        inputs["guide"] = guide
    return inputs


def validation_content(
    status: str,
    *,
    reason: str | None = None,
    message: str | None = None,
    connection: str | None = None,
    tool_name: str | None = None,
    step_id: str | None = None,
    evidence: list[JsonObject] | None = None,
    result: JsonObject | None = None,
) -> JsonObject:
    """成果物「資料との照合」の内容（接続ごとの結果にも使う）。"""
    valid = result.get("valid") if isinstance(result, dict) else None
    return {
        "status": status,
        "reason": reason,
        "message": message,
        "valid": valid if isinstance(valid, bool) else None,
        "connection": connection,
        "tool_name": tool_name,
        "step_id": step_id,
        "evidence": list(evidence or []),
        "result": result,
        # 段落の印・外した段落・回答全体の警告（#2738。判定が出たときだけ中身がある）。
        "passage_marks": [],
        "removed_passages": [],
        "answer_warnings": [],
    }


def usable_result(result: object) -> bool:
    """`rag_validate_answer` の結果が判定として使えるか（使えなければ検証の失敗として扱う）。"""
    return isinstance(result, dict) and result.get("status") in _USABLE_RESULT_STATUSES


def _refs_count(value: object) -> int:
    return len([item for item in value if isinstance(item, dict)]) if isinstance(value, list) else 0


def _paragraph_claim(claims: list[JsonObject]) -> JsonObject:
    """1 つの接続の、同じ段落の判定（段落に複数の主張があれば最も厳しいもの）。"""
    for claim in claims:
        if is_contradicted(claim):
            return claim
    for claim in claims:
        if claim.get("status") in INVALID_CLAIM_STATUSES:
            return claim
    return next((claim for claim in claims if claim.get("status") == "supported"), claims[0])


def _claim_range(answer: str, claim: JsonObject) -> tuple[int, int] | None:
    """判定の `answer_range`（契約 rag-tools.json。渡した回答の中の段落の位置）。位置の原文が
    `answer_quote` と違う・範囲が回答の外の判定は None。"""
    value = claim.get("answer_range")
    if not isinstance(value, dict):
        return None
    start, end = value.get("start"), value.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    if isinstance(start, bool) or isinstance(end, bool) or not 0 <= start < end <= len(answer):
        return None
    return (start, end) if answer[start:end] == claim.get("answer_quote") else None


def align_claims(answer: str, result: JsonObject) -> JsonObject:
    """RAG の段落の判定を、この回答の段落（`passage_spans`）に結び付け直した結果（#3240）。

    RAG と Agent の段落の分け方が同じとは限らない（RAG の分け方は RAG の内部の処理で、契約は判定の
    位置 `answer_range` だけを約束する）。判定の位置と重なる Agent の段落ごとに判定を写し、
    `answer_quote` をその段落の原文にする（元の段落の原文は `rag_answer_quote`）。RAG の段落が
    Agent の 2 つの段落にまたがれば両方に同じ判定が付き、Agent の 1 つの段落に RAG の段落が 2 つ
    入れば 2 つの判定が付いて厳しい方が採られる（`_paragraph_claim`。矛盾を本文に残さない）。
    位置の無い判定・位置の原文が `answer_quote` と違う判定・表の値を直した判定（`revised_text`。
    原文を置き換える）は変えない（原文で探す）。
    """
    passages = answer_passage_positions(answer)
    claims: list[object] = []
    changed = False
    for claim in result.get("claims") or []:
        span = _claim_range(answer, claim) if isinstance(claim, dict) else None
        if span is None or not isinstance(claim, dict) or claim.get("revised_text"):
            claims.append(claim)
            continue
        start, end = span
        matched = [text for begin, finish, text in passages if begin < end and start < finish]
        if not matched or matched == [claim.get("answer_quote")]:
            claims.append(claim)
            continue
        changed = True
        claims.extend(
            {**claim, "answer_quote": text, "rag_answer_quote": claim.get("answer_quote")}
            for text in matched
        )
    return {**result, "claims": claims} if changed else result


def merge_results(results: list[JsonObject]) -> JsonObject:
    """接続ごとの `rag_validate_answer` の結果（判定として使えるもの）を 1 つにまとめる（#1277）。

    どの接続の判定も同じ回答の段落に結び付けてある（`align_claims`）ので、段落（`answer_quote`）ごとに
    まとめる。
    接続の中では段落の最も厳しい判定、接続をまたいでは、どれかの接続が矛盾と判定すれば矛盾、
    そうでなくどれかの接続が通せば（裏付けあり・確かめる必要が無い行）その判定、どの接続も
    通さなければ最初の接続の判定（ほかの接続の根拠に無いだけで外さない）。結果が 1 つならそのまま。
    """
    if len(results) == 1:
        return results[0]
    per_result: list[dict[str, JsonObject]] = []
    order: list[str] = []
    for result in results:
        grouped: dict[str, list[JsonObject]] = {}
        for claim in result.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            quote = str(claim.get("answer_quote") or "")
            if quote not in grouped and quote not in order:
                order.append(quote)
            grouped.setdefault(quote, []).append(claim)
        per_result.append({quote: _paragraph_claim(items) for quote, items in grouped.items()})
    claims: list[JsonObject] = []
    for quote in order:
        judged = [item[quote] for item in per_result if quote in item]
        chosen = (
            next((item for item in judged if is_contradicted(item)), None)
            or next(
                (item for item in judged if item.get("status") not in INVALID_CLAIM_STATUSES),
                None,
            )
            or judged[0]
        )
        claims.append(dict(chosen))
    counts: dict[str, int] = {}
    for claim in claims:
        key = str(claim.get("status"))
        counts[key] = counts.get(key, 0) + 1
    stale = [item for result in results for item in result.get("stale_evidence") or []]
    missing = [item for result in results for item in result.get("missing_evidence") or []]
    statuses = {result.get("status") for result in results}
    status = next(item for item in _USABLE_RESULT_STATUSES if item in statuses)
    # 決定的な検査の指摘は接続ごとに同じ回答を確かめたものなので、重複を除いてすべて残す。
    findings: list[JsonObject] = []
    seen_findings: set[tuple[str, str, str]] = set()
    for result in results:
        for item in result.get("findings") or []:
            if not isinstance(item, dict):
                continue
            finding_key = (
                str(item.get("check")),
                str(item.get("code")),
                str(item.get("message")),
            )
            if finding_key not in seen_findings:
                seen_findings.add(finding_key)
                findings.append(item)
    checks = sorted({str(item) for result in results for item in result.get("checks") or []})
    guide_steps = merge_guide_steps(results)
    if guide_steps:
        # 接続のどれかで説明を確かめた手順は、ほかの接続の「説明が無い」を残さない。
        explained = {str(step["step_id"]) for step in guide_steps if step["status"] == "explained"}
        findings = [
            item
            for item in findings
            if item.get("code") != GUIDE_STEP_UNEXPLAINED or item.get("step_id") not in explained
        ]
    valid = (
        status == "completed"
        and not stale
        and not missing
        and not any(claim.get("status") in INVALID_CLAIM_STATUSES for claim in claims)
        and not error_findings({"findings": findings})
        and counts.get("supported", 0) > 0
    )
    return {
        "checks": checks,
        "findings": findings,
        "valid": valid,
        # RAG の回答の監査の判定を使い、モデルに渡さなかった段落の数（#1527。接続ごとの和）。
        "reused_claims": sum(int(result.get("reused_claims") or 0) for result in results),
        "status": status,
        "counts": counts,
        "claims": claims,
        "missing_evidence": missing,
        "stale_evidence": stale,
        "evidence_truncated": any(bool(result.get("evidence_truncated")) for result in results),
        "guide_steps": guide_steps,
    }


def merge_guide_steps(results: list[JsonObject]) -> list[JsonObject]:
    """接続ごとの業務ガイドの手順の充足（`guide_steps`）を、手順ごとに最も良い状態にまとめる（#2708）。"""
    merged: dict[str, JsonObject] = {}
    for result in results:
        for step in result.get("guide_steps") or []:
            if not isinstance(step, dict) or step.get("status") not in GUIDE_STEP_STATUS_ORDER:
                continue
            step_id = str(step.get("step_id") or "")
            current = merged.get(step_id)
            if current is None or GUIDE_STEP_STATUS_ORDER.index(
                step["status"]
            ) < GUIDE_STEP_STATUS_ORDER.index(current["status"]):
                merged[step_id] = dict(step)
    return list(merged.values())


def unexplained_guide_steps(result: JsonObject) -> list[JsonObject]:
    """本文の検査が完了し、不足を確認した必須の手順。

    未実行や候補の対応だけでは不足としない。旧結果の実行状態の欠落も未確認として残す。
    """
    return [
        step
        for step in result.get("guide_steps") or []
        if isinstance(step, dict)
        and step.get("required")
        and step.get("status") in {"partial", "reference_only", "unused"}
        and isinstance(validation := step.get("validation"), dict)
        and isinstance(coverage := validation.get("body_coverage"), dict)
        and coverage.get("execution") == "completed"
        and coverage.get("status") in {"missing", "partial"}
    ]


# 前の段落を受ける接続の語（#1833）。その前の段落を外すと受ける先が無くなる（「したがって、…」
# から本文が始まる）ので、外した段落の直後の段落では語を外す。意味を変える語（「ただし」
# 「なお」）は残す。
_DANGLING_CONNECTIVE = re.compile(
    r"(?:したがって|従って|そのため|このため|よって|ゆえに|以上から|以上より|以上のことから|このように"
    r"|つまり|すなわち|また|さらに|加えて)[、，,]\s*"
)


# 番号で一覧の項目を指す語（「手順 2 の後に」）。本文に残っていれば、項目を外しても番号を振り
# 直さない（指す先が変わるため。#2692）。
_STEP_REFERENCE = re.compile(r"(?:手順|ステップ|step)\s*[0-9０-９]{1,3}", re.IGNORECASE)
_FULLWIDTH_DIGITS = str.maketrans("0123456789", "０１２３４５６７８９")


def _with_number(line: str, number: int) -> str:
    """番号つきの項目の行（`_STEP_ITEM`）の番号を `number` にした行（番号の字の種類は保つ）。"""
    match = _STEP_ITEM.match(line)
    if match is None:
        return line
    name = next(key for key in ("dot", "paren", "circled") if match.group(key))
    old = match.group(name)
    if name == "circled":
        new = chr(ord("①") + number - 1) if 1 <= number <= 20 else old
    else:
        new = str(number)
        if any("０" <= char <= "９" for char in old):
            new = new.translate(_FULLWIDTH_DIGITS)
    return line[: match.start(name)] + new + line[match.end(name) :]


def _renumber(original: list[str], kept: dict[int, str]) -> None:
    """外した項目のある番号つきの一覧の、残った項目の番号を詰める（#2692）。

    `kept` は元の行の番号 → 残った行（書き換える）。

    項目の行が消えたか、番号の印ごと外れた（行の初めの段落を外した）項目を外した項目とする。同じ
    番号の繰り返しは保つ。本文に番号で項目を指す語（`_STEP_REFERENCE`）があれば振り直さない。
    """
    if any(_STEP_REFERENCE.search(line) for line in kept.values()):
        return
    for items in _step_ranges(original):
        numbers: list[tuple[int, int, bool]] = []
        for start, _end in items:
            item = _step_item(original[start])
            now = _step_item(kept[start]) if start in kept else None
            if item is not None:
                numbers.append((start, item[2], now is not None and now[:2] == item[:2]))
        if all(survived for _s, _n, survived in numbers):
            continue
        removed = {number for _s, number, survived in numbers if not survived} - {
            number for _s, number, survived in numbers if survived
        }
        for start, number, survived in numbers:
            shift = sum(1 for item in removed if item < number)
            if survived and shift:
                kept[start] = _with_number(kept[start], number - shift)


def withhold_paragraphs(
    answer: str, quotes: set[str], positions: frozenset[tuple[int, int]] = frozenset()
) -> str | None:
    """回答から `quotes` の段落を外した本文（位置を決められない段落があれば None）。

    `positions` は、原文ではなく位置（行の番号・行の中の開始の位置）で外す段落（`withheld_labels`
    の項目の名前。同じ原文の段落がほかにあっても、その項目だけを外す。#2692）。

    段落の位置は Agent の段落の分け方（`passage_spans`）で決める（文字列の置換では、同じ文を含む
    別の段落まで削ってしまうため。RAG の判定は `align_claims` でこの段落に結び付けてある）。
    段落が無くなった行は消し、続く空行は 1 つにする。段落を外した行に見出し・
    出典だけが残ればその行も消し、中身をすべて外した節の見出しも消す（#1306。番号つきの見出しも
    消す: 中身の無い操作の名前を実行の順の 1 つとして残さない。#2692）。番号つきの一覧の項目を
    外したら、残った項目の番号を詰める（`_renumber`。#1898 の番号の飛びを残さない）。見出し・出典の
    ほかに何も残らなければ空文字を返す（本文を載せない）。外した段落の直後（見出し・出典を挟んでも）の
    段落が前の段落を受ける接続の語（「したがって、」など）で始まれば、その語を外す（#1833）。
    """
    found: set[str] = set()
    headers = table_header_lines(answer)
    # 次の行で決まる番号つき・箇条書きの見出しの行（#1481 / #1486）。
    listed = list_heading_lines(answer)

    def structure(text: str) -> bool:
        return is_structure(text) or text in headers or text in listed

    # （元の行の番号, 行, 見出しの行か, 中身のある行か）。消した見出しは None にする。
    kept: list[tuple[int, str, bool, bool] | None] = []
    heading: int | None = None
    section_content = False
    section_removed = False
    # 直前の（主張の）段落を外したか（次の段落の先頭の接続の語を外す。#1833）。
    dangling = False

    def close_section() -> None:
        if heading is not None and section_removed and not section_content:
            kept[heading] = None

    original = answer.split("\n")
    for index, line in enumerate(original):
        spans = passage_spans(line)

        def removed(span: tuple[int, int, str], index: int = index) -> bool:
            return span[2] in quotes or (index, span[0]) in positions

        withheld = [span for span in spans if removed(span)]
        if not withheld and len(spans) == 1 and (is_heading(spans[0][2]) or spans[0][2] in listed):
            close_section()
            heading, section_content, section_removed = len(kept), False, False
            kept.append((index, line, True, False))
            continue
        # 行の中の消す範囲（外す段落と、外した段落の直後の段落の先頭の接続の語）。
        cuts: list[tuple[int, int]] = []
        for start, end, text in spans:
            if removed((start, end, text)):
                cuts.append((start, end))
                dangling = True
            elif not structure(text):
                connective = _DANGLING_CONNECTIVE.match(text) if dangling else None
                if connective and connective.end() < len(text):
                    cuts.append((start, start + connective.end()))
                dangling = False
        for start, end in reversed(cuts):
            line = line[:start] + line[end:]
        if not withheld:
            content = any(not structure(text) for _s, _e, text in spans)
            section_content = section_content or content
            kept.append((index, line, False, content))
            continue
        found.update(span[2] for span in withheld)
        section_removed = True
        rest = [text for _s, _e, text in passage_spans(line)]
        if line.strip() and any(not structure(text) for text in rest):
            section_content = True
            kept.append((index, line.rstrip(), False, True))
    close_section()
    if quotes - found:
        return None
    lines = [item for item in kept if item is not None]
    if not any(content for _index, _line, _heading, content in lines):
        return ""
    texts = {index: line for index, line, _h, _c in lines}
    _renumber(original, texts)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(texts[index] for index, *_ in lines)).strip()


# 番号つきの一覧の項目の行（「1. 」「2）」「（3）」「④」。字下げ・強調の印を許す。#2604）。
# 「1.5 倍」のような数は項目にしないため、半角の「.」の後は空白を求める。
_STEP_ITEM = re.compile(
    r"(?P<indent>[ \t\u3000]*)(?:\*\*|__)?"
    r"(?:(?P<dot>[0-9０-９]{1,3})(?:\.\s|[)．）]\s*)|[（(](?P<paren>[0-9０-９]{1,3})[）)]\s*"
    r"|(?P<circled>[①-⑳])\s*)\S"
)
# 番号つきの一覧を切る行（Markdown の見出し）。
_SECTION_BREAK = re.compile(r"\s*#{1,6}\s")
# 名前を強調で示すだけの番号つきの項目の行（「1. **名前**」「**1. 名前**」。末尾の「：」を許す。
# #2735）。
_LABEL_ITEM = re.compile(
    r"(?P<open>\*\*|__)?"
    r"(?:[0-9０-９]{1,3}(?:\.\s|[)．）]\s*)|[（(][0-9０-９]{1,3}[）)]\s*|[①-⑳]\s*)"
    r"(?(open)|(?:\*\*|__))[^*_\n]+(?:\*\*|__)\s*[:：]?"
)


@dataclass
class _StepRun:
    """番号つきの一覧の 1 つの並び（外側の一覧。字下げの深い番号は項目の中の行）。"""

    indent: int
    style: str
    last: int
    start: int
    # 最後の項目の行と、その項目が見出しの行（下の行に中身がある形）か。
    last_item: int
    heading_item: bool
    # 項目の行（並びの中の各項目の最初の行）。
    items: list[int]


def _step_item(line: str) -> tuple[int, str, int] | None:
    """番号つきの項目の行なら（字下げの幅・番号の種類・番号）。"""
    match = _STEP_ITEM.match(line)
    if not match:
        return None
    indent = len(match.group("indent").expandtabs(4))
    if match.group("circled"):
        return indent, "circled", ord(match.group("circled")) - ord("①") + 1
    style = "dot" if match.group("dot") else "paren"
    return indent, style, int(match.group("dot") or match.group("paren"))


def _step_ranges(lines: list[str]) -> list[list[tuple[int, int]]]:
    """回答の行のうち、外側の番号つきの一覧ごとの、項目の範囲（[開始の行, 終わりの行)）の並び。

    番号が 1 つずつ増える（同じ番号の繰り返しも許す）同じ字下げ・同じ種類の項目を 1 つの並びにし、
    項目と項目の間の行は前の項目の中身にする。最後の項目の中身は、字下げした行と、空行を挟まずに
    続く行（項目が見出しの行なら、次の見出しの行まで）。Markdown の見出しで並びを切る。
    """
    ranges: list[list[tuple[int, int]]] = []
    run: _StepRun | None = None

    def close(at: int) -> None:
        nonlocal run
        if run is None:
            return
        end = run.last_item + 1
        blank = False
        while end < at:
            line = lines[end]
            if not line.strip():
                blank = True
            elif run.heading_item:
                spans = passage_spans(line)
                if len(spans) == 1 and is_heading(spans[0][2]):
                    break
            elif blank and len(line) - len(line.lstrip()) <= run.indent:
                break
            end += 1
        while end > run.start and not lines[end - 1].strip():
            end -= 1
        bounds = [*run.items[1:], end]
        ranges.append(list(zip(run.items, bounds, strict=True)))
        run = None

    def heading_item(line: str) -> bool:
        spans = passage_spans(line)
        return len(spans) == 1 and (is_heading(spans[0][2]) or spans[0][2] in listed)

    listed = list_heading_lines("\n".join(lines))
    for index, line in enumerate(lines):
        if _SECTION_BREAK.match(line):
            close(index)
            continue
        item = _step_item(line)
        if item is None:
            continue
        indent, style, number = item
        if run is not None and indent > run.indent:
            continue
        if (
            run is not None
            and indent == run.indent
            and style == run.style
            and number in {run.last, run.last + 1}
        ):
            run.last, run.last_item, run.heading_item = number, index, heading_item(line)
            run.items.append(index)
            continue
        close(index)
        run = _StepRun(indent, style, number, index, index, heading_item(line), [index])
    close(len(lines))
    return ranges


def _label_item(line: str, listed: set[str]) -> bool:
    """番号つきの項目の行が、名前だけを示す見出しの形か（中身は下の行。#2735）。

    強調だけの名前（「1. **名前**」）か見出しの段落で、操作の指示（`is_operation_step`）ではない
    もの。「1. **申請書を提出する**」のように項目の行が操作なら、その項目は 1 つの手順。
    """
    spans = passage_spans(line)
    if len(spans) != 1:
        return False
    text = spans[0][2]
    shaped = bool(_LABEL_ITEM.fullmatch(text)) or is_heading(text) or text in listed
    return shaped and not is_operation_step(text)


def dependent_steps(
    answer: str, quotes: set[str], operations: frozenset[str] = frozenset()
) -> list[tuple[int, int, str]]:
    """段落（`quotes`。矛盾で外した段落か未確認の段落）に続く、番号つきの手順の段落（行の番号・行の
    中の開始の位置・原文。#2604）。

    番号つきの一覧で、利用者に操作・作業を行わせる段落（`is_operation_step`。手順の前提の確認など）
    が根拠と矛盾する・確かめられないなら、それより後の操作の段落は、その前提に続く操作になる。
    返した段落は本文に残し、警告の印（前の手順が根拠と矛盾・前提の手順が未確認）を付ける（#2738）。
    `operations` は、操作の形ではなくても操作を指す段落（操作を名前だけで示す項目。
    `withheld_labels`）。`quotes` の段落が操作ではない（事実・結果・状態を述べる）ときは、後の手順の
    前提は崩れないので何も返さない（#2735）。

    返すのは後の操作の段落だけで、事実を述べる段落は、操作の項目の中にあっても返さない（#2735）。
    名前だけの項目の行（「2. **訂正**」。`_label_item`）は、その項目の主張の段落をすべて返すときだけ
    返す（印は主張の段落だけに付ける）。

    どこまでを続く手順とみなすかは一覧の構造で決める（語は見ない。#2735）。項目の行に中身を書いた
    一覧（「1. 〇〇を開く。」「2. 〇〇を入力する。」）と、名前の項目の下に中身が 1 行ずつの一覧
    （「1. **確認**」の下に 1 つの操作）は、順に行う手順として、その操作より後の項目の操作も返す。
    名前の項目の下に中身が複数の行ある一覧（並列の方法・場合分けを、項目ごとの手順で並べたもの）は、
    各項目が 1 つの節（Markdown の見出しで切った節と同じ）として独立するので、項目をまたがず、同じ
    項目の中のその操作より後の操作だけを返す。

    一覧の外の段落（前の説明・一覧の後の段落）、一覧の中の `quotes` の段落より前の段落、主張では
    ない段落（出典の行・利用者への質問・不足の文など。`non_claim_passages`）は返さない。番号の無い箇条書きは
    順序を表さないので対象にしない。`quotes` に入っている段落は返さない。
    """
    lines = answer.split("\n")
    kinds = non_claim_passages(answer)
    listed = list_heading_lines(answer)
    structure = table_header_lines(answer) | listed

    def claim(index: int, text: str) -> bool:
        return _step_item(lines[index]) is not None or not (
            kinds.get(text) or is_structure(text) or text.strip() in structure
        )

    dependents: list[tuple[int, int, str]] = []
    for items in _step_ranges(lines):
        labels = [_label_item(lines[start], listed) for start, _end in items]
        # 名前の項目の下の、主張の段落のある中身の行の数（項目ごと）。
        bodies = [
            sum(
                any(claim(index, text) for _b, _e, text in passage_spans(lines[index]))
                for index in range(start + 1, end)
            )
            for start, end in items
        ]
        sections = all(labels) and max(bodies) > 1
        withheld = False
        for (start, end), label in zip(items, labels, strict=True):
            if sections:
                withheld = False
            spans = [
                (index, begin, text)
                for index in range(start, end)
                for begin, _end, text in passage_spans(lines[index])
            ]
            found: list[tuple[int, int, str]] = []
            for index, begin, text in spans:
                if text in quotes:
                    withheld = withheld or is_operation_step(text) or text in operations
                elif withheld and is_operation_step(text) and claim(index, text):
                    found.append((index, begin, text))
            rest = [
                (index, begin, text)
                for index, begin, text in spans[1:]
                if text not in quotes and (index, begin, text) not in found and claim(index, text)
            ]
            if found and label and not rest and spans[0][2] not in quotes:
                found.insert(0, spans[0])
            dependents += found
    return dependents


def withheld_labels(answer: str, quotes: set[str]) -> list[tuple[int, int, str]]:
    """根拠と矛盾して外す操作の段落（`quotes`）と同じ操作を名前だけで示す、番号つきの一覧の項目
    （行の番号・行の中の開始の位置・原文。#2692。外すのは矛盾のときだけ。#2738）。

    「1. **対象 A の属性訂正**」のような名前だけの項目（下に中身の無い項目。`list_item_label`）は
    主張として根拠と矛盾しなくても、実行の順の 1 つとして読める。外す段落が操作
    （`is_operation_step`）で、その操作を名前が指すとき（`label_operation_end`）、項目も外す。
    1 つの段落を複数の名前が指すときは、段落の文末の述語に近い名前（主の操作）だけを外す。
    下に中身のある見出しの項目は、中身をすべて外したときに `withhold_paragraphs` が消す（中身の
    正しい内容は残す）。章題・説明の名前（段落と語の重ならない名前）は外さない。
    """
    operations = [quote for quote in quotes if is_operation_step(quote)]
    if not operations:
        return []
    lines = answer.split("\n")
    labels: list[tuple[int, int, str, str]] = []
    for items in _step_ranges(lines):
        for start, end in items:
            spans = passage_spans(lines[start])
            if len(spans) != 1 or any(lines[index].strip() for index in range(start + 1, end)):
                continue
            begin, _end, text = spans[0]
            label = list_item_label(text)
            if label is not None and text not in quotes:
                labels.append((start, begin, text, label))
    chosen: dict[tuple[int, int], str] = {}
    for quote in operations:
        ends = [
            (position, item)
            for item in labels
            if (position := label_operation_end(item[3], quote)) is not None
        ]
        if ends:
            last = max(position for position, _item in ends)
            chosen.update(
                {(item[0], item[1]): item[2] for position, item in ends if position == last}
            )
    return [(index, begin, text) for (index, begin), text in sorted(chosen.items())]


def without_lookup_notes(answer: str) -> str:
    """回答から、調べるのに使ったツール・プロファイル・内部の ID だけを述べる行を外す（#2328）。

    最終の検証の前（と、検証をしない設定でも）に組み込み Runtime が呼ぶ。資料の主張では
    ないので検証に渡さず、「資料で確認できず除いた内容」にも書かない（内部の ID を利用者に
    見せない）。外すと本文が残らない・位置を決められないときはそのまま返す。
    """
    quotes = {
        text
        for line in answer.split("\n")
        for _start, _end, text in passage_spans(line)
        if is_lookup_note(text)
    }
    if not quotes:
        return answer
    body = withhold_paragraphs(answer, quotes)
    return body or answer


def error_findings(result: JsonObject) -> list[JsonObject]:
    """決定的な検査の error の指摘（valid にしない。回答全体の警告にする。warning は出さない）。"""
    missing = {str(step.get("step_id")) for step in unexplained_guide_steps(result)}
    return [
        item
        for item in result.get("findings") or []
        if isinstance(item, dict)
        and item.get("severity") == "error"
        and (item.get("code") != GUIDE_STEP_UNEXPLAINED or str(item.get("step_id")) in missing)
    ]


def mark_non_claims(answer: str, result: JsonObject) -> JsonObject:
    """判定の段落のうち、主張ではない段落（見出し・出典・表の形・質問・不足の文・最終の検証が足した
    節）に `non_claim`（判定の種類）を付けた結果の写しを返す（#1306。
    `answer_passages.non_claim_passages`）。

    RAG の判定（`status`）は変えずに残し、段落の印と外すかどうかの判断だけが `non_claim` の段落を
    除く。利用者に現場のデータ・記録の確認を求める段落には、判定とは別に
    `environment_check: true` を付ける（#1317。主張のまま確かめ、回答の対応だけが使う）。
    """
    kinds = non_claim_passages(answer)
    checks = environment_check_passages(answer)
    claims: list[object] = []
    for item in result.get("claims") or []:
        if isinstance(item, dict):
            quote = str(item.get("answer_quote") or "")
            item = {
                key: value
                for key, value in item.items()
                if key not in {"non_claim", "environment_check"}
            }
            if kinds.get(quote):
                item["non_claim"] = kinds[quote]
            if quote in checks:
                item["environment_check"] = True
        claims.append(item)
    return {**result, "claims": claims}


def reason_item(code: str, detail: object = "") -> JsonObject:
    """印・外した段落・回答全体の警告の理由 1 件（契約の `reasons` の項目）。"""
    label = (
        PASSAGE_REASONS[code][1]
        if code in PASSAGE_REASONS
        else REMOVED_REASONS.get(code) or ANSWER_WARNING_REASONS[code]
    )
    return {"code": code, "label": label, "detail": _short(detail, _MAX_DETAIL_CHARS)}


def claim_reason(claim: JsonObject) -> str | None:
    """本文に残す段落の判定の、印の理由の code（印にしない判定は None）。

    根拠と矛盾する段落（`is_contradicted`）は呼ぶ側が先に分ける（本文から外す）。RAG が監査の
    裏付けを採らなかった理由（`support_check`）のうち、別の対象・表の行の対応は警告、写した原文が
    根拠に無いのは未確認にする。図の画像を読めていない図の見た目の段落は `figure_not_read`。
    出典を照合できない段落は、引用が根拠に無ければ `quote_unmatched`、監査の元の判定が
    根拠に無い（`audit_status=unsupported`）なら `unsupported`。
    """
    status = claim.get("status")
    if status == "supported" or claim.get("non_claim"):
        return None
    if claim.get("figure_check") == FIGURE_IMAGE_NOT_READ:
        return "figure_not_read"
    support = claim.get("support_check")
    if support in _SUPPORT_CHECK_REASONS and status in INVALID_CLAIM_STATUSES:
        return str(support)
    if status == "citation_error":
        if claim.get("reference_check") == "quote_unmatched":
            return "quote_unmatched"
        if claim.get("audit_status") == "unsupported":
            return "unsupported"
    return _STATUS_REASONS.get(str(status))


def mark_state(reasons: list[JsonObject]) -> str:
    """理由の並びの印の状態（未確認を警告より優先する）。"""
    return (
        MARK_UNVERIFIED
        if any(PASSAGE_REASONS[str(item["code"])][0] == MARK_UNVERIFIED for item in reasons)
        else MARK_WARNING
    )


def answer_warnings(result: JsonObject) -> list[JsonObject]:
    """回答全体の警告（契約の `answer_warnings`。本文は置き換えない。#2738）。

    根拠を 1 件も読めなかった（`no_evidence`）、決定的な検査の error（要求の不足は `requests`、
    業務ガイドの手順・影響範囲・版は `guide_check`）、本文の検査で不足を確認した業務ガイドの必須の
    手順（`guide_check`）。未実行・未確認の充足は診断に残す。
    `detail` は RAG の指摘の文。同じ理由と文は 1 つにする。
    """
    warnings: list[JsonObject] = []
    seen: set[tuple[str, str]] = set()

    def add(code: str, detail: object = "") -> None:
        item = reason_item(code, detail)
        key = (code, str(item["detail"]))
        if key not in seen:
            seen.add(key)
            warnings.append(item)

    if result.get("status") == RESULT_NO_EVIDENCE:
        add("no_evidence")
    for item in error_findings(result):
        add("requests" if item.get("check") == "requests" else "guide_check", item.get("message"))
    missing = {str(step.get("step_id")) for step in unexplained_guide_steps(result)}
    for item in result.get("findings") or []:
        if (
            isinstance(item, dict)
            and item.get("code") == GUIDE_STEP_UNEXPLAINED
            and str(item.get("step_id")) in missing
            and item.get("message")
        ):
            add("guide_check", item["message"])
    return warnings


def _paragraph_order(answer: str) -> dict[str, int]:
    """回答の段落（原文）の出てきた順。"""
    order: dict[str, int] = {}
    for line in answer.split("\n"):
        for _start, _end, text in passage_spans(line):
            order.setdefault(text, len(order))
    return order


def claim_paragraphs(answer: str) -> list[str]:
    """回答の主張の段落（見出し・出典・表の形・質問・不足の文・節の行・名前だけの番号つきの項目を
    除く。出てきた順、重複なし）。
    """
    kinds = non_claim_passages(answer)
    structure = table_header_lines(answer) | list_heading_lines(answer)
    return [
        text
        for text in _paragraph_order(answer)
        if text.strip()
        and not kinds.get(text)
        and not is_structure(text)
        and text not in structure
        and not _LABEL_ITEM.fullmatch(text)
    ]


def _without_removed(answer: str, quotes: set[str], labels: list[tuple[int, int, str]]) -> str:
    """回答から根拠と矛盾した段落（と、その操作を名前だけで示す項目）を外した本文。

    段落の位置は Agent の段落の分け方で決める（`withhold_paragraphs`）。RAG の判定に位置が無く
    位置を決められない段落は、原文の一致で除く（矛盾した内容を本文に残さない）。
    """
    located = set(_paragraph_order(answer))
    positions = frozenset((index, begin) for index, begin, _text in labels)
    body = withhold_paragraphs(answer, quotes & located, positions) or ""
    for quote in sorted(quotes - located, key=len, reverse=True):
        body = body.replace(quote, "")
    return re.sub(r"\n{3,}", "\n\n", body).strip()


@dataclass
class PublishedAnswer:
    """最終の検証の判定から組み立てた、利用者に見せる回答と段落の印（#2738）。

    `body` は末尾の節の前の本文（何も変えなければ回答のまま）、`sections` は末尾に足す節
    （「根拠と矛盾し除いた内容」「確認が必要な点」）。
    """

    body: str
    sections: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    passage_marks: list[JsonObject] = field(default_factory=list)
    removed_passages: list[JsonObject] = field(default_factory=list)
    answer_warnings: list[JsonObject] = field(default_factory=list)
    # 節の項目（箇条書きの行）の数。
    listed: int = 0

    @property
    def text(self) -> str:
        """利用者に見せる回答（本文と末尾の節）。"""
        if not self.sections:
            return self.body
        return f"{self.body.rstrip()}\n\n" + "\n\n".join(self.sections)

    @property
    def withheld(self) -> JsonObject:
        """成果物の `withheld`（外した段落・未確認・警告の段落の数と、節の項目の数）。"""
        states = [str(item["state"]) for item in self.passage_marks]
        return {
            "removed": len(self.removed_passages),
            "unverified": states.count(MARK_UNVERIFIED),
            "warnings": states.count(MARK_WARNING),
            "all": False,
            "listed": self.listed,
        }

    def record(self) -> JsonObject:
        """成果物「資料との照合」に足す内容（印・外した段落・回答の警告・数・節の見出し）。"""
        content: JsonObject = {
            "passage_marks": self.passage_marks,
            "removed_passages": self.removed_passages,
            "answer_warnings": self.answer_warnings,
            "withheld": self.withheld,
        }
        if self.headings:
            # 回答の本文と節を分ける見出し（`heading` は本文の終わりの節）。
            content["notice"] = {
                "items": self.listed,
                "heading": self.headings[0],
                "headings": list(self.headings),
            }
        return content


def _dependent_marks(answer: str, found: list[tuple[int, int, str]]) -> list[str]:
    """続く手順の段落（`dependent_steps`）のうち、印を付ける主張の段落（名前だけの項目などを除く）。"""
    claims = set(claim_paragraphs(answer))
    return list(dict.fromkeys(text for _index, _begin, text in found if text in claims))


def _section_lines(
    result: JsonObject,
    removed: list[JsonObject],
    marks: list[JsonObject],
    warnings: list[JsonObject],
) -> tuple[list[str], list[str]]:
    """末尾の 2 つの節の項目（すべての段落・警告を 1 行ずつ。「ほか N 件」で切らない。#2361）。

    検証のモデルが書いた段落の理由（`detail`）は節に出さない（#1833。内部の語を含む。記録には
    残る）。回答の警告の `detail` は RAG の決定的な検査の文なので出す。根拠を 1 件も読めなかった
    ときは、すべての段落が未確認なので段落の一覧は出さない。
    """
    removed_lines = [
        f"- {item['reasons'][0]['label']}: 「{_short(item['text'], _MAX_QUOTE_CHARS)}」"
        for item in removed
    ]
    review = [
        f"- {item['label']}"
        + (f": {_short(item['detail'], _MAX_FINDING_CHARS)}" if item["detail"] else "")
        for item in warnings
    ]
    if result.get("status") != RESULT_NO_EVIDENCE:
        review += [
            f"- {'・'.join(str(reason['label']) for reason in item['reasons'])}: "
            f"「{_short(item['text'], _MAX_QUOTE_CHARS)}」"
            for item in marks
        ]
    stale = _refs_count(result.get("stale_evidence"))
    if stale:
        review.append(f"- 根拠の {stale} 件は文書の古い版です。最新の版で確かめ直してください。")
    missing = _refs_count(result.get("missing_evidence"))
    if missing:
        review.append(f"- 根拠の {missing} 件は見つかりません（削除された・参照できない）。")
    return removed_lines, review


def _apply_table_revisions(
    answer: str, result: JsonObject
) -> tuple[str, JsonObject, dict[str, JsonObject]]:
    """RAG が根拠の表の正しい値に直した段落（`revised_text`。#3005）を、本文の段落と置き換える。

    直した段落は根拠と矛盾しないので外さず、「根拠の表の値に直した」の警告の印を付ける（黙って
    直さない）。段落の原文が本文に 1 回だけ現れるときだけ置き換える。戻り値は (本文, 判定, 直した
    段落 → 印の理由)。
    """
    corrected: dict[str, JsonObject] = {}
    claims: list[Any] = []
    for claim in result.get("claims") or []:
        if not isinstance(claim, dict):
            claims.append(claim)
            continue
        quote = str(claim.get("answer_quote") or "")
        revised = str(claim.get("revised_text") or "").strip()
        if (
            claim.get("support_check") == "table_corrected"
            and revised
            and quote.strip()
            and answer.count(quote) == 1
        ):
            answer = answer.replace(quote, revised, 1)
            corrected[revised] = reason_item("table_corrected", claim.get("reason"))
            claim = {
                **claim,
                "answer_quote": revised,
                "status": "supported",
                "audit_status": "supported",
            }
        claims.append(claim)
    return answer, {**result, "claims": claims}, corrected


def publish_answer(answer: str, result: JsonObject) -> PublishedAnswer:
    """判定（判定として使える結果）から、利用者に見せる回答と段落の印を組み立てる（#2738）。

    - 根拠と矛盾する段落（`contradicted`。元の判定が矛盾のものを含む）だけを本文から外し、
      `removed_passages` と節「根拠と矛盾し除いた内容」に理由つきで示す。外した操作を名前だけで
      示す番号つきの項目（`withheld_labels`）と、中身をすべて外した見出しも外し、残った項目の
      番号を詰める（`withhold_paragraphs`）。
    - 根拠で確かめられない・出典を確かめられない・確かめが終わっていない・図の画像を読めていない
      段落は本文に残し、未確認（`unverified`）の印を付ける。RAG の規則の検査（別の対象・表の行の
      対応）は警告（`warning`）の印にする（`claim_reason`）。
    - 番号つきの手順で、外した操作・未確認の操作に続く操作の段落は本文に残し、警告の印
      （`after_contradicted_step` / `after_unverified_step`）を付ける（`dependent_steps`）。
    - 決定的な検査の error・説明を確かめられない業務ガイドの手順・根拠を読めない（`no_evidence`。
      すべての主張の段落を未確認にする）は本文を置き換えず、回答全体の警告（`answer_warnings`）に
      する。
    - 印・回答の警告・読めない根拠は節「確認が必要な点」に並べる（本文だけを読む MCP の利用者にも
      分かるように）。主張ではない段落（見出し・出典・質問・不足の文・節の行。#1306）には印を
      付けない。
    """
    answer, result, corrected = _apply_table_revisions(answer, result)
    claims = [item for item in mark_non_claims(answer, result)["claims"] if isinstance(item, dict)]
    # 表の見出しの行・次の行で決まる一覧の見出しの行・名前だけの番号つきの項目（中身は下の行）には
    # 印を付けない。
    structure = table_header_lines(answer) | list_heading_lines(answer)
    grouped: dict[str, list[JsonObject]] = {}
    for claim in claims:
        quote = str(claim.get("answer_quote") or "")
        if quote.strip() and not claim.get("non_claim"):
            grouped.setdefault(quote, []).append(claim)
    removed: dict[str, list[JsonObject]] = {}
    marks: dict[str, list[JsonObject]] = {}

    def add(text: str, reason: JsonObject) -> None:
        reasons = marks.setdefault(text, [])
        if all(item["code"] != reason["code"] for item in reasons):
            reasons.append(reason)

    for text, reason in corrected.items():
        add(text, reason)
    for quote, items in grouped.items():
        contradicted = next((item for item in items if is_contradicted(item)), None)
        if contradicted is not None:
            removed[quote] = [reason_item("contradicted", contradicted.get("reason"))]
            continue
        if is_structure(quote) or quote in structure or _LABEL_ITEM.fullmatch(quote):
            continue
        for item in items:
            code = claim_reason(item)
            if code is not None:
                add(quote, reason_item(code, item.get("reason")))
    if result.get("status") == RESULT_NO_EVIDENCE:
        for text in claim_paragraphs(answer):
            if text not in removed:
                add(text, reason_item("unsupported"))
    labels = withheld_labels(answer, set(removed)) if removed else []
    names = frozenset(text for _index, _begin, text in labels)
    if removed:
        found = dependent_steps(answer, set(removed) | names, names)
        for text in _dependent_marks(answer, found):
            if text not in removed:
                add(text, reason_item("after_contradicted_step"))
    unverified = {text for text, reasons in marks.items() if mark_state(reasons) == MARK_UNVERIFIED}
    if unverified:
        for text in _dependent_marks(answer, dependent_steps(answer, unverified)):
            if text not in removed:
                add(text, reason_item("after_unverified_step"))
    order = _paragraph_order(answer)

    def ordered(items: dict[str, list[JsonObject]]) -> list[str]:
        return sorted(items, key=lambda text: order.get(text, len(order)))

    removed_passages = [{"text": text, "reasons": removed[text]} for text in ordered(removed)]
    passage_marks = [
        {"text": text, "state": mark_state(marks[text]), "reasons": marks[text]}
        for text in ordered(marks)
    ]
    warnings = answer_warnings(result)
    removed_lines, review_lines = _section_lines(result, removed_passages, passage_marks, warnings)
    sections: list[str] = []
    headings: list[str] = []
    if removed_lines:
        headings.append(REMOVED_HEADING)
        sections.append(f"**{REMOVED_HEADING}**\n\n" + "\n".join([REMOVED_INTRO, *removed_lines]))
    if review_lines:
        headings.append(REVIEW_HEADING)
        sections.append(f"**{REVIEW_HEADING}**\n\n" + "\n".join([REVIEW_INTRO, *review_lines]))
    body = _without_removed(answer, set(removed), labels) if removed else answer
    return PublishedAnswer(
        body=body or ALL_REMOVED,
        sections=sections,
        headings=headings,
        passage_marks=passage_marks,
        removed_passages=removed_passages,
        answer_warnings=warnings,
        listed=len(removed_lines) + len(review_lines),
    )


def _with_figure_note(body: str, claims: list[JsonObject]) -> tuple[str, list[JsonObject]]:
    """図の画像で確かめた段落が本文に残れば、本文の後（末尾の節の前）に `FIGURE_READ_NOTE` を足す
    （#2678）。返すのは本文と、本文に残った段落と画像の参照の一覧。
    """
    kept = [
        {
            "answer_quote": claim.get("answer_quote"),
            "chunk_id": claim.get("chunk_id"),
            "image": claim.get("figure_image"),
        }
        for claim in claims
        if str(claim.get("answer_quote") or "") and str(claim.get("answer_quote")) in body
    ]
    if not kept:
        return body, []
    return f"{body.rstrip()}\n\n{FIGURE_READ_NOTE}", kept


def combine_validations(
    answer: str,
    validations: list[JsonObject],
    figures: dict[str, dict[str, JsonObject]] | None = None,
) -> tuple[JsonObject, str]:
    """接続ごとの内容（`validation_content`）を 1 つにし、利用者に見せる回答を返す（#1277）。

    - どれかの接続を確かめられなかった（`unvalidated`）: 全体も `unvalidated` で、回答は消さずに
      照合を終えられなかったこと（`UNVERIFIED_NOTICE`）を足す（基盤の障害で内容を落とさない）。
    - すべての接続で判定が出た: 段落ごとにまとめた判定で回答を組み立て（`publish_answer`）、
      段落の印・外した段落・回答の警告（#2738）を成果物に残す。

    `figures` は MCP 接続ごとの図の画像の読み取り（`figure_readings` と `carried_figure_readings`。
    #2678）。接続の結果の図の見た目の段落（`figure_only`）を、図の画像がモデルへ届いたかで判定し直して
    からまとめる（`apply_figure_readings`。渡さなければ、どの図の画像も届いていないものとして扱う）。
    図の画像で確かめた段落が回答に残れば、その旨の注記を本文の後に足し、段落と画像の参照を成果物の
    `figure_confirmed` に残す。
    """
    validations = [
        (
            {
                **item,
                "result": apply_figure_readings(
                    # RAG の段落の判定を位置でこの回答の段落に結び付ける（#3240）。
                    align_claims(answer, item["result"]),
                    (figures or {}).get(
                        _connection_prefix(str(item.get("tool_name") or "")) or "", {}
                    ),
                ),
            }
            if item["status"] == STATUS_COMPLETED and isinstance(item.get("result"), dict)
            else item
        )
        for item in validations
    ]
    single = validations[0] if len(validations) == 1 else None
    common: JsonObject = {
        "connection": single["connection"] if single else None,
        "tool_name": single["tool_name"] if single else None,
        "step_id": single["step_id"] if single else None,
        "evidence": [ref for item in validations for ref in item["evidence"]],
    }
    failed = next((item for item in validations if item["status"] != STATUS_COMPLETED), None)
    if failed is not None:
        content = validation_content(
            STATUS_UNVALIDATED,
            reason=failed["reason"],
            message=failed["message"],
            result=single["result"] if single else None,
            **common,
        )
        published = with_unverified_notice(answer)
    else:
        merged = mark_non_claims(answer, merge_results([item["result"] for item in validations]))
        content = validation_content(STATUS_COMPLETED, result=merged, **common)
        review = publish_answer(answer, merged)
        review.body, confirmed = _with_figure_note(review.body, figure_read_claims(merged))
        if confirmed:
            content["figure_confirmed"] = confirmed
        content.update(review.record())
        published = review.text
    content["connections"] = validations
    return content, published


def with_unverified_notice(answer: str) -> str:
    return f"{answer.rstrip()}\n\n{UNVERIFIED_NOTICE}"


def with_no_evidence_notice(answer: str) -> str:
    """資料の根拠を使わずに作った回答の末尾に、確かめていないことを足す（#1277）。"""
    return f"{answer.rstrip()}\n\n{NO_EVIDENCE_NOTICE}"
