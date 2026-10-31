"""業務ガイド（SupportGuide。#1237）の API。

検索・回答プロファイルの知識の 1 つ。下書きを保存（楽観ロック）・検証し、公開した版だけを回答に
使う。公開・ロールバックは新しい版を作り、前の版は履歴に残る。取り込みは下書きとして作るだけで、
公開は管理者が検証してから行う。

公開の前に、下書きで試しに答えられる（#1288。公開の版は変えない）。取り込みの確認は、同じ id・
名前の既存のガイドとの差分を示す。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError

from app.api.routes import search as search_route
from app.clients.oracle import OracleClient
from app.clients.support_guide_store import (
    SupportGuideConflictError,
    SupportGuideNotFoundError,
    SupportGuideStore,
    max_published_revisions,
)
from app.config import get_settings
from app.rag.rate_limit import enforce_rate_limit
from app.rag.search_answer_profile_config import resolve_search_answer_profile_settings
from app.rag.support_guide import diff_contents, has_errors, reference_issues, validate_content
from app.rag.support_guide_query import store_intent_embeddings
from app.rag.support_guide_runtime import GuidePreview
from app.schemas.common import ApiResponse
from app.schemas.document import FileStatus
from app.schemas.search import SearchRequest
from app.schemas.search_answer_profile import SearchAnswerProfileDetail
from app.schemas.support_guide import (
    SUPPORT_GUIDE_STEP_STATUSES,
    SupportGuideContent,
    SupportGuideCreateRequest,
    SupportGuideDetail,
    SupportGuideDraftTryData,
    SupportGuideDraftTryRequest,
    SupportGuideDraftTryStep,
    SupportGuideDraftUpdate,
    SupportGuideExportData,
    SupportGuideImportData,
    SupportGuideImportDiff,
    SupportGuideImportItem,
    SupportGuideImportPreviewData,
    SupportGuideImportRequest,
    SupportGuideIssue,
    SupportGuidePublishRequest,
    SupportGuideRevision,
    SupportGuideRollbackRequest,
    SupportGuideSummary,
    SupportGuideValidationData,
)
from app.security.dependencies import current_principal

router = APIRouter()
_BASE = "/{search_answer_profile_id}/support-guides"
_GUIDE = _BASE + "/{guide_id}"


def _user(request: Request) -> str | None:
    try:
        return current_principal(request).display_name
    except Exception:  # noqa: BLE001 - 認証の無い開発の構成（local）では記録しない
        return None


async def _profile(
    oracle: OracleClient, search_answer_profile_id: str
) -> SearchAnswerProfileDetail:
    view = await oracle.get_search_answer_profile(search_answer_profile_id)
    if view is None:
        raise HTTPException(status_code=404, detail="検索・回答プロファイルが見つかりません。")
    return view


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="業務ガイドが見つかりません。")


def _conflict(error: SupportGuideConflictError, action: str = "保存") -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            "読み込んだ後にほかの人が業務ガイドを保存しました。"
            f"最新の内容（版 {error.current_revision}）を読み込み直してから{action}してください。"
        ),
    )


def _refused(message: str, issues: list[SupportGuideIssue]) -> HTTPException:
    """公開・ロールバックを検証の問題で断る 422。

    共通の envelope は detail の文字の配列を `error_messages` にする（dict は Python の表記の
    文字 1 つにされて読めない）ので、1 つ目に要約、2 つ目以降に問題の文を並べる。
    位置つきの一覧は「検証」（validate）で返す。
    """
    return HTTPException(
        status_code=422,
        detail=[message, *(issue.message for issue in issues if issue.severity == "error")],
    )


async def _publish_issues(
    oracle: OracleClient, profile: SearchAnswerProfileDetail, content: SupportGuideContent
) -> list[SupportGuideIssue]:
    """公開の前の検証（内容の整合と、参照する文書がプロファイルで使えるか）。"""
    knowledge_base_ids = set(profile.config.knowledge_base_ids)

    async def lookup(document_id: str) -> str | None:
        document = await oracle.get_document(document_id)
        if document is None:
            return "文書が見つかりません。"
        bases = {ref.id for ref in await oracle.list_document_knowledge_bases(document_id)}
        if not bases & knowledge_base_ids:
            return "このプロファイルのナレッジベースに含まれていません。"
        if document.status != FileStatus.INDEXED:
            return "索引が済んでいません。"
        return None

    return [*validate_content(content), *await reference_issues(content, lookup)]


async def _store_embeddings(
    oracle: OracleClient,
    profile: SearchAnswerProfileDetail,
    guide_id: str,
    revision: int,
    content: SupportGuideContent,
) -> None:
    """公開の版の質問の例の埋め込みを作る（意味の照合が ON のプロファイルだけ。#2398）。"""
    settings, _ = resolve_search_answer_profile_settings(get_settings(), profile.config)
    await store_intent_embeddings(oracle, settings, guide_id, revision, content)


