"""RAG が Agent に公開する MCP ツール（#232）。

入口は `POST /api/mcp`（`app.api.routes.mcp`）。利用者はサービストークンの `sub`（Agent の Run の
利用者）で、権限・検索・回答プロファイル / ナレッ
ジベースの対象範囲は画面と同じ判定を使う。各ツールは

既存の route と同じ関数（rate limit・timeout・範囲の判定を含む）を呼ぶだけにする。

- `rag_list_search_answer_profiles`（検索・回答プロファイル一覧の route と同じ権限）:
  `search_answer_profiles.list_search_answer_profiles`（ACTIVE のみ）
- `rag_recommend_search_answer_profile`（`menu.search`）: 質問から検索・回答プロファイルを推薦する
  （`app.rag.profile_recommendation`。使えるプロファイルの参照先を 1 回 hybrid 検索し、当たった
  資料と名前・説明で決める。LLM は呼ばない。候補が割れたら推薦を null にして候補を返す。#1850）
- `rag_search`（`menu.search`）: `search._run_search_with_timeout`。根拠は `evidence`（場所・版・
  回答に使ったか・切り詰めの有無。#1219）で返す。
- `rag_read_source`（`menu.search`）: 根拠の本文の続きと親の本文を読む（検索と同じ見え方の
  条件。#1219）。`include_image=true` で図の根拠の元の画像の領域を、上限つきの PNG で MCP の
  content の image として返す（読むたびに同じ見え方の条件を確かめ直す。#1282）
- `rag_outline` / `rag_read_document`（`menu.search`）: 文書の章節の構成と、頁・章節・定位子・
  続きの位置から本文を順に読む（検索と同じ見え方の条件。#1332）。章節は文書の詳細の
  「章節ナビゲーション」が正本（#1438。`app.rag.section_map_service`）
- `rag_read_section`（`menu.search`）: 章節（section_id か、根拠の chunk の所属の章節。`up` で
  親の章節）の本文を章節の終わりまで順に読む（#1438）

RAG のチャットは画面の機能で、MCP では提供しない（#787）。MCP で提供するのは検索と根拠の
読み取りだけにする。

サービストークンにデータの範囲の claim（`profile_ids`。業務 Agent の定義。#1379）があれば、
すべてのツールを「利用者の権限 ∩ 範囲」で判定する（`app.mcp.profile_scope`）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast, get_args

from fastapi import HTTPException, Request
from pr_backend_core.api import OffsetParams
from pr_backend_core.api.validation import validation_tool_errors
from pr_backend_core.mcp import (
    TOOL_ARGUMENTS_INVALID_CODE,
    TOOL_FORBIDDEN_CODE,
    TOOL_INTERNAL_ERROR_CODE,
    McpServer,
    McpTool,
    McpToolError,
    McpToolResult,
)
from pr_backend_core.source_text import (
    SOURCE_TEXT_ITEM_MAX_TOKENS,
    SOURCE_TEXT_RESPONSE_BUDGET_TOKENS,
    SourceText,
    SourceTextItem,
    SourceTextKind,
    fit_source_texts,
    read_source_window,
)
from pr_backend_core.token_estimate import estimate_input_tokens
from pr_system_settings.auth.service_token import PROFILE_SCOPE_FORBIDDEN_CODE
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from rag_engine.generation.operation_audit import answer_passage_ranges
from rag_engine.generation.table_binding import evidence_tables
from rag_engine.knowledge.runtime_knowledge import (
    MAX_MATCHED_RULES,
    MAX_MATCHED_TERMS,
    RuntimeRule,
    RuntimeTerm,
)
from rag_engine.retrieval.entity_expansion import entity_expansion_relevant
from rag_parser_core.extraction import is_derived_content
from rag_parser_core.parsed_table import ROW_BASIS_PARSED_TABLE
from rag_parser_core.sheet_records import (
    ROW_BASIS_CSV,
    ROW_BASIS_KEY,
    ROW_BASIS_SHEET,
    ROW_BASIS_WORD_TABLE,
    SOURCE_LINE_END_KEY,
    SOURCE_LINE_START_KEY,
    TABLE_CELLS_KEY,
    TABLE_INDEX_KEY,
    TABLE_ROW_END_KEY,
    TABLE_ROW_KEY,
    TABLE_ROW_START_KEY,
)

from app.api.errors import DocumentFileMissingError
from app.api.routes import documents as documents_route
from app.api.routes import search as search_route
from app.api.routes import search_answer_profiles as search_answer_profiles_route
from app.clients.oci_genai import OciGenAiClient
from app.clients.oracle import OracleClient
from app.clients.section_map_store import SectionChunkRow, SectionMapStore
from app.config import get_settings
from app.mcp.profile_scope import (
    ProfileScope,
    check_profile,
    check_search,
    within_profile_scope,
)
from app.rag.answer_engine import (
    ANSWER_VALIDATION_STEP,
    CATALOG_ARTICLE_ROLE,
    AnswerScope,
    _answer_step_span,
)
from app.rag.answer_failure import ANSWER_API_FAILED_ERROR_CODE, describe_answer_api_failure
from app.rag.answer_validation import (
    MAX_ANSWER_TRACE_IDS,
    MAX_DATA_RESULTS,
    MAX_PREMISES,
    EvidenceRef,
    GuideCheckRef,
    GuideProfileNotFoundError,
    is_premise_id,
    validate_answer,
)
from app.rag.cross_references import (
    REFERENCE_FROM_KEY,
    REFERENCE_LABEL_KEY,
    REFERENCE_RANK_KEY,
    document_name_match,
    document_name_references,
    reference_targets,
    section_path_within,
    split_section_path,
)
from app.rag.derived_evidence import is_derived_chunk
from app.rag.document_crop import (
    BoundedCrop,
    CropTooLargeError,
    DocumentSourceNotFoundError,
    crop_png_bounded,
    load_parsed_source,
    page_sizes,
)
from app.rag.document_sections_service import current_section_pages
from app.rag.element_locator import (
    LOCATOR_MAX_LENGTH,
    chunk_element_locator,
    parse_element_locator,
)
from app.rag.figure_region import (
    FigureRegion,
    bbox_numbers,
    figure_evidence_type,
    figure_region,
)
from app.rag.figure_url import (
    FIGURE_URL_TTL_SECONDS,
    FigureUrlUnavailableError,
    issue_figure_token,
)
from app.rag.observability import new_trace_id
from app.rag.profile_recommendation import (
    RECOMMEND_LIMIT_DEFAULT,
    RECOMMEND_LIMIT_MAX,
    ProfileCandidate,
    recommend_search_answer_profiles,
)
from app.rag.question_understanding import profile_language_scope
from app.rag.rate_limit import enforce_rate_limit
from app.rag.rule_selection import apply_rule_selection_scope, recognize_rule_selections
from app.rag.search_answer_profile_config import resolve_search_answer_profile_settings
from app.rag.search_answer_profile_knowledge import (
    clarification_document_ids,
    load_runtime_knowledge_payload,
    match_runtime_knowledge,
    resolve_clarification,
    rule_answer,
    rule_matches,
    suggest_clarification,
)
from app.rag.section_catalog import compact_section_lines
from app.rag.section_chunk_map import ROOT_SECTION_ID, SectionNode
from app.rag.section_map_service import (
    SectionOutline,
    chunk_section,
    evidence_sections,
    section_outline,
)
from app.rag.signed_url import SignedUrlUnavailableError
from app.rag.source_preview import (
    SOURCE_PREVIEW_TTL_SECONDS,
    PreviewKind,
    issue_source_preview_token,
    preview_kind,
)
from app.rag.stored_answer import (
    PASSAGE_MARK_RECORD_KEYS,
    passage_mark_record,
    stored_citation_metadata,
)
from app.rag.support_guide_conditions import resolve_answered_conditions
from app.rag.support_guide_query import build_guide_query
from app.rag.support_guide_runtime import (
    GuideMatch,
    clarification_questions,
    impact_applies,
    rank_guides,
    visible_plan,
)
from app.schemas.document import DocumentPreprocessArtifact
from app.schemas.search import (
    INCLUDE_SUPERSEDED_FILTER_KEY,
    PAGE_RANGES_FILTER_KEY,
    SEARCH_TOP_K_DEFAULT,
    SEARCH_TOP_K_MAX,
    RetrievedChunk,
    SearchRequest,
    SearchResponse,
)
from app.schemas.search_answer_profile import (
    SearchAnswerProfileStatus,
    SearchAnswerProfileSummary,
)
from app.schemas.support_guide import SupportGuideReference
from app.search_defaults import search_top_k_default, search_top_k_schema
from app.security.permissions import MENU_SEARCH, ROUTE_PERMISSIONS

logger = logging.getLogger(__name__)

MCP_SERVER_NAME = "production-ready-rag"
# ツールの出力の版（出力の形を変えたら上げる。handoff §10。#1276）。
# 6: 根拠の場所に要素の定位子（locator.element_locator）を足した（#1330）。
# 7: 文書を順に読むツール（rag_outline / rag_read_document）を足した（#1332）。
# 8: 根拠の結果に、当たった文書の旧版（older_versions）を足した（#1405）。
# 9: 章節ナビゲーションを正本にした（#1438）。rag_outline を章節の階層・section_id で返し、
#    rag_read_section を足し、根拠に所属の章節（section）を足した。
# 10: rag_read_document / rag_read_section の chunks に表計算の場所（sheet_name・row_start・
#     row_end・cell_range）を足した（#1471）。
# 11: rag_validate_answer に、監査の判定を再利用した段落の数（reused_claims）を足した（#1527）。
# 12: 質問から検索・回答プロファイルを推薦するツール（rag_recommend_search_answer_profile）を
#     足した（#1850）。
# 13: rag_search の入力に skip_clarification、確認（clarifications）に multiple を足し、検索・回答
#     プロファイルのルールの回答前の確認を返すようにした（#2117）。
# 14: rag_outline に format（compact の目録 lines）・section_id・lead_chars を足した（#2252）。
# 15: rag_retrieve_evidence の根拠に、条の目録で選んだ条（role=catalog_article）を足した（#2254）。
# 16: rag_list_search_answer_profiles に、query が当たったか（query_matched）と次の手（next_step）を
#     足した（当たらなければ使える全件を返す）。rag_search / rag_retrieve_evidence に、id を
#     誤ったときに自動で選んだ検索・回答プロファイル（auto_selected_search_answer_profile）を
#     足した（#2355）。
# 17: rag_search に回答の注意書き（notice。API キーで呼んだ外部のクライアントだけ）を足した
#     （#2405）。
# 18: rag_lookup_guides の手順に目的・検索の手がかり・参照の資料と頁・必要な根拠・終わりの条件を、
#     ガイドに分岐（branches）・共通の資料（references）・完了の条件（completion）を足し、
#     分かっている条件で外れた分岐の手順を返さないようにした（#2396）。
# 19: rag_validate_answer の claims に、監査が返した判定（audit_status）と根拠の ID の照合の結果
#     （reference_check）を足し、ID を照合できない矛盾を矛盾のまま返すようにした（#2622）。
# 20: rag_validate_answer の claims に、段落の分類の照合の結果（classification_check）を
#     足した（#2625）。
# 21: rag_retrieve_evidence に、呼び出し元が検索文を作る契約（入力の planning・queries・request_id、
#     出力の planning・request_id・clarifications）を足し、根拠に検索の順位（retrieval_rank）と
#     本文の由来（origin）を足した（#2651）。
# 22: rag_validate_answer の claims に、裏付けを採らなかった理由（support_check）を足した（#2659）。
# 23: rag_retrieve_evidence に、元の質問に当たる検索・回答プロファイルの承認済みのルールと用語の
#     説明（profile_knowledge）を足した（#2701）。
# 24: rag_validate_answer に、業務ガイドの手順ごとの説明の充足（guide_steps）と、説明の無い
#     必須の手順の finding（guide_step_unexplained）を足し、手順の名前の一致だけで
#     guide_steps_unmatched にしないようにした（#2708）。
# 25: rag_validate_answer の status に、時間の上限までに一部の段落の監査が終わらなかった
#     partial と、どの段落も終わらなかった timed_out を足した（#2700）。
# 26: rag_lookup_guides の参照（references）に、表計算の資料のシートと行の範囲（sheet_ranges）を
#     足した（#2663）。
# 27: rag_validate_answer の claims の status に、図の見た目だけで決まる主張（figure_only）を、
#     reference_check に not_figure・single_figure を、support_check に figure_recheck を
#     足した（#2678）。
# 28: rag_search に、段落の印（passage_marks）・根拠と矛盾して除いた段落（removed_passages）・
#     回答全体の警告（answer_warnings）を足した。answer の末尾に「根拠と矛盾し除いた内容」
#     「確認が必要な点」の節が付く（#2738）。
# 29: rag_validate_answer の claims の support_check に、段落が勧める内容が利用者の確認の答えの
#     条件・制約と両立しない premise_conflict（status は contradicted）を足した。rag_lookup_guides・
#     rag_search の conditions で、選択式の条件に選択肢そのものでない答えを渡すと、答えが選んだ
#     選択肢に決めるようにした（#2726）。
# 30: rag_validate_answer の claims で、段落が根拠の表の別の行・列の値を結び付けた
#     （value_unbound）とき、status を unsupported から contradicted にし、理由に表の正しい対応を
#     書くようにした（#2998）。
# 31: rag_validate_answer の claims に、段落の表の対応の取り違えを表の正しい値に直した文
#     （revised_text。support_check は table_corrected）を足した（#3005）。
# 32: 根拠の excerpt を先頭 1000 文字から、既定で全文にした。応答の根拠の合計に予算を置いて順位の
#     順に入れ、入らない根拠は本文を省く（text_omitted。省いた数は evidence_text_omitted）。1 件の
#     上限を超える根拠だけ、質問に当たる部分を中心に文・段落（表は見出しの行を残して行）の境界で
#     切り、範囲（excerpt_range・header_range）と続きの位置（next_offset）を足した（#3321）。
# 33: 根拠の場所（locator）に、表の 1 行の根拠のセルの座標（table_row と、値のあるセルの列・表頭の
#     段の table_cells）を足した。取込み直す前の chunk・位置が原本で決まらない表は null / 空
#     （#3262）。
# 34: 読み取りのツール（rag_read_source・rag_read_section・rag_read_document）の本文を、上限の
#     文字数に入る最後の文・段落（表は行）の境界で切るようにした。表の続きは先頭に見出しの行を
#     付け、その位置（rag_read_source と読み取りの chunks の header_range）を足した（#3344）。
# 35: 根拠の場所（locator・読み取りの chunks）に行の番号の基準（row_basis。表計算のシート・CSV の
#     記録の番号・Word の表の行）と表の番号（table_index）、CSV の原本の物理行の範囲
#     （source_line_start・source_line_end）を足し、CSV の 1 行の根拠にセルの座標
#     （table_row・table_cells）、Word の 1 つの表の根拠に表の行の範囲を入れた。根拠に、表の行・
#     列の対応の検査が読める表か（table_binding）を、rag_search・rag_retrieve_evidence に、失敗して
#     縮退した任意の補助の処理（degraded_steps）を足した（#3262）。
# 36: 行の番号の基準に、解析が組み立てた表の格子（parsed_table。PDF・HTML・画像の表。原本の座標では
#     ない）を足し、その根拠に表の番号・格子の行の範囲、1 つのデータの行の根拠にセルの座標と、
#     解析が出したときはセルの領域（table_cells の bbox）を入れた（#3429）。
# 37: rag_validate_answer の claims に、渡した answer の中の段落の位置（answer_range）を足した。
#     rag_search・rag_retrieve_evidence の top_k の既定（省略時の件数）を契約に出した。契約に
#     ツールの失敗の code の一覧（errors）を足した（#3240）。
# 38: rag_search・rag_retrieve_evidence に、安全チェックを完了できず止めたか
#     （guardrail_degraded）と、根拠 0 件のときに 0 件になった段と件数（zero_evidence）を足した
#     （#3546。#3262 の残り）。
MCP_OUTPUT_SCHEMA_VERSION = 38
# 根拠の本文の 1 件の上限と、応答の根拠の合計の予算（token の見積もり。3 製品で共通の規則。
# 値と決め方は pr_backend_core.source_text。#3321）。
EVIDENCE_TEXT_ITEM_MAX_TOKENS = SOURCE_TEXT_ITEM_MAX_TOKENS
EVIDENCE_TEXT_BUDGET_TOKENS = SOURCE_TEXT_RESPONSE_BUDGET_TOKENS
EVIDENCE_LIMIT_DEFAULT = 12
EVIDENCE_LIMIT_MAX = 50
# rag_retrieve_evidence（回答を作らない根拠の収集）の既定の件数。検索する件数（SearchRequest の
# top_k の既定。#3512 で 50）と同じにし、検索で当たった chunk を既定で全部返す（#1365）。
# rag_search は回答に使った根拠が先に並ぶので 12 のまま。返す本文の合計は
# EVIDENCE_TEXT_BUDGET_TOKENS で抑える。
RETRIEVE_EVIDENCE_LIMIT_DEFAULT = min(SEARCH_TOP_K_DEFAULT, EVIDENCE_LIMIT_MAX)


def _retrieve_evidence_default() -> int:
    return min(search_top_k_default(), EVIDENCE_LIMIT_MAX)


def _retrieve_evidence_default_schema(schema: dict[str, Any]) -> None:
    schema["default"] = _retrieve_evidence_default()


# rag_retrieve_evidence の planning=caller（呼び出し元が検索文を作る。#2651）で query に足せる
# 検索文の本数と 1 本の長さの上限（全文検索に使う検索文の本数の上限 6 に、query を合わせて収める）。
CALLER_QUERIES_MAX = 5
CALLER_QUERY_MAX_CHARS = 1000
# rag_retrieve_evidence が返す検索・回答プロファイルのルール・用語（profile_knowledge。#2701）の
# 上限。件数は RAG が回答を作るときに prompt へ入れる件数と同じ。字数はルールの本文・用語の説明など
# 返す文字の合計で、超える項目は返さずに省いた数へ数える。
PROFILE_KNOWLEDGE_RULES_MAX = MAX_MATCHED_RULES
PROFILE_KNOWLEDGE_TERMS_MAX = MAX_MATCHED_TERMS
PROFILE_KNOWLEDGE_MAX_CHARS = 4000
# 根拠の結果に示す旧版（older_versions。#1405）の最大件数。
OLDER_VERSIONS_MAX = 10
# rag_read_source が 1 回で返す本文・親の本文の上限。
READ_SOURCE_MAX_CHARS_DEFAULT = 8000
READ_SOURCE_MAX_CHARS_LIMIT = 20000
# rag_read_document が 1 回で読む chunk の数の単位と、rag_outline が返す章節の上限（#1332）。
READ_DOCUMENT_BATCH = 50
OUTLINE_MAX_SECTIONS = 300
# rag_outline の compact（条の目録。#2252）の文字数の上限と、本文の書き出しの文字数。
OUTLINE_COMPACT_MAX_CHARS = 40000
OUTLINE_LEAD_CHARS_DEFAULT = 60
OUTLINE_LEAD_CHARS_MAX = 200
# rag_read_section が返す子の章節の上限と、up で上がれる段の上限（章節の階層は 1〜6）。
READ_SECTION_MAX_CHILDREN = 50
READ_SECTION_MAX_UP = 6
CURSOR_INVALID_CODE = "cursor_invalid"
# 続きの位置（next_cursor）。モデルが書き写して次の呼び出しに渡す文字列なので、短い平文にする
# （#2499）: rag_read_section は「<chunk_set の先頭>:<章節の id>:<章節の中の順>:<文字の位置>」、
# rag_read_document は「<chunk_set の先頭>:<chunk の順>:<文字の位置>」。chunk_set は先頭の字から
# 文書の chunk_set を引き直し、作り直された後（引けない）は source_stale にする。
CURSOR_CHUNK_SET_CHARS = 12
CURSOR_MAX_LENGTH = 256
SECTION_NOT_FOUND_CODE = "section_not_found"
SOURCE_NOT_FOUND_CODE = "source_not_found"
SOURCE_STALE_CODE = "source_stale"
# 見つからない・使えない検索・回答プロファイルの id（#2355）。message に次の手を書く。
SEARCH_ANSWER_PROFILE_NOT_FOUND_CODE = "search_answer_profile_not_found"
# 図の元の画像（rag_read_source の include_image。#1282）。長い辺は VLM が縮めずに読める大きさ、
# バイト数は 1 回の応答に収まる大きさにする。画像は structuredContent に入れず、MCP の content の
# image で返す（呼び出し側のモデルの文脈を base64 で膨らませない）。
IMAGE_MAX_EDGE_PX = 1568
IMAGE_MAX_BYTES = 1_500_000
IMAGE_MIME_TYPE: Literal["image/png"] = "image/png"
IMAGE_NOT_AVAILABLE_CODE = "image_not_available"
IMAGE_TOO_LARGE_CODE = "image_too_large"
IMAGE_SOURCE_MISSING_CODE = "image_source_missing"
# 図をブラウザで開く短命の URL（rag_read_source の include_image_url。#1311）。
IMAGE_URL_NOT_ALLOWED_CODE = "image_url_not_allowed"
IMAGE_URL_UNAVAILABLE_CODE = "image_url_unavailable"
# 根拠の原文のプレビュー（rag_read_source の include_preview。#2389）。
PREVIEW_NOT_ALLOWED_CODE = "preview_not_allowed"
PREVIEW_UNAVAILABLE_CODE = "preview_unavailable"
PREVIEW_FILE_MISSING_CODE = "preview_file_missing"
# ツールの失敗（`isError: true` の結果の `error_code`）の一覧と意味（#3240）。呼び出し側
# （Agent など）はここに載せた code だけで分岐する。一覧は契約（rag-tools.json の `errors`）に
# 書き出し、`*_CODE` の定数とツールが raise する code がすべて載ることをテストで確かめる。
MCP_ERROR_CODES: dict[str, str] = {
    TOOL_ARGUMENTS_INVALID_CODE: (
        "引数が inputSchema に合わない（details.errors に項目ごとの誤り）。"
    ),
    TOOL_FORBIDDEN_CODE: "呼び出した利用者にツールの権限が無い。",
    TOOL_INTERNAL_ERROR_CODE: "ツールの処理が想定外の理由で失敗した（理由は返さない）。",
    PROFILE_SCOPE_FORBIDDEN_CODE: (
        "検索・回答プロファイル・ナレッジベース・文書が、呼び出し元のデータの範囲の外にある。"
    ),
    SEARCH_ANSWER_PROFILE_NOT_FOUND_CODE: (
        "検索・回答プロファイルが無いか使えない（message に使える候補と次の手）。"
    ),
    ANSWER_API_FAILED_ERROR_CODE: "回答の生成の AI の呼び出しが失敗した。",
    SOURCE_NOT_FOUND_CODE: "根拠・文書・頁・処理レシピが見つからない（定位子の形の誤りを含む）。",
    SOURCE_STALE_CODE: "文書の版（chunk_set）が作り直され、根拠の id・定位子・続きの位置が古い。",
    SECTION_NOT_FOUND_CODE: "章節が無いか、本文が無い。",
    CURSOR_INVALID_CODE: "続きの位置（next_cursor）の形が違う（message に直し方）。",
    IMAGE_NOT_AVAILABLE_CODE: "根拠に図の画像が無い（図の根拠でない・領域が分からない）。",
    IMAGE_TOO_LARGE_CODE: "図の画像が 1 回の応答の上限を超える。",
    IMAGE_SOURCE_MISSING_CODE: "図の元のファイルが保存先に無い。",
    IMAGE_URL_NOT_ALLOWED_CODE: "図を開く URL をこの呼び出しでは作れない（画面の操作だけ）。",
    IMAGE_URL_UNAVAILABLE_CODE: "図を開く URL を今は作れない（一時的）。",
    PREVIEW_NOT_ALLOWED_CODE: "原文のプレビューをこの呼び出しでは作れない（画面の操作だけ）。",
    PREVIEW_UNAVAILABLE_CODE: "原文のプレビューを今は作れない（一時的）。",
    PREVIEW_FILE_MISSING_CODE: "原文のプレビューのファイルが保存先に無い。",
}
# ブラウザで開く URL（図・原文のプレビュー）を作ってよい呼び出し（Agent の画面の操作）の、
# サービストークンの
# claim `purpose` の値。Run の中でモデルが呼んだ token（purpose 無し）では作らない。
FIGURE_URL_PURPOSE = "figure_url"
SOURCE_PREVIEW_PURPOSE = "source_preview"
BROWSER_URL_PURPOSES = frozenset({FIGURE_URL_PURPOSE, SOURCE_PREVIEW_PURPOSE})
# 図の画像の読み取りの path（`app.api.routes.figures`。/api の下）。
FIGURE_URL_PATH_PREFIX = "/api/figures"
# 原文のプレビューの読み取りの path（`app.api.routes.source_previews`。/api の下）。
SOURCE_PREVIEW_PATH_PREFIX = "/api/source-previews"
# 根拠の種類（#1282）。figure_description=図を VLM が読んだ説明（元の図で確かめる）/
# ocr=図の中・周りの文字（解析の OCR・キャプション）。
EvidenceType = Literal["text", "table", "figure_description", "ocr"]
# rag_search の conditions のうち、検索・回答プロファイルのルールの確認の答え（#2117）。
RULE_CONDITION_PREFIX = "rule:"
GUIDES_UNAVAILABLE_MESSAGE = "業務ガイドを読み込めませんでした。時間をおいて再度お試しください。"

SEARCH_ANSWER_PROFILE_READ_PERMISSIONS = ROUTE_PERMISSIONS[("GET", "/search-answer-profiles")]
SEARCH_PERMISSIONS = frozenset({MENU_SEARCH})

# 処理順の Step 形式（docs/agents/prompts.md。#3830）。
INSTRUCTIONS = (
    "Production Ready RAG の検索・回答のツールです。\n\n"
    "## Step 1: 検索・回答プロファイルを選ぶ\n"
    "検索・回答プロファイルが決まっていなければ、まず rag_recommend_search_answer_profile に"
    "質問を渡して選び（recommended_profile_id が null で候補が複数なら、候補の名前を示して利用者に"
    "確かめる）、その id を rag_search に渡してください。名前で探すときは"
    " rag_list_search_answer_profiles を使います。\n\n"
    # 回答前の確認（業務ガイドの条件・検索・回答プロファイルのルールの確認。#2117）。
    # 引数の組み立ては conditions・skip_clarification の説明にある。
    "## Step 2: 回答前の確認に答える\n"
    "rag_search が outcome=needs_clarification を返したら、clarifications の問いと選択肢を"
    "利用者に確かめてから、答えを conditions に入れて呼び直してください。\n\n"
    # evidence は回答で引用した箇所だけではない（AI に渡した箇所の全体。#2040）。
    "## Step 3: 結果の根拠を読む\n"
    "evidence は AI に渡した資料の箇所です。used_in_answer は回答で引用した箇所、"
    "answer_use はその使われ方（confirmed=質問の条件への該当確認済みの記載 / "
    "reference=該当が確認できていない参考の記載）です。"
    # 根拠の続き・章節・図の読み方は rag_read_source・rag_read_section・rag_outline の説明にある。
)


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


# 前後の空白を除き、空文字は未指定として扱う文字列。
OptionalText = Annotated[str | None, AfterValidator(_strip_or_none)]


# ---- 入力 ----


class ListSearchAnswerProfilesInput(BaseModel):
    """検索・回答プロファイル一覧の条件。"""

    query: OptionalText = Field(
        default=None,
        max_length=200,
        description=(
            "名前・説明の部分一致（省略時は全件。当たらなければ全件を返し、query_matched が"
            " false）。"
        ),
    )
    limit: int = Field(default=50, ge=1, le=200, description="返す最大件数。")


class RecommendSearchAnswerProfileInput(BaseModel):
    """質問から検索・回答プロファイルを推薦する条件（question・profile_ids）。"""

    # NL2SQL の nl2sql_recommend_profile と同じ形（#1850）。

    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="利用者の質問（指示語・省略は前の質問で補い、それだけで意味の通る文にする）。",
    )
    profile_ids: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="推薦の候補にする検索・回答プロファイルの id（省略時は使えるすべて）。",
    )
    limit: int = Field(
        default=RECOMMEND_LIMIT_DEFAULT,
        ge=1,
        le=RECOMMEND_LIMIT_MAX,
        description="返す候補の最大件数。",
    )

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("質問を入力してください。")
        return cleaned

    @field_validator("profile_ids")
    @classmethod
    def _profile_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        ids = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not ids:
            raise ValueError("profile_ids に検索・回答プロファイルの id を指定してください。")
        return ids


class SearchInput(BaseModel):
    """検索・回答の条件。旧 scope を含む未定義項目は拒否する。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=8000, description="質問文。")
    original_question: OptionalText = Field(
        default=None,
        max_length=8000,
        description=(
            "利用者の元の質問。query を検索用に短く言い換えたときに、言い換える前の全文を渡す"
            "（番号つきの小問は番号のまま）。検索は query で行い、回答は元の質問の要求ごとに"
            "根拠を確かめて作る。条で構成された文書の根拠の条は query と合わせて選ぶ。"
        ),
    )
    search_answer_profile_id: OptionalText = Field(
        default=None,
        max_length=128,
        description=(
            "検索・回答プロファイルの id。参照するナレッジベースと検索・回答設定を使う。"
            "呼び出し元（業務 Agent）にデータの範囲があるときは必須。id を推測で作らない。"
            "見つからない id なら、使えるプロファイルが 1 つならそれを、そうでなければ質問から"
            "推薦したものを使い、auto_selected_search_answer_profile に示す（決まらなければ"
            "候補つきのエラー）。"
        ),
    )
    knowledge_base_ids: list[str] = Field(
        default_factory=list,
        max_length=200,
        description=(
            "検索するナレッジベースの id（省略時は検索・回答プロファイルの参照先）。"
            "呼び出し元にデータの範囲があるときは、検索・回答プロファイルの参照先の中だけ。"
        ),
    )
    # 既定を契約に出す（#3240。呼び出し側が説明の文から既定を写さない）。null は省略と同じ。
    top_k: int | None = Field(
        default_factory=search_top_k_default,
        ge=1,
        le=SEARCH_TOP_K_MAX,
        json_schema_extra=search_top_k_schema,
        description="検索する件数。",
    )
    filters: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "検索フィルター（例: category_name）。旧版を含める指定は filters ではなく"
            " include_superseded で渡す。"
        ),
    )
    include_superseded: bool = Field(
        default=False,
        description=(
            "新しい版に置き換えた文書（旧版）も検索するか。既定（false）は今の版だけを検索する。"
            "旧版・変更点・改定前との違いを尋ねるとき、旧版の年度・版を名指しするとき"
            "（「2025 年度と 2026 年度では」「2023 年度版の規程では」）は true を渡す（検索の"
            "範囲は変わらず、検索・回答プロファイル・ナレッジベースの中の旧版だけが加わる）。"
            "結果の older_versions に、当たった文書の旧版（文書名・タイトル）が出る。"
        ),
    )
    evidence_limit: int = Field(
        default=EVIDENCE_LIMIT_DEFAULT,
        ge=1,
        le=EVIDENCE_LIMIT_MAX,
        description=(
            "返す根拠の最大件数。回答に使った根拠、検索で当たった根拠（関連度の順）、前後の文脈の"
            "順に並べてから切る。"
        ),
    )
    conditions: dict[str, str] = Field(
        default_factory=dict,
        max_length=30,
        description=(
            "業務ガイドの条件と、検索・回答プロファイルのルールの確認（rule: で始まる id）の値"
            "（条件の id → 値）。outcome=needs_clarification の clarifications に利用者が答えた値"
            "（選んだ選択肢の表示名・自由入力。複数は「、」で区切る）を入れて呼び直すと、"
            "分かっている条件は聞き直さない。"
        ),
    )
    skip_clarification: bool = Field(
        default=False,
        description=(
            "true なら検索・回答プロファイルのルールの回答前の確認を出さずに回答する"
            "（利用者が確認せずに回答を選んだとき・利用者に確認できないとき）。"
        ),
    )

    @field_validator("filters")
    @classmethod
    def _superseded_is_not_a_filter(cls, value: dict[str, str]) -> dict[str, str]:
        # 旧版を含める指定は専用の真偽値の引数（#1392）。filters に書いた指定は黙って
        # 読み替えず、呼び直す引数を返す（モデルが同じ誤りを繰り返さないように）。
        if INCLUDE_SUPERSEDED_FILTER_KEY in value:
            raise ValueError(
                "旧版を含めるときは filters ではなく include_superseded に true を渡してください。"
            )
        return value


