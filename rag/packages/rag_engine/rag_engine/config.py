"""環境変数からアプリ設定と LLM provider 設定を構築する。"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rag_engine.adapters.oracle.connection import AdbSettings

from rag_engine.retrieval.text_search_tokenizer import (
    DEFAULT_TEXT_SEARCH_TOKENIZER_LATIN_STEMMER,
    DEFAULT_TEXT_SEARCH_TOKENIZER_SUDACHI_DICT,
)

# LLM は本リポジトリのモデル設定(OCI Enterprise AI, OpenAI 互換 Responses API)に一本化する。
# 回答系(生成・監査・CRAG・質問拡張)と Vision(図説明)で model だけを分ける。
ENTERPRISE_AI_LLM_PROVIDER = "enterprise-ai"
ENTERPRISE_AI_VISION_LLM_PROVIDER = "enterprise-ai-vision"
DEFAULT_LLM_PROVIDER = ENTERPRISE_AI_LLM_PROVIDER
DEFAULT_CHICAGO_REGION = "us-chicago-1"
DEFAULT_OSAKA_REGION = "ap-osaka-1"
DEFAULT_OCI_CONFIG_FILE = "~/.oci/config"
DEFAULT_OCI_PROFILE = "DEFAULT"
# 「今日」(検索の基準日の既定)を決める時間帯(#3249)。backend の共通の設定 `PLATFORM_TIMEZONE` の既定と同じ。
DEFAULT_TIMEZONE = "UTC"
OCI_INFERENCE_ENDPOINT_TEMPLATE = "https://inference.generativeai.{region}.oci.oraclecloud.com"
DEFAULT_OCI_VISION_ENDPOINT = OCI_INFERENCE_ENDPOINT_TEMPLATE.format(region=DEFAULT_CHICAGO_REGION)
OPENAI_RESPONSES_LLM_API_MODE = "openai_responses"
DEFAULT_LLM_API_MODE = OPENAI_RESPONSES_LLM_API_MODE
DEFAULT_EMBEDDING_MODEL = "text-embedding-v4"
DEFAULT_EMBEDDING_OUTPUT_DIMENSIONS = 1536
# 百煉（DashScope）の OpenAI 互換 API は 1 要求 10 件まで。超えると 400 が返る（#8）。
DEFAULT_EMBEDDING_BATCH_SIZE = 10
DEFAULT_RERANK_MODEL = "qwen3-rerank"
DEFAULT_IMAGE_EMBEDDING_RRF_WEIGHT = 0.75
DEFAULT_MAX_ANCHORS_PER_PARENT = 2
# rerank の後、起点（上位 top_k）に採る同じ文書の child の上限（#2167）。0 は上限なし（SDK の既定）。
DEFAULT_MAX_ANCHORS_PER_DOCUMENT = 0
# 検索の起点 child 数（UI の既定値）、context に入れる parent 数、生成に渡す原文の総予算（文字）の既定値 (#1054)。
# chunking.constants.DEFAULT_RETRIEVAL_TOP_K / answer_records.MAX_CONTEXT_RECORDS / evidence_selection.EVIDENCE_BUDGET_CHARS
# と同じ値（循環 import を避けて写す。一致は tests/test_retrieval_budget_settings.py で確認する）。
DEFAULT_RETRIEVAL_TOP_K_SETTING = 20
DEFAULT_MAX_CONTEXT_RECORDS = 12
DEFAULT_EVIDENCE_BUDGET_CHARS = 80000
DEFAULT_TEXT_SEARCH_QUERY_VARIANT_LIMIT = 6
DEFAULT_RERANK_MIN_RELEVANCE_SCORE = 0.0
# Reciprocal Rank Fusion の平滑化定数。ADB の hybrid 検索と回答側の順位融合で同じ値を使う。
RRF_K = 60


def oci_inference_endpoint(region: str) -> str:
    """region の OCI Generative AI inference endpoint（root URL）を返します。"""
    return OCI_INFERENCE_ENDPOINT_TEMPLATE.format(region=region)


def llm_provider_label(location: str, model: str) -> str:
    """UI に表示する provider label を、実際に設定された model ID から組み立てます。

    model ID はコードに既定値を持たず環境変数だけが決めるため、装飾せずそのまま表示します。
    未設定のときは location だけを返し、label から model 未設定だと分かるようにします。
    """
    model_id = model.strip()
    return f"{location} / {model_id}" if model_id else location


# 値の前後の空白・引用符を strip しない項目。password は空白や引用符が値の一部になり得る (#837)。
_VERBATIM_ENV_KEYS = frozenset({"RAG_ENGINE_AUTH_PASSWORD"})


def _dotenv_items(text: str) -> list[tuple[str, str]]:
    """.env の本文を (key, value) に分解する。load_dotenv と get_settings で同じ規則を使う。

    通常の項目は前後の空白と引用符を外す。_VERBATIM_ENV_KEYS は外側の一対の引用符（"…" / '…'）だけを外し、
    中身はそのまま（`" pa'ss "` → ` pa'ss `、`pass'` → `pass'`）。
    """
    items: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in _VERBATIM_ENV_KEYS:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
        else:
            value = value.strip().strip('"').strip("'")
        items.append((key, value))
    return items


def load_dotenv(path: str | Path = ".env") -> None:
    """外部依存なしで .env を読み込む。既存の環境変数は上書きしない。"""
    env_path = Path(path)
    if not env_path.exists():
        return
    for key, value in _dotenv_items(env_path.read_text(encoding="utf-8")):
        os.environ.setdefault(key, value)


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, default))
    except ValueError:
        return default


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    """1 以上の整数(値なし・整数でない・1 未満は既定)。"""
    value = _env_int(env, name, default)
    return value if value >= 1 else default


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, default))
    except ValueError:
        return default


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_llm_provider_id(value: str | None, default: str = DEFAULT_LLM_PROVIDER) -> str:
    """LLM provider の別名を内部 provider ID へ正規化します。"""
    normalized = str(value or "").strip().lower().replace("_", "-")
    if normalized in {"", "default"}:
        return default
    aliases = {
        "enterprise-ai": ENTERPRISE_AI_LLM_PROVIDER,
        "vision": ENTERPRISE_AI_VISION_LLM_PROVIDER,
        "enterprise-ai-vision": ENTERPRISE_AI_VISION_LLM_PROVIDER,
    }
    return aliases.get(normalized, normalized)


def normalize_llm_api_mode(value: str | None, default: str = DEFAULT_LLM_API_MODE) -> str:
    """LLM は openai SDK(Responses API)のみを使う。OCI SDK の LLM 経路は持たない。"""
    return OPENAI_RESPONSES_LLM_API_MODE


# 1 回の呼び出しの出力の上限の既定(tokens。#1719。platform の DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS と同じ)。
# 回答の上限(`answer_max_tokens`)の既定と、モデルの出力の上限が渡されないときの値。
DEFAULT_MODEL_OUTPUT_TOKENS = 32768


@dataclass(frozen=True)
class LlmProviderSettings:
    """1 つの LLM provider の接続先、認証、model 能力を保持します。"""

    provider_id: str
    label: str
    # 記録用(base URL の組み立てには使わない。#1440)。
    region: str
    # OCI の OpenAI 互換 API が要求する project OCID。他の OpenAI 互換 API では空のままにする。
    project_id: str
    api_key: str
    model: str
    supports_vision: bool = False
    endpoint: str = ""
    # 推論(思考)の量の送る値(#1425。Responses API の `reasoning.effort`)。空は送らない。
    # backend が platform の `reasoning_effort_to_send` で決めて渡す(none は空)。
    reasoning_effort: str = ""
    # モデルの 1 回の呼び出しの出力の上限(tokens。#1719。推論のあるモデルは推論の token も含む)。backend が
    # platform の `enterprise_ai_output_token_budget`(モデルの出力の上限と入力の上限の小さい方)で決めて渡す。
    max_output_tokens: int = DEFAULT_MODEL_OUTPUT_TOKENS

    @property
    def base_url(self) -> str:
        """OpenAI 互換 API の base URL(endpoint を入力どおり使う。#1440)。

        path は補わない(OCI Enterprise AI も利用者が `/openai/v1` まで書く)。backend は
        platform の `enterprise_ai_base_url` で決めた値を `OCI_ENTERPRISE_AI_ENDPOINT` で渡すので、
        ここでは前後の空白と末尾の `/` を除くだけにして、3 製品と同じ URL を呼ぶ。
        """
        return self.endpoint.strip().rstrip("/")


@dataclass(frozen=True)
class Settings:
    """アプリ全体の環境変数由来設定を保持します。"""

    host: str
    port: int
    output_dir: Path
    runtime_knowledge_path: Path | None
    render_dpi: int
    max_default_pages: int
    enabled_engines: list[str]
    docling_device: str
    docling_num_threads: int
    docling_do_ocr: bool
    docling_do_table_structure: bool
    docling_keep_picture_child_text: bool
    oci_config_file: str
    oci_profile: str
    oci_compartment_id: str
    oci_vision_endpoint: str
    llm_request_timeout_seconds: int
    llm_retries: int
    llm_retry_initial_wait_seconds: float
    llm_retry_max_wait_seconds: float
    # 構造化出力の呼び出し 1 回の出力の上限(tokens。既定 32768。#1719)。送る値はこれと呼ぶモデルの出力の上限
    # (`LlmProviderSettings.max_output_tokens`)の小さい方(`llm_output_token_limit`)。
    answer_max_tokens: int
    default_rerank_enabled: bool
    rerank_model: str
    # 再ランクの OpenAI 互換 Base URL。空なら埋め込みの Base URL から読み替える（#8。百煉は
    # 再ランクだけ `/compatible-api/v1` 配下で、`/compatible-mode/v1` に投げると 404）。
    rerank_base_url: str
    rerank_request_timeout_seconds: int
    rerank_retries: int
    rerank_retry_initial_wait_seconds: float
    rerank_retry_max_wait_seconds: float
    rerank_min_relevance_score: float
    # 粗→細の検索: rerank 後の候補を文書ごとに集約し、rerank の点数が明らかに低い文書を後回しにする (#1028)。既定 on (#1040)。
    # 命中の件数は判定に使わない（#3504）。
    document_selection_enabled: bool
    document_selection_score_ratio: float
    # 質問から操作すべき画面を画面目録で選び、その画面の根拠を検索候補に加える (#1108)。1 質問につき LLM 呼出が
    # 1 回増える。効果を 83 問で確かめるまで既定 off。
    screen_linking_enabled: bool
    # 1 以上なら、選んだ画面ごとの最上位の child を rerank 後の順位 N 位（2 つ目は N+1 位…）より下に置かない。
    # 0（既定）は候補に加えるだけで順位は rerank に任せる。比較実験用 (#1108)。
    screen_linking_reserve_rank: int
    # 埋め込みの OpenAI 互換 Base URL。空なら LLM のプライマリ接続の endpoint を使う（#8）。
    embedding_base_url: str
    embedding_model: str
    embedding_output_dimensions: int
    embedding_batch_size: int
    image_embedding_enabled: bool
    image_embedding_rrf_weight: float
    text_search_query_variant_limit: int
    # 文脈構築で同じ親から起点（retrieved_anchor）に採る child の上限（#669、#889）。1 以上。
    max_anchors_per_parent: int
    # 起点に採る同じ文書の child の上限（#2167）。超えた child は落とさず後ろへ回す。0 は上限なし。
    max_anchors_per_document: int
    text_search_tokenizer_sudachi_dict: str
    text_search_tokenizer_sudachi_config: str
    text_search_tokenizer_latin_stemmer: str
    llm_api_mode: str
    llm_providers: dict[str, LlmProviderSettings]
    # 画像を読む取り込みの処理（図の説明・解析の VLM）の既定のモデル。回答には使わない（#3444）。
    default_vision_llm: str
    # 回答のモデル。回答の実行の LLM の呼び出しはすべてこのモデルで行い、根拠の画像を添付するかは
    # このモデルが画像を読めるか（`supports_vision`）で決める（#3444）。
    default_answer_llm: str

    # None は保存済み辞書、空 tuple は評価用の辞書無効化を表す。共有ファイルは変更しない。
    domain_keywords_override: tuple[str, ...] | None = None
    # 検索の比較実験用。重み付けを切ると全検索文が同じ票、original_query_only は原質問だけで検索する。
    original_query_weighting_enabled: bool = True
    original_query_only: bool = False
    profile_channel_enabled: bool = True
    workspace_dir: Path | None = None
    prompt_dir: Path | None = None
    faq_path: Path | None = None
    adb_settings: AdbSettings | None = None
    approved_faq_semantic_matching_enabled: bool = True
    approved_faq_semantic_suggestions_enabled: bool = False
    # Gradio UI のログイン。password が空のままでは create_app() が起動を拒否する。
    auth_user: str = "admin"
    auth_password: str = ""
    # 真のときだけ UI を認証なしで公開する。ローカル検証専用の逸脱で、既定は必ず False。
    auth_disabled: bool = False
    # 検索の起点 child 数（UI の既定値。API・SDK は引数で指定）、context に入れる parent 数、生成の原文予算（文字）(#1054)。
    retrieval_top_k: int = DEFAULT_RETRIEVAL_TOP_K_SETTING
    # 基本候補と、拡張を含む精排入力の合計は回答起点数から独立する。
    retrieval_candidate_limit: int = 150
    rerank_candidate_limit: int = 0
    # 呼出元が解決した、各候補経路の既存の上限。KB/レシピには設定しない。
    retrieval_stage_limits: dict[str, int] = field(default_factory=dict)
    max_context_records: int = DEFAULT_MAX_CONTEXT_RECORDS
    evidence_budget_chars: int = DEFAULT_EVIDENCE_BUDGET_CHARS
    # 要求ごとの根拠の覆域を判定し、根拠の無い要求だけを 1 回ずつ再検索する (#1279)。LLM は呼ばない
    # （embedding・hybrid 検索・rerank だけ）。回数・足す根拠の数・期限で抑える。
    request_coverage_retrieval_enabled: bool = False
    request_coverage_max_queries: int = 2
    request_coverage_max_chunks: int = 4
    request_coverage_deadline_seconds: float = 10.0
    # 丸数字・(1) などで並べた小問ごとの検索をする小問の数と、小問ごとに確保する根拠の数 (#2399)。
    request_coverage_max_subquestions: int = 5
    request_coverage_chunks_per_subquestion: int = 2
    # 主の検索の後に足す検索（業務ガイドの手順ごとの検索・小問ごとの検索・根拠の無い要求の再検索）の回数の
    # 合計の枠 (#2395)。種類ごとの上限はこの枠の中で数える（`retrieval.extra_retrieval`）。
    extra_retrieval_max_queries: int = 8
    # 章節の拡張で入力の上限に入らない章節を LLM で抜き出す(#1437)。False なら rerank の順で上限まで入れ、残りは
    # 「回答に未反映の関連章節」に示す(#2800。backend の既定は False)。
    section_extraction_enabled: bool = True
    # 業務ガイドの手順ごとに検索し、手順が参照する資料の頁の根拠を回答の材料に入れる (#2395)。LLM は呼ばない。
    # 必須対象は追加検索の合計枠を先に確保する。回数はその後の補助検索、参照数は最低1件以外の追加候補を制限する。
    guide_step_retrieval_enabled: bool = True
    guide_step_max_queries: int = 4
    guide_step_chunks_per_step: int = 2
    guide_reference_max_chunks: int = 4
    guide_step_deadline_seconds: float = 60.0
    # 当たった chunk の章節から親の章節へ段階的に広げて文脈に入れる (#1436)。章節と chunk の対応を注入した
    # 呼び出し元（`AnswerDependencies.section_outline` / `section_chunks`）でだけ動く。上限は、根拠の入力 token
    # （モデルの入力の上限に対する割合）・上がる段の数。期限は回答全体のものを使う。
    section_expansion_enabled: bool = True
    section_expansion_max_levels: int = 4
    section_expansion_input_token_percent: int = 50
    # 業務ガイドの手順ごとの検索の後に広げる段のために、最初の段で使わずに残す入力の割合（上の入力の上限に対する %。#2816）。
    section_expansion_followup_reserve_percent: int = 20
    # 回答のモデルの入力の上限（tokens。#1437）。backend が共通のモデル設定の登録モデルの値を
    # `OCI_ENTERPRISE_AI_MAX_INPUT_TOKENS` で渡す（platform の enterprise_ai_max_input_tokens。既定 128000）。
    # 章節の拡張の根拠の入力の上限と、抽出してから統合の 1 回分の大きさをこの割合で決める。
    answer_max_input_tokens: int = 128_000
    # 同時に呼ぶ数（既定 4）。
    section_extraction_concurrency: int = 4
    # 条の目録から条を選ぶ段（#2254）。融合の候補の上位の条で構成された文書（article_catalog_max_documents 件まで）の
    # 章節の目録を LLM に文書ごとに 1 回渡して条を選ばせ（文書ごとに article_catalog_max_articles 件まで）、その条の
    # 根拠を候補に加える。選んだ条は rerank の順に article_catalog_context_records 件まで、context の起点・親の枠の外に
    # 足す（検索の根拠を減らさない）。目録と chunk を注入した呼び出し元（`AnswerDependencies.article_catalogs` /
    # `article_chunks`）でだけ動き、条で構成された文書が候補に無ければ LLM を呼ばない。
    article_catalog_enabled: bool = True
    # 既定は評価の実測（必要な文書の順位は最大 8・条の 1 段目の位置は最大 6）に余裕を持たせた 2 のべき（#3518）。
    article_catalog_max_documents: int = 16
    article_catalog_max_articles: int = 8
    article_catalog_context_records: int = 6
    # 検索の範囲の条で構成された文書の一覧から LLM が選ぶ文書の数（候補の文書の外。手続の一般的な規則の文書など）。
    # 0 は候補の文書だけを読む。一覧を注入した呼び出し元（`AnswerDependencies.article_documents`）でだけ動く。
    article_catalog_scope_documents: int = 2
    # 元の質問（注入されたとき）でも 1 回ベクトル検索し、その候補を目録を読む文書の順位に足す（候補・context には足さない）。
    # 業務 Agent の短い検索文では、事例の論点の文書（政令・手続の規則など）が候補の上位に出ないため。
    article_catalog_original_question_search: bool = True
    # 条の選択の LLM の呼び出し 1 回の出力の上限（token）と時間の上限（秒）。選ぶ出力は目録の番号の列で、通常は数百
    # token・数秒で終わる。まれにモデルが同じ出力を繰り返して回答の上限まで書き続け、1 回で数分かかって段全体を
    # 待たせるため、役目に合わせて抑え、超えた文書は選ばずに続ける（工程に残す）。0 は抑えない。
    # 条の選択の 2 段目: 1 段目で選んだ条と同じ章の条と、選んだ条の番号を書き出しで名指しする別の章の条（附則の特例・
    # 読替えなど。#3517）を合わせて article_catalog_refine_candidates 件まで（名指しの条はその 3 分の 1 まで。#3531）
    # 本文の書き出しつきで見せ、足りない論点の条を文書ごとに article_catalog_refine_articles 件まで足させる。条の名前が
    # 質問の語から遠い条（重なったときの扱い・特例など）を拾う。書き出しを注入した呼び出し元
    # （`AnswerDependencies.article_leads`）でだけ動く。0 は 2 段目を行わない。
    article_catalog_refine_articles: int = 6
    article_catalog_refine_candidates: int = 60
    article_catalog_output_token_cap: int = 8192
    article_catalog_call_timeout_seconds: float = 180.0
    # 目録で選んだ条を生成が引かなかったとき、条ごとに原文が支える説明を別の呼び出しで検討させ、その説明の出典に足す
    # （#3526。`grounded.CATALOG_REVIEW_SYSTEM_PROMPT`）。評価で確かめた説明に根拠の無い条の出典が付いたので既定は無効。
    # 無効なら検討の呼び出しをしない。
    article_catalog_review: bool = False
    # 条の選択と範囲の文書の選択の結果を、process をまたいで時間を区切って使う記録の置き場所・有効期間（秒）・件数の
    # 上限（#3507）。key は索引の版（chunk_set）・目録の内容・質問・モデル・選択の設定。ディレクトリが無い・有効期間か
    # 件数が 0 なら process の中の記録だけを使う。選び方は変えない（同じ入力の LLM の結果を使い回すだけ）。
    article_catalog_cache_dir: Path | None = None
    article_catalog_cache_ttl_seconds: float = 86400.0
    article_catalog_cache_max_entries: int = 4096
    # 回答の最終の検証（rag_validate_answer）でモデルに渡す入力の上限（UTF-8 の bytes。指示・質問・回答・根拠を含む）。
    # 根拠は渡された順に収まるだけ渡し、収まらない根拠は検証に使わない（結果の evidence_truncated）。品質評価の
    # 主張の監査の予算（`MAX_EVALUATION_INPUT_BYTES`）とは別。
    answer_validation_max_input_bytes: int = 96_000
    # 用途ごとの推論(思考)の量の上書き(#1499)。空はモデルの登録値(`LlmProviderSettings.reasoning_effort`)の
    # まま、none は送らない、low / medium / high はその値を送る。質問拡張戦略の判定と検索文の生成、根拠の評価
    # (CRAG の評価と章節の拡張の十分性の判定)、回答文の生成、生成後の監査(#1528)で分ける。モデルの登録値が
    # none(送らない)のモデルには、上書きしても送らない(推論を受け付けないモデルがある。platform の
    # `pr_system_settings.model` の実測)。
    strategy_reasoning_effort: str = ""
    judge_reasoning_effort: str = ""
    generation_reasoning_effort: str = ""
    audit_reasoning_effort: str = ""
    # 規則の信号が強いとき CRAG の評価(1 回目)を飛ばす(#1499。既定 off)。rerank の最上位の score が閾値以上で、
    # 質問の要求に語の一致で根拠の無いもの(missing)が無いときだけ(#1528 で曖昧な帯だけ呼ぶ形に見直した)。
    # 列挙・エラー文の問、定義本文の無い問、候補の除外が要る(旧版・重複の)根拠があるときは必ず呼ぶ。
    # 生成後の監査は飛ばさない。
    crag_skip_on_strong_signals: bool = False
    crag_skip_min_rerank_score: float = 0.8
    # CRAG の評価の入力を詰める(#1528): 候補を生成と同じ親の数(max_context_records)にし、JSON を詰めて書き、
    # 候補の ID は chunk_uid だけにする。候補の除外・missing_aspects・列挙の項目の働きは変えない。
    crag_compact_input: bool = False
    # 詰めた入力の候補の chunk_uid を短い ID（C1・C1-1）にする(#1864)。評価器が判定ごとに書き写す ID が短くなり、
    # 出力の token と時間が減る。返った ID は chunk_uid に戻すので、判定の働きは変わらない。
    crag_short_candidate_ids: bool = False
    # 生成後の監査の reason を、判定が通った item（supported かつ matched）と答えている要求（addressed）では空文字に
    # させる(#1912)。見える出力は減るが、同じ入力の対の比較で監査が甘くなったため既定 OFF（品質評価の比較用）。
    answer_audit_compact_reasons: bool = False
    # 回答文の生成と根拠確認の是正の組（作り直し）の回数の上限(#1912。0〜2)。既定 2 は従来の MAX_ROUNDS と同じ。
    answer_max_correction_rounds: int = 2
    # 回答の監査の型(#2795・#2800)。none は生成の後の LLM の監査と作り直しをしない(決定的な検査だけ)。
    # standard は未使用の根拠を列挙させず、作り直しは事実の問題の 1 組まで(完全性の不足は本文に
    # 示す)。detailed は未使用の根拠も列挙させ、完全性の不足でも作り直す(従来)。engine の既定は従来どおり detailed、
    # backend の既定は none。
    answer_evidence_refinement_mode: str = "legacy"
    answer_audit_mode: str = "detailed"
    # 回答の期限のある LLM の呼び出しを streaming で受け、停滞（event が来ない・出力が直近の速さより遅い）を
    # 検出したら打ち切って送り直す(#1926。既定 off。backend が RAG_LLM_STALL_DETECTION_ENABLED で ON にする)。
    # 判定の速さと間隔は同じ接続・モデルの直近の完了した呼び出しから学ぶ（実測が 3 回たまるまでは検出しない）。
    llm_stall_detection: bool = False
    # event が来ない秒の下限（直近の呼び出しの最大の間隔の 3 倍がこれより長ければそちら）。
    llm_stall_idle_seconds: float = 20.0
    # 直近 10 秒の出力の速さ（delta の event/秒）が、直近の完了した呼び出しの速さの中央値のこの割合を下回ったら停滞。
    llm_stall_min_rate_ratio: float = 0.25
    # 停滞で打ち切って送り直す回数の上限（1 回の呼び出しあたり。最後の送信は検出せずに待つ）。
    llm_stall_resends: int = 1
    # streaming で受ける呼び出しで、出力が同じ並びの繰り返し・空白だけで続いたら打ち切って送り直す(#2966。既定 off。
    # backend が RAG_LLM_RUNAWAY_DETECTION_ENABLED で ON にする)。streaming しない呼び出しは、出力の上限で未完了の
    # 応答に同じ暴走があれば、上限を広げずに同じ送り直しにする(#3835)。
    llm_runaway_detection: bool = False
    # 章節の拡張で、列挙の問の読む範囲(minimum_scope)まで読んだら CRAG の評価し直しを省いて生成へ進む(#1528)。
    section_skip_regrade_at_scope: bool = False
    # 質問拡張で LLM が作る検索文の本数の上限(#1499。既定は従来の値 MAX_GENERATED_QUERY_COUNT と同じ 5)。
    query_expansion_max_queries: int = 5
    # 分かっている条件（業務ガイドの条件の名前と値。MCP の conditions・質問で決まった条件。#1492）。backend が
    # 業務ガイドの照合の結果から渡す。回答の確認の項目のうち、これを確かめる項目を決定的に除く。
    known_conditions: tuple[tuple[str, str], ...] = ()
    # backendが利用者の確定した回答・質問から選別した前提。設定保存や環境変数からは読み込まない。
    confirmed_premises: tuple[tuple[str, str], ...] = ()
    confirmed_premise_sources: tuple[tuple[str, str, str], ...] = ()
    # 業務ガイドの照合で分からなかった選択の条件（#2404）。(条件の id, 名前, ((選択肢, (言い換え, ...)), ...))。
    # 選択肢が項目の種類に対応する条件だけを、根拠の画面の図の項目の種類で決める（generation.form_field_kinds）。
    evidence_conditions: tuple[tuple[str, str, tuple[tuple[str, tuple[str, ...]], ...]], ...] = ()
    # 根拠で条件が決まったときの業務ガイドの手順の計画（#2689）。backend が決まり方ごとに前もって作る。
    # (決まった条件の (id, 値) の列, 残る手順の (id, 必須か) の列, 業務ガイドのルールの id, そのルールの本文)。
    evidence_plans: tuple[tuple[tuple[tuple[str, str], ...], tuple[tuple[str, bool], ...], str, str], ...] = ()
    # 「今日」(検索の基準日の既定)を決める IANA の時間帯(#3249。`PLATFORM_TIMEZONE`)。OS の時間帯は使わない。
    timezone: str = DEFAULT_TIMEZONE

    @property
    def vision_model(self) -> str:
        """既定 Vision provider に設定された model 名を返します。"""
        return get_llm_provider(self, self.default_vision_llm).model


def get_settings(*, environ: Mapping[str, str] | None = None, dotenv_path: str | Path | None = ".env", **overrides) -> Settings:
    """設定 snapshot を構築する。明示値 > 環境変数 > .env の順。

    environ と dotenv_path=None で外部環境を読み込まずに利用できる。
    os.environ は変更せず、ネットワーク接続やディレクトリ作成も行わない。
    """
    env = dict(os.environ if environ is None else environ)
    if dotenv_path is not None and Path(dotenv_path).is_file():
        for key, value in _dotenv_items(Path(dotenv_path).read_text(encoding="utf-8")):
            env.setdefault(key, value)
    enabled = env.get(
        "RAG_ENGINE_ENABLED_ENGINES",
        "docling",
    )
    enabled_engines = [part.strip() for part in enabled.split(",") if part.strip()]
    llm_api_mode = normalize_llm_api_mode(env.get("LLM_API_MODE"), DEFAULT_LLM_API_MODE)
    # 接続先・認証は backend のモデル設定(OCI_ENTERPRISE_AI_*)を共有する。model ID は既定値を持たず、
    # 未設定は呼び出し時に _validated_llm_provider() が不足として報告する。
    endpoint = env.get("OCI_ENTERPRISE_AI_ENDPOINT", "").strip()
    project = env.get("OCI_ENTERPRISE_AI_PROJECT_OCID", "").strip()
    api_key = env.get("OCI_ENTERPRISE_AI_API_KEY", "").strip()
    answer_model = env.get("OCI_ENTERPRISE_AI_DEFAULT_MODEL", "").strip()
    vision_model = env.get("OCI_ENTERPRISE_AI_VLM_MODEL", "").strip() or answer_model
    # Vision のモデルが別の接続のとき(#533)は OCI_ENTERPRISE_AI_VLM_* を使う。無ければ回答と同じ。
    vision_endpoint = env.get("OCI_ENTERPRISE_AI_VLM_ENDPOINT", "").strip() or endpoint
    vision_project = env.get("OCI_ENTERPRISE_AI_VLM_PROJECT_OCID", "").strip() or project
    vision_api_key = env.get("OCI_ENTERPRISE_AI_VLM_API_KEY", "").strip() or api_key
    # 推論(思考)の量はモデルごと(#1425)。Vision のモデルを指定しないときは回答のモデルの値。
    answer_reasoning = env.get("OCI_ENTERPRISE_AI_REASONING_EFFORT", "").strip()
    vision_reasoning = (
        env.get("OCI_ENTERPRISE_AI_VLM_REASONING_EFFORT", "").strip()
        if env.get("OCI_ENTERPRISE_AI_VLM_MODEL", "").strip()
        else answer_reasoning
    )
    # 出力の上限もモデルごと(#1719)。backend が platform の `enterprise_ai_output_token_budget` で決めて渡す。
    # Vision のモデルを指定しないときは回答のモデルの値。
    answer_output_tokens = _positive_int(env, "OCI_ENTERPRISE_AI_MAX_OUTPUT_TOKENS", DEFAULT_MODEL_OUTPUT_TOKENS)
    vision_output_tokens = (
        _positive_int(env, "OCI_ENTERPRISE_AI_VISION_MAX_OUTPUT_TOKENS", DEFAULT_MODEL_OUTPUT_TOKENS)
        if env.get("OCI_ENTERPRISE_AI_VLM_MODEL", "").strip()
        else answer_output_tokens
    )
    llm_providers = {
        ENTERPRISE_AI_LLM_PROVIDER: LlmProviderSettings(
            provider_id=ENTERPRISE_AI_LLM_PROVIDER,
            label=llm_provider_label("Enterprise AI", answer_model),
            region="",
            project_id=project,
            api_key=api_key,
            model=answer_model,
            # 回答のモデルが画像を読めるか(#3444。backend がモデル設定の登録モデルの vision_enabled で決めて渡す)。
            supports_vision=_env_bool(env, "OCI_ENTERPRISE_AI_DEFAULT_MODEL_VISION", False),
            endpoint=endpoint,
            reasoning_effort=answer_reasoning,
            max_output_tokens=answer_output_tokens,
        ),
        ENTERPRISE_AI_VISION_LLM_PROVIDER: LlmProviderSettings(
            provider_id=ENTERPRISE_AI_VISION_LLM_PROVIDER,
            label=llm_provider_label("Enterprise AI Vision", vision_model),
            region="",
            project_id=vision_project,
            api_key=vision_api_key,
            model=vision_model,
            supports_vision=True,
            endpoint=vision_endpoint,
            reasoning_effort=vision_reasoning,
            max_output_tokens=vision_output_tokens,
        ),
    }
    default_vision_llm = normalize_llm_provider_id(
        env.get("DEFAULT_VISION_LLM"),
        ENTERPRISE_AI_VISION_LLM_PROVIDER,
    )
    default_answer_llm = normalize_llm_provider_id(
        env.get("DEFAULT_ANSWER_LLM"),
        ENTERPRISE_AI_LLM_PROVIDER,
    )
    runtime_knowledge_raw = env.get("RUNTIME_KNOWLEDGE_PATH", "").strip()
    cache_dir_raw = env.get("RAG_ENGINE_ARTICLE_CATALOG_CACHE_DIR", "").strip()
    settings = Settings(
        original_query_weighting_enabled=_env_bool(env, "RETRIEVAL_ORIGINAL_QUERY_WEIGHTING", True),
        original_query_only=_env_bool(env, "RETRIEVAL_ORIGINAL_QUERY_ONLY", False),
        profile_channel_enabled=_env_bool(env, "RETRIEVAL_PROFILE_CHANNEL", True),
        auth_user=env.get("RAG_ENGINE_AUTH_USER", "").strip() or "admin",
        # password は前後の空白も値の一部になり得るため strip しない。
        auth_password=env.get("RAG_ENGINE_AUTH_PASSWORD", ""),
        auth_disabled=_env_bool(env, "RAG_ENGINE_DISABLE_AUTH", False),
        host=env.get("RAG_ENGINE_HOST", "0.0.0.0"),
        port=_env_int(env, "RAG_ENGINE_PORT", 8080),
        output_dir=Path(env.get("RAG_ENGINE_OUTPUT_DIR", ".runs")),
        runtime_knowledge_path=Path(runtime_knowledge_raw).expanduser() if runtime_knowledge_raw else None,
        render_dpi=_env_int(env, "RAG_ENGINE_RENDER_DPI", 300),
        max_default_pages=_env_int(env, "RAG_ENGINE_MAX_DEFAULT_PAGES", 1),
        enabled_engines=enabled_engines,
        docling_device=env.get("DOCLING_DEVICE", "cpu"),
        docling_num_threads=_env_int(env, "DOCLING_NUM_THREADS", _env_int(env, "OMP_NUM_THREADS", 4)),
        docling_do_ocr=_env_bool(env, "DOCLING_DO_OCR", True),
        docling_do_table_structure=_env_bool(env, "DOCLING_DO_TABLE_STRUCTURE", True),
        docling_keep_picture_child_text=_env_bool(env, "DOCLING_KEEP_PICTURE_CHILD_TEXT", False),
        oci_config_file=env.get("OCI_CONFIG_FILE", DEFAULT_OCI_CONFIG_FILE),
        oci_profile=env.get("OCI_PROFILE", DEFAULT_OCI_PROFILE),
        oci_compartment_id=env.get("OCI_COMPARTMENT_ID", "").strip(),
        oci_vision_endpoint=env.get("OCI_VISION_ENDPOINT", DEFAULT_OCI_VISION_ENDPOINT).strip(),
        # 旧名 VISION_* は Vision 限定に見えるため改名した。リポジトリ外の既存 .env が黙って既定値へ
        # 戻らないよう、新しい名前が未設定のときだけ旧名を読む (#912)。
        llm_request_timeout_seconds=_env_int(env, "LLM_REQUEST_TIMEOUT_SECONDS", _env_int(env, "VISION_REQUEST_TIMEOUT_SECONDS", 90)),
        llm_retries=_env_int(env, "LLM_RETRIES", _env_int(env, "VISION_RETRIES", 3)),
        llm_retry_initial_wait_seconds=_env_float(
            env, "LLM_RETRY_INITIAL_WAIT_SECONDS", _env_float(env, "VISION_RETRY_INITIAL_WAIT_SECONDS", 2.0)
        ),
        llm_retry_max_wait_seconds=_env_float(env, "LLM_RETRY_MAX_WAIT_SECONDS", _env_float(env, "VISION_RETRY_MAX_WAIT_SECONDS", 30.0)),
        answer_max_tokens=_positive_int(env, "ANSWER_MAX_TOKENS", DEFAULT_MODEL_OUTPUT_TOKENS),
        default_rerank_enabled=_env_bool(env, "RAG_ENGINE_DEFAULT_RERANK_ENABLED", True),
        approved_faq_semantic_matching_enabled=_env_bool(env, "APPROVED_FAQ_SEMANTIC_MATCHING_ENABLED", True),
        approved_faq_semantic_suggestions_enabled=_env_bool(env, "APPROVED_FAQ_SEMANTIC_SUGGESTIONS_ENABLED", False),
        rerank_model=env.get("RERANK_MODEL", DEFAULT_RERANK_MODEL).strip() or DEFAULT_RERANK_MODEL,
        rerank_base_url=env.get("RERANK_BASE_URL", "").strip(),
        rerank_request_timeout_seconds=_env_int(env, "RERANK_REQUEST_TIMEOUT_SECONDS", 90),
        rerank_retries=_env_int(env, "RERANK_RETRIES", 3),
        rerank_retry_initial_wait_seconds=_env_float(env, "RERANK_RETRY_INITIAL_WAIT_SECONDS", 2.0),
        rerank_retry_max_wait_seconds=_env_float(env, "RERANK_RETRY_MAX_WAIT_SECONDS", 30.0),
        rerank_min_relevance_score=max(
            0.0,
            _env_float(env, "RERANK_MIN_RELEVANCE_SCORE", DEFAULT_RERANK_MIN_RELEVANCE_SCORE),
        ),
        document_selection_enabled=_env_bool(env, "RAG_ENGINE_DOCUMENT_SELECTION", True),
        document_selection_score_ratio=max(0.0, min(1.0, _env_float(env, "RAG_ENGINE_DOCUMENT_SELECTION_SCORE_RATIO", 0.5))),
        screen_linking_enabled=_env_bool(env, "RAG_ENGINE_SCREEN_LINKING", False),
        screen_linking_reserve_rank=max(0, _env_int(env, "RAG_ENGINE_SCREEN_LINKING_RESERVE_RANK", 0)),
        embedding_base_url=env.get("EMBEDDING_BASE_URL", "").strip(),
        embedding_model=env.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip() or DEFAULT_EMBEDDING_MODEL,
        embedding_output_dimensions=_env_int(
            env,
            "EMBEDDING_OUTPUT_DIMENSIONS",
            DEFAULT_EMBEDDING_OUTPUT_DIMENSIONS,
        ),
        embedding_batch_size=_env_int(env, "EMBEDDING_BATCH_SIZE", DEFAULT_EMBEDDING_BATCH_SIZE),
        image_embedding_enabled=_env_bool(env, "IMAGE_EMBEDDING_ENABLED", False),
        image_embedding_rrf_weight=max(
            0.0,
            _env_float(env, "IMAGE_EMBEDDING_RRF_WEIGHT", DEFAULT_IMAGE_EMBEDDING_RRF_WEIGHT),
        ),
        text_search_query_variant_limit=max(
            1,
            _env_int(env, "TEXT_SEARCH_QUERY_VARIANT_LIMIT", DEFAULT_TEXT_SEARCH_QUERY_VARIANT_LIMIT),
        ),
        max_anchors_per_parent=max(1, _env_int(env, "RAG_ENGINE_MAX_ANCHORS_PER_PARENT", DEFAULT_MAX_ANCHORS_PER_PARENT)),
        max_anchors_per_document=max(0, _env_int(env, "RAG_ENGINE_MAX_ANCHORS_PER_DOCUMENT", DEFAULT_MAX_ANCHORS_PER_DOCUMENT)),
        retrieval_top_k=max(1, _env_int(env, "RAG_ENGINE_RETRIEVAL_TOP_K", DEFAULT_RETRIEVAL_TOP_K_SETTING)),
        retrieval_candidate_limit=max(1, _env_int(env, "RAG_ENGINE_RETRIEVAL_CANDIDATE_LIMIT", 150)),
        rerank_candidate_limit=max(0, _env_int(env, "RAG_ENGINE_RERANK_CANDIDATE_LIMIT", 0)),
        retrieval_stage_limits={
            str(key): max(0, int(value)) for key, value in json.loads(env.get("RAG_ENGINE_RETRIEVAL_STAGE_LIMITS", "{}")).items()
        },
        max_context_records=max(1, _env_int(env, "RAG_ENGINE_MAX_CONTEXT_RECORDS", DEFAULT_MAX_CONTEXT_RECORDS)),
        evidence_budget_chars=max(1000, _env_int(env, "RAG_ENGINE_EVIDENCE_BUDGET_CHARS", DEFAULT_EVIDENCE_BUDGET_CHARS)),
        request_coverage_retrieval_enabled=_env_bool(env, "RAG_ENGINE_REQUEST_COVERAGE_RETRIEVAL", False),
        request_coverage_max_queries=max(0, _env_int(env, "RAG_ENGINE_REQUEST_COVERAGE_MAX_QUERIES", 2)),
        request_coverage_max_chunks=max(0, _env_int(env, "RAG_ENGINE_REQUEST_COVERAGE_MAX_CHUNKS", 4)),
        request_coverage_deadline_seconds=max(0.0, _env_float(env, "RAG_ENGINE_REQUEST_COVERAGE_DEADLINE_SECONDS", 10.0)),
        request_coverage_max_subquestions=max(0, _env_int(env, "RAG_ENGINE_REQUEST_COVERAGE_MAX_SUBQUESTIONS", 5)),
        request_coverage_chunks_per_subquestion=max(0, _env_int(env, "RAG_ENGINE_REQUEST_COVERAGE_CHUNKS_PER_SUBQUESTION", 2)),
        extra_retrieval_max_queries=max(0, _env_int(env, "RAG_ENGINE_EXTRA_RETRIEVAL_MAX_QUERIES", 8)),
        section_extraction_enabled=_env_bool(env, "RAG_ENGINE_SECTION_EXTRACTION_ENABLED", True),
        guide_step_retrieval_enabled=_env_bool(env, "RAG_ENGINE_GUIDE_STEP_RETRIEVAL", True),
        guide_step_max_queries=max(0, _env_int(env, "RAG_ENGINE_GUIDE_STEP_MAX_QUERIES", 4)),
        guide_step_chunks_per_step=max(1, _env_int(env, "RAG_ENGINE_GUIDE_STEP_CHUNKS_PER_STEP", 2)),
        guide_reference_max_chunks=max(0, _env_int(env, "RAG_ENGINE_GUIDE_REFERENCE_MAX_CHUNKS", 4)),
        guide_step_deadline_seconds=max(0.0, _env_float(env, "RAG_ENGINE_GUIDE_STEP_DEADLINE_SECONDS", 60.0)),
        section_expansion_enabled=_env_bool(env, "RAG_ENGINE_SECTION_EXPANSION", True),
        section_expansion_max_levels=max(0, _env_int(env, "RAG_ENGINE_SECTION_EXPANSION_MAX_LEVELS", 4)),
        section_expansion_input_token_percent=max(1, min(100, _env_int(env, "RAG_ENGINE_SECTION_EXPANSION_INPUT_TOKEN_PERCENT", 50))),
        section_expansion_followup_reserve_percent=max(
            0, min(50, _env_int(env, "RAG_ENGINE_SECTION_EXPANSION_FOLLOWUP_RESERVE_PERCENT", 20))
        ),
        answer_max_input_tokens=max(1, _env_int(env, "OCI_ENTERPRISE_AI_MAX_INPUT_TOKENS", 128_000)),
        section_extraction_concurrency=max(1, _env_int(env, "RAG_ENGINE_SECTION_EXTRACTION_CONCURRENCY", 4)),
        article_catalog_enabled=_env_bool(env, "RAG_ENGINE_ARTICLE_CATALOG", True),
        article_catalog_max_documents=max(0, _env_int(env, "RAG_ENGINE_ARTICLE_CATALOG_MAX_DOCUMENTS", 16)),
        article_catalog_max_articles=max(0, _env_int(env, "RAG_ENGINE_ARTICLE_CATALOG_MAX_ARTICLES", 8)),
        article_catalog_context_records=max(0, _env_int(env, "RAG_ENGINE_ARTICLE_CATALOG_CONTEXT_RECORDS", 6)),
        article_catalog_scope_documents=max(0, _env_int(env, "RAG_ENGINE_ARTICLE_CATALOG_SCOPE_DOCUMENTS", 2)),
        article_catalog_original_question_search=_env_bool(env, "RAG_ENGINE_ARTICLE_CATALOG_ORIGINAL_QUESTION_SEARCH", True),
        article_catalog_refine_articles=max(0, _env_int(env, "RAG_ENGINE_ARTICLE_CATALOG_REFINE_ARTICLES", 6)),
        article_catalog_refine_candidates=max(0, _env_int(env, "RAG_ENGINE_ARTICLE_CATALOG_REFINE_CANDIDATES", 60)),
        article_catalog_output_token_cap=max(0, _env_int(env, "RAG_ENGINE_ARTICLE_CATALOG_OUTPUT_TOKEN_CAP", 8192)),
        article_catalog_call_timeout_seconds=max(0.0, _env_float(env, "RAG_ENGINE_ARTICLE_CATALOG_CALL_TIMEOUT_SECONDS", 180.0)),
        article_catalog_review=_env_bool(env, "RAG_ENGINE_ARTICLE_CATALOG_REVIEW", False),
        article_catalog_cache_dir=Path(cache_dir_raw).expanduser() if cache_dir_raw else None,
        article_catalog_cache_ttl_seconds=max(0.0, _env_float(env, "RAG_ENGINE_ARTICLE_CATALOG_CACHE_TTL_SECONDS", 86400.0)),
        article_catalog_cache_max_entries=max(0, _env_int(env, "RAG_ENGINE_ARTICLE_CATALOG_CACHE_MAX_ENTRIES", 4096)),
        answer_validation_max_input_bytes=max(16_000, _env_int(env, "RAG_ENGINE_ANSWER_VALIDATION_MAX_INPUT_BYTES", 96_000)),
        strategy_reasoning_effort=normalize_reasoning_effort_override(env.get("RAG_ENGINE_STRATEGY_REASONING_EFFORT")),
        judge_reasoning_effort=normalize_reasoning_effort_override(env.get("RAG_ENGINE_JUDGE_REASONING_EFFORT")),
        generation_reasoning_effort=normalize_reasoning_effort_override(env.get("RAG_ENGINE_GENERATION_REASONING_EFFORT")),
        audit_reasoning_effort=normalize_reasoning_effort_override(env.get("RAG_ENGINE_AUDIT_REASONING_EFFORT")),
        crag_compact_input=_env_bool(env, "RAG_ENGINE_CRAG_COMPACT_INPUT", False),
        crag_short_candidate_ids=_env_bool(env, "RAG_ENGINE_CRAG_SHORT_CANDIDATE_IDS", False),
        answer_audit_compact_reasons=_env_bool(env, "RAG_ENGINE_ANSWER_AUDIT_COMPACT_REASONS", False),
        answer_max_correction_rounds=max(0, min(2, _env_int(env, "RAG_ENGINE_ANSWER_MAX_CORRECTION_ROUNDS", 2))),
        answer_evidence_refinement_mode=(
            refinement
            if (refinement := env.get("RAG_ENGINE_ANSWER_EVIDENCE_REFINEMENT_MODE", "legacy")) in {"legacy", "cited_only", "cited_required"}
            else "legacy"
        ),
        answer_audit_mode=(mode if (mode := env.get("RAG_ENGINE_ANSWER_AUDIT_MODE", "")) in {"none", "standard"} else "detailed"),
        llm_stall_detection=_env_bool(env, "RAG_ENGINE_LLM_STALL_DETECTION", False),
        llm_stall_idle_seconds=max(1.0, _env_float(env, "RAG_ENGINE_LLM_STALL_IDLE_SECONDS", 20.0)),
        llm_stall_min_rate_ratio=max(0.0, min(1.0, _env_float(env, "RAG_ENGINE_LLM_STALL_MIN_RATE_RATIO", 0.25))),
        llm_stall_resends=max(0, min(2, _env_int(env, "RAG_ENGINE_LLM_STALL_RESENDS", 1))),
        llm_runaway_detection=_env_bool(env, "RAG_ENGINE_LLM_RUNAWAY_DETECTION", False),
        section_skip_regrade_at_scope=_env_bool(env, "RAG_ENGINE_SECTION_SKIP_REGRADE_AT_SCOPE", False),
        crag_skip_on_strong_signals=_env_bool(env, "RAG_ENGINE_CRAG_SKIP_ON_STRONG_SIGNALS", False),
        crag_skip_min_rerank_score=max(0.0, min(1.0, _env_float(env, "RAG_ENGINE_CRAG_SKIP_MIN_RERANK_SCORE", 0.8))),
        query_expansion_max_queries=max(1, _env_int(env, "RAG_ENGINE_QUERY_EXPANSION_MAX_QUERIES", 5)),
        text_search_tokenizer_sudachi_dict=env.get(
            "TEXT_SEARCH_TOKENIZER_SUDACHI_DICT",
            DEFAULT_TEXT_SEARCH_TOKENIZER_SUDACHI_DICT,
        ).strip()
        or DEFAULT_TEXT_SEARCH_TOKENIZER_SUDACHI_DICT,
        text_search_tokenizer_sudachi_config=env.get(
            "TEXT_SEARCH_TOKENIZER_SUDACHI_CONFIG",
            "",
        ).strip(),
        text_search_tokenizer_latin_stemmer=env.get(
            "TEXT_SEARCH_TOKENIZER_LATIN_STEMMER",
            DEFAULT_TEXT_SEARCH_TOKENIZER_LATIN_STEMMER,
        ).strip()
        or DEFAULT_TEXT_SEARCH_TOKENIZER_LATIN_STEMMER,
        llm_api_mode=llm_api_mode,
        llm_providers=llm_providers,
        default_vision_llm=default_vision_llm,
        default_answer_llm=default_answer_llm,
        timezone=env.get("PLATFORM_TIMEZONE", "").strip() or DEFAULT_TIMEZONE,
    )

    if "adb_settings" not in overrides and all(env.get(key) for key in ("ADB_WALLET_PASSWORD", "ADB_DB_PASSWORD")):
        from rag_engine.adapters.oracle.connection import load_adb_settings

        settings = replace(settings, adb_settings=load_adb_settings(environ=env, env_path=dotenv_path or ".env"))
    return replace(settings, **overrides)


# 回答の LLM の呼び出しの用途(#1499)。用途ごとに推論の量を上書きできる。
# 質問拡張戦略の判定と検索文の生成(strategy)・根拠の評価(judge。CRAG の評価と章節の拡張の十分性の判定)・
# 回答文の生成(generation)・生成後の監査(audit。#1528)。
LLM_PURPOSE_STRATEGY = "strategy"
LLM_PURPOSE_JUDGE = "judge"
LLM_PURPOSE_GENERATION = "generation"
LLM_PURPOSE_AUDIT = "audit"
_PURPOSE_EFFORT_FIELDS = {
    LLM_PURPOSE_STRATEGY: "strategy_reasoning_effort",
    LLM_PURPOSE_JUDGE: "judge_reasoning_effort",
    LLM_PURPOSE_GENERATION: "generation_reasoning_effort",
    LLM_PURPOSE_AUDIT: "audit_reasoning_effort",
}
_PURPOSE_LABELS = {
    LLM_PURPOSE_STRATEGY: "質問拡張戦略の判定・検索文の生成",
    LLM_PURPOSE_JUDGE: "根拠の評価",
    LLM_PURPOSE_GENERATION: "回答文の生成",
    LLM_PURPOSE_AUDIT: "回答の監査",
}
# 上書きに使える値(platform の推論の量の選択肢と同じ。none は送らない)。
REASONING_EFFORT_OVERRIDES = ("none", "low", "medium", "high")
REASONING_EFFORT_OMIT = "none"


def normalize_reasoning_effort_override(value: str | None) -> str:
    """用途ごとの推論の量の上書きの値。選択肢に無い値(空・model など)は "" (モデルの登録値のまま)。"""
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in REASONING_EFFORT_OVERRIDES else ""


def settings_for_llm_purpose(settings: Settings, purpose: str) -> Settings:
    """用途の推論の量を反映した設定(#1499)。上書きが無ければ同じ設定を返す。

    LLM の呼び出しに渡す設定だけを差し替える(provider の `reasoning_effort` は送る値)。モデルの登録値が
    送らない(空)の provider は、上書きしても送らない。
    """
    override = normalize_reasoning_effort_override(getattr(settings, _PURPOSE_EFFORT_FIELDS.get(purpose, ""), ""))
    if not override:
        return settings
    current = getattr(settings, "llm_providers", None)
    if not isinstance(settings, Settings) or not isinstance(current, dict):
        return settings
    to_send = "" if override == REASONING_EFFORT_OMIT else override
    providers = {
        key: replace(provider, reasoning_effort=to_send) if provider.reasoning_effort else provider for key, provider in current.items()
    }
    if providers == current:
        return settings
    return replace(settings, llm_providers=providers)


def llm_reasoning_effort_note(settings: Settings, purpose: str, provider_id: str | None = None) -> str:
    """実行記録に残す、その用途で送る推論の量と根拠(#1499)。例: 「推論の量: low（根拠の評価の設定）」。"""
    if not isinstance(settings, Settings):
        return ""
    try:
        registered = get_llm_provider(settings, provider_id or settings.default_answer_llm).reasoning_effort
    except RuntimeError:
        return ""
    override = normalize_reasoning_effort_override(getattr(settings, _PURPOSE_EFFORT_FIELDS.get(purpose, ""), ""))
    label = _PURPOSE_LABELS.get(purpose, purpose)
    if not registered:
        return "推論の量: 送らない（モデルの登録値が none）"
    if not override:
        return f"推論の量: {registered}（モデルの登録値）"
    if override == REASONING_EFFORT_OMIT:
        return f"推論の量: 送らない（{label}の設定）"
    return f"推論の量: {override}（{label}の設定。モデルの登録値は {registered}）"


def get_llm_provider(settings: Settings, provider_id: str | None, default: str = DEFAULT_LLM_PROVIDER) -> LlmProviderSettings:
    """provider ID を解決し、利用する LlmProviderSettings を返します。"""
    normalized = normalize_llm_provider_id(provider_id, default)
    provider = settings.llm_providers.get(normalized)
    if provider is None:
        raise RuntimeError(f"Unknown LLM provider: {provider_id}")
    return provider


def answer_model_reads_images(settings: Settings, provider_id: str | None = None) -> bool:
    """回答のモデル(``provider_id``。None は既定の回答のモデル)が画像を読めるか(#3444)。"""
    try:
        provider = get_llm_provider(settings, provider_id or settings.default_answer_llm)
    except RuntimeError:
        return False
    return bool(provider.supports_vision and provider.model)


def llm_output_token_limit(settings: Settings, provider: Any) -> int:
    """構造化出力の呼び出し 1 回の出力の上限(#1719)。

    回答の上限(`answer_max_tokens`。既定 32768)と、呼ぶモデルの出力の上限(`max_output_tokens`。backend が
    platform の `enterprise_ai_output_token_budget` で決めて渡す)の小さい方。
    """
    model_limit = int(getattr(provider, "max_output_tokens", DEFAULT_MODEL_OUTPUT_TOKENS) or DEFAULT_MODEL_OUTPUT_TOKENS)
    return max(1, min(int(settings.answer_max_tokens), model_limit))