async def _detail(
    store: SupportGuideStore, search_answer_profile_id: str, guide_id: str
) -> SupportGuideDetail:
    try:
        summary, draft = await store.get_guide(search_answer_profile_id, guide_id)
    except SupportGuideNotFoundError as error:
        raise _not_found() from error
    published = None
    if summary.published_revision is not None:
        published = (await store.get_revision(guide_id, summary.published_revision)).content
    return SupportGuideDetail(
        **summary.model_dump(),
        draft=draft,
        published=published,
        revisions=await store.list_revisions(guide_id),
        max_published_revisions=max_published_revisions(),
        issues=validate_content(draft),
    )


@router.get(_BASE, response_model=ApiResponse[list[SupportGuideSummary]])
async def list_support_guides(
    search_answer_profile_id: str,
    include_archived: bool = Query(default=False),
) -> ApiResponse[list[SupportGuideSummary]]:
    """業務ガイドの一覧（アーカイブしたものは既定で出さない）。"""
    oracle = OracleClient()
    await _profile(oracle, search_answer_profile_id)
    store = SupportGuideStore(oracle)
    return ApiResponse(
        data=await store.list_guides(search_answer_profile_id, include_archived=include_archived)
    )


@router.post(_BASE, response_model=ApiResponse[SupportGuideDetail], status_code=201)
async def create_support_guide(
    request: Request, search_answer_profile_id: str, body: SupportGuideCreateRequest
) -> ApiResponse[SupportGuideDetail]:
    """下書きとして作る（公開は別の操作）。"""
    oracle = OracleClient()
    await _profile(oracle, search_answer_profile_id)
    store = SupportGuideStore(oracle)
    guide_id = await store.create_guide(search_answer_profile_id, body.draft, user=_user(request))
    return ApiResponse(data=await _detail(store, search_answer_profile_id, guide_id))


@router.get(_BASE + "/export", response_model=ApiResponse[SupportGuideExportData])
async def export_support_guides(
    search_answer_profile_id: str,
) -> ApiResponse[SupportGuideExportData]:
    """公開の版を JSON で書き出す（下書きは含めない）。"""
    oracle = OracleClient()
    await _profile(oracle, search_answer_profile_id)
    published = await SupportGuideStore(oracle).published_contents(search_answer_profile_id)
    return ApiResponse(data=SupportGuideExportData(guides=[content for _, _, content in published]))


# 取り込む JSON の各ガイドに付けてよい、既存のガイドの id（差分の突き合わせだけに使う。#1288）。
_IMPORT_GUIDE_ID_KEY = "guide_id"