# 呼び出し元（業務 Agent）が検索文を作る契約（planning=caller。#2651）は、入力を役割で分ける:
# 元の質問（original_question）・呼び出し元の検索文（query と queries）・対象（検索・回答
# プロファイル / ナレッジベース）・フィルター（filters・include_superseded）・件数（top_k・
# evidence_limit）・追跡 ID（request_id）。対象と認可は RAG が強制し、渡した対象を広げない。
class RetrieveEvidenceInput(SearchInput):
    """根拠の収集の条件（rag_search の項目に、検索文を作る主体の指定を足す）。"""

    query: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description=(
            "検索文。planning=caller はこの文と queries だけで検索する（言い換えない）。"
            "planning=rag は質問文でよい。"
        ),
    )
    # 回答を作らないので、元の質問は条の選択（とルールの確認）にだけ使う
    # （rag_search と説明を分ける。#2665）。
    original_question: OptionalText = Field(
        default=None,
        max_length=8000,
        description=(
            "利用者の元の質問。query を検索用に短く言い換えたときに渡す。条で構成された文書の"
            "根拠の条を選ぶときに query と合わせて使う（検索は query で行う）。"
        ),
    )
    planning: Literal["rag", "caller"] = Field(
        default="rag",
        description=(
            "検索文を作る主体。rag=RAG が質問に応じて言い換え・関連の条の選択（AI）を足して"
            "検索する。caller=呼び出し元が作った query と queries だけで検索し、RAG は質問に"
            "応じた AI の処理をしない（用語の別名・rerank・前後の文脈の取得は同じ）。"
        ),
    )
    queries: list[Annotated[str, Field(min_length=1, max_length=CALLER_QUERY_MAX_CHARS)]] = Field(
        default_factory=list,
        max_length=CALLER_QUERIES_MAX,
        description=(
            "query に足す検索文（planning=caller のときだけ）。同じ根拠を別の観点・別表記・資料の"
            f"表記で言い表した文。最大 {CALLER_QUERIES_MAX} 本。"
        ),
    )
    request_id: OptionalText = Field(
        default=None,
        max_length=128,
        description="呼び出し元の追跡 ID（結果の request_id にそのまま返す）。",
    )
    conditions: dict[str, str] = Field(
        default_factory=dict,
        max_length=30,
        description=(
            "planning=caller のとき、検索・回答プロファイルのルールの確認（rule: で始まる id）の"
            "答え（clarifications の condition_id → 利用者が答えた値。複数は「、」で区切る）。"
        ),
    )
    skip_clarification: bool = Field(
        default=False,
        description=(
            "planning=caller のとき、true ならルールの確認を出さずに検索する"
            "（利用者が確認せずに進めると選んだとき・利用者に確認できないとき）。"
        ),
    )
    evidence_limit: int = Field(
        default_factory=_retrieve_evidence_default,
        json_schema_extra=_retrieve_evidence_default_schema,
        ge=1,
        le=EVIDENCE_LIMIT_MAX,
        description=(
            "返す根拠の最大件数。検索で当たった根拠（関連度の順）、前後の文脈の順に並べてから切る。"
            "省略時は設定の検索する件数（top_k）を基準に、この件数の上限まで返し、"
            "検索で当たった根拠を全部返す。"
        ),
    )

    @model_validator(mode="after")
    def _queries_need_caller_planning(self) -> RetrieveEvidenceInput:
        # planning=rag は RAG が検索文を足すので、呼び出し元の検索文を黙って捨てず・混ぜずに
        # 直し方を返す（#2651）。
        if self.queries and self.planning != "caller":
            raise ValueError(
                "queries は planning=caller のときだけ渡せます。"
                "planning に caller を渡すか、queries を外して呼び直してください。"
            )
        return self


class LookupGuidesInput(BaseModel):
    """質問に当たる業務ガイドを引く条件。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=8000, description="質問文（利用者の目的）。")
    search_answer_profile_id: str = Field(
        ..., min_length=1, max_length=128, description="検索・回答プロファイルの id。"
    )
    conditions: dict[str, str] = Field(
        default_factory=dict, max_length=30, description="分かっている条件の値（条件の id → 値）。"
    )
    limit: int = Field(default=3, ge=1, le=10, description="返す業務ガイドの最大件数。")


class EvidenceRefInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(..., min_length=1, max_length=128)
    chunk_id: str = Field(..., min_length=1, max_length=512)


class ValidateRequestInput(BaseModel):
    """AnswerEnvelope の要求 1 件（rag_search の requests と同じ形）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=64, description="要求の ID（Q1 など）。")
    text: str = Field(default="", max_length=2000, description="要求の原文。")
    status: Literal["addressed", "partial", "missing", "unknown"] = Field(
        description=(
            "充足（addressed=答えた / partial=一部 / missing=答えていない / unknown=未確認）。"
        )
    )


class ValidateGuideInput(BaseModel):
    """回答が沿った業務ガイド（rag_search の guide・rag_lookup_guides の guides の 1 件）。"""

    model_config = ConfigDict(extra="forbid")

    search_answer_profile_id: str = Field(
        ..., min_length=1, max_length=128, description="業務ガイドの検索・回答プロファイルの id。"
    )
    guide_id: str = Field(..., min_length=1, max_length=64, description="業務ガイドの id。")
    revision: int = Field(..., ge=1, description="回答に使った公開の版。")
    conditions: dict[str, str] = Field(
        default_factory=dict,
        max_length=30,
        description=(
            "分かっている条件の値（条件の id → 値）。当たらない分岐の手順を見分けるのに使う。"
        ),
    )


class ValidatePremiseInput(BaseModel):
    """利用者が回答の前の確認で答えた前提 1 件（確認の質問と答え）。"""

    # 前提を根拠と並べて確かめる（#1898）。

    model_config = ConfigDict(extra="forbid")

    question: str = Field(default="", max_length=1000, description="確認の質問の文。")
    answer: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="利用者の答え（選んだ選択肢の名前・自由入力）。",
    )


class ValidateDataResultInput(BaseModel):
    """業務データの照会の結果 1 件（照会の質問・SQL・結果）。"""

    # 業務データの結果を根拠と並べて確かめる（#1950）。

    model_config = ConfigDict(extra="forbid")

    source: str = Field(
        default="",
        max_length=200,
        description="照会の出所（例: データ問い合わせ（NL2SQL）の業務プロファイル名）。",
    )
    question: str = Field(default="", max_length=1000, description="照会の質問の文。")
    sql: str = Field(default="", max_length=4000, description="実行した SQL。")
    result: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="結果（列と行。JSON などの文）。",
    )


class ValidateAnswerInput(BaseModel):
    """回答の最終の検証の条件（requests・guide は渡したときだけ確かめる）。"""

    # #1246。requests・guide の決定的な確認は #1276。

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=8000, description="利用者の質問。")
    answer: str = Field(..., min_length=1, max_length=20000, description="検証する回答の本文。")
    evidence: list[EvidenceRefInput] = Field(
        ...,
        min_length=1,
        max_length=30,
        description=(
            "回答の根拠（rag_search / rag_retrieve_evidence が返した document_id と chunk_id）。"
            "本文はサーバーが今の権限と版で読み直す。"
        ),
    )
    requests: list[ValidateRequestInput] | None = Field(
        default=None,
        max_length=30,
        description=(
            "回答の要求ごとの充足（AnswerEnvelope の requests）。渡すと partial・missing の要求を"
            "確かめる（回答に不足として示していなければ error）。"
        ),
    )
    gaps: list[Annotated[str, Field(max_length=2000)]] = Field(
        default_factory=list,
        max_length=30,
        description="回答に示した資料から確かめられない点（AnswerEnvelope の gaps）。",
    )
    guide: ValidateGuideInput | None = Field(
        default=None,
        description=(
            "回答が沿った業務ガイド。渡すと公開の版を読み直し、手順ごとの説明の有無・手順の順序・"
            "依存・分岐と影響範囲・承認の記載を確かめる。"
        ),
    )
    answer_trace_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list,
        max_length=MAX_ANSWER_TRACE_IDS,
        description=(
            # その回答の監査を通った段落は、根拠を今の権限と版で読み直して本文が変わっていなければ
            # 判定を使い、モデルに渡さない。記録は呼び出した利用者が読めるものだけ（#1527）。
            "回答に使った rag_search の trace_id。渡すと、その回答で監査を通った段落の判定を"
            "使う（速くなる）。"
        ),
    )
    premises: list[ValidatePremiseInput] = Field(
        default_factory=list,
        max_length=MAX_PREMISES,
        description=(
            "利用者が回答の前の確認で答えた前提（確認の質問と答え）。資料ではなく利用者の"
            "状況の申告として根拠と並べ、答えをそのまま述べた段落と、資料の規則を答えに当てはめた"
            "結論を確かめる（資料・規程・手順の内容の裏付けには使わない）。前提だけで裏付けた段落の"
            "chunk_id は null。"
        ),
    )
    data_results: list[ValidateDataResultInput] = Field(
        default_factory=list,
        max_length=MAX_DATA_RESULTS,
        description=(
            "回答に使った業務データの照会の結果（照会の質問・SQL・結果）。資料ではない根拠として"
            "根拠と並べ、結果の値をそのまま述べた段落と、資料の規則を結果の値に当てはめた結論を確かめる"
            "（資料・規程・手順の内容の裏付けには使わない）。結果だけで裏付けた段落の chunk_id は"
            " null。"
        ),
    )


class ReadSourceInput(BaseModel):
    """根拠の本文を読む条件（evidence の document_id と、chunk_id か要素の定位子）。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(..., min_length=1, max_length=128, description="文書の id。")
    chunk_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="根拠の chunk の id。locator とどちらか一方を渡す。",
    )
    locator: str | None = Field(
        default=None,
        min_length=1,
        max_length=LOCATOR_MAX_LENGTH,
        description=(
            "根拠の要素の定位子（evidence の locator.element_locator）。文書分割の設定を"
            "変えて chunk を作り直しても、同じ解析の結果の同じ要素を含む今の chunk を返す。"
            "解析をやり直した後の古い定位子は source_stale のエラー。"
            "chunk_id とどちらか一方を渡す。"
        ),
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="本文の読み始めの位置（文字数。続きは前の結果か根拠の next_offset）。",
    )
    max_chars: int = Field(
        default=READ_SOURCE_MAX_CHARS_DEFAULT,
        ge=1,
        le=READ_SOURCE_MAX_CHARS_LIMIT,
        description="返す本文の最大文字数（表の続きに付ける見出しの行を含む）。",
    )
    include_image: bool = Field(
        default=False,
        description=(
            "true のとき、図の根拠（image_ref がある根拠）の元の画像の領域を返す。"
            "evidence_type が figure_description の根拠（図を AI が読んだ説明）を元の図で確かめる"
            "ときに使う。画像は MCP の"
            f" content の image（PNG。長い辺 {IMAGE_MAX_EDGE_PX} px 以下・{IMAGE_MAX_BYTES} バイト"
            "以下）で返し、structuredContent の image には大きさだけを入れる。図でない・場所が"
            "分からない根拠は image_not_available のエラー。"
        ),
    )
    include_image_url: bool = Field(
        default=False,
        description=(
            "true のとき、図の根拠の元の画像をブラウザで開く短命の URL（image_url）を返す"
            f"（{FIGURE_URL_TTL_SECONDS} 秒で切れ、読むたびに今の権限・版を確かめる）。Agent の"
            "画面の操作からだけ使える（それ以外は image_url_not_allowed のエラー）。"
        ),
    )
    include_preview: bool = Field(
        default=False,
        description=(
            "true のとき、根拠の文書の原文をブラウザで開くプレビューの情報（preview）を返す。"
            "Agent の画面の操作からだけ使える（それ以外は preview_not_allowed のエラー）。"
        ),
    )

    @model_validator(mode="after")
    def _one_reference(self) -> ReadSourceInput:
        if (self.chunk_id is None) == (self.locator is None):
            raise ValueError("chunk_id と locator のどちらか一方だけを渡してください。")
        if self.locator is not None:
            parsed = parse_element_locator(self.locator)
            if parsed is None:
                raise ValueError(
                    "locator の形が違います（doc:{document_id}/ext:{…}/page:{頁}/el:{要素}）。"
                )
            if parsed.document_id != self.document_id:
                raise ValueError("locator の文書が document_id と違います。")
        return self


class OutlineInput(BaseModel):
    """文書の章節の構成を読む条件。"""

    # 章節の構成（#1332 / #1438）。compact・section_id は #2252。

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(..., min_length=1, max_length=128, description="文書の id。")
    format: Literal["sections", "compact"] = Field(
        default="sections",
        description=(
            "sections は章節ごとの構造（section_id・頁・文字数）。compact は章節を 1 行ずつの目録"
            "（階層で字下げ。見出しの語の無い章節は本文の書き出しを添える）の lines で返す。"
            "条の多い文書から読む条を選ぶときは compact。"
        ),
    )
    section_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="この章節（rag_outline の section_id）の下だけを返す。省略すると文書全体。",
    )
    lead_chars: int = Field(
        default=OUTLINE_LEAD_CHARS_DEFAULT,
        ge=0,
        le=OUTLINE_LEAD_CHARS_MAX,
        description=(
            "compact で、見出しの語の無い章節に添える本文の書き出しの文字数（0 は付けない）。"
        ),
    )


class ReadDocumentInput(BaseModel):
    """文書の本文を順に読む条件。読み始めの位置はどれか 1 つ（無ければ文書の先頭）。"""

    # 文書を順に読む（#1332）。

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(..., min_length=1, max_length=128, description="文書の id。")
    cursor: str | None = Field(
        default=None,
        min_length=1,
        max_length=CURSOR_MAX_LENGTH,
        description="続きの位置（前の rag_read_document の next_cursor）。",
    )
    locator: str | None = Field(
        default=None,
        min_length=1,
        max_length=LOCATOR_MAX_LENGTH,
        description=(
            "根拠の要素の定位子（evidence の locator.element_locator）。その要素の chunk から読む。"
        ),
    )
    page: int | None = Field(default=None, ge=1, description="この頁（PDF の物理頁）から読む。")
    section_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description=(
            "この章節（rag_outline の section_id）の始まりから読む（章節の終わりで止まらない。"
            "章節だけを読むときは rag_read_section）。"
        ),
    )
    max_chars: int = Field(
        default=READ_SOURCE_MAX_CHARS_DEFAULT,
        ge=1,
        le=READ_SOURCE_MAX_CHARS_LIMIT,
        description="返す本文の最大文字数（頁の区切りの行を含む）。",
    )

    @model_validator(mode="after")
    def _one_start(self) -> ReadDocumentInput:
        starts = [self.cursor, self.locator, self.page, self.section_id]
        if sum(value is not None for value in starts) > 1:
            raise ValueError(
                "cursor・locator・page・section_id はどれか 1 つだけを渡してください。"
            )
        if self.locator is not None:
            parsed = parse_element_locator(self.locator)
            if parsed is None or parsed.document_id != self.document_id:
                raise ValueError("locator の形が違うか、文書が document_id と違います。")
        return self


class ReadSectionInput(BaseModel):
    """章節の本文を読む条件。章節は section_id・根拠の chunk_id・cursor のどれか 1 つ。"""

    # 章節を読み切る（#1438）。

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(..., min_length=1, max_length=128, description="文書の id。")
    section_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="章節の id（rag_outline の section_id、または根拠の section.section_id）。",
    )
    chunk_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="根拠の chunk の id（evidence_id）。その chunk が属する章節を読む。",
    )
    up: int = Field(
        default=0,
        ge=0,
        le=READ_SECTION_MAX_UP,
        description=(
            "章節から何段上の親の章節を読むか（0 はその章節。文書全体より上には上がらない）。"
            "章節を読んでも手順・一覧・条件が足りないときに 1 ずつ上げる。"
            "cursor とは一緒に渡さない。"
        ),
    )
    cursor: str | None = Field(
        default=None,
        min_length=1,
        max_length=CURSOR_MAX_LENGTH,
        description="続きの位置（前の rag_read_section の next_cursor）。",
    )
    max_chars: int = Field(
        default=READ_SOURCE_MAX_CHARS_DEFAULT,
        ge=1,
        le=READ_SOURCE_MAX_CHARS_LIMIT,
        description="返す本文の最大文字数（頁の区切りの行を含む）。",
    )

    @model_validator(mode="after")
    def _one_section(self) -> ReadSectionInput:
        starts = [self.section_id, self.chunk_id, self.cursor]
        if sum(value is not None for value in starts) != 1:
            raise ValueError("section_id・chunk_id・cursor のどれか 1 つだけを渡してください。")
        if self.cursor is not None and self.up:
            raise ValueError("cursor と up は一緒に渡せません（続きは同じ章節を読みます）。")
        return self


# ---- 出力 ----


class SearchAnswerProfileItem(BaseModel):
    id: str
    name: str
    description: str | None = None
    status: str
    knowledge_base_count: int


class VersionedOutput(BaseModel):
    """ツールの出力の共通部分（出力の形の版）。"""

    schema_version: int = Field(
        default=MCP_OUTPUT_SCHEMA_VERSION, description="出力の形の版（変わったら上がる）。"
    )


# 保存した根拠の確かめ直し（内部のツール）で 1 回に渡せる文書の数
# （Agent の Run の根拠の上限と同じ）。
READABLE_DOCUMENTS_MAX = 200


class ReadableDocumentsInput(BaseModel):
    """保存した根拠の文書を、呼び出した利用者が今見られるかを確かめる条件。"""

    # Agent の画面の保存した根拠の出し直し（#3252）。

    model_config = ConfigDict(extra="forbid")

    document_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        ..., min_length=1, max_length=READABLE_DOCUMENTS_MAX, description="文書の id の一覧。"
    )


class ReadableDocumentsOutput(VersionedOutput):
    readable_document_ids: list[str] = Field(
        description="渡した文書のうち、今の権限で見られる文書の id（渡した順）。"
    )


class OutlineSection(BaseModel):
    """文書の 1 つの章節（章節ナビゲーションの章節。#1438）。"""

    section_id: str = Field(description="章節の id（rag_read_section の section_id に渡す）。")
    title: str = Field(description="章節の名前（文書全体の章節は文書名）。")
    level: int = Field(description="章節の階層（1〜6。0 は文書全体）。")
    parent_section_id: str | None = Field(
        default=None, description="親の章節の id（文書全体の章節は null）。"
    )
    path: list[str] = Field(
        default_factory=list, description="文書の下からこの章節までの章節の名前の列。"
    )
    page_start: int | None = Field(default=None, description="章節の開始の頁。")
    page_end: int | None = Field(default=None, description="章節の終了の頁。")
    chunk_count: int = Field(description="章節の chunk の数（子の章節の chunk を含む）。")
    chars: int = Field(description="章節の本文の文字数（子の章節の本文を含む）。")


class OutlineOutput(VersionedOutput):
    document_id: str
    file_name: str | None = None
    chunk_set_id: str = Field(
        description="読んだ chunk_set（続きの cursor はこの chunk_set に縛る）。"
    )
    root_section_id: str = Field(
        default=ROOT_SECTION_ID,
        description="文書全体を表す章節の id（rag_read_section で文書全体を読むときに渡す）。",
    )
    chunk_count: int
    chars: int = Field(default=0, description="文書の本文の文字数。")
    page_start: int | None = None
    page_end: int | None = None
    sections: list[OutlineSection] = Field(
        description=(
            "章節（文書の詳細の章節ナビゲーションの並び順。階層は level、親は parent_section_id）。"
            "章節の無い文書は空（文書全体を root_section_id で読む）。"
        )
    )
    sections_omitted: int = Field(default=0, description="上限を超えて省いた章節の数。")
    superseded: bool = Field(default=False, description="新しい版に置き換えた文書（旧版）か。")
    lines: list[str] = Field(
        default_factory=list,
        description=(
            "format=compact の目録（章節を 1 行ずつ、階層で字下げ。見出しの語の無い章節は"
            "「見出し｜本文の書き出し」）。sections は空。"
        ),
    )


class ExcerptRange(BaseModel):
    """本文の中の範囲（文字の位置。[start, end)）。"""

    start: int
    end: int


RowBasis = Literal["sheet", "csv", "word_table", "parsed_table"]
ROW_BASIS_DESCRIPTION = (
    "row_start・row_end・table_row の行の番号の基準（sheet=表計算のシートの行 / csv=CSV の"
    "記録の番号〔RFC 7111 の行。表頭の記録を含め、空の行も 1 記録として先頭から数える。表計算の"
    "ソフトで開いたときの行〕/ word_table=Word の table_index の表の行。表頭の行も数え、大きい表を"
    "分けた部分は先頭に繰り返した表頭を数えない / parsed_table=解析が PDF・HTML・画像の表から"
    "組み立てた table_index の表の格子の行。原本の座標ではなく、解析し直すと変わることがある。"
    "表頭の行も数え〔表頭が 2 段なら最初のデータの行は 3〕、分けた部分は先頭に繰り返した表頭を"
    "数えない）。行の位置が分からない根拠（解析が表の格子を出さなかった表など）は null。"
)
SOURCE_LINE_START_DESCRIPTION = (
    "原本のテキストの開始の物理行（1 始まり。row_basis=csv だけ。引用符の中の改行を含む記録は"
    "source_line_end と違う）。"
)
SOURCE_LINE_END_DESCRIPTION = "原本のテキストの終了の物理行（row_basis=csv だけ）。"
TABLE_INDEX_DESCRIPTION = (
    "表の番号（1 始まり。row_basis=word_table は本文の表の順、parsed_table は文書の中の表の順、"
    "sheet は 1 シートを複数の表に分けたときの表の順）。無ければ null。"
)


class ReadDocumentChunk(BaseModel):
    """返した本文に含まれる chunk（根拠として引用するときの場所）。"""

    chunk_id: str
    element_locator: str | None = None
    section_path: list[str] = Field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    # 表計算・表の場所（検索の根拠の locator と同じ。#1471・#3262）。行の位置の無い chunk は null。
    sheet_name: str | None = Field(default=None, description="表計算のシート名（#1221）。")
    row_basis: RowBasis | None = Field(default=None, description=ROW_BASIS_DESCRIPTION)
    table_index: int | None = Field(default=None, description=TABLE_INDEX_DESCRIPTION)
    row_start: int | None = Field(
        default=None, description="開始の行（1 始まり。番号の基準は row_basis）。"
    )
    row_end: int | None = Field(default=None, description="終了の行。")
    source_line_start: int | None = Field(default=None, description=SOURCE_LINE_START_DESCRIPTION)
    source_line_end: int | None = Field(default=None, description=SOURCE_LINE_END_DESCRIPTION)
    cell_range: str | None = Field(default=None, description="セル範囲（例: A3:F7）。")
    start: int = Field(description="返した本文の中の開始の位置（文字数）。")
    end: int = Field(description="返した本文の中の終了の位置（文字数）。")
    header_range: ExcerptRange | None = Field(
        default=None,
        description=(
            "表の chunk を途中から読んだとき、本文の先頭に付けた見出しの行の位置（chunk の本文の"
            "中の文字の [start, end)）。無ければ null。"
        ),
    )


class ReadDocumentOutput(VersionedOutput):
    document_id: str
    file_name: str | None = None
    chunk_set_id: str
    text: str = Field(description="本文。頁が変わる所に「--- p.N ---」の行を入れる。")
    chunks: list[ReadDocumentChunk]
    next_cursor: str | None = Field(
        default=None, description="続きを読む cursor（文書の最後まで読んだら null）。"
    )
    superseded: bool = Field(default=False, description="新しい版に置き換えた文書（旧版）か。")


