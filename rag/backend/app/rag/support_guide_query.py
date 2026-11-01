"""業務ガイドの照合に使う質問を作る（#2398）。

検索（``_resolve_query_context``）・チャットの確認の判定（``clarifications/suggest``）・MCP の
``rag_lookup_guides`` の 3 か所が、同じ ``build_guide_query`` で質問を作る。

1. 用語の別名の展開（常に行う。モデルは呼ばない）: 検索・回答プロファイルの用語・同義語
   （runtime knowledge）の terms の別名で、質問を展開する（``expand_guide_query``）。
2. 意味の照合（``RAG_SUPPORT_GUIDE_SEMANTIC_MATCH_ENABLED`` か検索・回答プロファイルで ON のとき
   だけ）: 質問を埋め込みモデルで埋め込み、公開の版ごとに持つ質問の例の埋め込みと比べる。
   質問の例の埋め込みは公開のときに作る（``store_intent_embeddings``）。無い・モデルや例が変わった
   版は、照合のときに作って保存する（下書きで試すガイドは保存しない）。埋め込みに失敗したら意味の
   照合を使わずに続ける（``semantic=failed``）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from app.clients.genai import EmbeddingInputType, GenAiClient
from app.clients.oracle import OracleClient
from app.clients.support_guide_store import StoredIntentEmbeddings, SupportGuideStore
from app.config import Settings
from app.rag.support_guide_runtime import GuideQuery, GuideTriple, expand_guide_query
from app.schemas.support_guide import SupportGuideContent

logger = logging.getLogger(__name__)

# 質問と質問の例はどちらも質問なので、同じ種類で埋め込む（近さを対称にする）。
_INPUT_TYPE: EmbeddingInputType = "SEARCH_QUERY"


class GuideEmbedder(Protocol):
    async def embed(
        self, texts: list[str], *, input_type: EmbeddingInputType = ...
    ) -> list[list[float]]: ...


def guide_embedder(settings: Settings) -> GuideEmbedder:
    """意味の照合の埋め込みの呼び出し先（テストで差し替える）。"""
    return GenAiClient(settings=settings)


def examples_sha256(content: SupportGuideContent) -> str:
    """質問の例の指紋（例が変わったら埋め込みを作り直す）。"""
    payload = json.dumps(content.goal.intent_examples, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fresh(
    stored: StoredIntentEmbeddings, revision: int, content: SupportGuideContent, settings: Settings
) -> bool:
    return (
        stored.revision == revision
        and stored.model_id == settings.genai_embedding_model
        and stored.dimension == settings.genai_embedding_dim
        and stored.examples_sha256 == examples_sha256(content)
        and len(stored.vectors) == len(content.goal.intent_examples)
    )


async def store_intent_embeddings(
    oracle: OracleClient,
    settings: Settings,
    guide_id: str,
    revision: int,
    content: SupportGuideContent,
) -> None:
    """公開の版の質問の例の埋め込みを作って保存する（意味の照合が ON のときの公開・戻す）。

    失敗しても公開は取り消さない（照合のときに作り直す）。
    """
    if not settings.rag_support_guide_semantic_match_enabled:
        return
    examples = list(content.goal.intent_examples)
    if not examples:
        return
    try:
        vectors = await guide_embedder(settings).embed(examples, input_type=_INPUT_TYPE)
        await SupportGuideStore(oracle).save_intent_embeddings(
            guide_id,
            revision,
            model_id=settings.genai_embedding_model,
            dimension=settings.genai_embedding_dim,
            examples_sha256=examples_sha256(content),
            vectors=vectors,
        )
    except Exception:  # noqa: BLE001 - 埋め込みは補助。照合のときに作り直す。
        logger.warning("support guide intent embeddings failed on publish", exc_info=True)


async def _example_vectors(
    oracle: OracleClient,
    settings: Settings,
    embedder: GuideEmbedder,
    search_answer_profile_id: str,
    guides: Sequence[GuideTriple],
    unsaved: frozenset[str],
) -> dict[str, list[list[float]]]:
    """ガイドごとの質問の例の埋め込み（無い・古い版は作り、公開の版なら保存する）。"""
    store = SupportGuideStore(oracle)
    stored = await store.published_intent_embeddings(search_answer_profile_id)
    vectors: dict[str, list[list[float]]] = {}
    missing: list[GuideTriple] = []
    for guide_id, revision, content in guides:
        if not content.goal.intent_examples:
            continue
        item = stored.get(guide_id)
        if (
            item is not None
            and guide_id not in unsaved
            and _fresh(item, revision, content, settings)
        ):
            vectors[guide_id] = [list(vector) for vector in item.vectors]
        else:
            missing.append((guide_id, revision, content))
    if not missing:
        return vectors
    texts = [example for _, _, content in missing for example in content.goal.intent_examples]
    embedded = await embedder.embed(texts, input_type=_INPUT_TYPE)
    offset = 0
    for guide_id, revision, content in missing:
        count = len(content.goal.intent_examples)
        vectors[guide_id] = embedded[offset : offset + count]
        offset += count
        if guide_id in unsaved:
            continue
        try:
            await store.save_intent_embeddings(
                guide_id,
                revision,
                model_id=settings.genai_embedding_model,
                dimension=settings.genai_embedding_dim,
                examples_sha256=examples_sha256(content),
                vectors=vectors[guide_id],
            )
        except Exception:  # noqa: BLE001 - 保存できなくても今回の照合には使う。
            logger.warning("support guide intent embeddings save failed", exc_info=True)
    return vectors


async def build_guide_query(
    oracle: OracleClient,
    settings: Settings,
    search_answer_profile_id: str,
    guides: Sequence[GuideTriple],
    text: str,
    runtime_knowledge: Mapping[str, Any] | None,
    *,
    unsaved: frozenset[str] = frozenset(),
) -> GuideQuery:
    """業務ガイドの照合に使う質問（別名の展開と、ON のときは意味の照合の埋め込み）。

    ``settings`` は検索・回答プロファイルを重ねた設定。``unsaved`` は埋め込みを保存しないガイド
    （下書きで試すガイド）の id。
    """
    if not settings.rag_support_guide_enabled:
        return GuideQuery(text=text)
    query = expand_guide_query(text, runtime_knowledge)
    if not settings.rag_support_guide_semantic_match_enabled or not any(
        content.goal.intent_examples for _, _, content in guides
    ):
        return query
    embedder = guide_embedder(settings)
    try:
        examples = await _example_vectors(
            oracle, settings, embedder, search_answer_profile_id, guides, unsaved
        )
        [vector] = await embedder.embed([text], input_type=_INPUT_TYPE)
    except Exception:  # noqa: BLE001 - 意味の照合は補助。語の照合だけで続ける。
        logger.warning("support guide semantic match failed", exc_info=True)
        return GuideQuery(
            text=query.text,
            expanded=query.expanded,
            alias_terms=query.alias_terms,
            semantic="failed",
        )
    return GuideQuery(
        text=query.text,
        expanded=query.expanded,
        alias_terms=query.alias_terms,
        vector=tuple(vector),
        example_vectors=examples,
        semantic_threshold=settings.rag_support_guide_semantic_threshold,
        semantic="on",
    )


__all__ = [
    "GuideEmbedder",
    "build_guide_query",
    "examples_sha256",
    "guide_embedder",
    "store_intent_embeddings",
]