def _import_items(
    body: SupportGuideImportRequest,
) -> list[tuple[SupportGuideImportItem, SupportGuideContent | None, str | None]]:
    """各ガイドの検証の結果・内容・既存のガイドの id（JSON にあれば）。"""
    items: list[tuple[SupportGuideImportItem, SupportGuideContent | None, str | None]] = []
    for index, original in enumerate(body.guides):
        raw = dict(original)
        hint = raw.pop(_IMPORT_GUIDE_ID_KEY, None)
        guide_id = hint.strip() if isinstance(hint, str) and hint.strip() else None
        try:
            content = SupportGuideContent.model_validate(raw)
        except ValidationError as error:
            issues = [
                SupportGuideIssue(
                    severity="error",
                    code="invalid_content",
                    path=".".join(str(part) for part in item["loc"]),
                    message=str(item["msg"]),
                )
                for item in error.errors()[:20]
            ]
            title = raw.get("title") if isinstance(raw.get("title"), str) else None
            items.append(
                (
                    SupportGuideImportItem(index=index, title=title, valid=False, issues=issues),
                    None,
                    guide_id,
                )
            )
            continue
        issues = validate_content(content)
        items.append(
            (
                SupportGuideImportItem(
                    index=index, title=content.title, valid=not has_errors(issues), issues=issues
                ),
                content,
                guide_id,
            )
        )
    return items


def _existing_guide(
    guides: list[SupportGuideSummary], content: SupportGuideContent, guide_id: str | None
) -> tuple[SupportGuideSummary, Literal["id", "title"]] | None:
    """取り込むガイドと同じ id、無ければ同じ名前の既存のガイド。

    同じ名前が複数あれば、アーカイブしたものより使っているものを先にする。
    """
    if guide_id:
        found = next((guide for guide in guides if guide.guide_id == guide_id), None)
        if found is not None:
            return found, "id"
    title = content.title.strip()
    same = [guide for guide in guides if guide.title.strip() == title]
    same.sort(key=lambda guide: guide.status != "active")
    return (same[0], "title") if same else None


async def _import_diff(
    store: SupportGuideStore,
    search_answer_profile_id: str,
    guides: list[SupportGuideSummary],
    content: SupportGuideContent,
    guide_id: str | None,
) -> SupportGuideImportDiff | None:
    """既存のガイド（公開の版、無ければ下書き）と取り込むガイドの差分。同じものが無ければ None。"""
    found = _existing_guide(guides, content, guide_id)
    if found is None:
        return None
    summary, matched_by = found
    try:
        if summary.published_revision is not None:
            base: Literal["published", "draft"] = "published"
            revision = summary.published_revision
            existing = (await store.get_revision(summary.guide_id, revision)).content
        else:
            base, revision = "draft", summary.draft_revision
            existing = (await store.get_guide(search_answer_profile_id, summary.guide_id))[1]
    except SupportGuideNotFoundError:
        return None
    return SupportGuideImportDiff(
        guide_id=summary.guide_id,
        title=summary.title,
        matched_by=matched_by,
        base=base,
        revision=revision,
        status=summary.status,
        changes=diff_contents(existing, content),
    )


@router.post(_BASE + "/import/preview", response_model=ApiResponse[SupportGuideImportPreviewData])
async def preview_support_guide_import(
    search_answer_profile_id: str, body: SupportGuideImportRequest
) -> ApiResponse[SupportGuideImportPreviewData]:
    """取り込む前に、各ガイドの内容を検証し、同じ id・名前の既存のガイドとの差分を示す（#1288）。"""
    oracle = OracleClient()
    await _profile(oracle, search_answer_profile_id)
    store = SupportGuideStore(oracle)
    existing = await store.list_guides(search_answer_profile_id, include_archived=True)
    items: list[SupportGuideImportItem] = []
    for item, content, guide_id in _import_items(body):
        if content is not None:
            item.existing = await _import_diff(
                store, search_answer_profile_id, existing, content, guide_id
            )
        items.append(item)
    return ApiResponse(
        data=SupportGuideImportPreviewData(
            items=items, importable_count=sum(item.valid for item in items)
        )
    )