class ReadSectionOutput(VersionedOutput):
    document_id: str
    file_name: str | None = None
    chunk_set_id: str
    section: OutlineSection = Field(description="読んだ章節（ページの範囲・親の章節の id）。")
    child_sections: list[OutlineSection] = Field(
        default_factory=list,
        description=f"子の章節（最大 {READ_SECTION_MAX_CHILDREN} 件。本文は section に含む）。",
    )
    text: str = Field(description="章節の本文。頁が変わる所に「--- p.N ---」の行を入れる。")
    chunks: list[ReadDocumentChunk]
    section_complete: bool = Field(
        description="章節の終わりまで読んだか（false なら next_cursor で続きを読む）。"
    )
    next_cursor: str | None = Field(
        default=None, description="章節の続きを読む cursor（章節の終わりまで読んだら null）。"
    )
    superseded: bool = Field(default=False, description="新しい版に置き換えた文書（旧版）か。")


class ListSearchAnswerProfilesOutput(VersionedOutput):
    search_answer_profiles: list[SearchAnswerProfileItem]
    query_matched: bool = Field(
        default=True,
        description=(
            "query に当たる検索・回答プロファイルがあったか（false なら使える全件を返した）。"
        ),
    )
    next_step: str | None = Field(default=None, description="次にすること。")


class RecommendedProfileCandidate(BaseModel):
    id: str
    name: str
    description: str | None = None
    reason: str | None = Field(default=None, description="推薦の理由（当たった資料など）。")
    score: float = Field(description="1 位の候補を 1 とした相対の点。")
    documents: list[str] = Field(
        default_factory=list, description="質問で当たった資料の名前（上位の数件）。"
    )


class RecommendSearchAnswerProfileOutput(VersionedOutput):
    recommended_profile_id: str | None = Field(
        description=(
            "質問に合う検索・回答プロファイルの id。候補が違う資料に割れたとき・当たりが無いときは"
            " null（候補が複数なら利用者に確かめる）。"
        )
    )
    candidates: list[RecommendedProfileCandidate] = Field(
        description="候補（点の高い順。回答前の確認の選択肢にできる）。"
    )


class EvidenceTableCell(BaseModel):
    """表の 1 行の根拠の、値のあるセル（セルの座標は table_row と table_col）。"""

    table_col: str = Field(
        description=(
            "列（表計算の列の記号。例: B。CSV・parsed_table は列の位置を同じ記号で示し、"
            "結合したセルは左の列）。"
        )
    )
    header_path: list[str] = Field(
        description="列の表頭（複数段の表頭は上の段から。表頭の文字の無い列は空）。"
    )
    bbox: list[float] | None = Field(
        default=None,
        description=(
            "セルの領域 [x0, y0, x1, y1]（locator の bbox と同じ頁・座標。解析が出したときだけ）。"
        ),
    )


class EvidenceLocator(BaseModel):
    """根拠の場所（種類によって空の項目がある）。"""

    section_path: list[str] = Field(default_factory=list, description="節の見出しの列。")
    page_start: int | None = Field(default=None, description="開始の頁（PDF の物理頁）。")
    page_end: int | None = Field(default=None, description="終了の頁（PDF の物理頁）。")
    sheet_name: str | None = Field(default=None, description="表計算のシート名（#1221）。")
    row_basis: RowBasis | None = Field(default=None, description=ROW_BASIS_DESCRIPTION)
    table_index: int | None = Field(default=None, description=TABLE_INDEX_DESCRIPTION)
    row_start: int | None = Field(
        default=None, description="開始の行（1 始まり。番号の基準は row_basis）。"
    )
    row_end: int | None = Field(default=None, description="終了の行。")
    source_line_start: int | None = Field(default=None, description=SOURCE_LINE_START_DESCRIPTION)
    source_line_end: int | None = Field(default=None, description=SOURCE_LINE_END_DESCRIPTION)
    cell_range: str | None = Field(
        default=None, description="表計算のセル範囲（例: A3:F7。row_basis=sheet だけ）。"
    )
    page_label_start: str | None = Field(
        default=None,
        description="開始の頁の印刷の頁番号（PDF のページラベル。物理頁と違うときだけ。#1244）。",
    )
    page_label_end: str | None = Field(default=None, description="終了の頁の印刷の頁番号。")
    bbox: list[float] | None = Field(
        default=None, description="開始の頁の中の領域 [x0, y0, x1, y1]（分かるときだけ）。"
    )
    bbox_unit: str | None = Field(
        default=None, description="bbox の単位（absolute=頁の座標 / normalized=0〜1）。"
    )
    element_locator: str | None = Field(
        default=None,
        description=(
            "根拠の先頭の要素の定位子（#1330。doc:{document_id}/ext:{解析の結果}/page:{頁}/el:{要素}）。"
            "文書分割を作り直しても変わらず、rag_read_source の locator で読み直せる。"
            "要素を持たない分割の根拠は null。"
        ),
    )
    table_row: int | None = Field(
        default=None,
        description=(
            "根拠が表の 1 つのデータの行（表計算のシートの 1 行・CSV の 1 記録・解析の表の 1 行）の"
            "とき、その行の番号（row_basis の番号）。複数のデータの行の根拠・Word の表・行の位置が"
            "分からない表は null。"
        ),
    )
    table_cells: list[EvidenceTableCell] = Field(
        default_factory=list,
        description="table_row の行の値のあるセル（本文の順）。table_row が null なら空。",
    )


class EvidenceReference(BaseModel):
    """根拠の本文が参照する節・文書（「第3章を参照」など。取込時に抜き出す。#1280）。"""

    label: str = Field(description="本文の参照の表記（例: 第3章、別紙1、「権限」）。")
    document_title: str | None = Field(
        default=None, description="他の文書への参照の文書名（同じ文書なら null）。"
    )
    section_path: list[str] = Field(
        default_factory=list,
        description="同じ文書の参照先の節の見出しの列（解決できたときだけ）。",
    )
    resolved: bool = Field(
        description=(
            "参照先の節を取込時に決められたか（他の文書への参照は回答のときに検索範囲から"
            "決めるので false）。"
        )
    )


class EvidenceImageRef(BaseModel):
    """図の根拠の元の画像の領域（#1282）。

    場所を示すだけで、読む権限は持たない。画像は rag_read_source に document_id・chunk_id と
    include_image=true を渡して読む（読むたびに今の権限・版で確かめ、領域はサーバーが保存した
    場所から決める）。文書の版（chunk_set）が変わると source_stale になる。
    """

    document_id: str
    chunk_id: str
    chunk_set_id: str | None = Field(default=None, description="領域を記録した処理の版。")
    page: int = Field(description="図のある頁（PDF の物理頁。画像ファイルは 1）。")
    page_label: str | None = Field(
        default=None, description="印刷の頁番号（物理頁と違うときだけ。#1244）。"
    )
    bbox: list[float] = Field(description="頁の中の図の領域 [x0, y0, x1, y1]。")
    bbox_unit: Literal["absolute", "ratio", "percent"] = Field(
        description="bbox の単位（absolute=頁の座標 / ratio=0〜1 / percent=0〜100）。"
    )


class EvidenceImage(BaseModel):
    """rag_read_source が content の image で返した図の画像の大きさ（#1282）。"""

    mime_type: Literal["image/png"] = IMAGE_MIME_TYPE
    width: int = Field(description="画像の幅（px）。")
    height: int = Field(description="画像の高さ（px）。")
    byte_size: int = Field(description="画像のバイト数。")
    sha256: str = Field(description="画像の sha256（content の image と同じか確かめる）。")
    content_index: int = Field(
        default=1, description="MCP の content の何番目（0 始まり）に画像があるか。"
    )


class EvidenceImageUrl(BaseModel):
    """図の根拠の元の画像をブラウザで開く短命の URL（rag_read_source の include_image_url。#1311）。

    URL は利用者・根拠・版に縛った署名つきのトークンを path に持ち、期限（5 分）で切れる。読むたびに
    今の権限・版を確かめ直す（版が変われば 409、見えなければ 404）。
    """

    url: str = Field(description="図の画像の URL（MCP の呼び出しを受けた RAG の起点から作る）。")
    # path の始まりを契約に出す（#3240。呼び出し側は付け替えてよい path をこれで確かめる）。
    path: str = Field(
        pattern=f"^{FIGURE_URL_PATH_PREFIX}/",
        description="url の path（RAG の公開の起点に付け替えるときに使う）。",
    )
    expires_at: str = Field(description="期限（ISO 8601、UTC）。")
    expires_in_seconds: int = Field(description="発行からの有効な秒数。")


class EvidencePreviewPage(BaseModel):
    """プレビューの 1 ページの表示寸法（pt）。"""

    page_number: int = Field(description="1 始まりのページ番号。")
    width: float
    height: float


class EvidencePreview(BaseModel):
    """根拠の文書の原文をブラウザで開くプレビュー（rag_read_source の include_preview。#2389）。

    URL は利用者・根拠・版・ファイルに縛った署名つきのトークンを path に持ち、期限（5 分）で
    切れる。読むたびに今の権限・版を確かめ直す（版が変われば 409、見えなければ 404）。
    """

    kind: PreviewKind = Field(
        description=(
            "pdf=ページの画像（Office は処理後の PDF）/ image=画像 / text=本文 / "
            "unsupported=表示できない形式。"
        )
    )
    file_name: str
    pages: list[EvidencePreviewPage] = Field(
        default_factory=list,
        description=(
            "kind=pdf のページの一覧。ページの画像を描けないファイルは空（content_url を開く）。"
        ),
    )
    page_url_template: str | None = Field(
        default=None,
        description=(
            "ページの画像の URL のひな形（{page} をページ番号に置き換え、query の dpi で"
            "解像度を選ぶ）。"
        ),
    )
    page_path_template: str | None = Field(
        default=None,
        pattern=f"^{SOURCE_PREVIEW_PATH_PREFIX}/",
        description="page_url_template の path（RAG の公開の起点に付け替えるときに使う）。",
    )
    content_url: str = Field(
        description=(
            "原文のファイルの URL（画像・本文・ページの画像を描けない PDF。query の "
            "disposition=attachment でダウンロード）。"
        )
    )
    content_path: str = Field(
        pattern=f"^{SOURCE_PREVIEW_PATH_PREFIX}/", description="content_url の path。"
    )
    highlight_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "強調する位置を決める根拠の metadata（頁・bbox・座標の解釈・要素の表示領域だけ）。"
        ),
    )
    expires_at: str = Field(description="期限（ISO 8601、UTC）。")
    expires_in_seconds: int = Field(description="発行からの有効な秒数。")


class EvidenceSection(BaseModel):
    """根拠の chunk が属する章節（章節ナビゲーションの章節の中で最も深いもの。#1438）。"""

    section_id: str = Field(description="章節の id（rag_read_section の section_id に渡す）。")
    title: str = Field(description="章節の名前（どの章節にも入らない chunk は文書名）。")
    level: int = Field(description="章節の階層（1〜6。0 は文書全体）。")
    page_start: int | None = Field(default=None, description="章節の開始の頁。")
    page_end: int | None = Field(default=None, description="章節の終了の頁。")
    chars: int = Field(description="章節の本文の文字数（excerpt より長ければ章節を読む）。")
    chunk_count: int = Field(description="章節の chunk の数。")
    parent_section_id: str | None = Field(default=None, description="親の章節の id。")
    parent_title: str | None = Field(default=None, description="親の章節の名前。")
    parent_chars: int | None = Field(default=None, description="親の章節の本文の文字数。")


# 根拠の本文を表の行・列の対応の検査（``rag_engine.generation.table_binding``）が読めるか（#3262）。
TableBinding = Literal["checkable", "not_checkable"]


class RagEvidence(BaseModel):
    """検索・回答の根拠 1 件。"""

    evidence_id: str = Field(description="根拠の id（chunk_id と同じ）。")
    document_id: str
    chunk_id: str
    file_name: str | None = None
    chunk_set_id: str | None = Field(
        default=None,
        description=(
            "根拠を作った処理の版（取込み直すと変わる。文書の改訂の版ではない。改訂した文書は"
            "別の document_id で、置き換えた旧版は superseded）。"
        ),
    )
    recipe_id: str | None = Field(default=None, description="根拠を作った処理レシピ。")
    content_kind: str | None = Field(default=None, description="内容の種類（text / table など）。")
    evidence_type: EvidenceType = Field(
        default="text",
        description=(
            "根拠の種類（text=本文 / table=表 / figure_description=図を AI（VLM）が読んだ説明。"
            "原文ではないので元の図で確かめる / ocr=図の中・周りの文字（OCR・キャプション））。"
        ),
    )
    image_ref: EvidenceImageRef | None = Field(
        default=None,
        description=(
            "図の元の画像の領域（rag_read_source の include_image で読む）。無ければ null。"
        ),
    )
    locator: EvidenceLocator
    excerpt: str = Field(
        description=(
            "本文（既定は全文）。応答の根拠の合計の予算（token の見積もり "
            f"{EVIDENCE_TEXT_BUDGET_TOKENS}）に順位の順で入れ、入らない根拠は空（text_omitted）。"
            f"1 件で {EVIDENCE_TEXT_ITEM_MAX_TOKENS} を超える根拠は、質問に当たる部分を中心に"
            "文・段落（表は行）の境界で切った一部（truncated）。"
        )
    )
    truncated: bool = Field(description="excerpt が本文の一部だけか（範囲は excerpt_range）。")
    text_omitted: bool = Field(
        default=False,
        description="応答の予算に入らず excerpt を空にしたか（本文は rag_read_source で読む）。",
    )
    text_length: int = Field(description="本文の文字数。")
    excerpt_range: ExcerptRange | None = Field(
        default=None,
        description="excerpt の本文の位置（文字の [start, end)）。text_omitted なら null。",
    )
    header_range: ExcerptRange | None = Field(
        default=None,
        description=(
            "切った表で excerpt の先頭に付けた見出しの行の位置（excerpt_range と続かないとき"
            "だけ）。無ければ null。"
        ),
    )
    next_offset: int | None = Field(
        default=None,
        description=(
            "excerpt の後ろの続きを読むときの rag_read_source の offset（続きが無ければ null）。"
        ),
    )
    used_in_answer: bool = Field(
        description="回答で引用した箇所か（AI に渡した箇所のうち、回答の本文に示したもの）。"
    )
    answer_use: Literal["confirmed", "reference"] | None = Field(
        default=None,
        description=(
            "回答での使われ方（#2040）。confirmed=質問の条件への該当確認済みの記載として"
            "引用 / reference=該当が確認できていない参考の記載として引用（回答の「参考情報」と"
            "「資料から確認できなかった事項」の節）。回答で引用していないとき・使われ方が"
            "分からないときは null。"
        ),
    )
    role: str | None = Field(
        default=None,
        description=(
            "根拠の役割（retrieved_anchor / entity_expansion / catalog_article / parent_context"
            " など）。entity_expansion は、質問・検索の上位の根拠の実体（システム・部署の略号など）"
            "から実体の表で 1 段だけたどって足した根拠（台帳の行・略号の表など。#1362）。"
            "catalog_article は、条で構成された文書の条の目録から質問の根拠として選んだ条"
            "（検索の語と条文の語が違っても選ぶ。#2254）。"
        ),
    )
    score: float | None = None
    rerank_score: float | None = None
    retrieval_rank: int | None = Field(
        default=None,
        description=(
            "検索で当たった根拠の関連度の順位（1 始まり）。前後の文脈など、検索で当たったのでは"
            "ない根拠は null。"
        ),
    )
    origin: Literal["source", "derived"] = Field(
        default="source",
        description=(
            "本文の由来。source=文書の原文（表・図の中の文字を含む）/ derived=取込が原文から作った"
            "要約・抽出した値（引用する前に原文を読んで確かめる）。"
        ),
    )
    table_binding: TableBinding | None = Field(
        default=None,
        description=(
            "回答の検査（回答の生成と rag_validate_answer）が、文の値を表の別の行・列の値と"
            "取り違えていないかをこの根拠の本文で確かめられるか。checkable=2 行以上の表（HTML・"
            "Markdown の表・「名前: 値」の行の並び）を読める / not_checkable=表の根拠だが読めない"
            "（1 行の記録・列の並びが崩れた本文）ので、値の行・列は locator のセルの座標か原文で"
            "確かめる / null=表の無い根拠。"
        ),
    )
    superseded: bool = Field(
        default=False,
        description=(
            "新しい版に置き換えた文書（旧版）の根拠か。旧版は既定では検索しない"
            "（include_superseded に true を渡すと含める）。"
        ),
    )
    references: list[EvidenceReference] = Field(
        default_factory=list, description="この根拠の本文が参照する節・文書（#1280）。"
    )
    reference_from: str | None = Field(
        default=None,
        description=(
            "別の根拠の本文の参照（「第3章を参照」など）を辿って足した根拠なら、その根拠の"
            " evidence_id（#1280）。"
        ),
    )
    reference_label: str | None = Field(
        default=None, description="reference_from の根拠の本文の参照の表記。"
    )
    section: EvidenceSection | None = Field(
        default=None,
        description=(
            "根拠が属する章節（#1438。章節と chunk の対応がまだ無い文書は null）。手順・一覧・"
            "条件のまとまりが excerpt より長いときは rag_read_section で章節を読む。"
        ),
    )


class OlderVersion(BaseModel):
    """検索で当たった文書を置き換えた旧版（#1405）。"""

    document_id: str = Field(description="旧版の文書の id。")
    file_name: str | None = Field(default=None, description="旧版の文書名。")
    title: str | None = Field(
        default=None,
        description=(
            "旧版のタイトル（文書の先頭の見出し。版・年度を含むことが多い。"
            "例: 「定期保守計画 2025年度」）。見出しが無ければ null。"
        ),
    )
    superseded_by_document_id: str = Field(
        description="この旧版を置き換えた新しい版（検索で当たった根拠の文書）の id。"
    )


OLDER_VERSIONS_DESCRIPTION = (
    "検索で当たった今の版の文書に、新しい版に置き換えた旧版があれば、その旧版（検索の範囲の"
    "中のもの。最大 10 件）。include_superseded を渡さない検索だけで返し、旧版の根拠は検索して"
    "いない。質問が旧版の年度・版（タイトル・文書名の「2025年度」など）や改定前との違いを尋ねる"
    "なら、include_superseded に true を渡して検索し直す。無ければ空。"
)


AnswerOutcome = Literal[
    "answered",
    "conditional",
    "needs_clarification",
    "needs_environment_data",
    "needs_human",
    "insufficient_evidence",
]


class AnswerRequest(BaseModel):
    id: str = Field(
        description="質問の要求単位の ID（Q1 など）。業務ガイドの手順は guide:<手順の id>。"
    )
    text: str = Field(description="要求の原文（業務ガイドの手順は手順の名前）。")
    status: Literal["addressed", "partial", "missing", "unknown"] = Field(
        description=(
            "充足（addressed=答えた / partial=一部 / missing=答えていない / unknown=未確認）。"
        )
    )


class PassageReason(BaseModel):
    code: str = Field(description="理由の種類。")
    label: str = Field(description="利用者に示す短い理由。")
    detail: str = Field(default="", description="理由の詳細（無ければ空）。")


class PassageMark(BaseModel):
    text: str = Field(description="answer の段落（行）の原文。")
    state: Literal["unverified", "warning"] = Field(
        description="unverified=根拠で確かめられない / warning=検査で注意が必要。"
    )
    reasons: list[PassageReason] = Field(default_factory=list, description="印の理由。")


class RemovedPassage(BaseModel):
    text: str = Field(description="answer から除いた段落。")
    reasons: list[PassageReason] = Field(default_factory=list, description="除いた理由。")


class GuideClarification(BaseModel):
    condition_id: str = Field(description="条件の id（conditions に値を入れて呼び直す）。")
    label: str = Field(description="条件の名前。")
    question: str = Field(description="利用者に確かめる問い。")
    options: list[str] = Field(default_factory=list, description="選択肢（無ければ自由に答える）。")
    multiple: bool = Field(default=False, description="選択肢を複数選べるか。")


class GuideCondition(BaseModel):
    id: str
    label: str
    state: Literal["known", "unknown", "conflicting"] = Field(
        default="unknown",
        description=(
            "条件の状態（known=分かっている / unknown=分からない / conflicting=質問に複数の値が"
            "出ていて決められない。値は candidates）。"
        ),
    )
    value: str | None = None
    source: str | None = Field(
        default=None,
        description=(
            "user=利用者が答えた / question=質問の文から読んだ / document=回答の根拠の画面の図から"
            "決めた。"
        ),
    )
    handling: str | None = Field(
        default=None, description="不明のときの扱い（ask=確かめる / branch=分岐 / handoff=人へ）。"
    )
    candidates: list[str] = Field(
        default_factory=list, description="state=conflicting のとき、質問に出た値。"
    )


class GuideRef(BaseModel):
    guide_id: str
    revision: int = Field(description="使った公開の版。")
    title: str
    decision: Literal["answer", "branch", "clarify", "handoff"] = Field(
        description=(
            "answer=条件がそろった / branch=分岐で答えた / clarify=確かめる / handoff=人へ。"
        )
    )
    known_conditions: list[GuideCondition] = Field(default_factory=list)
    unknown_conditions: list[GuideCondition] = Field(
        default_factory=list, description="確かめる・分岐する・引き継ぐ条件（不明・矛盾）。"
    )
    applicability: dict[str, Literal["matched", "unverified"]] = Field(
        default_factory=dict,
        description=(
            "値のある適用範囲の項目（business_domains / object_types / versions）ごとの状態。"
            "matched=質問・絞り込みの手がかりと合った / unverified=手がかりが無く確かめていない。"
            "空の項目は制限なし（出さない）。手がかりと合わないガイドは返さない。"
        ),
    )


class AnswerProvenance(BaseModel):
    """回答を作った設定の版（回答の記録の provenance。#1276）。"""

    search_answer_profile_id: str | None = None
    search_answer_profile_updated_at: str | None = Field(
        default=None, description="検索・回答プロファイルの更新時刻（版）。"
    )
    search_answer_profile_config_sha256: str | None = Field(
        default=None, description="検索・回答プロファイルの設定の sha256。"
    )
    prompt_version: str | None = Field(
        default=None, description="回答フローのプロンプトの版（内容の sha256 の先頭）。"
    )


class AutoSelectedProfile(BaseModel):
    """id を誤ったときに自動で選んだ検索・回答プロファイル（#2355）。"""

    id: str
    name: str
    requested_id: str | None = Field(
        default=None, description="渡した search_answer_profile_id（省略したときは null）。"
    )
    method: Literal["only_usable", "recommended"] = Field(
        description="only_usable=使えるのが 1 つだけ / recommended=質問から推薦した。"
    )


AUTO_SELECTED_PROFILE_DESCRIPTION = (
    "見つからない search_answer_profile_id を渡したときに自動で選んだ検索・回答プロファイル"
    "（それ以外は null）。"
)


# 回答の診断の degraded_steps（#3259）の公開の形（#3262）。
class DegradedStep(BaseModel):
    """失敗して縮退した任意の補助の処理。"""

    kind: str = Field(description="処理の種類（例: rerank・section_outline・reference_load）。")
    count: int = Field(description="失敗した回数。")


DEGRADED_STEPS_DESCRIPTION = (
    "失敗して縮退した任意の補助の処理（rerank・章節の目録・参照先の読み込みなど）。その処理を"
    "使わずに続けた結果（例: rerank の失敗は検索の順のまま）で、空なら縮退していない。必須の能力"
    "（主の検索・回答の生成・有効な安全チェック）の失敗は縮退せずエラーで返る。"
)


# 根拠 0 件の診断（回答の診断の zero_evidence）の公開の形（#3546）。検索の条件の値・検索文・
# 例外の文は出さない（段と件数だけ）。
ZeroEvidenceStage = Literal["retrieval", "filter", "evidence_check", "context"]


class ZeroEvidence(BaseModel):
    """根拠が 0 件になった段。"""

    stage: ZeroEvidenceStage = Field(
        description=(
            "retrieval=検索の候補が 0 件 / filter=質問から自動で足した検索条件（項目の値・文書名・"
            "頁）が効いたまま候補が 0 件 / evidence_check=根拠確認が候補をすべて質問と別の話題と"
            "判定 / context=候補はあるが回答に渡す根拠が 0 件。"
        )
    )
    candidate_count: int = Field(description="検索の候補の件数。")
    rejected_count: int = Field(description="根拠確認で外した候補の件数。")


ZERO_EVIDENCE_DESCRIPTION = (
    "根拠が 0 件のとき、0 件になった段と件数（根拠があれば null）。degraded_steps が空でなければ、"
    "補助の処理の失敗で候補を読めなかった可能性があり、資料に記載が無いとは限らない。"
)
GUARDRAIL_DEGRADED_DESCRIPTION = (
    "安全チェック（質問・回答の検査）を完了できず、処理を止めたか。true のとき資料は調べて"
    "いない（answer は停止の文・evidence は空）か回答を止めており、資料に根拠が無いことを意味"
    "しない。"
)


class SearchOutput(VersionedOutput):
    answer: str
    trace_id: str
    guardrail_warnings: list[str]
    guardrail_degraded: bool = Field(default=False, description=GUARDRAIL_DEGRADED_DESCRIPTION)
    outcome: AnswerOutcome = Field(
        description=(
            "回答の対応。answered=資料で答えた / conditional=条件・不足を示して答えた / "
            "needs_environment_data=利用者に聞いても分からない現場の値・記録の確認が要る / "
            "insufficient_evidence=資料から答えられない。needs_clarification / needs_human は"
            "利用者への確認・人への引き継ぎ。利用者に聞けば分かる条件（対象・ロール・承認など）だけを"
            "確かめる回答は conditional（conditions に載る。#1453）。"
        )
    )
    requests: list[AnswerRequest] = Field(
        default_factory=list, description="質問の要求単位ごとの充足（背景の文は含めない）。"
    )
    conditions: list[str] = Field(
        default_factory=list,
        description=(
            "回答の説明が成り立つ条件（原文の語句と、利用者に確かめる条件（質問で特定されていない対象・"
            "利用者のロールや承認など）。質問から確かめられないもの）。"
        ),
    )
    gaps: list[str] = Field(default_factory=list, description="資料から確かめられない点。")
    confirmations: list[str] = Field(
        default_factory=list, description="回答を確定するために確かめる現場のデータ・別の資料。"
    )
    clarifications: list[GuideClarification] = Field(
        default_factory=list,
        description=(
            "outcome=needs_clarification のとき、利用者に確かめる条件と問い（業務ガイドと、"
            "検索・回答プロファイルのルールの確認。ルールの確認は検索・回答をせずに返す）。"
        ),
    )
    guide: GuideRef | None = Field(
        default=None, description="回答に使った業務ガイド（公開の版）。使わなければ null。"
    )
    insufficient_reason: str | None = Field(
        default=None, description="根拠が足りず答えきれなかった理由（無ければ null）。"
    )
    needs_human_review: bool = Field(default=False, description="人の確認が要る回答か。")
    passage_marks: list[PassageMark] = Field(
        default_factory=list,
        description="answer に残した段落のうち、根拠で確かめられない・注意が必要な段落と理由。",
    )
    removed_passages: list[RemovedPassage] = Field(
        default_factory=list, description="根拠と矛盾するため answer から除いた段落と理由。"
    )
    answer_warnings: list[PassageReason] = Field(
        default_factory=list,
        description="回答全体の警告（質問からずれている可能性など）。",
    )
    degraded_steps: list[DegradedStep] = Field(
        default_factory=list, description=DEGRADED_STEPS_DESCRIPTION
    )
    zero_evidence: ZeroEvidence | None = Field(default=None, description=ZERO_EVIDENCE_DESCRIPTION)
    evidence: list[RagEvidence] = Field(
        description=(
            "AI に渡した資料の箇所。回答で引用した箇所（used_in_answer。回答で引用した順）を先に、"
            "検索で当たった根拠"
            "（role=retrieved_anchor と、実体からたどった role=entity_expansion。関連度の順）、"
            "前後の文脈（neighbor_context など）の順。実体からたどった根拠（前後の文脈の役割に"
            "なったものも含む）と、その同じ親の前後の文脈は、evidence_limit の 3 割まで上限の内に"
            "入れる。"
        )
    )
    evidence_omitted: int = Field(description="evidence_limit を超えて返さなかった根拠の数。")
    evidence_text_omitted: int = Field(
        default=0, description="応答の予算に入らず本文を省いた根拠（text_omitted）の数。"
    )
    older_versions: list[OlderVersion] = Field(
        default_factory=list, description=OLDER_VERSIONS_DESCRIPTION
    )
    provenance: AnswerProvenance | None = Field(
        default=None, description="回答を作った検索・回答プロファイルとプロンプトの版。"
    )
    auto_selected_search_answer_profile: AutoSelectedProfile | None = Field(
        default=None, description=AUTO_SELECTED_PROFILE_DESCRIPTION
    )
    # 回答の注意書き（#2405）。API キーで呼んだ外部のクライアントにだけ返す（Agent がサービス
    # トークンで呼んだときは返さず、Agent の回答の末尾には Agent が付ける 1 つだけを出す）。
    notice: str | None = Field(
        default=None, description="利用者に回答を示すときは、末尾にこの文をそのまま添える。"
    )


