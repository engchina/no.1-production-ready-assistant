"""言語資源の画面の API(#2181)。

- ``GET /api/settings/language-pack``: 項目の定義・組み込みの値・全体の上書き・重ねた値。
- 草稿の保存は実行設定を変えず、公開・回退だけが現在版を切り替える。
- ``POST /api/settings/language-pack/preview``: 例の文を、保存した言語資源と編集中の上書きの
  それぞれで読む。
- ``GET /api/settings/language-pack/releases``: 不変の完全な公開版をページングで返す。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from rag_parser_core.language import (
    FIELD_SPECS,
    LanguagePack,
    LanguagePackError,
    bind_pack,
    build_pack,
    builtin_pack,
    overrides_from,
    resolve,
)

from app.clients.language_pack_release_store import (
    LanguagePackReleaseConflict,
    LanguagePackReleaseStore,
)
from app.clients.oracle import OracleClient
from app.rag.cross_references import document_name_aliases, extract_references, heading_keys
from app.rag.language_pack_releases import compare_releases, pack_from_release
from app.rag.published_language import initialized_release_store
from app.schemas.common import ApiResponse
from app.schemas.language_pack_release import (
    LanguagePackDraftUpdate,
    LanguagePackIndexRelation,
    LanguagePackPublishRequest,
    LanguagePackRelease,
    LanguagePackReleaseComparison,
    LanguagePackReleaseList,
    LanguagePackReleaseState,
    LanguagePackRestoreRequest,
)
from app.schemas.settings import (
    LanguagePackData,
    LanguagePackFieldData,
    LanguagePackPreviewData,
    LanguagePackPreviewReference,
    LanguagePackPreviewRequest,
    LanguagePackPreviewResult,
)
from app.security.dependencies import current_principal

router = APIRouter()
logger = logging.getLogger(__name__)


def _sections(pack: LanguagePack) -> dict[str, dict[str, object]]:
    data = pack.to_dict()
    data.pop("locale", None)
    return data


def _resolve(overrides: Mapping[str, Mapping[str, Any]]) -> LanguagePack:
    try:
        return resolve(builtin_pack(), overrides)
    except LanguagePackError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc


def _data(pack: LanguagePack) -> LanguagePackData:
    builtin = builtin_pack()
    return LanguagePackData(
        fields=[
            LanguagePackFieldData(
                section=spec.section,
                key=spec.key,
                kind=spec.kind.value,
                choices=list(spec.choices),
                ingest=spec.ingest,
                max_chars=spec.max_chars,
                min_value=spec.min_value,
            )
            for spec in FIELD_SPECS
        ],
        builtin=_sections(builtin),
        overrides=overrides_from(pack, builtin),
        effective=_sections(pack),
        hash=pack.hash,
    )


async def release_store() -> LanguagePackReleaseStore:
    """初回だけ既存の有効設定を完全な公開版として取り込む。既存索引へは付けない。"""
    return await initialized_release_store()


@router.get("/language-pack", response_model=ApiResponse[LanguagePackData])
async def get_language_pack() -> ApiResponse[LanguagePackData]:
    published = await (await release_store()).current()
    data = _data(pack_from_release(published))
    data.current_release = published
    oracle = OracleClient()
    try:
        data.outdated_documents = await oracle.count_documents(
            language_pack_relation=LanguagePackIndexRelation.DIFFERENT,
            current_release_id=published.id,
        )
        data.unrecorded_documents = await oracle.count_documents(
            language_pack_relation=LanguagePackIndexRelation.UNRECORDED,
            current_release_id=published.id,
        )
    except Exception:  # noqa: BLE001 - 件数は補助。取得できないときは不明として返す。
        logger.warning("language pack document relation count failed", exc_info=True)
    return ApiResponse(data=data)


def _user(request: Request) -> str | None:
    try:
        return current_principal(request).display_name
    except Exception:  # noqa: BLE001 - local の認証文脈が無い場合は記録しない。
        return None


@router.get("/language-pack/draft", response_model=ApiResponse[LanguagePackReleaseState])
async def get_language_pack_draft() -> ApiResponse[LanguagePackReleaseState]:
    return ApiResponse(data=await (await release_store()).state())


@router.get(
    "/language-pack/draft/comparison", response_model=ApiResponse[LanguagePackReleaseComparison]
)
async def compare_language_pack_draft(
    expected_revision: int = Query(ge=1),
    expected_current_id: str = Query(min_length=1, max_length=64),
) -> ApiResponse[LanguagePackReleaseComparison]:
    state = await (await release_store()).state()
    if state.draft_revision != expected_revision or state.current.id != expected_current_id:
        raise HTTPException(
            status_code=409, detail="草稿または公開版が変わりました。差分を確認し直してください。"
        )
    pack = build_pack(state.draft_effective)
    draft = state.current.model_copy(
        update={
            "id": f"draft:{state.draft_revision}",
            "effective": pack.to_dict(),
            "hash": pack.hash,
            "ingest_hash": pack.ingest_hash,
        }
    )
    return ApiResponse(data=compare_releases(state.current, draft))


@router.put("/language-pack/draft", response_model=ApiResponse[LanguagePackReleaseState])
async def save_language_pack_draft(
    payload: LanguagePackDraftUpdate,
) -> ApiResponse[LanguagePackReleaseState]:
    pack = _resolve(payload.overrides)
    try:
        state = await (await release_store()).save_draft(
            pack.to_dict(), expected_revision=payload.expected_revision
        )
    except LanguagePackReleaseConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(data=state)


@router.get("/language-pack/releases", response_model=ApiResponse[LanguagePackReleaseList])
async def list_language_pack_releases(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ApiResponse[LanguagePackReleaseList]:
    return ApiResponse(data=await (await release_store()).list(limit=limit, offset=offset))


@router.get(
    "/language-pack/releases/compare", response_model=ApiResponse[LanguagePackReleaseComparison]
)
async def compare_language_pack_releases(
    before_id: str = Query(min_length=1, max_length=64),
    after_id: str = Query(min_length=1, max_length=64),
) -> ApiResponse[LanguagePackReleaseComparison]:
    store = await release_store()
    before, after = await store.get(before_id), await store.get(after_id)
    if before is None or after is None:
        raise HTTPException(status_code=404, detail="言語資源の公開版が見つかりません。")
    return ApiResponse(data=compare_releases(before, after))


@router.get("/language-pack/releases/{release_id}", response_model=ApiResponse[LanguagePackRelease])
async def get_language_pack_release(release_id: str) -> ApiResponse[LanguagePackRelease]:
    published = await (await release_store()).get(release_id)
    if published is None:
        raise HTTPException(status_code=404, detail="言語資源の公開版が見つかりません。")
    return ApiResponse(data=published)


@router.post("/language-pack/publish", response_model=ApiResponse[LanguagePackReleaseState])
async def publish_language_pack(
    payload: LanguagePackPublishRequest, request: Request
) -> ApiResponse[LanguagePackReleaseState]:
    try:
        state = await (await release_store()).publish(
            expected_revision=payload.expected_revision,
            expected_current_id=payload.expected_current_id,
            change_note=payload.change_note,
            published_by=_user(request),
        )
    except LanguagePackReleaseConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(data=state)


@router.post(
    "/language-pack/releases/{release_id}/restore",
    response_model=ApiResponse[LanguagePackReleaseState],
)
async def restore_language_pack_release(
    release_id: str, payload: LanguagePackRestoreRequest, request: Request
) -> ApiResponse[LanguagePackReleaseState]:
    store = await release_store()
    if await store.get(release_id) is None:
        raise HTTPException(status_code=404, detail="言語資源の公開版が見つかりません。")
    try:
        state = await store.restore(
            release_id,
            expected_revision=payload.expected_revision,
            expected_current_id=payload.expected_current_id,
            change_note=payload.change_note,
            published_by=_user(request),
        )
    except LanguagePackReleaseConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(data=state)


def _read(pack: LanguagePack, text: str) -> LanguagePackPreviewResult:
    with bind_pack(pack):
        references = [
            LanguagePackPreviewReference(
                label=spec.label, kind=spec.kind, key=spec.key, document_title=spec.document_title
            )
            for spec in extract_references(text)
        ]
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        return LanguagePackPreviewResult(
            references=references,
            title_keys=sorted(document_name_aliases(first_line)),
            heading_keys=sorted(f"{kind}:{key}" for kind, key in heading_keys(first_line)),
        )


@router.post("/language-pack/preview", response_model=ApiResponse[LanguagePackPreviewData])
async def preview_language_pack(
    payload: LanguagePackPreviewRequest,
) -> ApiResponse[LanguagePackPreviewData]:
    saved = pack_from_release(await (await release_store()).current())
    draft = _resolve(payload.overrides)
    return ApiResponse(
        data=LanguagePackPreviewData(
            saved=_read(saved, payload.text), draft=_read(draft, payload.text)
        )
    )