@router.post(_BASE + "/import", response_model=ApiResponse[SupportGuideImportData])
async def import_support_guides(
    request: Request, search_answer_profile_id: str, body: SupportGuideImportRequest
) -> ApiResponse[SupportGuideImportData]:
    """検証を通ったガイドを下書きとして作る（公開しない）。1 件でも不正なら何も作らない。"""
    oracle = OracleClient()
    await _profile(oracle, search_answer_profile_id)
    items = _import_items(body)
    invalid = [item.index for item, content, _ in items if content is None or not item.valid]
    if invalid:
        numbers = ", ".join(str(i + 1) for i in invalid)
        raise HTTPException(
            status_code=422, detail=f"取り込めないガイドがあります（{numbers} 件目）。"
        )
    store = SupportGuideStore(oracle)
    user = _user(request)
    created: list[SupportGuideSummary] = []
    for _, content, _ in items:
        assert content is not None
        guide_id = await store.create_guide(search_answer_profile_id, content, user=user)
        created.append((await store.get_guide(search_answer_profile_id, guide_id))[0])
    return ApiResponse(data=SupportGuideImportData(created=created))


@router.get(_GUIDE, response_model=ApiResponse[SupportGuideDetail])
async def get_support_guide(
    search_answer_profile_id: str, guide_id: str
) -> ApiResponse[SupportGuideDetail]:
    """下書き・公開の版・履歴と、下書きの検証の結果。"""
    oracle = OracleClient()
    await _profile(oracle, search_answer_profile_id)
    return ApiResponse(
        data=await _detail(SupportGuideStore(oracle), search_answer_profile_id, guide_id)
    )


@router.put(_GUIDE, response_model=ApiResponse[SupportGuideDetail])
async def save_support_guide_draft(
    request: Request, search_answer_profile_id: str, guide_id: str, body: SupportGuideDraftUpdate
) -> ApiResponse[SupportGuideDetail]:
    """下書きを保存する（`base_revision` が今の版と違えば 409）。公開の版は変わらない。"""
    oracle = OracleClient()
    await _profile(oracle, search_answer_profile_id)
    store = SupportGuideStore(oracle)
    try:
        await store.save_draft(
            search_answer_profile_id,
            guide_id,
            body.draft,
            base_revision=body.base_revision,
            user=_user(request),
        )
    except SupportGuideNotFoundError as error:
        raise _not_found() from error
    except SupportGuideConflictError as error:
        raise _conflict(error) from error
    return ApiResponse(data=await _detail(store, search_answer_profile_id, guide_id))


@router.post(_GUIDE + "/validate", response_model=ApiResponse[SupportGuideValidationData])
async def validate_support_guide(
    search_answer_profile_id: str, guide_id: str
) -> ApiResponse[SupportGuideValidationData]:
    """下書きを公開の前と同じ規則で検証する（参照する文書も確かめる）。"""
    oracle = OracleClient()
    profile = await _profile(oracle, search_answer_profile_id)
    try:
        _, draft = await SupportGuideStore(oracle).get_guide(search_answer_profile_id, guide_id)
    except SupportGuideNotFoundError as error:
        raise _not_found() from error
    issues = await _publish_issues(oracle, profile, draft)
    return ApiResponse(data=SupportGuideValidationData(valid=not has_errors(issues), issues=issues))


@router.post(_GUIDE + "/publish", response_model=ApiResponse[SupportGuideDetail])
async def publish_support_guide(
    request: Request, search_answer_profile_id: str, guide_id: str, body: SupportGuidePublishRequest
) -> ApiResponse[SupportGuideDetail]:
    """下書きを検証して新しい公開の版にする。検証に通らなければ 422（前の公開の版のまま）。"""
    oracle = OracleClient()
    profile = await _profile(oracle, search_answer_profile_id)
    store = SupportGuideStore(oracle)
    try:
        summary, draft = await store.get_guide(search_answer_profile_id, guide_id)
    except SupportGuideNotFoundError as error:
        raise _not_found() from error
    if summary.status != "active":
        raise HTTPException(status_code=409, detail="アーカイブした業務ガイドは公開できません。")
    if summary.draft_revision != body.base_revision:
        raise _conflict(SupportGuideConflictError(summary.draft_revision))
    issues = await _publish_issues(oracle, profile, draft)
    if has_errors(issues):
        raise _refused("検証で問題が見つかったため公開できません。", issues)
    try:
        revision = await store.publish(
            search_answer_profile_id,
            guide_id,
            draft,
            base_revision=body.base_revision,
            user=_user(request),
        )
    except SupportGuideConflictError as error:
        raise _conflict(error) from error
    await _store_embeddings(oracle, profile, guide_id, revision, draft)
    return ApiResponse(data=await _detail(store, search_answer_profile_id, guide_id))