class GuidePageRangeItem(BaseModel):
    page_start: int
    page_end: int


class GuideSheetRangeItem(BaseModel):
    sheet_name: str
    row_start: int | None = Field(default=None, description="始まりの行（無ければシートの全体）。")
    row_end: int | None = None
    cell_range: str = Field(default="", description="A1 形式のセルの範囲（書いてあれば）。")


class GuideReferenceItem(BaseModel):
    document_id: str
    title: str = Field(default="", description="表示名（参照の正本は document_id）。")
    section_path: list[str] = Field(default_factory=list, description="節の見出しの列。")
    page_ranges: list[GuidePageRangeItem] = Field(
        default_factory=list,
        description=(
            "根拠の頁の範囲（両端を含む。空なら文書の全体）。rag_read_document に document_id と "
            "page（page_start）を渡すと、その頁から読める。"
        ),
    )
    sheet_ranges: list[GuideSheetRangeItem] = Field(
        default_factory=list,
        description="表計算の資料の根拠のシートと行の範囲（両端を含む。頁ではない）。",
    )


class GuideStepItem(BaseModel):
    id: str
    title: str
    purpose: str = ""
    depends_on: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    retrieval_hints: list[str] = Field(
        default_factory=list, description="この手順の根拠を引く検索の手がかり（語・画面名など）。"
    )
    references: list[GuideReferenceItem] = Field(
        default_factory=list, description="この手順の根拠がある資料と頁。"
    )
    evidence_requirements: list[str] = Field(
        default_factory=list, description="この手順で示すべき根拠。"
    )
    done_when: str = Field(default="", description="この手順の終わりの条件。")


class GuideBranchItem(BaseModel):
    id: str
    condition_id: str
    operator: Literal["equals", "in", "unknown"]
    values: list[str] = Field(default_factory=list)
    goto_step: str = Field(description="条件に当たるときに進む手順の id。")
    applies: bool | None = Field(
        description=(
            "分かっている条件に当たるか（true=当たる / null=条件が分からず決まらない）。"
            "当たらない分岐は返さない。"
        )
    )
    note: str = ""


class GuideCompletionItem(BaseModel):
    id: str
    description: str
    check_method: str = ""


class LookupGuideItem(GuideRef):
    expected_result: str
    score: int = Field(description="質問との照合の点（大きいほど合う）。")
    clarifications: list[GuideClarification] = Field(default_factory=list)
    steps: list[GuideStepItem] = Field(
        default_factory=list,
        description="手順（確かめる順。分かっている条件で外れた分岐の手順は含めない）。",
    )
    branches: list[GuideBranchItem] = Field(
        default_factory=list, description="当たる・決まらない分岐（当たらない分岐は含めない）。"
    )
    references: list[GuideReferenceItem] = Field(
        default_factory=list, description="全手順に共通の資料と頁。"
    )
    completion: list[GuideCompletionItem] = Field(
        default_factory=list, description="回答を終えてよい条件。"
    )
    impact_scope: Literal["individual", "group", "all"]
    approval_required: bool
    impact_steps: list[str] = Field(
        default_factory=list,
        description="影響範囲と承認が係る手順の id（空なら業務ガイドのすべての場合に係る）。",
    )
    impact_applies: bool = Field(
        default=True,
        description=(
            "分かっている条件の場合に影響範囲・承認が係るか（false なら、外れた分岐の手順だけに"
            "係るので、回答に影響範囲・承認を書かない。#1320）。"
        ),
    )
    handoff_contact: str = ""


class LookupGuidesOutput(VersionedOutput):
    guides: list[LookupGuideItem] = Field(
        description=(
            "質問に当たり、適用範囲の手がかりと合う公開の業務ガイド（照合の点の高い順）。"
            "無ければ空。"
        )
    )


ProfileKnowledgeStatus = Literal["approved", "stale_review_needed"]
PROFILE_KNOWLEDGE_STATUS_DESCRIPTION = (
    "approved=承認済み / stale_review_needed=承認済みで、見直しの期日を過ぎている"
)


class ProfileKnowledgeRule(BaseModel):
    id: str
    title: str
    content: str = Field(description="ルールの本文（判断の方針）。")
    source: str = Field(description="ルールを登録した人が書いた出典（無ければ空）。")
    status: ProfileKnowledgeStatus = Field(description=PROFILE_KNOWLEDGE_STATUS_DESCRIPTION)
    updated_at: str = Field(description="ルールを最後に更新した時刻（不明なら空）。")


class ProfileKnowledgeTerm(BaseModel):
    term: str
    aliases: list[str] = Field(default_factory=list)
    description: str = Field(description="用語の説明（無ければ空）。")
    source: str = Field(description="用語を登録した人が書いた出典（無ければ空）。")
    status: ProfileKnowledgeStatus = Field(description=PROFILE_KNOWLEDGE_STATUS_DESCRIPTION)
    updated_at: str = Field(description="用語を最後に更新した時刻（不明なら空）。")


class ProfileKnowledge(BaseModel):
    search_answer_profile_id: str
    revision: str | None = Field(
        description=(
            "照合した用語・同義語と回答ルールの登録の版（登録の内容の sha256 の先頭。"
            "読めなければ null）。"
        )
    )
    matched_on: Literal["original_question", "query"] = Field(
        description="照合に使った文（original_question があればそれ、無ければ query）。"
    )
    rules: list[ProfileKnowledgeRule] = Field(
        description="当たったルール（一致の強い順）。資料の原文ではない。"
    )
    terms: list[ProfileKnowledgeTerm] = Field(
        description="当たった用語と説明（一致の強い順）。資料の原文ではない。"
    )
    rules_omitted: int = Field(description="件数・字数の上限を超えて返さなかったルールの数。")
    terms_omitted: int = Field(description="件数・字数の上限を超えて返さなかった用語の数。")
    load_failed: bool = Field(
        default=False,
        description=(
            "用語・同義語と回答ルールを読めなかった（rules・terms は空。"
            "当たるものが無いとは限らない）。"
        ),
    )


class RetrieveEvidenceOutput(VersionedOutput):
    trace_id: str
    guardrail_warnings: list[str]
    guardrail_degraded: bool = Field(default=False, description=GUARDRAIL_DEGRADED_DESCRIPTION)
    degraded_steps: list[DegradedStep] = Field(
        default_factory=list, description=DEGRADED_STEPS_DESCRIPTION
    )
    zero_evidence: ZeroEvidence | None = Field(default=None, description=ZERO_EVIDENCE_DESCRIPTION)
    evidence: list[RagEvidence] = Field(
        description=(
            "検索で当たった根拠（role=retrieved_anchor。関連度＝rerank の順）と、実体からたどった"
            "根拠（role=entity_expansion。候補の中の位置の順）を先に、前後の文脈"
            "（neighbor_context など）をその後ろに並べる。実体からたどった根拠（前後の文脈の"
            "役割になったものも含む）と、その同じ親の前後の文脈は、evidence_limit の 3 割まで"
            "上限の内に入れる。条の目録で選んだ条（role=catalog_article）は evidence_limit に"
            "数えず、選んだ順に末尾に足す（件数はサーバーの設定で上限がある）。回答は作らないので"
            " used_in_answer は false。"
        )
    )
    evidence_omitted: int = Field(description="evidence_limit を超えて返さなかった根拠の数。")
    evidence_text_omitted: int = Field(
        default=0, description="応答の予算に入らず本文を省いた根拠（text_omitted）の数。"
    )
    older_versions: list[OlderVersion] = Field(
        default_factory=list, description=OLDER_VERSIONS_DESCRIPTION
    )
    auto_selected_search_answer_profile: AutoSelectedProfile | None = Field(
        default=None, description=AUTO_SELECTED_PROFILE_DESCRIPTION
    )
    planning: Literal["rag", "caller"] = Field(
        default="rag", description="検索文を作った主体（入力の planning）。"
    )
    request_id: str | None = Field(default=None, description="入力の request_id。")
    clarifications: list[GuideClarification] = Field(
        default_factory=list,
        description=(
            "planning=caller で、検索・回答プロファイルのルールが検索の前に利用者に確かめる条件"
            "（あれば検索せず、evidence は空）。利用者の答えを conditions の condition_id に入れて"
            "呼び直す。"
        ),
    )
    profile_knowledge: ProfileKnowledge | None = Field(
        default=None,
        description=(
            "検索・回答プロファイルに登録・承認された判断の方針（rules）と用語の説明（terms）のうち、"
            "利用者の元の質問に当たるもの。資料の原文ではないので evidence と分け、根拠・出典として"
            "引用しない。根拠の読み方と回答の判断に使う。上限（ルール "
            f"{PROFILE_KNOWLEDGE_RULES_MAX} 件・用語 {PROFILE_KNOWLEDGE_TERMS_MAX} 件・"
            f"文字の合計 {PROFILE_KNOWLEDGE_MAX_CHARS} 字）を超えた分は rules_omitted・"
            "terms_omitted に数える。"
            "検索・回答プロファイルを使わない検索・当たるものが無いときは null。"
        ),
    )


class ValidatedClaim(BaseModel):
    answer_quote: str = Field(description="回答の段落（原文）。")
    answer_range: ExcerptRange | None = Field(
        default=None,
        description=(
            "渡した answer の中の段落の位置（文字の位置。answer の [start, end) が answer_quote）。"
            "段落の分け方は呼び出し側に写さず、この位置で回答の段落を特定する。位置を決められない"
            "段落は null。"
        ),
    )
    status: str = Field(
        description=(
            "supported=根拠で裏付けられる / unsupported=根拠で確かめられない / contradicted=根拠と"
            "矛盾 / data_confirmation=実データの確認を促すだけ / "
            "citation_error=出典を照合できない / unassessed=監査されなかった / "
            "figure_only=図の見た目（配置・矢印・つながり・所属）だけで決まり、図の本文では確かめ"
            "られない（chunk_id はその図。図の画像を読んで確かめたかは呼び出し側が決める）"
        )
    )
    chunk_id: str | None = Field(
        default=None,
        description=(
            "裏付け・矛盾の根拠の chunk_id（利用者の前提〔premises〕・業務データの照会の結果"
            "〔data_results〕だけで裏付けた段落と、根拠の ID を照合できなかった矛盾は null）。"
        ),
    )
    reason: str
    audit_status: str | None = Field(
        default=None,
        description=(
            "監査が返した判定（status は根拠の ID を照合できないと変わる。例: supported の ID を"
            "照合できなければ status は citation_error）。"
        ),
    )
    reference_check: str | None = Field(
        default=None,
        description=(
            "裏付け・矛盾の根拠の ID の照合の結果（resolved / "
            "resolved_by_answer=回答が書いた ID で照合 / "
            "answer_id_invalid=回答が書いた ID が根拠に無い / "
            "audit_id_invalid=監査が返した ID だけが根拠に無い / "
            "ambiguous_id=省いた ID が複数の根拠に当たる / audit_id_missing=ID が無い / "
            "quote_unmatched=引用が根拠に無い / "
            "not_figure=figure_only の根拠が図ではない〔status は unsupported〕 / "
            "single_figure=figure_only の ID が決まらず、根拠の唯一の図に結び付けた）。"
            "照合しない判定は null。"
        ),
    )
    classification_check: str | None = Field(
        default=None,
        description=(
            "段落の分類の照合の結果（non_claim_mismatch=監査が見出しとしたが本文の段落で、"
            "内容を確かめていない〔status は unassessed〕 / "
            "reaudited=その段落を内容で監査し直した判定）。食い違いが無ければ null。"
        ),
    )
    support_check: str | None = Field(
        default=None,
        description=(
            "監査の supported を採らなかった理由（target_mismatch=根拠は別の対象"
            "〔処理・画面・成果物・状態〕の記載 / "
            "value_unbound=段落が根拠の表の別の行・列の値を結び付けている〔status は contradicted。"
            "理由に表の正しい対応〕か、監査の理由だけが表と違う対応を述べている〔unsupported〕 / "
            "quote_unmatched=裏付けとして写した原文が根拠に無い / "
            "figure_recheck=図の項目だけの裏付けを確かめ直し、図の見た目だけで決まる・図と矛盾する"
            "とした / premise_conflict=段落が勧める内容が、利用者が確認で答えた条件・制約と"
            "両立しない〔status は contradicted〕 / table_corrected=段落の表の対応の取り違えを、"
            "表の正しい値に直した〔status は contradicted。直した文は revised_text〕）。"
            "採ったときは null。"
        ),
    )
    revised_text: str | None = Field(
        default=None,
        description=(
            "support_check が table_corrected のとき、段落を根拠の表の正しい値に直した文"
            "（表の照合で確かめた）。段落を外す代わりに、この文に置き換えて示せる。"
            "それ以外は null。"
        ),
    )


class ValidationFinding(BaseModel):
    check: Literal["requests", "guide", "guide_steps", "impact"] = Field(
        description=(
            "検査（requests=要求の充足 / guide=業務ガイドの版 / guide_steps=手順 / "
            "impact=影響範囲）。"
        )
    )
    code: str = Field(
        description=(
            "request_missing / request_partial / request_unverified / guide_unavailable / "
            "guide_revision_stale / guide_steps_unmatched / guide_step_unexplained / step_order / "
            "step_wrong_branch / step_dependency_missing / step_following_missing / "
            "impact_scope_missing / approval_missing"
        )
    )
    severity: Literal["error", "warning"] = Field(
        description="error は valid にしない。warning は確かめたい点。"
    )
    message: str
    request_id: str | None = None
    step_id: str | None = None
    related_step_id: str | None = Field(
        default=None, description="関係する手順（前の手順・後の手順）。"
    )


class GuideBodyCoverageValidation(BaseModel):
    execution: Literal["completed", "not_run", "error", "timed_out", "partial", "unknown"]
    status: Literal["explained", "missing", "partial", "unknown"]
    reason: str | None = None


class GuideSourceAssociationValidation(BaseModel):
    execution: Literal["completed", "not_run", "error", "timed_out", "partial", "unknown"]
    linked: bool


class GuideStepValidation(BaseModel):
    body_coverage: GuideBodyCoverageValidation
    source_association: GuideSourceAssociationValidation | None = None


class ValidatedGuideStep(BaseModel):
    """業務ガイドの手順 1 つの、回答での説明の充足（#2708）。"""

    step_id: str
    title: str
    required: bool = Field(
        description="回答の充足で必須の手順か（分かっている条件で決まらない分岐の手順は false）。"
    )
    validation: GuideStepValidation | None = Field(
        default=None,
        description="出典の対応と本文の充足の検査状態。未実行・未完了は不足や誤りの確認を意味しない。古い結果では省略。",
    )
    status: Literal[
        "explained", "partial", "unknown", "reference_only", "unused", "no_evidence", "not_searched"
    ] = Field(
        description=(
            "explained=必須の操作・対象・条件・順序を、根拠で裏付けた説明として確認した / "
            "partial=手順の一部だけ説明を確認した / unknown=説明の充足を検証できていない / "
            "reference_only=根拠の原文を参考として示しただけ / "
            "unused=手順の根拠は渡した根拠にあるが、手順を説明した段落が無い / "
            "no_evidence=手順の根拠が渡した根拠に無い（その手順の根拠を集めていない）/ "
            "not_searched=上限で検索を打ち切り、充足を検証していない。"
        )
    )


class PremiseConsistencyResult(BaseModel):
    """段落が勧める内容と、原質問・確認の答えの条件・制約との両立の比較 1 件の採否。"""

    # 値は ID・状態・語彙の code だけ（#3800。モデルが書いた制約・推奨・原文は返さない）。
    answer_range: ExcerptRange | None = Field(
        default=None,
        description=(
            "渡した answer の中の段落の位置（段落に結び付かない記録・位置を決められない段落は"
            " null）。"
        ),
    )
    status: str | None = Field(
        default=None,
        description="比較の実行（completed / not_run / failed / timed_out）。",
    )
    decision: str = Field(
        description=(
            "採否の理由（conflict_adopted=矛盾として採った / not_conflicting / "
            "execution_prohibition_does_not_prohibit_proposal / scope_unverified / "
            "constraint_quote_unmatched / passage_quote_unmatched / unknown_passage_id / "
            "duplicate_passage_id / missing_passage_id / not_required / deadline_exceeded / "
            "check_failed）。"
        )
    )
    adopted: bool = Field(description="矛盾として判定（contradicted）に採ったか。")
    fit: str | None = Field(default=None, description="比較の判断（fits / conflicts / unknown）。")
    constraint_scope: str | None = Field(
        default=None,
        description="制約の対象（execution_only / proposal / content / unknown）。",
    )
    passage_mode: str | None = Field(
        default=None,
        description="段落の様相（proposal / execution / explanation / unknown）。",
    )


class ValidateAnswerOutput(VersionedOutput):
    valid: bool = Field(
        description=(
            "矛盾・裏付けの無い主張・読めない根拠・error の finding が無く、裏付けのある主張が"
            " 1 つ以上あるか。"
        )
    )
    status: str = Field(
        description=(
            "completed / partial（時間の上限までに一部の段落の監査が終わらず、"
            "その段落は unassessed）/ timed_out（どの段落の監査も終わらなかった）"
            "/ no_claims / no_evidence / input_too_large。"
        )
    )
    counts: dict[str, int] = Field(default_factory=dict, description="判定ごとの段落の数。")
    claims: list[ValidatedClaim] = Field(default_factory=list)
    missing_evidence: list[EvidenceRefInput] = Field(
        default_factory=list, description="見つからない（削除・権限の外）根拠。"
    )
    stale_evidence: list[EvidenceRefInput] = Field(
        default_factory=list, description="文書の古い版の根拠（検索し直す）。"
    )
    evidence_truncated: bool = Field(
        default=False, description="根拠が多く、後ろの根拠を監査に渡しきれなかったか。"
    )
    checks: list[Literal["requests", "guide", "guide_steps", "impact"]] = Field(
        default_factory=list,
        description="行った決定的な検査（requests・guide を渡したときだけ。モデルは呼ばない）。",
    )
    findings: list[ValidationFinding] = Field(
        default_factory=list, description="決定的な検査で見つけた点。"
    )
    guide_revision: int | None = Field(
        default=None, description="検査に使った業務ガイドの今の公開の版。"
    )
    guide_steps: list[ValidatedGuideStep] = Field(
        default_factory=list,
        description=(
            "guide の手順ごとの説明の充足（分かっている条件で外れた分岐の手順は含めない。guide を"
            "渡さないか、段落の判定が出なければ空）。本文の充足検査が完了し、不足を確認した必須の"
            "手順だけを guide_step_unexplained の finding にする。"
            "未実行・未確認は validation に残る。"
        ),
    )
    reused_claims: int = Field(
        default=0,
        description=(
            "answer_trace_ids の回答の監査の判定を使い、モデルに渡さなかった段落の数（#1527）。"
        ),
    )
    premise_checks: list[PremiseConsistencyResult] = Field(
        default_factory=list,
        description=(
            "段落が勧める内容と、query・premises の条件・制約との両立の比較の採否。"
            "adopted=true の段落は claims で contradicted になる。比較しなかったときは"
            " status=not_run の 1 件。"
        ),
    )


class ReadSourceOutput(VersionedOutput):
    evidence_id: str
    document_id: str
    chunk_id: str
    file_name: str | None = None
    chunk_set_id: str | None = None
    recipe_id: str | None = None
    content_kind: str | None = None
    evidence_type: EvidenceType = "text"
    image_ref: EvidenceImageRef | None = None
    image: EvidenceImage | None = Field(
        default=None,
        description=(
            "include_image=true で返した図の画像の大きさ（画像は content の image）。"
            "include_image=false なら null。"
        ),
    )
    image_url: EvidenceImageUrl | None = Field(
        default=None,
        description="include_image_url=true で返した、図をブラウザで開く短命の URL（#1311）。",
    )
    preview: EvidencePreview | None = Field(
        default=None,
        description="include_preview=true で返した、原文をブラウザで開くプレビュー（#2389）。",
    )
    locator: EvidenceLocator
    text: str = Field(
        description=(
            "offset から max_chars 文字に入る最後の文・段落（表は行）の境界までの本文"
            "（1 つの文・行だけで超えるときはその途中まで）。表を途中から読むときは、先頭に"
            "見出しの行（header_range）を付ける。"
        )
    )
    offset: int
    text_length: int
    truncated: bool = Field(description="本文の続きがあるか。")
    next_offset: int | None = Field(
        default=None,
        description="続きを読むときの offset（text の本文の終わりの位置。続きが無ければ null）。",
    )
    header_range: ExcerptRange | None = Field(
        default=None,
        description=(
            "text の先頭に付けた表の見出しの行の、本文の中の位置（文字の [start, end)）。"
            "text の見出しの後ろが offset からの本文。無ければ null。"
        ),
    )
    parent_text: str | None = Field(
        default=None,
        description="親の本文（前後の文脈。先頭から max_chars 文字に入る文・段落の境界まで）。",
    )
    parent_truncated: bool = False
    superseded: bool = Field(
        default=False, description="新しい版に置き換えた文書（旧版）の根拠か。"
    )
    references: list[EvidenceReference] = Field(
        default_factory=list,
        description="本文が参照する節・文書（#1280。続きは rag_search で参照先を検索する）。",
    )