@router.post(_GUIDE + "/try", response_model=ApiResponse[SupportGuideDraftTryData])
async def try_support_guide_draft(
    http_request: Request,
    search_answer_profile_id: str,
    guide_id: str,
    body: SupportGuideDraftTryRequest,
) -> ApiResponse[SupportGuideDraftTryData]:
    """保存した下書きで試しに答える（#1288）。公開の版は変えない。

    権限は業務ガイドの管理と同じ（manifest）。検索・回答は通常の回答と同じ流れで、照合の時だけ
    このガイドの公開の版の代わりに下書きを使う。回答の記録には ``guide_preview`` を残し、利用者の
    回答の履歴・フィードバック・評価に混ぜない。読み込んだ下書きの版（``draft_revision``）が今の版と
    違えば 409、下書きが構造の検証に通らなければ 422。
    """
    enforce_rate_limit("search", http_request)
    oracle = OracleClient()
    await _profile(oracle, search_answer_profile_id)
    store = SupportGuideStore(oracle)
    try:
        summary, draft = await store.get_guide(search_answer_profile_id, guide_id)
    except SupportGuideNotFoundError as error:
        raise _not_found() from error
    if summary.status != "active":
        raise HTTPException(status_code=409, detail="アーカイブした業務ガイドは試せません。")
    if summary.draft_revision != body.draft_revision:
        raise _conflict(SupportGuideConflictError(summary.draft_revision), "試")
    issues = validate_content(draft)
    if has_errors(issues):
        raise _refused("検証で問題が見つかったため、この下書きでは試せません。", issues)
    preview = GuidePreview(
        guide_id=guide_id,
        draft_revision=summary.draft_revision,
        content=draft,
        published_revision=summary.published_revision,
    )
    result = await search_route.run_guide_preview(
        SearchRequest(
            query=body.query,
            search_answer_profile_id=search_answer_profile_id,
            conditions=body.conditions,
        ),
        preview,
    )
    answer = result.diagnostics.answer or {}
    raw_guide = answer.get("guide")
    guide = dict(raw_guide) if isinstance(raw_guide, dict) else None
    envelope = answer.get("envelope")
    raw_clarifications = envelope.get("clarifications") if isinstance(envelope, dict) else None
    outcome = answer.get("outcome")
    guide_used = bool(guide and guide.get("guide_id") == guide_id and guide.get("draft") is True)
    return ApiResponse(
        data=SupportGuideDraftTryData(
            trace_id=result.trace_id,
            guide_id=guide_id,
            draft_revision=summary.draft_revision,
            published_revision=summary.published_revision,
            guide_used=guide_used,
            guide=guide,
            outcome=outcome if isinstance(outcome, str) else None,
            answer=result.answer,
            notice=result.notice,
            citations=result.citations,
            clarifications=[
                dict(item) for item in raw_clarifications or [] if isinstance(item, dict)
            ]
            if isinstance(raw_clarifications, list)
            else [],
            steps=_try_steps(guide, draft) if guide is not None and guide_used else [],
            elapsed_ms=result.elapsed_ms,
        )
    )


def _try_steps(
    guide: Mapping[str, Any], draft: SupportGuideContent
) -> list[SupportGuideDraftTryStep]:
    """下書きで試した回答の手順ごとの充足（#2686）。回答の記録の業務ガイドの ``step_coverage`` に、
    下書きの手順の題名を付ける（記録には題名を持たない）。"""
    raw = guide.get("step_coverage")
    coverage = raw if isinstance(raw, list) else []
    titles = {step.id: step.title for step in draft.steps}
    return [
        SupportGuideDraftTryStep(
            step_id=step_id,
            title=titles.get(step_id, step_id),
            status=status,
            required=step.get("required") is not False,
        )
        for step in coverage
        if isinstance(step, Mapping)
        and isinstance(step_id := step.get("step_id"), str)
        and (status := step.get("status")) in SUPPORT_GUIDE_STEP_STATUSES
    ]


@router.get(_GUIDE + "/revisions/{revision}", response_model=ApiResponse[SupportGuideRevision])
async def get_support_guide_revision(
    search_answer_profile_id: str, guide_id: str, revision: int
) -> ApiResponse[SupportGuideRevision]:
    """公開した版の内容。"""
    oracle = OracleClient()
    await _profile(oracle, search_answer_profile_id)
    store = SupportGuideStore(oracle)
    try:
        await store.get_guide(search_answer_profile_id, guide_id)
        return ApiResponse(data=await store.get_revision(guide_id, revision))
    except SupportGuideNotFoundError as error:
        raise _not_found() from error


@router.post(_GUIDE + "/rollback", response_model=ApiResponse[SupportGuideDetail])
async def rollback_support_guide(
    request: Request,
    search_answer_profile_id: str,
    guide_id: str,
    body: SupportGuideRollbackRequest,
) -> ApiResponse[SupportGuideDetail]:
    """古い公開の版を新しい版として公開し直す（下書きもその内容になる）。参照は今の状態で検証する。

    下書きを置き換えるので、読み込んだ下書きの版（``base_revision``）を照合し、違えば 409
    （ほかの人の保存した下書きを黙って失わない。#1278）。
    """
    oracle = OracleClient()
    profile = await _profile(oracle, search_answer_profile_id)
    store = SupportGuideStore(oracle)
    try:
        summary, _ = await store.get_guide(search_answer_profile_id, guide_id)
        target = await store.get_revision(guide_id, body.revision)
    except SupportGuideNotFoundError as error:
        raise _not_found() from error
    if summary.status != "active":
        raise HTTPException(status_code=409, detail="アーカイブした業務ガイドは公開できません。")
    if summary.draft_revision != body.base_revision:
        raise _conflict(SupportGuideConflictError(summary.draft_revision), "戻")
    issues = await _publish_issues(oracle, profile, target.content)
    if has_errors(issues):
        raise _refused("戻す版は、今の資料の状態では検証に通りません。", issues)
    try:
        revision = await store.publish(
            search_answer_profile_id,
            guide_id,
            target.content,
            base_revision=body.base_revision,
            user=_user(request),
            rollback_from=body.revision,
        )
    except SupportGuideConflictError as error:
        raise _conflict(error, "戻") from error
    await _store_embeddings(oracle, profile, guide_id, revision, target.content)
    return ApiResponse(data=await _detail(store, search_answer_profile_id, guide_id))


@router.post(_GUIDE + "/archive", response_model=ApiResponse[SupportGuideDetail])
async def archive_support_guide(
    request: Request, search_answer_profile_id: str, guide_id: str
) -> ApiResponse[SupportGuideDetail]:
    """アーカイブする（回答に使わなくなる。履歴は残る）。"""
    return await _set_status(request, search_answer_profile_id, guide_id, "archived")


@router.post(_GUIDE + "/restore", response_model=ApiResponse[SupportGuideDetail])
async def restore_support_guide(
    request: Request, search_answer_profile_id: str, guide_id: str
) -> ApiResponse[SupportGuideDetail]:
    """アーカイブから戻す（公開の版があれば、また回答に使う）。"""
    return await _set_status(request, search_answer_profile_id, guide_id, "active")


async def _set_status(
    request: Request, search_answer_profile_id: str, guide_id: str, status: str
) -> ApiResponse[SupportGuideDetail]:
    oracle = OracleClient()
    await _profile(oracle, search_answer_profile_id)
    store = SupportGuideStore(oracle)
    try:
        await store.set_status(search_answer_profile_id, guide_id, status, user=_user(request))
    except SupportGuideNotFoundError as error:
        raise _not_found() from error
    return ApiResponse(data=await _detail(store, search_answer_profile_id, guide_id))