def _metadata_str(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value.strip() or None if isinstance(value, str) else None


def _metadata_int(metadata: dict[str, Any], key: str) -> int | None:
    value = metadata.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _answer_use(metadata: Mapping[str, Any]) -> Literal["confirmed", "reference"] | None:
    """回答での使われ方（``evidence_answer_use``。#2040）。回答に使っていない箇所は null。"""
    if metadata.get("evidence_model_used") is not True:
        return None
    value = metadata.get("evidence_answer_use")
    if value == "confirmed":
        return "confirmed"
    if value == "reference":
        return "reference"
    return None


def _table_cells(metadata: dict[str, Any]) -> list[EvidenceTableCell]:
    """表の 1 行の chunk の値のあるセル（``table_cells_json``。#3262）。壊れた値は空にする。"""
    raw = metadata.get(TABLE_CELLS_KEY)
    if not isinstance(raw, str) or not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    cells: list[EvidenceTableCell] = []
    for item in items if isinstance(items, list) else []:
        # [列, 表頭の段] か、解析の表のセルの領域つきの [列, 表頭の段, bbox]（#3429）。
        if not isinstance(item, list) or len(item) not in {2, 3}:
            return []
        column, path = item[0], item[1]
        if not isinstance(column, str) or not column or not isinstance(path, list):
            return []
        if not all(isinstance(part, str) for part in path):
            return []
        bbox = bbox_numbers(item[2]) if len(item) == 3 else None
        if len(item) == 3 and bbox is None:
            return []
        cells.append(EvidenceTableCell(table_col=column, header_path=path, bbox=bbox))
    return cells


def _table_rows(
    metadata: dict[str, Any], sheet_name: str | None
) -> tuple[RowBasis, int | None, int | None, int | None] | None:
    """行の番号の基準・表の番号・行の範囲（#3262）。原本の行の位置が無い根拠は None。

    表計算はシート名があれば、CSV は表頭の行（``csv_header_row``）があれば、基準を記録する前の
    chunk でも基準が決まる。CSV の行は記録の番号（RFC 7111 の行）で、記録の番号を記録する前の
    chunk は行を返さない（物理行は ``_source_lines``）。Word は 1 つの表だけを含むまとまりで取込が
    記録したときだけ。解析の表の格子（parsed_table。#3429）も、取込が格子と本文の行を対応させて
    記録したときだけ（``rag_parser_core.parsed_table``）。
    """
    basis = _metadata_str(metadata, ROW_BASIS_KEY)
    if sheet_name:
        return (
            ROW_BASIS_SHEET,
            _metadata_int(metadata, "sheet_table_index"),
            _metadata_int(metadata, "row_start"),
            _metadata_int(metadata, "row_end"),
        )
    if basis == ROW_BASIS_CSV or (
        basis is None and _metadata_int(metadata, "csv_header_row") is not None
    ):
        start = _metadata_int(metadata, TABLE_ROW_START_KEY)
        end = _metadata_int(metadata, TABLE_ROW_END_KEY)
        if start is None or end is None or end < start:
            return ROW_BASIS_CSV, None, None, None
        return ROW_BASIS_CSV, None, start, end
    if basis in {ROW_BASIS_WORD_TABLE, ROW_BASIS_PARSED_TABLE}:
        table_index = _metadata_int(metadata, TABLE_INDEX_KEY)
        start = _metadata_int(metadata, TABLE_ROW_START_KEY)
        end = _metadata_int(metadata, TABLE_ROW_END_KEY)
        if table_index is None or start is None or end is None or end < start:
            return None
        return cast("RowBasis", basis), table_index, start, end
    return None


def _source_lines(metadata: dict[str, Any]) -> tuple[int | None, int | None]:
    """CSV の根拠の原本の物理行の範囲（記録する前の chunk は ``row_start`` / ``row_end``）。"""
    start = _metadata_int(metadata, SOURCE_LINE_START_KEY)
    end = _metadata_int(metadata, SOURCE_LINE_END_KEY)
    if start is None:
        start, end = _metadata_int(metadata, "row_start"), _metadata_int(metadata, "row_end")
    if start is None or end is None or end < start:
        return None, None
    return start, end


def evidence_row_location(metadata: dict[str, Any]) -> dict[str, Any]:
    """根拠の行の番号の基準と行の範囲（``EvidenceLocator`` と同じ名前・値。行が分からなければ空）。

    評価の必要な根拠の行の条件（``row_basis``・``row_start``・``row_end``）を、MCP の根拠の場所と
    同じ行で照合するために使う（#3547。chunk の metadata の ``row_start`` は CSV では物理行、解析の
    表の格子の行は ``table_row_start`` にある）。
    """
    rows = _table_rows(metadata, _metadata_str(metadata, "sheet_name"))
    if rows is None or rows[2] is None:
        return {}
    basis, _table_index, row_start, row_end = rows
    return {"row_basis": basis, "row_start": row_start, "row_end": row_end}


def _locator(metadata: dict[str, Any]) -> EvidenceLocator:
    section = metadata.get("section_path")
    if isinstance(section, list):
        path = [str(item).strip() for item in section if str(item).strip()]
    elif isinstance(section, str):
        path = [part.strip() for part in section.split(" > ") if part.strip()]
    else:
        path = []
    page_start = _metadata_int(metadata, "page_start") or _metadata_int(metadata, "page_number")
    page_end = _metadata_int(metadata, "page_end") or page_start
    sheet_name = _metadata_str(metadata, "sheet_name")
    rows = _table_rows(metadata, sheet_name)
    basis, table_index, row_start, row_end = rows if rows is not None else (None, None, None, None)
    source_lines = _source_lines(metadata) if basis == ROW_BASIS_CSV else (None, None)
    # セルの座標は表計算・CSV の 1 行の記録だけが持つ（取込で記録したときだけ。推測しない）。
    table_row = (
        _metadata_int(metadata, TABLE_ROW_KEY)
        if basis in {ROW_BASIS_SHEET, ROW_BASIS_CSV, ROW_BASIS_PARSED_TABLE}
        else None
    )
    return EvidenceLocator(
        section_path=path,
        page_start=page_start,
        page_end=page_end,
        sheet_name=sheet_name,
        row_basis=basis,
        table_index=table_index,
        row_start=row_start,
        row_end=row_end,
        source_line_start=source_lines[0],
        source_line_end=source_lines[1],
        cell_range=_metadata_str(metadata, "cell_range") if basis == ROW_BASIS_SHEET else None,
        page_label_start=_metadata_str(metadata, "page_label_start") if page_start else None,
        page_label_end=_metadata_str(metadata, "page_label_end") if page_start else None,
        bbox=bbox_numbers(metadata.get("bbox")),
        bbox_unit=_metadata_str(metadata, "bbox_unit")
        if bbox_numbers(metadata.get("bbox"))
        else None,
        element_locator=str(element) if (element := chunk_element_locator(metadata)) else None,
        table_row=table_row,
        table_cells=_table_cells(metadata) if table_row is not None else [],
    )


async def _with_extraction_recipe_ids(
    oracle: OracleClient | None, chunks: list[RetrievedChunk]
) -> list[RetrievedChunk]:
    """根拠の chunk の metadata に解析の結果の ID を入れる（要素の定位子を作るため。#1330）。

    chunk の行には解析の結果の ID が無く、chunk_set の列にある。読めなかったときは定位子を
    付けずに続ける（検索の結果は返す）。
    """
    chunk_set_ids = [
        value for chunk in chunks if (value := _metadata_str(chunk.metadata, "chunk_set_id"))
    ]
    if not chunk_set_ids:
        return chunks
    try:
        client = oracle if oracle is not None else OracleClient()
        extraction_ids = await client.chunk_set_extraction_recipe_ids(chunk_set_ids)
    except Exception:  # noqa: BLE001 - 定位子は補助の情報。検索の結果を止めない
        logger.warning("rag_mcp_extraction_recipe_lookup_failed", exc_info=True)
        return chunks
    enriched: list[RetrievedChunk] = []
    for chunk in chunks:
        extraction_id = extraction_ids.get(_metadata_str(chunk.metadata, "chunk_set_id") or "")
        if extraction_id is None:
            enriched.append(chunk)
            continue
        metadata = {
            "document_id": chunk.document_id,
            **chunk.metadata,
            "extraction_recipe_id": extraction_id,
        }
        enriched.append(chunk.model_copy(update={"metadata": metadata}))
    return enriched


def _references(metadata: dict[str, Any]) -> list[EvidenceReference]:
    """chunk の metadata に保存した交差参照（#1280）を根拠の参照にする。"""
    return [
        EvidenceReference(
            label=target.label,
            document_title=target.document_title,
            section_path=list(split_section_path(target.section_path)),
            resolved=target.resolved,
        )
        for target in reference_targets(metadata)
    ]


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _evidence_type(metadata: dict[str, Any]) -> EvidenceType:
    """根拠の種類（#1282。図の種類は `figure_evidence_type`）。"""
    if _metadata_str(metadata, "content_kind") == "table":
        return "table"
    return figure_evidence_type(metadata) or "text"


def _image_ref(
    chunk: RetrievedChunk, metadata: dict[str, Any], region: FigureRegion | None
) -> EvidenceImageRef | None:
    if region is None:
        return None
    return EvidenceImageRef(
        document_id=chunk.document_id,
        chunk_id=chunk.chunk_id,
        chunk_set_id=_metadata_str(metadata, "chunk_set_id"),
        page=region.page,
        page_label=_metadata_str(metadata, "page_label_start"),
        bbox=list(region.bbox),
        bbox_unit=region.unit,
    )


def _evidence_section(
    found: tuple[SectionNode, SectionNode | None] | None,
) -> EvidenceSection | None:
    if found is None:
        return None
    node, parent = found
    return EvidenceSection(
        section_id=node.section_id,
        title=node.title,
        level=node.level,
        page_start=node.page_start,
        page_end=node.page_end,
        chars=node.chars,
        chunk_count=node.chunk_count,
        parent_section_id=node.parent_section_id,
        parent_title=parent.title if parent is not None else None,
        parent_chars=parent.chars if parent is not None else None,
    )


async def _chunk_sections(
    chunks: Sequence[RetrievedChunk],
) -> dict[str, tuple[SectionNode, SectionNode | None]]:
    """根拠の chunk の所属の章節（#1438）。読めなかったときは付けずに続ける（検索の結果は返す）。"""
    # 対応は chunk_set ごとにある（chunk_set の無い旧い保存の chunk は対象にしない）。
    chunk_ids = [
        chunk.chunk_id for chunk in chunks if _metadata_str(chunk.metadata, "chunk_set_id")
    ]
    if not chunk_ids:
        return {}
    try:
        return await evidence_sections(chunk_ids)
    except Exception:  # noqa: BLE001 - 章節は補助の情報。検索の結果を止めない
        logger.warning("rag_mcp_evidence_sections_lookup_failed", exc_info=True)
        return {}


def _evidence(
    chunk: RetrievedChunk,
    sections: dict[str, tuple[SectionNode, SectionNode | None]] | None = None,
) -> RagEvidence:
    """根拠 1 件（本文は空。`_fit_evidence` が予算に入れて埋める）。"""
    metadata = dict(chunk.metadata)
    return RagEvidence(
        evidence_id=chunk.chunk_id,
        document_id=chunk.document_id,
        chunk_id=chunk.chunk_id,
        file_name=chunk.file_name,
        chunk_set_id=_metadata_str(metadata, "chunk_set_id"),
        recipe_id=_metadata_str(metadata, "recipe_id"),
        content_kind=_metadata_str(metadata, "content_kind"),
        evidence_type=_evidence_type(metadata),
        image_ref=_image_ref(chunk, metadata, figure_region(metadata)),
        locator=_locator(metadata),
        excerpt="",
        truncated=False,
        text_length=len(chunk.text),
        used_in_answer=metadata.get("evidence_model_used") is True,
        answer_use=_answer_use(metadata),
        role=_metadata_str(metadata, "evidence_role"),
        score=chunk.score,
        rerank_score=chunk.rerank_score
        if chunk.rerank_score is not None
        else _float_or_none(metadata.get("rerank_score")),
        retrieval_rank=_metadata_int(metadata, "evidence_retrieval_rank"),
        # 派生の判定は取込・章節と同じ（#2590・#2641）。
        origin="derived"
        if is_derived_content(metadata.get("content_kind"), metadata)
        else "source",
        table_binding=_table_binding(chunk.text, metadata),
        superseded=metadata.get("document_superseded") is True,
        references=_references(metadata),
        reference_from=_metadata_str(metadata, REFERENCE_FROM_KEY),
        reference_label=_metadata_str(metadata, REFERENCE_LABEL_KEY),
        section=_evidence_section((sections or {}).get(chunk.chunk_id)),
    )


def _is_table_evidence(metadata: dict[str, Any]) -> bool:
    """表の根拠か（表の chunk・表計算と CSV の表の 1 行の記録）。"""
    return (
        _metadata_str(metadata, "content_kind") == "table"
        or _metadata_str(metadata, "sheet_block_kind") == "row"
        or _metadata_str(metadata, "record_kind") == "row"
        or _metadata_int(metadata, TABLE_ROW_KEY) is not None
    )


def _table_binding(text: str, metadata: dict[str, Any]) -> TableBinding | None:
    """表の行・列の対応の検査がこの根拠の本文を表として読めるか（#3262）。

    検査（``rag_engine.generation.table_binding``）と同じ読み方で、本文から 2 行以上の表を読めれば
    checkable。表の根拠なのに読めなければ not_checkable（検査が働かないことを呼び出し元に示す）。
    """
    if evidence_tables(text):
        return "checkable"
    return "not_checkable" if _is_table_evidence(metadata) else None


def _answer_range(
    claim: Mapping[str, Any], passage_ranges: Mapping[str, tuple[int, int]], answer: str
) -> ExcerptRange | None:
    """段落の判定の、回答の中の位置（段落の ID の位置。ID が無ければ原文が回答に 1 回だけ
    現れる位置）。

    位置の原文が段落の原文と違えば null（呼び出し側は原文で探す）。
    """
    quote = str(claim.get("answer_quote") or "")
    if not quote:
        return None
    span = passage_ranges.get(str(claim.get("answer_passage_id") or ""))
    if span is None and answer.count(quote) == 1:
        start = answer.find(quote)
        span = (start, start + len(quote))
    if span is None or answer[span[0] : span[1]] != quote:
        return None
    return ExcerptRange(start=span[0], end=span[1])


def _passage_range(
    passage_id: object, passage_ranges: Mapping[str, tuple[int, int]], answer: str
) -> ExcerptRange | None:
    """段落の ID の、回答の中の位置（ID が無い・位置が回答の範囲の外なら null）。"""
    span = passage_ranges.get(str(passage_id or ""))
    if span is None or not 0 <= span[0] < span[1] <= len(answer):
        return None
    return ExcerptRange(start=span[0], end=span[1])


def _excerpt_range(value: object) -> ExcerptRange | None:
    start, end = getattr(value, "start", None), getattr(value, "end", None)
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    return ExcerptRange(start=start, end=end)


def _with_text(evidence: RagEvidence, piece: SourceText) -> RagEvidence:
    return evidence.model_copy(
        update={
            "excerpt": piece.text,
            "truncated": piece.truncated,
            "text_omitted": piece.text_omitted,
            "text_length": piece.text_length,
            "excerpt_range": _excerpt_range(piece.excerpt_range),
            "header_range": _excerpt_range(piece.header_range),
            "next_offset": piece.next_offset,
        }
    )


def _output_tokens(output: BaseModel) -> int:
    """出力の token の見積もり（MCP が返す JSON の形）。"""
    return estimate_input_tokens(output.model_dump_json())


def _json_overhead_tokens(text: str) -> int:
    """本文を JSON の文字列にしたときに増える分（エスケープ）と、一覧の区切りの token。"""
    escaped = json.dumps(text, ensure_ascii=False)[1:-1]
    return estimate_input_tokens(escaped) - estimate_input_tokens(text) + 1


def _fit_evidence(
    chunks: Sequence[RetrievedChunk],
    sections: dict[str, tuple[SectionNode, SectionNode | None]] | None,
    *,
    queries: Sequence[str | None],
    reserved_tokens: int,
) -> tuple[list[RagEvidence], int]:
    """根拠の本文を応答の予算に入れる（#3321）。根拠と、本文を省いた数を返す。

    予算は応答の全体（`reserved_tokens` は根拠の外の項目の分）。根拠の本文の外の項目（ID・場所・
    章節など）は省いた根拠にも残るので、先に引いてから本文を順位の順に入れる。
    """
    evidence = [_evidence(chunk, sections) for chunk in chunks]
    fitted = fit_source_texts(
        [
            SourceTextItem(
                text=chunk.text,
                kind="table" if item.evidence_type == "table" else "text",
                fixed_tokens=_output_tokens(item) + _json_overhead_tokens(chunk.text),
            )
            for chunk, item in zip(chunks, evidence, strict=True)
        ],
        budget_tokens=EVIDENCE_TEXT_BUDGET_TOKENS - reserved_tokens,
        item_max_tokens=EVIDENCE_TEXT_ITEM_MAX_TOKENS,
        queries=queries,
    )
    filled = [_with_text(item, piece) for item, piece in zip(evidence, fitted, strict=True)]
    return filled, sum(1 for piece in fitted if piece.text_omitted)


def _is_catalog_article(chunk: RetrievedChunk) -> bool:
    return _metadata_str(chunk.metadata, "evidence_role") == CATALOG_ARTICLE_ROLE


async def _catalog_article_evidence(
    result: SearchResponse, citations: Sequence[RetrievedChunk], *, limit: int
) -> list[RetrievedChunk]:
    """回答の検索が条の目録で選んだ条(#2254)のうち、根拠に無い条の chunk(選んだ順に limit 件まで)。

    選んだ条は回答の診断の ``article_catalog`` にある。検索と同じ見え方の条件で読み、読めない条は
    足さない。役割は ``catalog_article``。選んだ条の数は検索の設定(文書の数 × 1 文書の条の数)で
    決まり、全部を足すと業務 Agent のモデルの入力の上限を超えることがあるので、
    ``rag_mcp_catalog_article_evidence_limit`` 件で切る(選んだ順は各文書の条を順に回した順)。
    """
    if limit <= 0:
        return []
    catalog = _answer_diagnostics(result).get("article_catalog")
    articles = catalog.get("articles") if isinstance(catalog, dict) else None
    if not isinstance(articles, list):
        return []
    seen = {chunk.chunk_id for chunk in citations}
    oracle: OracleClient | None = None
    added: list[RetrievedChunk] = []
    for article in sorted(
        (item for item in articles if isinstance(item, dict)),
        key=lambda item: int(item.get("order") or 0),
    ):
        chunk_id = str(article.get("chunk_id") or "")
        document_id = str(article.get("document_id") or "")
        if not chunk_id or not document_id or chunk_id in seen:
            continue
        seen.add(chunk_id)
        oracle = oracle or OracleClient()
        try:
            chunk = await oracle.retrievable_chunk(document_id, chunk_id)
        except Exception:  # noqa: BLE001 - 選んだ条は補助。読めない条は足さない。
            logger.warning("catalog article load failed", exc_info=True)
            continue
        if chunk is not None:
            metadata = {**chunk.metadata, "evidence_role": CATALOG_ARTICLE_ROLE}
            added.append(chunk.model_copy(update={"metadata": metadata}))
            if len(added) >= limit:
                break
    return added


def _answer_diagnostics(result: SearchResponse) -> dict[str, Any]:
    diagnostics = result.diagnostics
    answer = diagnostics.answer if diagnostics is not None else None
    return dict(answer) if isinstance(answer, dict) else {}


# 検索で当たった chunk（関連度の順位を持つ）の役割。前後の文脈（neighbor_context など）と分ける。
RETRIEVED_ANCHOR_ROLE = "retrieved_anchor"
# 実体の 1 段の拡張で足した chunk の役割（#1362）。検索で当たった chunk と同じく順位を持つ。
ENTITY_EXPANSION_ROLE = "entity_expansion"
_RANKED_ROLES = frozenset({RETRIEVED_ANCHOR_ROLE, ENTITY_EXPANSION_ROLE})


# evidence_limit のうち、実体の 1 段の拡張で足した根拠に確保する割合（#1362）。
ENTITY_EXPANSION_LIMIT_SHARE = 0.3
# evidence_limit のうち、検索で当たった上位の chunk の前後の文脈・章の参照の先に確保する割合
# （#1390）。
LINKED_CONTEXT_LIMIT_SHARE = 0.3
# 前後の文脈・章の参照の先をたどる、検索で当たった上位の chunk の数（回答の検索で参照をたどる起点
# と同じ 5 件。``answer_engine._REFERENCE_SOURCE_ANCHORS``）。
LINKED_CONTEXT_ANCHORS = 5


def _is_entity_expansion(chunk: RetrievedChunk) -> bool:
    """実体の 1 段の拡張で足した根拠か（役割が前後の文脈になったものも含む。#1362）。"""
    metadata = chunk.metadata
    return _metadata_str(metadata, "evidence_role") == ENTITY_EXPANSION_ROLE or isinstance(
        metadata.get("entity_expansion"), dict
    )


def _rerank_score(chunk: RetrievedChunk) -> float | None:
    if chunk.rerank_score is not None:
        return chunk.rerank_score
    return _float_or_none(chunk.metadata.get("rerank_score"))


def _is_reserved_entity_expansion(chunk: RetrievedChunk, *, reranked: bool) -> bool:
    """evidence_limit の内に枠を確保する拡張の根拠か（#1362・#1390）。

    回答の文脈の枠（rag_engine の ``is_reserved_entity_expansion``）と同じ規則で、rerank の関連度
    が起点の種類（質問の実体 / 上位の chunk の実体）の下限以上のもの。質問と関係の薄い属性の
    chunk（別のシステムの台帳の行・質問と別の章）で枠を使わない。関連度の無い拡張の根拠は、
    rerank を実行した検索（``reranked``）では起点にならず前後の文脈に入ったもの（同じ親の確保
    する根拠があればその文脈として確保する）で、rerank を実行しなかった検索では確保する（#1362
    と同じ）。
    """
    if not _is_entity_expansion(chunk):
        return False
    score = _rerank_score(chunk)
    if score is None:
        return not reranked
    return entity_expansion_relevant(score, chunk.metadata.get("entity_expansion"))


def _evidence_group(chunk: RetrievedChunk) -> tuple[str, str] | None:
    """根拠の親のかたまり（文書と chunk_group_id）。無ければ None。"""
    group = _metadata_str(chunk.metadata, "chunk_group_id")
    return (chunk.document_id, group) if group else None


def _same_version(left: RetrievedChunk, right: RetrievedChunk) -> bool:
    """同じ文書の同じ版（chunk_set）の chunk か（版の分からない chunk は文書だけで比べる）。"""
    if left.document_id != right.document_id:
        return False
    left_set = _metadata_str(left.metadata, "chunk_set_id")
    right_set = _metadata_str(right.metadata, "chunk_set_id")
    return left_set is None or right_set is None or left_set == right_set


def _same_document_targets(chunk: RetrievedChunk) -> list[str]:
    """chunk が本文で参照する、同じ文書の節（見出しの列。#1280・#1382）。"""
    return [
        target.section_path
        for target in reference_targets(chunk.metadata)
        if target.document_title is None and target.section_path
    ]


def _referenced_document_names(chunk: RetrievedChunk) -> list[str]:
    """chunk が本文で文書名だけで参照する別の文書の名前（「定期保守計画で確かめます」。#1400）。"""
    names = [
        target.document_title
        for target in reference_targets(chunk.metadata)
        if target.kind == "document" and target.document_title
    ]
    names.extend(
        spec.document_title for spec in document_name_references(chunk.text) if spec.document_title
    )
    return list(dict.fromkeys(names))


def _referenced_by_name(hit: RetrievedChunk, context: RetrievedChunk) -> bool:
    """当たった chunk が文書名で参照する文書の、回答の検索が参照先として足した chunk か（#1400）。

    参照先として足した chunk（``reference_from_chunk_id``）は、回答の検索が参照先の文書の中から
    質問に関連の高い順に選んだもの。起点が別の候補でも、文書名で参照する当たった chunk の参照の
    先として確保する。確保するのは参照先 1 つにつき最も関連の高い 1 件（``reference_rank`` が 1）
    だけ（2 件目は質問と関係の薄い節・台帳の別の行が多く、当たった chunk を上限の外へ押し出す）。
    文書は文書名とタイトル（見出しの列の先頭）で照合する（``document_name_match``）。
    """
    if (
        context.document_id == hit.document_id
        or not _metadata_str(context.metadata, REFERENCE_FROM_KEY)
        or _metadata_int(context.metadata, REFERENCE_RANK_KEY) != 1
    ):
        return False
    root = split_section_path(context.metadata.get("section_path"))[:1]
    return any(
        document_name_match(name, context.file_name or "", root) is not None
        for name in _referenced_document_names(hit)
    )


def _references_between(hit: RetrievedChunk, context: RetrievedChunk) -> bool:
    """当たった chunk と前後の文脈が章の参照でつながるか（#1390）。

    当たった chunk が参照する節の chunk（「第 2 章の共通の保守枠で保守します」→ 第 2 章）、
    当たった chunk の節を参照する chunk（「連絡の手段は第 3 章を参照」→ 当たった第 3 章）、
    回答の検索が当たった chunk の参照先として足した chunk（``reference_from_chunk_id``。他の
    文書の参照も含む）、当たった chunk が文書名で参照する文書の、参照先として足した chunk
    （「時間帯は定期保守計画で確かめます」→ 定期保守計画の共通の保守枠。#1400）。
    """
    if _metadata_str(context.metadata, REFERENCE_FROM_KEY) == hit.chunk_id:
        # 文書名だけの参照の参照先は、最も関連の高い 1 件だけ（2 件目以降は確保しない。#1400）。
        rank = _metadata_int(context.metadata, REFERENCE_RANK_KEY)
        return rank is None or rank == 1
    if _referenced_by_name(hit, context):
        return True
    if not _same_version(hit, context):
        return False
    context_path = context.metadata.get("section_path")
    hit_path = hit.metadata.get("section_path")
    outgoing = any(section_path_within(context_path, path) for path in _same_document_targets(hit))
    return outgoing or any(
        section_path_within(hit_path, path) for path in _same_document_targets(context)
    )


def _adjacent(hit: RetrievedChunk, context: RetrievedChunk) -> bool:
    """同じ文書の同じ版で、当たった chunk のすぐ前・すぐ後の chunk か（chunk の番号の差が 1）。"""
    if not _same_version(hit, context):
        return False
    left = _metadata_int(hit.metadata, "chunk_index")
    right = _metadata_int(context.metadata, "chunk_index")
    return left is not None and right is not None and abs(left - right) == 1


def _linked_contexts(ordered: Sequence[RetrievedChunk], tiers: Sequence[int]) -> list[int]:
    """検索で当たった上位の chunk の章の参照の先と前後の文脈（``ordered`` の位置。#1390）。

    起点は当たった chunk（回答に使った根拠と、関連度の順位を持つ根拠。拡張の根拠は除く）の上位
    ``LINKED_CONTEXT_ANCHORS`` 件。確保する順は、先に章の参照でつながる chunk（起点の順）、次に
    すぐ前・すぐ後の chunk（起点の順）。対象は前後の文脈（3 の段）の根拠と、回答の検索が参照先と
    して足した chunk（``reference_from_chunk_id``。関連度の順位が起点より下のもの。#1400）。
    """
    hits = [
        chunk
        for chunk, tier in zip(ordered, tiers, strict=True)
        if tier < 2 and not _is_entity_expansion(chunk)
    ][:LINKED_CONTEXT_ANCHORS]
    hit_ids = {chunk.chunk_id for chunk in hits}
    contexts = [
        index
        for index, tier in enumerate(tiers)
        if tier == 2
        or (
            tier == 1
            and ordered[index].chunk_id not in hit_ids
            and _metadata_str(ordered[index].metadata, REFERENCE_FROM_KEY) is not None
        )
    ]
    linked: list[int] = []
    for related in (_references_between, _adjacent):
        for hit in hits:
            linked.extend(
                index for index in contexts if index not in linked and related(hit, ordered[index])
            )
    return linked


def mcp_evidence_order(
    citations: Sequence[RetrievedChunk], limit: int | None = None
) -> list[RetrievedChunk]:
    """MCP の根拠の並び（evidence_limit で切る前。#1348）。

    検索の結果（citations）は、文書ごとのかたまりの中を文書の順（前後の文脈を含む）に並べている
    （画面の検索結果・回答の文脈の並び）。そのまま上限で切ると、上位の文書の前置き・前の章で枠が
    埋まり、検索で当たった chunk が落ちる。MCP では次の順にしてから切る。

    1. 回答に使った根拠（used_in_answer）。回答に使った順（citations の順）を保つ。
    2. 検索で当たった chunk（role=retrieved_anchor と、役割の無い根拠）と、実体からたどった chunk
       （role=entity_expansion。#1362）。関連度の順位（rerank の後の順。実体の拡張は候補の中の位置。
       ``evidence_retrieval_rank``）の順で、順位が無ければ citations の順。
    3. 前後の文脈（neighbor_context / same_page_context / parent_context など）。citations の順。

    ``limit``（evidence_limit）を渡すと、上限の内に次の枠をこの順に確保する（すでに上限の内の 2 の
    根拠は動かさない。確保した根拠は 2 の後・3 の前に置き、上限の内に収まらないときは上限の末尾に
    置く）。

    - 検索で当たった上位の chunk の章の参照の先・すぐ前とすぐ後の chunk（3 の段の根拠）を、上限の
      ``LINKED_CONTEXT_LIMIT_SHARE`` の割合（最低 1 件）まで（#1390。``_linked_contexts``）。
    - 実体の 1 段の拡張で足した根拠（台帳の行・略号の表。親の文脈の役割になったものも含む）のうち、
      rerank の関連度が下限以上のもの（``_is_reserved_entity_expansion``）と、その根拠と同じ親の
      かたまり（文書と ``chunk_group_id``）の前後の文脈（略号の表の続きなど）を、上限の
      ``ENTITY_EXPANSION_LIMIT_SHARE`` の割合（最低 1 件）まで（#1362）。関連度の低い拡張の根拠は
      確保せず、2 の関連度の順位のまま。
    """

    def key(item: tuple[int, RetrievedChunk]) -> tuple[int, int, int]:
        position, chunk = item
        metadata = chunk.metadata
        if metadata.get("evidence_model_used") is True:
            return (0, 0, position)
        role = _metadata_str(metadata, "evidence_role")
        if role is not None and role not in _RANKED_ROLES:
            return (2, 0, position)
        rank = _metadata_int(metadata, "evidence_retrieval_rank")
        return (1, rank if rank is not None and rank > 0 else sys.maxsize, position)

    keyed = sorted(enumerate(citations), key=key)
    ordered = [chunk for _, chunk in keyed]
    if limit is None or limit <= 0 or len(ordered) <= limit:
        return ordered
    tiers = [key((0, chunk))[0] for chunk in ordered]
    # rerank を実行した検索か（関連度を持つ根拠がある）。
    reranked = any(_rerank_score(chunk) is not None for chunk in ordered)
    expansions = [
        index
        for index, chunk in enumerate(ordered)
        if _is_reserved_entity_expansion(chunk, reranked=reranked)
    ]
    # 拡張の根拠と同じ親のかたまりの前後の文脈（略号の表の続きなど）も、拡張の根拠の後に確保する。
    groups = {_evidence_group(ordered[index]) for index in expansions} - {None}
    expansion_set = set(expansions)
    contexts = [
        index
        for index, chunk in enumerate(ordered)
        if index not in expansion_set and tiers[index] == 2 and _evidence_group(chunk) in groups
    ]
    # 確保する根拠（上限の内の 2 の根拠はその位置のまま、それ以外は 2 の後へ移す）。
    fixed: set[int] = set()
    moved: list[int] = []
    for reserved, share in (
        (_linked_contexts(ordered, tiers), LINKED_CONTEXT_LIMIT_SHARE),
        ([*expansions, *contexts], ENTITY_EXPANSION_LIMIT_SHARE),
    ):
        candidates = [index for index in reserved if index not in fixed and index not in moved]
        quota = min(len(candidates), max(1, math.ceil(limit * share)))
        for index in candidates[:quota]:
            if index < limit and tiers[index] < 2:
                fixed.add(index)
            else:
                moved.append(index)
    if not any(index >= limit for index in moved):
        return ordered
    # 上限の内: 確保しない根拠を上限の残りの数だけ元の順で残し、2 の段（と上限の内の確保した 2 の
    # 根拠）、確保して移す根拠、残した 3 の段の順に並べる。残りは元の順で後ろへ。
    selected = fixed | set(moved)
    others = [index for index in range(len(ordered)) if index not in selected]
    kept = set(others[: max(0, limit - len(selected))])
    head = [
        index
        for index in range(len(ordered))
        if (index in fixed or index in kept) and tiers[index] < 2
    ]
    tail = [index for index in others if index in kept and tiers[index] == 2]
    after = [index for index in others if index not in kept]
    return [ordered[index] for index in (*head, *moved, *tail, *after)]


def retrieve_evidence_limit(arguments: RetrieveEvidenceInput) -> int:
    """rag_retrieve_evidence が返す根拠の件数（#1365）。

    evidence_limit を省略して top_k を既定より大きくしたときは top_k まで返す（検索で当たった
    chunk を全部返す。上限は EVIDENCE_LIMIT_MAX）。渡した evidence_limit はそのまま使う。
    """
    if (
        "evidence_limit" in arguments.model_fields_set
        or "top_k" not in arguments.model_fields_set
        or arguments.top_k is None
    ):
        return arguments.evidence_limit
    return min(max(arguments.evidence_limit, arguments.top_k), EVIDENCE_LIMIT_MAX)


def _answer_fields(result: SearchResponse, evidence_limit: int) -> dict[str, Any]:
    """rag_search の出力の項目（根拠の本文は予算に入れてから足すので evidence は空）。"""
    # 回答に使った根拠を先に、検索で当たった chunk、前後の文脈の順にする（#1348）。
    ordered = mcp_evidence_order(result.citations, evidence_limit)
    answer = _answer_diagnostics(result)
    reason = answer.get("insufficient_reason")
    raw_envelope = answer.get("envelope")
    envelope: dict[str, Any] = raw_envelope if isinstance(raw_envelope, dict) else {}
    outcome = envelope.get("outcome") or answer.get("outcome")
    if outcome not in get_args(AnswerOutcome):
        # 構造の無い回答（古い記録など）は、引用と人の確認の印から決める。
        if not result.citations:
            outcome = "insufficient_evidence"
        else:
            outcome = "conditional" if answer.get("needs_human_review") is True else "answered"
    return {
        "answer": result.answer,
        "trace_id": result.trace_id,
        "guardrail_warnings": list(result.guardrail_warnings),
        "guardrail_degraded": _guardrail_degraded(result),
        "outcome": outcome,
        "requests": [
            request
            for request in envelope.get("requests", [])
            if isinstance(request, dict)
            and request.get("status") in {"addressed", "partial", "missing", "unknown"}
        ],
        "conditions": _strings(envelope.get("conditions")),
        "gaps": _strings(envelope.get("gaps")),
        "confirmations": _strings(envelope.get("confirmations")),
        "clarifications": [
            item for item in envelope.get("clarifications", []) if isinstance(item, dict)
        ],
        "guide": _guide_ref(answer.get("guide")),
        "insufficient_reason": reason.strip() or None if isinstance(reason, str) else None,
        "needs_human_review": answer.get("needs_human_review") is True,
        # 段落の印・外した段落・回答全体の警告（#2738）。古い記録は envelope から読み、無ければ空。
        **passage_mark_record(
            answer if any(key in answer for key in PASSAGE_MARK_RECORD_KEYS) else envelope
        ),
        "degraded_steps": _degraded_steps(answer),
        "zero_evidence": _zero_evidence(answer, result),
        "evidence": [],
        "evidence_omitted": max(0, len(ordered) - evidence_limit),
        "provenance": _provenance(answer.get("provenance")),
    }


def _degraded_steps(answer: Mapping[str, Any]) -> list[DegradedStep]:
    """回答の診断の degraded_steps（#3259）の処理の種類と回数だけ（例外の型の名前は出さない）。"""
    counts: dict[str, int] = {}
    raw = answer.get("degraded_steps")
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind, count = item.get("kind"), item.get("count")
        if (
            isinstance(kind, str)
            and kind
            and isinstance(count, int)
            and not isinstance(count, bool)
        ):
            counts[kind] = counts.get(kind, 0) + max(0, count)
    return [DegradedStep(kind=kind, count=count) for kind, count in counts.items() if count]


def _guardrail_degraded(result: SearchResponse) -> bool:
    """安全チェック（OCI Guardrails）を完了できず止めたか（検索の診断の guardrail_degraded）。"""
    diagnostics = result.diagnostics
    return diagnostics is not None and diagnostics.guardrail_degraded


def _zero_evidence(answer: Mapping[str, Any], result: SearchResponse) -> ZeroEvidence | None:
    """根拠 0 件の診断（#2401）の段と件数だけ（根拠があれば None）。

    検索の条件の項目・値、検索ごとの記録は出さない（質問・文書の内容を含みうる）。縮退した補助の
    処理は ``degraded_steps`` に別に出す。
    """
    if result.citations:
        return None
    raw = answer.get("zero_evidence")
    if not isinstance(raw, Mapping):
        return None
    stage = raw.get("stage")
    if stage not in get_args(ZeroEvidenceStage):
        return None
    return ZeroEvidence(
        stage=cast(ZeroEvidenceStage, stage),
        candidate_count=_count(raw.get("candidate_count")),
        rejected_count=_count(raw.get("rejected_count")),
    )


def _count(value: object) -> int:
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _provenance(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    profile = value.get("search_answer_profile")
    profile = profile if isinstance(profile, dict) else {}

    def text(item: object) -> str | None:
        return item if isinstance(item, str) and item else None

    return {
        "search_answer_profile_id": text(profile.get("id")),
        "search_answer_profile_updated_at": text(profile.get("updated_at")),
        "search_answer_profile_config_sha256": text(profile.get("config_sha256")),
        "prompt_version": text(value.get("prompt_version")),
    }


def _guide_conditions(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _guide_ref(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value.get("guide_id"):
        return None
    return {
        "guide_id": str(value["guide_id"]),
        "revision": int(value.get("revision") or 0),
        "title": str(value.get("title") or ""),
        "decision": value.get("decision") or "answer",
        "known_conditions": _guide_conditions(value.get("known_conditions")),
        "unknown_conditions": _guide_conditions(value.get("unknown_conditions")),
        "applicability": (
            dict(value["applicability"]) if isinstance(value.get("applicability"), dict) else {}
        ),
    }


def _strings(value: object) -> list[str]:
    return (
        [item for item in value if isinstance(item, str) and item]
        if isinstance(value, list)
        else []
    )


def _text_kind(metadata: dict[str, Any]) -> SourceTextKind:
    """本文の切り方の種類（表は行の境界で切り、続きに見出しの行を付ける。#3344）。"""
    return "table" if _evidence_type(metadata) == "table" else "text"


async def crop_figure(
    oracle: OracleClient,
    chunk: RetrievedChunk,
    metadata: dict[str, Any],
    region: FigureRegion | None,
) -> BoundedCrop:
    """図の領域を、解析に使ったファイル（処理レシピの artifact）から上限つきで切り出す（#1282）。

    MCP の content の画像（include_image）と、署名つきの URL の読み取り（#1311）で共用する。
    """
    if region is None:
        raise McpToolError(
            IMAGE_NOT_AVAILABLE_CODE,
            "この根拠には元の図の画像がありません（図ではないか、図の場所が分かりません）。",
        )
    try:
        source = await load_parsed_source(
            oracle, chunk.document_id, _metadata_str(metadata, "recipe_id")
        )
    except (DocumentSourceNotFoundError, ValueError) as exc:
        raise McpToolError(
            IMAGE_SOURCE_MISSING_CODE,
            "図を切り出す元のファイルが見つかりません。文書を再処理してください。",
            status=404,
        ) from exc
    try:
        return await asyncio.to_thread(
            crop_png_bounded,
            source,
            region.page,
            region.bbox,
            region.page_size,
            max_edge=IMAGE_MAX_EDGE_PX,
            max_bytes=IMAGE_MAX_BYTES,
        )
    except CropTooLargeError as exc:
        raise McpToolError(
            IMAGE_TOO_LARGE_CODE,
            "図の画像が大きすぎて返せません。頁と領域（image_ref）で元の文書を確かめてください。",
            status=413,
        ) from exc
    except ValueError as exc:
        raise McpToolError(
            IMAGE_NOT_AVAILABLE_CODE,
            "元の図を切り出せませんでした（ファイルの形式か図の場所が対応していません）。",
        ) from exc


async def _read_image(
    oracle: OracleClient,
    chunk: RetrievedChunk,
    metadata: dict[str, Any],
    region: FigureRegion | None,
) -> tuple[EvidenceImage, dict[str, Any]]:
    """図の領域を切り出し、大きさと MCP の content の image のブロックを返す（#1282）。"""
    crop = await crop_figure(oracle, chunk, metadata, region)
    image = EvidenceImage(
        width=crop.width,
        height=crop.height,
        byte_size=len(crop.png),
        sha256=hashlib.sha256(crop.png).hexdigest(),
    )
    block = {
        "type": "image",
        "data": base64.b64encode(crop.png).decode("ascii"),
        "mimeType": IMAGE_MIME_TYPE,
    }
    return image, block


async def readable_chunk(oracle: OracleClient, document_id: str, chunk_id: str) -> RetrievedChunk:
    """検索と同じ見え方の条件で chunk を読み直す（見えない・古い版は区別できるエラー）。

    rag_read_source と、図の署名つきの URL の読み取り（#1311）で同じ判定を使う。
    """
    chunk = await oracle.retrievable_chunk(document_id, chunk_id)
    if chunk is not None:
        return chunk
    if await oracle.accessible_chunk_exists(document_id, chunk_id):
        raise McpToolError(
            SOURCE_STALE_CODE,
            "この根拠は文書の古い版のものです。rag_search で検索し直してください。",
        )
    raise McpToolError(
        SOURCE_NOT_FOUND_CODE,
        "根拠が見つかりません（削除されたか、利用できる範囲の外です）。",
    )


async def readable_element_chunk(
    oracle: OracleClient, document_id: str, locator: str
) -> RetrievedChunk:
    """要素の定位子が指す要素を含む今の chunk を、検索と同じ見え方の条件で読む（#1330）。"""
    parsed = parse_element_locator(locator)
    if parsed is None or parsed.document_id != document_id:
        raise McpToolError(SOURCE_NOT_FOUND_CODE, "根拠の定位子の形が違います。")
    chunk = await oracle.retrievable_element_chunk(
        document_id, parsed.extraction_recipe_id, parsed.element_id
    )
    if chunk is not None:
        metadata = {
            "document_id": chunk.document_id,
            **chunk.metadata,
            "extraction_recipe_id": parsed.extraction_recipe_id,
        }
        return chunk.model_copy(update={"metadata": metadata})
    if await oracle.accessible_document_exists(document_id):
        raise McpToolError(
            SOURCE_STALE_CODE,
            "この根拠は文書の古い解析の結果のものです。rag_search で検索し直してください。",
        )
    raise McpToolError(
        SOURCE_NOT_FOUND_CODE,
        "根拠が見つかりません（削除されたか、利用できる範囲の外です）。",
    )


def _read_cursor(chunk_set_id: str, chunk_index: int, offset: int = 0) -> str:
    return f"{chunk_set_id[:CURSOR_CHUNK_SET_CHARS]}:{chunk_index}:{offset}"


@dataclass(frozen=True)
class _Cursor:
    """続きの位置を分けたもの（#2499）。section_id は rag_read_section の cursor だけが持つ。"""

    chunk_set_prefix: str
    section_id: str | None
    order: int
    offset: int


def _parse_cursor(cursor: str, *, section: bool) -> _Cursor:
    """next_cursor を分ける。形が違えば、どう直せばよいかを書いた cursor_invalid にする。"""
    parts = cursor.strip().split(":")
    numbers = parts[-2:]
    valid = len(parts) >= 3 and all(part.isdigit() for part in numbers) and bool(parts[0])
    if valid and section and len(parts) == 3:
        raise McpToolError(
            CURSOR_INVALID_CODE,
            "この cursor は rag_read_document の続きの位置です。rag_read_document に渡すか、"
            "section_id を渡して章節の最初から読み直してください。",
        )
    if valid and not section and len(parts) > 3:
        raise McpToolError(
            CURSOR_INVALID_CODE,
            "この cursor は rag_read_section の続きの位置です。rag_read_section に"
            "（document_id と一緒に）渡してください。",
        )
    if not valid or (section and not ":".join(parts[1:-2])):
        tool = "rag_read_section" if section else "rag_read_document"
        raise McpToolError(
            CURSOR_INVALID_CODE,
            f"cursor の形が違います。直前の {tool} が返した next_cursor を、1 字も変えずにそのまま"
            "渡してください。分からなければ section_id を渡して章節の最初から読み直してください。",
        )
    return _Cursor(
        chunk_set_prefix=parts[0],
        section_id=":".join(parts[1:-2]) if section else None,
        order=int(numbers[0]),
        offset=int(numbers[1]),
    )


async def _cursor_chunk_set(oracle: OracleClient, document_id: str, prefix: str) -> str | None:
    """cursor の chunk_set の先頭の字から、文書の chunk_set を引き直す。

    1 つに決まらなければ None。
    """
    found = await SectionMapStore(oracle).chunk_set_ids_with_prefix(document_id, prefix)
    return found[0] if len(found) == 1 else None


async def _first_chunk_or_missing(
    oracle: OracleClient, document_id: str, *, stale: bool
) -> McpToolError:
    """読める chunk が無いときのエラー（見える文書なら古い位置、見えなければ無い）。"""
    if stale and await oracle.accessible_document_exists(document_id):
        return McpToolError(
            SOURCE_STALE_CODE,
            "この位置は文書の古い版のものです。rag_outline か rag_search で読み直してください。",
        )
    return McpToolError(
        SOURCE_NOT_FOUND_CODE,
        "文書が見つかりません（削除されたか、利用できる範囲の外か、まだ検索の対象になっていません）。",
    )


def _outline_section(outline: SectionOutline, node: SectionNode) -> OutlineSection:
    return OutlineSection(
        section_id=node.section_id,
        title=node.title,
        level=node.level,
        parent_section_id=node.parent_section_id,
        path=outline.path(node),
        page_start=node.page_start,
        page_end=node.page_end,
        chunk_count=node.chunk_count,
        chars=node.chars,
    )


def _subtree(outline: SectionOutline, section_id: str | None) -> list[SectionNode]:
    """章節の一覧（根を除く）。section_id があればその章節と下の章節だけ（文書の順）。"""
    nodes = [node for node in outline.sections if node.section_id != ROOT_SECTION_ID]
    if section_id is None or section_id == ROOT_SECTION_ID:
        return nodes
    if outline.get(section_id) is None:
        raise McpToolError(
            SECTION_NOT_FOUND_CODE,
            "その章節は文書にありません。rag_outline で章節の id を確かめてください。",
        )
    keep = {section_id}
    for node in nodes:
        if node.parent_section_id in keep:
            keep.add(node.section_id)
    return [node for node in nodes if node.section_id in keep]


async def readable_documents(arguments: ReadableDocumentsInput) -> ReadableDocumentsOutput:
    """保存した根拠の文書のうち、呼び出した利用者が今見られる文書（#3252）。

    Agent の画面が Run・会話の保存した根拠を出し直すとき、画面を見ている利用者を ``sub`` にして
    呼ぶ（内部のツール。``tools/list`` に出さない）。条件は検索・``rag_read_source`` と同じ見え方
    （tenant・ナレッジベースの範囲。データの範囲の claim があればその範囲）で、削除した文書は
    含まない。
    """
    readable = await OracleClient().accessible_document_ids(arguments.document_ids)
    return ReadableDocumentsOutput(
        readable_document_ids=list(
            dict.fromkeys(item for item in arguments.document_ids if item in readable)
        )
    )


async def outline_document(arguments: OutlineInput) -> OutlineOutput:
    """文書の章節の構成を、章節ナビゲーションの章節から返す（#1332 / #1438）。

    format=compact は章節を 1 行ずつの目録で返す（条の多い文書から読む条を選ぶため。#2252）。
    """
    oracle = OracleClient()
    outline = await section_outline(arguments.document_id, oracle=oracle)
    if outline is None:
        raise await _first_chunk_or_missing(oracle, arguments.document_id, stale=False)
    root = outline.get(ROOT_SECTION_ID)
    nodes = _subtree(outline, arguments.section_id)
    common = {
        "document_id": arguments.document_id,
        "file_name": outline.chunk_set.file_name,
        "chunk_set_id": outline.chunk_set.chunk_set_id,
        "chunk_count": root.chunk_count if root is not None else 0,
        "chars": root.chars if root is not None else 0,
        "page_start": root.page_start if root is not None else None,
        "page_end": root.page_end if root is not None else None,
        "superseded": outline.chunk_set.superseded,
    }
    if arguments.format == "compact":
        lines, omitted = await compact_section_lines(
            oracle, outline, nodes, arguments.lead_chars, max_chars=OUTLINE_COMPACT_MAX_CHARS
        )
        return OutlineOutput(**common, sections=[], lines=lines, sections_omitted=omitted)
    sections = [_outline_section(outline, node) for node in nodes]
    return OutlineOutput(
        **common,
        sections=sections[:OUTLINE_MAX_SECTIONS],
        sections_omitted=max(0, len(sections) - OUTLINE_MAX_SECTIONS),
    )


def _section_cursor(chunk_set_id: str, section_id: str, order: int, offset: int = 0) -> str:
    return f"{chunk_set_id[:CURSOR_CHUNK_SET_CHARS]}:{section_id}:{order}:{offset}"


async def _section_start(
    oracle: OracleClient, arguments: ReadSectionInput
) -> tuple[SectionOutline, SectionNode, int, int]:
    """読む章節と、読み始めの (章節の中の順, 文字の位置)。"""
    if arguments.cursor is not None:
        cursor = _parse_cursor(arguments.cursor, section=True)
        chunk_set_id = await _cursor_chunk_set(
            oracle, arguments.document_id, cursor.chunk_set_prefix
        )
        outline = (
            await section_outline(arguments.document_id, chunk_set_id=chunk_set_id, oracle=oracle)
            if chunk_set_id is not None
            else None
        )
        node = outline.get(cursor.section_id or "") if outline is not None else None
        if outline is None or node is None:
            raise await _first_chunk_or_missing(oracle, arguments.document_id, stale=True)
        return outline, node, cursor.order, cursor.offset
    if arguments.chunk_id is not None:
        found = await chunk_section(arguments.document_id, arguments.chunk_id, oracle=oracle)
        if found is None:
            # 見えない・古い版の根拠は rag_read_source と同じエラーにする。
            await readable_chunk(oracle, arguments.document_id, arguments.chunk_id)
            raise McpToolError(
                SECTION_NOT_FOUND_CODE,
                "この根拠の章節が見つかりません。rag_read_source で根拠の本文を読んでください。",
            )
        chunk_set_id, section_id = found
        outline = await section_outline(
            arguments.document_id, chunk_set_id=chunk_set_id, oracle=oracle
        )
        node = outline.get(section_id) if outline is not None else None
    else:
        outline = await section_outline(arguments.document_id, oracle=oracle)
        if outline is None:
            raise await _first_chunk_or_missing(oracle, arguments.document_id, stale=False)
        node = outline.get(arguments.section_id or "")
    if outline is None or node is None:
        raise McpToolError(
            SECTION_NOT_FOUND_CODE,
            "その章節は文書にありません。rag_outline で章節の id を確かめてください。",
        )
    return outline, outline.ancestor(node, arguments.up), 0, 0


async def read_section_tool(arguments: ReadSectionInput) -> ReadSectionOutput:
    """章節の本文を、章節の中の順に max_chars まで返す（章節の終わりで止まる。#1438）。"""
    oracle = OracleClient()
    outline, node, order, offset = await _section_start(oracle, arguments)
    chunk_set_id = outline.chunk_set.chunk_set_id
    store = SectionMapStore(oracle)
    budget = arguments.max_chars
    parts: list[str] = []
    length = 0
    chunks: list[ReadDocumentChunk] = []
    last_page: int | None = None
    next_cursor: str | None = None
    first = True
    while next_cursor is None:
        rows: list[SectionChunkRow] = await store.section_chunks(
            arguments.document_id,
            chunk_set_id=chunk_set_id,
            section_id=node.section_id,
            from_order=order,
            limit=READ_DOCUMENT_BATCH,
        )
        if not rows:
            if first and arguments.cursor is not None:
                raise await _first_chunk_or_missing(oracle, arguments.document_id, stale=True)
            break
        enriched = await _with_extraction_recipe_ids(oracle, [row.chunk for row in rows])
        for row, chunk in zip(rows, enriched, strict=True):
            metadata = dict(chunk.metadata)
            page_start = _metadata_int(metadata, "page_start") or _metadata_int(
                metadata, "page_number"
            )
            marker = f"--- p.{page_start} ---\n" if page_start and page_start != last_page else ""
            body = chunk.text[offset:] if first else chunk.text
            separator = "\n\n" if parts else ""
            room = budget - length - len(separator) - len(marker)
            if room <= 0 and not chunks:
                # 頁の区切りの行だけで上限に届くときは、区切りを省いて本文を読み進める。
                marker = ""
                room = budget - length - len(separator)
            if room <= 0 or (len(body) > room and chunks):
                # 入りきらない chunk は次の回の先頭にする（最初の chunk だけは途中で切る）。
                next_cursor = _section_cursor(chunk_set_id, node.section_id, row.chunk_order)
                break
            # 最初の chunk だけは、上限に入る最後の文・段落（表は行）の境界で切る（#3344）。
            piece = read_source_window(
                chunk.text,
                offset=offset if first else 0,
                max_chars=room,
                kind=_text_kind(metadata),
            )
            taken = piece.text
            start = length + len(separator) + len(marker)
            parts.append(separator + marker + taken)
            length = start + len(taken)
            locator = _locator(metadata)
            chunks.append(
                ReadDocumentChunk(
                    chunk_id=chunk.chunk_id,
                    element_locator=locator.element_locator,
                    section_path=locator.section_path,
                    page_start=locator.page_start,
                    page_end=locator.page_end,
                    sheet_name=locator.sheet_name,
                    row_basis=locator.row_basis,
                    table_index=locator.table_index,
                    row_start=locator.row_start,
                    row_end=locator.row_end,
                    source_line_start=locator.source_line_start,
                    source_line_end=locator.source_line_end,
                    cell_range=locator.cell_range,
                    start=start,
                    end=length,
                    header_range=_excerpt_range(piece.header_range),
                )
            )
            if page_start:
                last_page = _metadata_int(metadata, "page_end") or page_start
            used = piece.excerpt_range.end if piece.excerpt_range else len(chunk.text)
            first = False
            offset = 0
            if used < len(chunk.text):
                next_cursor = _section_cursor(chunk_set_id, node.section_id, row.chunk_order, used)
                break
            order = row.chunk_order + 1
        if len(rows) < READ_DOCUMENT_BATCH:
            break
    if next_cursor is None and order < node.chunk_count:
        # 最後の batch がちょうど上限の件数だったとき、続きがあるかを確かめる。
        more = await store.section_chunks(
            arguments.document_id,
            chunk_set_id=chunk_set_id,
            section_id=node.section_id,
            from_order=order,
            limit=1,
        )
        if more:
            next_cursor = _section_cursor(chunk_set_id, node.section_id, more[0].chunk_order)
    return ReadSectionOutput(
        document_id=arguments.document_id,
        file_name=outline.chunk_set.file_name,
        chunk_set_id=chunk_set_id,
        section=_outline_section(outline, node),
        child_sections=[
            _outline_section(outline, child)
            for child in outline.children(node.section_id)[:READ_SECTION_MAX_CHILDREN]
        ],
        text="".join(parts),
        chunks=chunks,
        section_complete=next_cursor is None,
        next_cursor=next_cursor,
        superseded=outline.chunk_set.superseded,
    )


async def _read_start(
    oracle: OracleClient, arguments: ReadDocumentInput
) -> tuple[str, int, int, bool]:
    """読み始めの (chunk_set_id, chunk_index, 文字の位置, 古い位置を指せるか)。"""
    if arguments.cursor is not None:
        cursor = _parse_cursor(arguments.cursor, section=False)
        chunk_set_id = await _cursor_chunk_set(
            oracle, arguments.document_id, cursor.chunk_set_prefix
        )
        if chunk_set_id is None:
            raise await _first_chunk_or_missing(oracle, arguments.document_id, stale=True)
        return chunk_set_id, cursor.order, cursor.offset, True
    if arguments.locator is not None:
        chunk = await readable_element_chunk(oracle, arguments.document_id, arguments.locator)
        chunk_set_id = _metadata_str(dict(chunk.metadata), "chunk_set_id") or ""
        index = _metadata_int(dict(chunk.metadata), "chunk_index") or 0
        return chunk_set_id, index, 0, True
    index_chunk_set_id, rows = await oracle.document_reading_index(arguments.document_id)
    if index_chunk_set_id is None or not rows:
        raise await _first_chunk_or_missing(oracle, arguments.document_id, stale=False)
    target = rows[0]
    if arguments.page is not None:
        page = arguments.page
        matched = [
            row
            for row in rows
            if isinstance(row.get("page_end") or row.get("page_start"), int)
            and int(str(row.get("page_end") or row.get("page_start"))) >= page
        ]
        if not matched:
            raise McpToolError(SOURCE_NOT_FOUND_CODE, f"文書に {page} 頁はありません。")
        target = matched[0]
    elif arguments.section_id is not None:
        outline = await section_outline(
            arguments.document_id, chunk_set_id=index_chunk_set_id, oracle=oracle
        )
        node = outline.get(arguments.section_id) if outline is not None else None
        if node is None:
            raise McpToolError(
                SECTION_NOT_FOUND_CODE,
                "その章節は文書にありません。rag_outline で章節の id を確かめてください。",
            )
        if node.first_chunk_index is None:
            raise McpToolError(SECTION_NOT_FOUND_CODE, "その章節には本文がありません。")
        return index_chunk_set_id, node.first_chunk_index, 0, False
    return index_chunk_set_id, int(str(target["chunk_index"])), 0, False


async def read_document(arguments: ReadDocumentInput) -> ReadDocumentOutput:
    """文書の本文を、読み始めの位置から chunk の順に max_chars まで返す（#1332）。

    頁が変わる所に「--- p.N ---」の行を入れ、返した本文に含まれる chunk（chunk_id・定位子・位置）を
    添える。続きは next_cursor で読む（cursor は chunk_set に縛り、作り直された後は source_stale）。
    """
    oracle = OracleClient()
    chunk_set_id, index, offset, from_position = await _read_start(oracle, arguments)
    budget = arguments.max_chars
    parts: list[str] = []
    length = 0
    chunks: list[ReadDocumentChunk] = []
    last_page: int | None = None
    next_cursor: str | None = None
    file_name: str | None = None
    superseded = False
    first = True
    skipped = False
    while True:
        batch = await oracle.readable_document_chunks(
            arguments.document_id,
            chunk_set_id=chunk_set_id,
            from_index=index,
            limit=READ_DOCUMENT_BATCH,
        )
        if not batch:
            if first and not skipped:
                raise await _first_chunk_or_missing(
                    oracle, arguments.document_id, stale=from_position
                )
            break
        batch = await _with_extraction_recipe_ids(oracle, batch)
        stop = False
        for chunk in batch:
            metadata = dict(chunk.metadata)
            chunk_index = _metadata_int(metadata, "chunk_index") or index
            if is_derived_chunk(chunk):
                # 派生の chunk(章節の要約・RAPTOR の要約・項目抽出の値)は文書の本文ではないので
                # 返さない(#2641)。
                index = chunk_index + 1
                offset = 0
                skipped = True
                continue
            if first:
                file_name = chunk.file_name
                superseded = metadata.get("document_superseded") is True
            page_start = _metadata_int(metadata, "page_start") or _metadata_int(
                metadata, "page_number"
            )
            marker = f"--- p.{page_start} ---\n" if page_start and page_start != last_page else ""
            body = chunk.text[offset:] if first else chunk.text
            separator = "\n\n" if parts else ""
            room = budget - length - len(separator) - len(marker)
            if room <= 0 and not chunks:
                # 頁の区切りの行だけで上限に届くときは、区切りを省いて本文を読み進める。
                marker = ""
                room = budget - length - len(separator)
            if room <= 0 or (len(body) > room and chunks):
                # 入りきらない chunk は次の回の先頭にする（最初の chunk だけは途中で切る）。
                next_cursor = _read_cursor(chunk_set_id, chunk_index, 0)
                stop = True
                break
            # 最初の chunk だけは、上限に入る最後の文・段落（表は行）の境界で切る（#3344）。
            piece = read_source_window(
                chunk.text,
                offset=offset if first else 0,
                max_chars=room,
                kind=_text_kind(metadata),
            )
            taken = piece.text
            start = length + len(separator) + len(marker)
            parts.append(separator + marker + taken)
            length = start + len(taken)
            locator = _locator(metadata)
            chunks.append(
                ReadDocumentChunk(
                    chunk_id=chunk.chunk_id,
                    element_locator=locator.element_locator,
                    section_path=locator.section_path,
                    page_start=locator.page_start,
                    page_end=locator.page_end,
                    sheet_name=locator.sheet_name,
                    row_basis=locator.row_basis,
                    table_index=locator.table_index,
                    row_start=locator.row_start,
                    row_end=locator.row_end,
                    source_line_start=locator.source_line_start,
                    source_line_end=locator.source_line_end,
                    cell_range=locator.cell_range,
                    start=start,
                    end=length,
                    header_range=_excerpt_range(piece.header_range),
                )
            )
            if page_start:
                last_page = _metadata_int(metadata, "page_end") or page_start
            used = piece.excerpt_range.end if piece.excerpt_range else len(chunk.text)
            first = False
            offset = 0
            if used < len(chunk.text):
                next_cursor = _read_cursor(chunk_set_id, chunk_index, used)
                stop = True
                break
            index = chunk_index + 1
        if stop or len(batch) < READ_DOCUMENT_BATCH:
            if not stop:
                next_cursor = None
            break
    if next_cursor is None and chunks:
        # 最後の batch がちょうど上限の件数だったとき、続きがあるかを確かめる。
        more = await oracle.readable_document_chunks(
            arguments.document_id,
            chunk_set_id=chunk_set_id,
            from_index=index,
            limit=READ_DOCUMENT_BATCH,
        )
        # 続きが派生の chunk だけ(文書の末尾の要約など)なら、続きは無い(#2641)。
        if any(not is_derived_chunk(chunk) for chunk in more):
            next_cursor = _read_cursor(chunk_set_id, index, 0)
    return ReadDocumentOutput(
        document_id=arguments.document_id,
        file_name=file_name,
        chunk_set_id=chunk_set_id,
        text="".join(parts),
        chunks=chunks,
        next_cursor=next_cursor,
        superseded=superseded,
    )


@dataclass(frozen=True, slots=True)
class FigureUrlRequest:
    """ブラウザで開く署名つきの URL（図 #1311・原文のプレビュー #2389）を作る文脈。

    MCP の呼び出しの利用者と、URL の起点。
    """

    subject: str
    base_url: str


def _figure_url(
    issuer: FigureUrlRequest | None,
    chunk: RetrievedChunk,
    metadata: dict[str, Any],
    region: FigureRegion | None,
) -> EvidenceImageUrl:
    if region is None:
        raise McpToolError(
            IMAGE_NOT_AVAILABLE_CODE,
            "この根拠には元の図の画像がありません（図ではないか、図の場所が分かりません）。",
        )
    if issuer is None:
        raise McpToolError(
            IMAGE_URL_NOT_ALLOWED_CODE,
            "図を開く URL は、Agent の画面の操作からだけ作れます。",
            status=403,
        )
    try:
        token, expires_at = issue_figure_token(
            get_settings().app_service_token_secret,
            subject=issuer.subject,
            document_id=chunk.document_id,
            chunk_id=chunk.chunk_id,
            chunk_set_id=_metadata_str(metadata, "chunk_set_id"),
        )
    except FigureUrlUnavailableError as exc:
        raise McpToolError(IMAGE_URL_UNAVAILABLE_CODE, str(exc), status=503) from exc
    path = f"{FIGURE_URL_PATH_PREFIX}/{token}"
    return EvidenceImageUrl(
        url=f"{issuer.base_url.rstrip('/')}{path}",
        path=path,
        expires_at=datetime.fromtimestamp(expires_at, tz=UTC).isoformat(),
        expires_in_seconds=FIGURE_URL_TTL_SECONDS,
    )


async def preview_document(
    oracle: OracleClient, document_id: str, recipe_id: str | None
) -> tuple[Any, DocumentPreprocessArtifact | None]:
    """プレビューする文書と、処理後のファイル（処理レシピの根拠はそのレシピの処理後のファイル）。

    呼ぶ前に、根拠の chunk が検索と同じ見え方の条件で見えることを確かめておく
    （``readable_chunk``）。
    """
    detail = await oracle.get_document(document_id)
    if detail is None or getattr(detail, "object_storage_path", None) is None:
        raise McpToolError(SOURCE_NOT_FOUND_CODE, "根拠の文書が見つかりません。")
    artifact: DocumentPreprocessArtifact | None = detail.preprocess_artifact
    if recipe_id:
        row = await oracle.get_document_recipe(document_id, recipe_id)
        if row is None:
            raise McpToolError(SOURCE_NOT_FOUND_CODE, "根拠の処理レシピが見つかりません。")
        raw = row.get("preprocess_artifact")
        artifact = DocumentPreprocessArtifact.model_validate(raw) if raw else None
    return detail, artifact


async def preview_file(
    detail: Any,
    artifact: DocumentPreprocessArtifact | None,
    variant: Literal["original", "prepared"],
) -> bytes:
    """プレビューするファイルの中身（文書の画面のプレビューと同じ読み方）。

    ファイルが保存先に無ければ preview_file_missing のエラー。
    """
    try:
        data, _file_name, _content_type = await documents_route.load_document_content(
            detail, variant=variant, preprocess_artifact=artifact
        )
    except DocumentFileMissingError as exc:
        raise McpToolError(PREVIEW_FILE_MISSING_CODE, str(exc.detail), status=404) from exc
    except HTTPException as exc:
        raise McpToolError(PREVIEW_FILE_MISSING_CODE, str(exc.detail), status=404) from exc
    return data


async def _source_preview(
    issuer: FigureUrlRequest | None,
    oracle: OracleClient,
    chunk: RetrievedChunk,
    metadata: dict[str, Any],
) -> EvidencePreview:
    if issuer is None:
        raise McpToolError(
            PREVIEW_NOT_ALLOWED_CODE,
            "原文のプレビューは、Agent の画面の操作からだけ作れます。",
            status=403,
        )
    recipe_id = _metadata_str(metadata, "recipe_id")
    detail, artifact = await preview_document(oracle, chunk.document_id, recipe_id)
    file_name = str(getattr(detail, "file_name", None) or chunk.file_name or chunk.document_id)
    kind, variant = preview_kind(file_name, artifact)
    try:
        token, expires_at = issue_source_preview_token(
            get_settings().app_service_token_secret,
            subject=issuer.subject,
            document_id=chunk.document_id,
            chunk_id=chunk.chunk_id,
            chunk_set_id=_metadata_str(metadata, "chunk_set_id"),
            recipe_id=recipe_id,
            variant=variant,
        )
    except SignedUrlUnavailableError as exc:
        raise McpToolError(PREVIEW_UNAVAILABLE_CODE, str(exc), status=503) from exc
    pages: list[EvidencePreviewPage] = []
    if kind == "pdf":
        data = await preview_file(detail, artifact, variant)
        try:
            sizes = await asyncio.to_thread(page_sizes, data)
        except ValueError:
            # ページの画像を描けないファイルは、画面が原文の URL をブラウザの PDF 表示で開く。
            sizes = []
        pages = [
            EvidencePreviewPage(page_number=index + 1, width=width, height=height)
            for index, (width, height) in enumerate(sizes)
        ]
    base_path = f"{SOURCE_PREVIEW_PATH_PREFIX}/{token}"
    base_url = issuer.base_url.rstrip("/")
    page_path = f"{base_path}/pages/{{page}}" if pages else None
    return EvidencePreview(
        kind=kind,
        file_name=file_name,
        pages=pages,
        page_url_template=f"{base_url}{page_path}" if page_path else None,
        page_path_template=page_path,
        content_url=f"{base_url}{base_path}/content",
        content_path=f"{base_path}/content",
        highlight_metadata=stored_citation_metadata(metadata),
        expires_at=datetime.fromtimestamp(expires_at, tz=UTC).isoformat(),
        expires_in_seconds=SOURCE_PREVIEW_TTL_SECONDS,
    )


async def read_source(
    arguments: ReadSourceInput, *, figure_url_request: FigureUrlRequest | None = None
) -> ReadSourceOutput | McpToolResult:
    """検索と同じ見え方の条件で根拠の本文を読む（見えない・古い版は区別できるエラー）。

    図の画像（include_image）も、読むたびに同じ条件で chunk を読み直してから切り出す。
    図を開く URL（include_image_url。#1311）は、同じ条件で見える図の根拠にだけ作る。
    """
    oracle = OracleClient()
    if arguments.locator is not None:
        chunk = await readable_element_chunk(oracle, arguments.document_id, arguments.locator)
    else:
        chunk = await readable_chunk(oracle, arguments.document_id, arguments.chunk_id or "")
        chunk = (await _with_extraction_recipe_ids(oracle, [chunk]))[0]
    metadata = dict(chunk.metadata)
    piece = read_source_window(
        chunk.text,
        offset=arguments.offset,
        max_chars=arguments.max_chars,
        kind=_text_kind(metadata),
    )
    parent = metadata.get("parent_text")
    parent_piece = (
        read_source_window(parent, max_chars=arguments.max_chars)
        if isinstance(parent, str) and parent.strip()
        else None
    )
    region = figure_region(metadata)
    image: EvidenceImage | None = None
    image_url: EvidenceImageUrl | None = None
    preview: EvidencePreview | None = None
    blocks: tuple[dict[str, Any], ...] = ()
    if arguments.include_image_url:
        image_url = _figure_url(figure_url_request, chunk, metadata, region)
    if arguments.include_preview:
        preview = await _source_preview(figure_url_request, oracle, chunk, metadata)
    if arguments.include_image:
        image, block = await _read_image(oracle, chunk, metadata, region)
        blocks = (block,)
    output = ReadSourceOutput(
        evidence_id=chunk.chunk_id,
        document_id=chunk.document_id,
        chunk_id=chunk.chunk_id,
        file_name=chunk.file_name,
        chunk_set_id=_metadata_str(metadata, "chunk_set_id"),
        recipe_id=_metadata_str(metadata, "recipe_id"),
        content_kind=_metadata_str(metadata, "content_kind"),
        evidence_type=_evidence_type(metadata),
        image_ref=_image_ref(chunk, metadata, region),
        image=image,
        image_url=image_url,
        preview=preview,
        locator=_locator(metadata),
        text=piece.text,
        offset=arguments.offset,
        text_length=len(chunk.text),
        truncated=piece.next_offset is not None,
        next_offset=piece.next_offset,
        header_range=_excerpt_range(piece.header_range),
        parent_text=parent_piece.text if parent_piece is not None else None,
        parent_truncated=parent_piece is not None and parent_piece.next_offset is not None,
        superseded=metadata.get("document_superseded") is True,
        references=_references(metadata),
    )
    return McpToolResult(output=output, content=blocks) if blocks else output


def _search_request(arguments: SearchInput) -> SearchRequest:
    payload: dict[str, Any] = {
        "query": arguments.query,
        "knowledge_base_ids": arguments.knowledge_base_ids,
        "filters": arguments.filters,
    }
    if arguments.original_question is not None:
        payload["original_question"] = arguments.original_question
    if arguments.include_superseded:
        # 検索の要求の中では旧版の扱いは filters の 1 つ（画面の検索と同じ。#1248）。
        payload["filters"] = {**arguments.filters, INCLUDE_SUPERSEDED_FILTER_KEY: "true"}
    if arguments.search_answer_profile_id is not None:
        payload["search_answer_profile_id"] = arguments.search_answer_profile_id
    if "top_k" in arguments.model_fields_set and arguments.top_k is not None:
        payload["top_k"] = arguments.top_k
    # ルールの確認の答え（rule:）は業務ガイドの条件ではない（rag_search が前提にする。#2117）。
    conditions = {
        key: value
        for key, value in arguments.conditions.items()
        if not key.startswith(RULE_CONDITION_PREFIX)
    }
    if conditions:
        payload["conditions"] = conditions
    try:
        return SearchRequest.model_validate(payload)
    except ValidationError as exc:
        errors = validation_tool_errors(exc.errors(include_url=False, include_input=False))
        raise McpToolError(
            TOOL_ARGUMENTS_INVALID_CODE,
            "検索条件が正しくありません。",
            details={"errors": errors},
        ) from exc


async def rule_clarification_step(
    arguments: SearchInput, request: SearchRequest
) -> tuple[SearchRequest, AnswerScope | None] | SearchOutput:
    """検索・回答プロファイルのルールの回答前の確認（RAG のチャットと同じ判定。#1839・#2117）。

    ``conditions`` の ``rule:<id>`` の答えが今の質問に一致するルールのものなら、回答の前提と
    ``page_ranges`` にして返す（会話の前の答えを別の話題に当てない）。答えが無く、方針が確認で、
    質問に一致したルールが確認を持てば、検索・回答をせずに needs_clarification を返す。
    """
    profile_id = arguments.search_answer_profile_id
    if profile_id is None or arguments.skip_clarification:
        return request, None
    # 利用者の範囲外のプロファイルは、検索と同じく無いもの（None）になる。
    oracle = OracleClient()
    view = await oracle.get_search_answer_profile(profile_id)
    if view is None:
        # プロファイルが無いときの 404 は検索が返す。
        return request, None
    search_route.ensure_search_answer_profile_not_archived(view, profile_id)
    settings, _ = resolve_search_answer_profile_settings(
        get_settings(), view.config, apply_answer_model=False
    )
    with profile_language_scope(settings):
        payload = await load_runtime_knowledge_payload(oracle, profile_id)
        for key, value in arguments.conditions.items():
            rule_id = key.removeprefix(RULE_CONDITION_PREFIX)
            if rule_id == key or not rule_matches(payload, arguments.query, rule_id):
                continue
            answer = rule_answer(payload, rule_id, value)
            resolved = (
                resolve_clarification(
                    payload,
                    answer,
                    await current_section_pages(
                        oracle, clarification_document_ids(payload, answer)
                    ),
                )
                if answer is not None
                else None
            )
            if resolved is None:
                continue
            scope, page_ranges = resolved
            if page_ranges:
                request = request.model_copy(
                    update={"filters": {**request.filters, PAGE_RANGES_FILTER_KEY: page_ranges}}
                )
            return request, scope
        if view.config.clarification_policy == "assume":
            return request, None
        selection_settings, _ = resolve_search_answer_profile_settings(get_settings(), view.config)
        selection_usage: dict[str, float] = {}
        selected_answers = await recognize_rule_selections(
            payload, arguments.query, selection_settings, usage=selection_usage
        )
        request._rule_selection_state["prepared"] = True
        request._rule_selection_state["elapsed_seconds"] = selection_usage.get(
            "elapsed_seconds", 0.0
        )
        request = apply_rule_selection_scope(
            request,
            payload,
            selected_answers,
            current_pages=await current_section_pages(
                oracle,
                {
                    doc
                    for answer in selected_answers
                    for doc in clarification_document_ids(payload, answer)
                },
            )
            if selected_answers
            else None,
        )
        found = await asyncio.to_thread(
            suggest_clarification, payload, arguments.query, answers=selected_answers
        )
        if found is None:
            return request, None
        rule, clarification = found
        rule_id = str(rule.get("id", ""))
        return SearchOutput(
            answer=f"回答の前に確認が必要です: {clarification.question}",
            trace_id=new_trace_id(),
            guardrail_warnings=[],
            outcome="needs_clarification",
            clarifications=[
                GuideClarification(
                    condition_id=f"{RULE_CONDITION_PREFIX}{rule_id}",
                    label=str(rule.get("title", "") or rule_id),
                    question=clarification.question,
                    options=[option.label for option in clarification.options],
                    multiple=clarification.multiple,
                )
            ],
            evidence=[],
            evidence_omitted=0,
        )


async def _older_versions(
    arguments: SearchInput, request: SearchRequest, citations: Sequence[RetrievedChunk]
) -> list[OlderVersion]:
    """根拠の文書（今の版）を置き換えた旧版（#1405。旧版を検索しなかったときだけ）。

    旧版を含めて検索し直したときと同じ範囲（検索の filters と、検索・回答プロファイルの参照
    ナレッジベース。利用者の権限・データの範囲は Oracle の条件が絞る）の中の旧版だけを返す。
    読めなかったときは返さずに続ける（検索の結果は返す）。
    """
    if arguments.include_superseded:
        return []
    current = [
        chunk.document_id
        for chunk in citations
        if chunk.document_id and chunk.metadata.get("document_superseded") is not True
    ]
    if not current:
        return []
    try:
        oracle = OracleClient()
        filters = dict(request.filters)
        if not request.knowledge_base_ids and request.search_answer_profile_id:
            # 検索は検索・回答プロファイルの参照 KB を範囲にする（_resolve_query_context と同じ）。
            view = await oracle.get_search_answer_profile(request.search_answer_profile_id)
            scope = search_route.profile_scope_filters(view) if view is not None else None
            if scope is None:
                return []
            filters.update(scope)
        versions = await oracle.retrieval_older_versions(filters, current, limit=OLDER_VERSIONS_MAX)
    except Exception:  # noqa: BLE001 - 旧版の案内は補助の情報。検索の結果を止めない
        logger.warning("rag_mcp_older_versions_lookup_failed", exc_info=True)
        return []
    return [
        OlderVersion(
            document_id=version.document_id,
            file_name=version.file_name,
            title=version.title,
            superseded_by_document_id=version.superseded_by_document_id,
        )
        for version in versions
    ]


def _knowledge_status(status: str) -> ProfileKnowledgeStatus:
    # 状態が空の登録は承認済みとして使う（回答の生成と同じ有効の判定。無効・下書きは照合で除く）。
    return "stale_review_needed" if status == "stale_review_needed" else "approved"


def _knowledge_rule(rule: RuntimeRule) -> ProfileKnowledgeRule:
    return ProfileKnowledgeRule(
        id=rule.rule_id,
        title=rule.title,
        content=rule.content,
        source=rule.source,
        status=_knowledge_status(rule.status),
        updated_at=rule.updated_at,
    )


def _knowledge_term(term: RuntimeTerm) -> ProfileKnowledgeTerm:
    return ProfileKnowledgeTerm(
        term=term.term,
        aliases=list(term.aliases),
        description=term.description,
        source=term.source,
        status=_knowledge_status(term.status),
        updated_at=term.updated_at,
    )


def _knowledge_revision(payload: Mapping[str, object]) -> str:
    text = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def _profile_knowledge(arguments: RetrieveEvidenceInput) -> ProfileKnowledge | None:
    """元の質問に当たる検索・回答プロファイルの承認済みのルールと用語の説明（#2701）。

    RAG が回答を作らない根拠の取得では、回答を作る呼び出し元（業務 Agent）へ渡す。照合は回答の
    生成と同じ（有効な登録だけ・語の一致。LLM は呼ばない）で、呼び出し元が言い換えた検索文では
    なく利用者の元の質問に当てる。対象は検索に使った検索・回答プロファイルだけ（利用者の範囲外は
    無いものになる）。ルールを先に、件数・字数の上限で切り、省いた数を示す。読めなければ
    ``load_failed`` を返す（当たるものが無いこと〔null〕と区別する）。
    """
    profile_id = arguments.search_answer_profile_id
    if profile_id is None:
        return None
    question = arguments.original_question or arguments.query
    matched_on: Literal["original_question", "query"] = (
        "original_question" if arguments.original_question else "query"
    )
    try:
        oracle = OracleClient()
        view = await oracle.get_search_answer_profile(profile_id)
        if view is None:
            return None
        payload = await load_runtime_knowledge_payload(oracle, view.id)
        context = await asyncio.to_thread(match_runtime_knowledge, payload, question)
    except Exception:  # noqa: BLE001 - 根拠の結果は返し、読めなかったことを示す
        logger.warning("rag_mcp_profile_knowledge_load_failed", exc_info=True)
        context = None
    if context is None or context.error:
        if context is not None:
            logger.warning("rag_mcp_profile_knowledge_invalid")
        return ProfileKnowledge(
            search_answer_profile_id=profile_id,
            revision=None,
            matched_on=matched_on,
            rules=[],
            terms=[],
            rules_omitted=0,
            terms_omitted=0,
            load_failed=True,
        )
    generation_rules = context.rules_for_stage("generation")
    if not (generation_rules or context.matched_terms):
        return None
    remaining = PROFILE_KNOWLEDGE_MAX_CHARS
    rules: list[ProfileKnowledgeRule] = []
    for rule in generation_rules:
        size = len(rule.title) + len(rule.content) + len(rule.source)
        if len(rules) < PROFILE_KNOWLEDGE_RULES_MAX and size <= remaining:
            rules.append(_knowledge_rule(rule))
            remaining -= size
    terms: list[ProfileKnowledgeTerm] = []
    for term in context.matched_terms:
        size = len(term.term) + sum(map(len, term.aliases)) + len(term.description)
        size += len(term.source)
        if len(terms) < PROFILE_KNOWLEDGE_TERMS_MAX and size <= remaining:
            terms.append(_knowledge_term(term))
            remaining -= size
    return ProfileKnowledge(
        search_answer_profile_id=profile_id,
        revision=_knowledge_revision(payload),
        matched_on=matched_on,
        rules=rules,
        terms=terms,
        rules_omitted=len(generation_rules) - len(rules),
        terms_omitted=len(context.matched_terms) - len(terms),
    )


def _profile_knowledge_log(knowledge: ProfileKnowledge | None) -> dict[str, object]:
    """モデルへ渡したルール・用語の記録（数と版だけ。本文・名前は業務の内容なので残さない）。"""
    if knowledge is None:
        return {"profile_knowledge": None}
    return {
        "profile_knowledge": {
            "revision": knowledge.revision,
            "matched_on": knowledge.matched_on,
            "rules": len(knowledge.rules),
            "terms": len(knowledge.terms),
            "rules_omitted": knowledge.rules_omitted,
            "terms_omitted": knowledge.terms_omitted,
            "load_failed": knowledge.load_failed,
        }
    }


def _guide_reference(reference: SupportGuideReference) -> GuideReferenceItem:
    return GuideReferenceItem(
        document_id=reference.document_id,
        title=reference.title,
        section_path=list(reference.section_path),
        page_ranges=[
            GuidePageRangeItem(
                page_start=item.page_start, page_end=item.page_end or item.page_start
            )
            for item in reference.page_ranges
        ],
        sheet_ranges=[
            GuideSheetRangeItem(
                sheet_name=item.sheet_name,
                row_start=item.row_start,
                row_end=item.row_end,
                cell_range=item.cell_range,
            )
            for item in reference.sheet_ranges
        ],
    )


def _lookup_item(match: GuideMatch) -> LookupGuideItem:
    summary = match.summary()
    content = match.content
    # 分かっている条件で外れた分岐の手順は返さない（回答の材料と同じ。#2396）。
    steps, branches = visible_plan(match)
    return LookupGuideItem(
        **summary,
        expected_result=content.goal.expected_result,
        score=match.score,
        clarifications=[GuideClarification(**item) for item in clarification_questions(match)],
        steps=[
            GuideStepItem(
                id=step.id,
                title=step.title,
                purpose=step.purpose,
                depends_on=list(step.depends_on),
                allowed_tools=list(step.allowed_tools),
                retrieval_hints=list(step.retrieval_hints),
                references=[_guide_reference(item) for item in step.references],
                evidence_requirements=list(step.evidence_requirements),
                done_when=step.done_when,
            )
            for step in steps
        ],
        branches=[
            GuideBranchItem(
                id=branch.id,
                condition_id=branch.when.condition_id,
                operator=branch.when.operator,
                values=list(branch.when.values),
                goto_step=branch.goto_step,
                applies=applies,
                note=branch.note,
            )
            for branch, applies in branches
        ],
        references=[_guide_reference(item) for item in content.references],
        completion=[
            GuideCompletionItem(
                id=item.id, description=item.description, check_method=item.check_method
            )
            for item in content.completion
        ],
        impact_scope=content.impact.scope,
        approval_required=content.impact.approval_required,
        impact_steps=list(content.impact.steps),
        impact_applies=impact_applies(match),
        handoff_contact=content.handoff.contact,
    )


def called_with_api_key(http_request: Request) -> bool:
    """利用者の API キー（`prak_…`）で呼んだ外部の MCP クライアントか（#2405）。

    Agent のサービストークン・local（認証なし）の呼び出しは False。
    """
    return getattr(http_request.state, "api_key", None) is not None


def _figure_url_request(http_request: Request) -> FigureUrlRequest | None:
    """ブラウザで開く URL（図 #1311・原文のプレビュー #2389）を作ってよい呼び出しの文脈を返す。

    作ってよければ、その利用者と URL の起点を返す（作れなければ None）。

    URL は Agent の画面の操作（Agent の backend が閲覧者を ``sub`` にし、``purpose`` に
    ``figure_url`` か ``source_preview`` を入れたサービストークンで呼ぶ）でだけ作る。Run
    の中でモデルが
    呼んだときは作らない（URL を Run の記録・モデルの文脈に残さない）。local（認証なし）
    は確かめない。
    """
    principal = getattr(http_request.state, "principal", None)
    subject = getattr(principal, "user_uuid", None)
    if not isinstance(subject, str) or not subject:
        return None
    if not get_settings().local_debug_enabled:
        claims = getattr(http_request.state, "service_token_claims", None)
        if not isinstance(claims, dict) or claims.get("purpose") not in BROWSER_URL_PURPOSES:
            return None
    return FigureUrlRequest(subject=subject, base_url=str(http_request.base_url))


def _check_search_scope(scope: ProfileScope | None, arguments: SearchInput) -> None:
    check_search(scope, arguments.search_answer_profile_id, arguments.knowledge_base_ids)


def _check_guides_scope(scope: ProfileScope | None, arguments: LookupGuidesInput) -> None:
    check_profile(scope, arguments.search_answer_profile_id)


def _claim_chunk_id(source_id: object) -> str | None:
    """判定の根拠の chunk_id（利用者の前提〔premise:…〕だけで裏付けた段落は None。#1898）。"""
    value = str(source_id or "")
    return value if value and not is_premise_id(value) else None


def _check_validate_scope(scope: ProfileScope | None, arguments: ValidateAnswerInput) -> None:
    if arguments.guide is not None:
        check_profile(scope, arguments.guide.search_answer_profile_id)


LIST_PROFILES_TOOL = "rag_list_search_answer_profiles"
RECOMMEND_PROFILE_TOOL = "rag_recommend_search_answer_profile"
QUERY_UNMATCHED_NEXT_STEP = (
    "query に当たる検索・回答プロファイルが無いため、使える全件を返しました。この中から選ぶか、"
    f"{RECOMMEND_PROFILE_TOOL} に質問を渡して選んでください。"
)
NO_PROFILES_NEXT_STEP = (
    "使える検索・回答プロファイルがありません（管理者に利用の範囲を確かめてください）。"
)


async def _active_profiles(query: str | None, limit: int) -> list[SearchAnswerProfileSummary]:
    """利用者が使える（今の監査 context の範囲の）ACTIVE の検索・回答プロファイル。"""
    response = await search_answer_profiles_route.list_search_answer_profiles(
        OffsetParams(limit=limit, offset=0),
        status=SearchAnswerProfileStatus.ACTIVE,
        q=query,
    )
    if response.warning_messages:
        # DB 停止時の縮退（空一覧）は、Agent には「0 件」と区別できるエラーで返す。
        raise HTTPException(status_code=503, detail=response.warning_messages[0])
    page = response.data
    return list(page.items) if page is not None else []


async def profile_not_found_error(search_answer_profile_id: str) -> McpToolError:
    """見つからない・使えない検索・回答プロファイルの id のエラー（次の手つき。#2355）。

    使えるプロファイルは今の監査 context（利用者の権限とデータの範囲）の中だけを数えるので、
    ほかの利用者のプロファイルは案内に出ない。1 つだけならその id を案内する。
    """
    try:
        usable = await _active_profiles(None, 2)
    except HTTPException:
        usable = None  # 一覧を読めなくても、id の誤りと次の手は返す
    message = f"検索・回答プロファイル「{search_answer_profile_id}」は見つからないか、使えません。"
    details: dict[str, Any] = {
        "search_answer_profile_id": search_answer_profile_id,
        "next_tools": [RECOMMEND_PROFILE_TOOL, LIST_PROFILES_TOOL],
    }
    if usable is not None and len(usable) == 1:
        only = usable[0]
        details["suggested_search_answer_profile_id"] = only.id
        message += (
            f"使える検索・回答プロファイルは「{only.name}」（id: {only.id}）だけです。"
            "この id を search_answer_profile_id に渡して呼び直してください。"
        )
    elif usable is not None and not usable:
        details["next_tools"] = []
        message += NO_PROFILES_NEXT_STEP
    else:
        message += (
            f"{RECOMMEND_PROFILE_TOOL} に質問を渡して選ぶか、{LIST_PROFILES_TOOL}（query なし）の"
            "一覧の id を search_answer_profile_id に渡して呼び直してください。"
        )
    return McpToolError(SEARCH_ANSWER_PROFILE_NOT_FOUND_CODE, message, status=404, details=details)


async def _run_search(request: SearchRequest, **options: Any) -> SearchResponse:
    """検索・回答の pipeline を実行し、AI の API の失敗を種類つきのツールのエラーにする（#2475）。

    汎用の内部エラー（`MCP_TOOL_INTERNAL_ERROR`）にすると、呼び出し元（業務 Agent）が検索の失敗を
    「資料に根拠が無い」と区別できない。エラーには種類・HTTP status・工程・例外の型だけを入れ、
    質問・文書・呼び先の応答の本文は入れない（ログも同じ項目だけ）。
    """
    try:
        return await search_route._run_search_with_timeout(request, **options)
    except search_route.AnswerApiFailedHTTPError as exc:
        # 検索の REST と同じ分類（#2667）を、種類つきのツールのエラーにする。
        failure = exc.failure
        cause: BaseException = exc
    except HTTPException:
        raise
    except Exception as exc:
        api_failure = describe_answer_api_failure(exc)
        if api_failure is None:
            raise
        failure, cause = api_failure, exc
    logger.warning(
        "rag_mcp_answer_api_failed",
        extra={"error_code": ANSWER_API_FAILED_ERROR_CODE, **failure.details()},
    )
    raise McpToolError(
        ANSWER_API_FAILED_ERROR_CODE,
        failure.message,
        status=failure.status,
        details=failure.details(),
    ) from cause


def _is_profile_not_found(exc: HTTPException) -> bool:
    """検索の解決（`search_route._resolve_query_context`）の、プロファイルが無い 404 か。"""
    return (
        exc.status_code == 404
        and isinstance(exc.detail, str)
        and exc.detail.startswith(search_route.SEARCH_ANSWER_PROFILE_NOT_FOUND_PREFIX)
    )


# 自動で選べなかったときに、エラーの message に示す推薦の候補の数。
SELECTION_CANDIDATES_SHOWN = 5


async def _select_profile(
    question: str,
) -> tuple[AutoSelectedProfile | None, list[SearchAnswerProfileSummary], list[ProfileCandidate]]:
    """使えるプロファイルが 1 つならそれ、そうでなければ質問の推薦が決めたものを選ぶ（#2355）。

    戻り値は (選んだもの〔決まらなければ None〕, 使えるプロファイル〔最大 2 件〕, 推薦の候補)。
    数えるのは今の監査 context（利用者の権限とデータの範囲）の中だけ。
    """
    usable = await _active_profiles(None, 2)
    if len(usable) == 1:
        only = usable[0]
        return AutoSelectedProfile(id=only.id, name=only.name, method="only_usable"), usable, []
    if not usable:
        return None, usable, []
    result = await recommend_search_answer_profiles(
        question,
        oracle=OracleClient(),
        embedder=OciGenAiClient(settings=get_settings()),
    )
    chosen = next(
        (item for item in result.candidates if item.id == result.recommended_profile_id), None
    )
    if chosen is None:
        return None, usable, list(result.candidates)
    return (
        AutoSelectedProfile(id=chosen.id, name=chosen.name, method="recommended"),
        usable,
        list(result.candidates),
    )


def _selection_error(
    requested: str | None,
    usable: Sequence[SearchAnswerProfileSummary],
    candidates: Sequence[ProfileCandidate],
) -> McpToolError:
    """検索・回答プロファイルを自動で 1 つに決められなかったエラー（候補と次の手つき）。"""
    message = (
        f"検索・回答プロファイル「{requested}」は見つからないか、使えません。"
        if requested is not None
        else ""
    )
    details: dict[str, Any] = {
        "search_answer_profile_id": requested,
        "next_tools": [LIST_PROFILES_TOOL],
    }
    if not usable:
        details["next_tools"] = []
        return McpToolError(
            SEARCH_ANSWER_PROFILE_NOT_FOUND_CODE,
            message + NO_PROFILES_NEXT_STEP,
            status=404,
            details=details,
        )
    shown = list(candidates)[:SELECTION_CANDIDATES_SHOWN]
    if shown:
        details["candidates"] = [{"id": item.id, "name": item.name} for item in shown]
        listed = "、".join(f"「{item.name}」（id: {item.id}）" for item in shown)
        message += (
            f"質問からは検索・回答プロファイルを 1 つに決められませんでした。候補: {listed}。"
            "利用者に確かめるか、合うものの id を search_answer_profile_id に渡して"
            "呼び直してください。"
        )
    else:
        message += (
            "質問からは検索・回答プロファイルを決められませんでした。"
            f"{LIST_PROFILES_TOOL}（query なし）の一覧から選び、その id を"
            " search_answer_profile_id に渡して呼び直してください。"
        )
    return McpToolError(SEARCH_ANSWER_PROFILE_NOT_FOUND_CODE, message, status=404, details=details)


def _with_profile_selection[A: SearchInput, R: SearchOutput | RetrieveEvidenceOutput](
    handler: Callable[[A], Awaitable[R]],
) -> Callable[[A], Awaitable[R]]:
    """検索・回答プロファイルの id を誤ったときに、自動で選んで続ける（#2355）。

    モデルが推測した id で失敗させない: 見つからない・使えない id なら、使えるプロファイルが
    1 つならそれを、そうでなければ質問（original_question、無ければ query）の推薦が決めたものを
    使い、結果の auto_selected_search_answer_profile に示す。決まらなければ、推薦の候補と次の手の
    付いたエラーにする。
    """

    async def run_selected(arguments: A, requested: str | None) -> R:
        selected, usable, candidates = await _select_profile(
            arguments.original_question or arguments.query
        )
        if selected is None:
            raise _selection_error(requested, usable, candidates)
        selected = selected.model_copy(update={"requested_id": requested})
        result = await handler(
            arguments.model_copy(update={"search_answer_profile_id": selected.id})
        )
        return cast(R, result.model_copy(update={"auto_selected_search_answer_profile": selected}))

    async def wrapped(arguments: A) -> R:
        requested = arguments.search_answer_profile_id
        if requested is None:
            # 省略は今までどおり（使えるナレッジベースを全体の既定の設定で検索する。解決の順の
            # 「request の明示 > 検索・回答プロファイル > 全体の既定」）。
            return await handler(arguments)
        try:
            return await handler(arguments)
        except HTTPException as exc:
            if not _is_profile_not_found(exc):
                raise
            try:
                return await run_selected(arguments, requested)
            except McpToolError as error:
                raise error from exc

    return wrapped


def build_rag_mcp_server(http_request: Request) -> McpServer:
    """1 リクエスト分の MCP サーバー（rate limit に呼び出し元の request を使う）。

    handler はデータの範囲（サービストークンの claim。#1379）の判定と、範囲に絞った監査 context の
    中で実行する（`within_profile_scope`。claim が無ければ今までどおり）。
    """

    def scoped[A, R](
        handler: Callable[[A], Awaitable[R]],
        check: Callable[[ProfileScope | None, A], None] | None = None,
    ) -> Callable[[A], Awaitable[R]]:
        return within_profile_scope(http_request, handler, check)

    async def list_search_answer_profiles(
        arguments: ListSearchAnswerProfilesInput,
    ) -> ListSearchAnswerProfilesOutput:
        items = await _active_profiles(arguments.query, arguments.limit)
        query_matched = True
        next_step: str | None = None
        if arguments.query is not None and not items:
            # query が名前・説明に当たらなければ、空で返さずに使える全件から選ばせる（#2355）。
            query_matched = False
            items = await _active_profiles(None, arguments.limit)
            next_step = QUERY_UNMATCHED_NEXT_STEP if items else NO_PROFILES_NEXT_STEP
        return ListSearchAnswerProfilesOutput(
            query_matched=query_matched,
            next_step=next_step,
            search_answer_profiles=[
                SearchAnswerProfileItem(
                    id=view.id,
                    name=view.name,
                    description=view.description,
                    status=view.status.value,
                    knowledge_base_count=view.knowledge_base_count,
                )
                for view in items
            ],
        )

    async def recommend_search_answer_profile(
        arguments: RecommendSearchAnswerProfileInput,
    ) -> RecommendSearchAnswerProfileOutput:
        # 検索を 1 回する（LLM は呼ばない）ので、検索と同じ上限で守る。
        enforce_rate_limit("search", http_request)
        result = await recommend_search_answer_profiles(
            arguments.question,
            oracle=OracleClient(),
            embedder=OciGenAiClient(settings=get_settings()),
            profile_ids=arguments.profile_ids,
            limit=arguments.limit,
        )
        return RecommendSearchAnswerProfileOutput(
            recommended_profile_id=result.recommended_profile_id,
            candidates=[
                RecommendedProfileCandidate(
                    id=item.id,
                    name=item.name,
                    description=item.description,
                    reason=item.reason or None,
                    score=item.score,
                    documents=item.documents,
                )
                for item in result.candidates
            ],
        )

    async def search(arguments: SearchInput) -> SearchOutput:
        request = _search_request(arguments)
        enforce_rate_limit("search", http_request)
        rule = await rule_clarification_step(arguments, request)
        if isinstance(rule, SearchOutput):
            return rule
        request, scope = rule
        result = await _run_search(request, scope=scope)
        citations = await _with_extraction_recipe_ids(None, list(result.citations))
        result = result.model_copy(update={"citations": citations})
        returned = mcp_evidence_order(citations, arguments.evidence_limit)[
            : arguments.evidence_limit
        ]
        sections = await _chunk_sections(returned)
        output = SearchOutput(
            **_answer_fields(result, arguments.evidence_limit),
            older_versions=await _older_versions(arguments, request, citations),
            notice=result.notice if called_with_api_key(http_request) else None,
        )
        evidence, text_omitted = _fit_evidence(
            returned,
            sections,
            queries=[arguments.query, arguments.original_question],
            reserved_tokens=_output_tokens(output),
        )
        return output.model_copy(
            update={"evidence": evidence, "evidence_text_omitted": text_omitted}
        )

    async def lookup_guides(arguments: LookupGuidesInput) -> LookupGuidesOutput:
        oracle = OracleClient()
        view = await oracle.get_search_answer_profile(arguments.search_answer_profile_id)
        if view is None:
            raise await profile_not_found_error(arguments.search_answer_profile_id)
        # rag_search と同じく、アーカイブ済みのプロファイルは使わない（409。#1278）。
        search_route.ensure_search_answer_profile_not_archived(
            view, arguments.search_answer_profile_id
        )
        settings, _ = resolve_search_answer_profile_settings(get_settings(), view.config)
        with profile_language_scope(settings):
            if not settings.rag_support_guide_enabled:
                return LookupGuidesOutput(guides=[])
            guides, failed = await search_route.load_published_guides(oracle, view.id)
            if failed:
                # 読めなかったことを「当たるガイドが無い」（0 件）と区別する（#1278）。
                raise HTTPException(status_code=503, detail=GUIDES_UNAVAILABLE_MESSAGE)
            context = await search_route.support_guide_context(
                oracle, guides, arguments.query, search_route.profile_scope_filters(view)
            )
            # 検索と同じく、用語の別名で展開し、ON なら意味の照合を使う（#2398）。
            query = await build_guide_query(
                oracle,
                settings,
                view.id,
                guides,
                arguments.query,
                await load_runtime_knowledge_payload(oracle, view.id),
            )
            matches = rank_guides(
                guides, query, arguments.conditions, limit=arguments.limit, context=context
            )
            if arguments.conditions:
                # 確認に自由入力で答えた選択式の条件は、答えの文が選んだ選択肢に決める（#2726）。
                resolved = await asyncio.gather(
                    *(
                        resolve_answered_conditions(match, arguments.conditions, settings)
                        for match in matches
                    )
                )
                matches = [item for item in resolved if isinstance(item, GuideMatch)]
            return LookupGuidesOutput(guides=[_lookup_item(match) for match in matches])

    async def retrieve_evidence(arguments: RetrieveEvidenceInput) -> RetrieveEvidenceOutput:
        # planning=rag: 検索の画面・チャットと同じ流れで質問の理解・拡張・文書の検索（rerank
        # を含む）まで行い、その後の根拠の補い（CRAG・章節の拡張・要求ごとの探し直し）と回答の
        # 生成はしない（補いは Agent が rag_read_section などで行う。#3506）。planning=caller
        # （#2651）: 呼び出し元の検索文で検索し、質問に応じた推論の LLM / VLM も呼ばない
        # （hybrid search・rerank・文脈の取得は同じ）。
        request = _search_request(arguments).model_copy(update={"generate_answer": False})
        enforce_rate_limit("search", http_request)
        echo = {"planning": arguments.planning, "request_id": arguments.request_id}
        if arguments.planning == "caller":
            # 検索・回答プロファイルのルールの確認は rag_search と同じ判定（ルールは利用者の質問に
            # 当てる）。答えがあれば、その範囲（頁）に絞って検索する（範囲は広げない）。
            rule = await rule_clarification_step(
                arguments.model_copy(
                    update={"query": arguments.original_question or arguments.query}
                ),
                request,
            )
            if isinstance(rule, SearchOutput):
                return RetrieveEvidenceOutput(
                    trace_id=rule.trace_id,
                    guardrail_warnings=[],
                    evidence=[],
                    evidence_omitted=0,
                    clarifications=rule.clarifications,
                    **echo,
                )
            request, scope = rule
            result = await _run_search(
                request,
                scope=scope,
                caller_queries=list(arguments.queries),
                complete_evidence=False,
            )
        else:
            result = await _run_search(request, complete_evidence=False)
        # 回答を作る呼び出し元へ、元の質問に当たる承認済みのルール・用語の説明を渡す（#2701）。
        knowledge = await _profile_knowledge(arguments)
        logger.info(
            "rag_mcp_retrieve_evidence",
            extra={
                **echo,
                "trace_id": result.trace_id,
                "queries": len(arguments.queries),
                **_profile_knowledge_log(knowledge),
            },
        )
        limit = retrieve_evidence_limit(arguments)
        # 検索で当たった chunk を関連度の順に先に、前後の文脈を後ろにしてから切る（#1348）。
        # 条の目録で選んだ条とそのために足した親の chunk(#2254・#2261)は、上限に数えず後ろに足す
        # (検索の根拠を減らさない)。
        hits = [chunk for chunk in result.citations if not _is_catalog_article(chunk)]
        catalog_limit = get_settings().rag_mcp_catalog_article_evidence_limit
        catalog = [chunk for chunk in result.citations if _is_catalog_article(chunk)][
            :catalog_limit
        ]
        ordered = mcp_evidence_order(hits, limit)
        citations = await _with_extraction_recipe_ids(None, [*ordered[:limit], *catalog])
        citations = [
            *citations,
            *await _catalog_article_evidence(
                result, citations, limit=max(0, catalog_limit - len(catalog))
            ),
        ]
        sections = await _chunk_sections(citations)
        output = RetrieveEvidenceOutput(
            trace_id=result.trace_id,
            guardrail_warnings=list(result.guardrail_warnings),
            guardrail_degraded=_guardrail_degraded(result),
            degraded_steps=_degraded_steps(_answer_diagnostics(result)),
            zero_evidence=_zero_evidence(_answer_diagnostics(result), result),
            evidence=[],
            evidence_omitted=max(0, len(hits) - limit),
            older_versions=await _older_versions(arguments, request, result.citations),
            profile_knowledge=knowledge,
            **echo,
        )
        evidence, text_omitted = _fit_evidence(
            citations,
            sections,
            queries=[arguments.query, arguments.original_question, *arguments.queries],
            reserved_tokens=_output_tokens(output),
        )
        return output.model_copy(
            update={"evidence": evidence, "evidence_text_omitted": text_omitted}
        )

    async def read(arguments: ReadSourceInput) -> ReadSourceOutput | McpToolResult:
        if arguments.include_image or arguments.include_preview:
            # 図の切り出し・プレビューのページの一覧は元のファイルを読むので、
            # 検索と同じ上限で守る（#1282）。
            enforce_rate_limit("search", http_request)
        browser_url = arguments.include_image_url or arguments.include_preview
        return await read_source(
            arguments,
            figure_url_request=_figure_url_request(http_request) if browser_url else None,
        )

    async def validate(arguments: ValidateAnswerInput) -> ValidateAnswerOutput:
        enforce_rate_limit("search", http_request)
        guide = arguments.guide
        try:
            # 回答フローの「資料との照合」と同じ工程の span で包む（呼び出し元の Agent の
            # 処理の経過で、MCP の呼び出しの下に RAG の工程として出る。#3470）。
            with _answer_step_span(ANSWER_VALIDATION_STEP):
                result = await validate_answer(
                    arguments.query,
                    arguments.answer,
                    [EvidenceRef(item.document_id, item.chunk_id) for item in arguments.evidence],
                    get_settings(),
                    requests=(
                        [item.model_dump() for item in arguments.requests]
                        if arguments.requests is not None
                        else None
                    ),
                    gaps=arguments.gaps,
                    answer_trace_ids=arguments.answer_trace_ids,
                    premises=[item.model_dump() for item in arguments.premises],
                    data_results=[item.model_dump() for item in arguments.data_results],
                    guide=(
                        GuideCheckRef(
                            guide.search_answer_profile_id,
                            guide.guide_id,
                            guide.revision,
                            dict(guide.conditions),
                        )
                        if guide is not None
                        else None
                    ),
                )
        except GuideProfileNotFoundError as exc:
            assert guide is not None  # 業務ガイドを渡したときだけ起きる
            raise await profile_not_found_error(guide.search_answer_profile_id) from exc
        passage_ranges = {
            passage_id: (start, end)
            for passage_id, start, end in answer_passage_ranges(arguments.answer)
        }
        return ValidateAnswerOutput(
            valid=result.valid,
            status=result.status,
            counts=result.counts,
            claims=[
                ValidatedClaim(
                    answer_quote=str(claim.get("answer_quote") or ""),
                    # 段落の位置（#3240。呼び出し側が段落の分け方を写さずに段落を特定する）。
                    answer_range=_answer_range(claim, passage_ranges, arguments.answer),
                    status=str(claim.get("status") or ""),
                    chunk_id=_claim_chunk_id(claim.get("source_id")),
                    reason=str(claim.get("reason") or ""),
                    # 監査の判定と ID の照合の結果（#2622。判定を変えた理由を追えるようにする）。
                    audit_status=str(claim.get("audit_status") or "") or None,
                    reference_check=str(claim.get("reference_check") or "") or None,
                    # 段落の分類の食い違いと監査し直し（#2625）。
                    classification_check=str(claim.get("classification_check") or "") or None,
                    # 裏付けを採らなかった理由（#2659。監査の判定は audit_status）。
                    support_check=str(claim.get("support_check") or "") or None,
                    # 表の正しい値に直した段落（#3005）。
                    revised_text=str(claim.get("revised_text") or "") or None,
                )
                for claim in result.claim_checks
            ],
            missing_evidence=[
                EvidenceRefInput(document_id=ref.document_id, chunk_id=ref.chunk_id)
                for ref in result.missing_evidence
            ],
            stale_evidence=[
                EvidenceRefInput(document_id=ref.document_id, chunk_id=ref.chunk_id)
                for ref in result.stale_evidence
            ],
            evidence_truncated=result.evidence_truncated,
            checks=result.checks,
            findings=[ValidationFinding.model_validate(item.as_dict()) for item in result.findings],
            guide_revision=result.guide_revision,
            guide_steps=[ValidatedGuideStep.model_validate(step) for step in result.guide_steps],
            reused_claims=result.reused_claims,
            premise_checks=[
                PremiseConsistencyResult(
                    answer_range=_passage_range(
                        check.get("answer_passage_id"), passage_ranges, arguments.answer
                    ),
                    status=check.get("status"),
                    decision=str(check.get("decision") or ""),
                    adopted=bool(check.get("adopted")),
                    fit=check.get("fit"),
                    constraint_scope=check.get("constraint_scope"),
                    passage_mode=check.get("passage_mode"),
                )
                for check in result.premise_consistency_checks
            ],
        )

    return McpServer(
        name=MCP_SERVER_NAME,
        version=get_settings().app_version,
        instructions=INSTRUCTIONS,
        tools=[
            McpTool(
                name="rag_list_search_answer_profiles",
                title="検索・回答プロファイルの一覧",
                description="利用できる検索・回答プロファイル（ACTIVE）の一覧を返します。",
                input_model=ListSearchAnswerProfilesInput,
                handler=scoped(list_search_answer_profiles),
                output_model=ListSearchAnswerProfilesOutput,
                permissions=(SEARCH_ANSWER_PROFILE_READ_PERMISSIONS,),
            ),
            McpTool(
                name="rag_recommend_search_answer_profile",
                title="検索・回答プロファイルの推薦",
                description=(
                    # 使えるプロファイルの参照先を 1 回検索し、当たった資料と名前・説明で決める
                    # （回答は作らず、LLM も呼ばない）。
                    "質問に合う検索・回答プロファイルを推薦します（回答は作りません）。"
                    "候補が違う資料に割れたとき・当たりが無いときは recommended_profile_id が null"
                    "です。profile_ids を渡すと、その中からだけ推薦します。"
                    # 会話の続きは前のターンのプロファイルを使う（#1880。指示語のままの質問で
                    # 推薦し直さない。question の書き方は question の説明にある）。
                    "同じ会話の続きの質問では前のターンと同じプロファイルを使い、推薦し直すのは"
                    "話題が変わったときだけです。"
                ),
                input_model=RecommendSearchAnswerProfileInput,
                handler=scoped(recommend_search_answer_profile),
                output_model=RecommendSearchAnswerProfileOutput,
                permissions=(SEARCH_PERMISSIONS,),
            ),
            McpTool(
                name="rag_search",
                title="ナレッジベースの検索",
                description=(
                    "検索・回答プロファイルのナレッジベースを検索し、根拠（evidence。場所・版・"
                    "回答に使ったか付き）と回答を返します。"
                ),
                input_model=SearchInput,
                handler=scoped(_with_profile_selection(search), _check_search_scope),
                output_model=SearchOutput,
                permissions=(SEARCH_PERMISSIONS,),
            ),
            McpTool(
                name="rag_lookup_guides",
                title="業務ガイドの照会",
                description=(
                    "質問に当たる公開の業務ガイド（目的・確かめる条件と状態・手順の順と各手順の"
                    "検索の手がかり・参照の資料と頁・分岐・完了の条件・影響範囲・引き継ぎ先）を"
                    "返します。回答は作りません。"
                ),
                input_model=LookupGuidesInput,
                handler=scoped(lookup_guides, _check_guides_scope),
                output_model=LookupGuidesOutput,
                permissions=(SEARCH_PERMISSIONS,),
            ),
            McpTool(
                name="rag_retrieve_evidence",
                title="根拠の取得",
                description=(
                    "回答を作らずに、検索・回答プロファイルのナレッジベースから根拠（evidence。"
                    "場所・版付き）だけを返します。rag_search より速く、根拠を集める段で使います。"
                ),
                input_model=RetrieveEvidenceInput,
                handler=scoped(_with_profile_selection(retrieve_evidence), _check_search_scope),
                output_model=RetrieveEvidenceOutput,
                permissions=(SEARCH_PERMISSIONS,),
            ),
            McpTool(
                name="rag_validate_answer",
                title="資料との照合",
                description=(
                    "回答の段落ごとの主張を、渡した根拠（今の権限と版で読み直す）で監査します。"
                    "公開する前の最終の検証に使います。見出し・出典の行・"
                    "利用者への質問の段落は主張ではないので監査せず、claims に含めません。"
                    "requests（要求の充足）・guide（業務ガイド）を渡すと、要求の漏れ・手順ごとの"
                    "説明と順序・分岐・影響範囲も確かめます。"
                ),
                input_model=ValidateAnswerInput,
                handler=scoped(validate, _check_validate_scope),
                output_model=ValidateAnswerOutput,
                permissions=(SEARCH_PERMISSIONS,),
            ),
            McpTool(
                name="rag_read_source",
                title="根拠の本文の読み取り",
                description=(
                    "rag_search の根拠（document_id と chunk_id）の本文の続きと親の本文を読みます。"
                    "根拠の本文が省かれた（text_omitted）・切られた（truncated）とき・前後の文脈が"
                    "要るときに使います。"
                    "検索と同じ利用範囲の根拠だけを読めます。include_image=true で、図の根拠の"
                    "元の画像の領域を画像（content の image）で返します。"
                ),
                input_model=ReadSourceInput,
                handler=scoped(read),
                output_model=ReadSourceOutput,
                permissions=(SEARCH_PERMISSIONS,),
            ),
            McpTool(
                name="rag_outline",
                title="文書の章節の構成",
                description=(
                    "文書の章節の構成（文書の詳細の章節ナビゲーションの章節。section_id・階層・"
                    "親・頁の範囲・chunk の数・文字数）を返します。"
                    "長い文書のどこを読むかを決めるときに使います。format=compact は章節を 1 行"
                    "ずつの目録（見出しの語の無い条は本文の書き出しつき）で返し、section_id で"
                    "その章節の下だけに絞れます（条の多い文書から根拠の条を選ぶとき）。"
                ),
                input_model=OutlineInput,
                handler=scoped(outline_document),
                output_model=OutlineOutput,
                permissions=(SEARCH_PERMISSIONS,),
            ),
            McpTool(
                name="rag_read_document",
                title="文書の本文の読み取り",
                description=(
                    "文書の本文を、頁・章節・根拠の定位子・続きの cursor の位置から順に読みます"
                    "（頁の区切り付き。章節の終わりで止まらない。続きは next_cursor）。検索で"
                    "当たらなかった前後の章や、「第 3 章を参照」の先を確かめるときに使います。"
                ),
                input_model=ReadDocumentInput,
                handler=scoped(read_document),
                output_model=ReadDocumentOutput,
                permissions=(SEARCH_PERMISSIONS,),
            ),
            McpTool(
                name="rag_readable_documents",
                title="根拠の文書の閲覧の確かめ",
                # Agent の画面が保存した根拠を出し直すときだけ名前で呼ぶ内部のツール（#3252）。
                # モデル・外部の MCP クライアントには見せない。
                description="渡した文書のうち、今の権限で見られる文書の id を返します。",
                input_model=ReadableDocumentsInput,
                handler=scoped(readable_documents),
                output_model=ReadableDocumentsOutput,
                permissions=(SEARCH_PERMISSIONS,),
                listed=False,
            ),
            McpTool(
                name="rag_read_section",
                title="章節の本文の読み取り",
                description=(
                    "章節（section_id、または根拠の chunk_id が属する章節。up で親の章節）の"
                    "本文を、章節の始まりから終わりまで順に読みます（頁の区切り付き。続きは"
                    " next_cursor、終わりまで読んだら section_complete=true）。手順・一覧・条件の"
                    "まとまりが根拠の抜粋より長い（根拠の section.chars が大きい）とき、"
                    "読み切るのに使います。"
                ),
                input_model=ReadSectionInput,
                handler=scoped(read_section_tool),
                output_model=ReadSectionOutput,
                permissions=(SEARCH_PERMISSIONS,),
            ),
        ],
    )
