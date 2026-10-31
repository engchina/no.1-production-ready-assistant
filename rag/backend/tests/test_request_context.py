"""監査用リクエストコンテキストのテスト。"""

import pytest

from app.config import Settings, get_settings
from app.main import create_app
from app.rag.request_context import (
    NO_ACCESS_AUDIT_REQUEST_CONTEXT,
    AuditRequestContext,
    audit_request_context_from_headers,
    current_audit_request_context,
    reset_audit_request_context,
    set_audit_request_context,
    system_access_context,
    unrestricted_access_scope,
    with_system_access,
    without_access_scope,
)
from tests.support import AsgiTestClient


def test_audit_request_context_hashes_tenant_and_user_headers() -> None:
    """tenant/user header は raw 値ではなく hash として保持する。"""
    context = audit_request_context_from_headers(
        {
            "x-tenant-id": "tenant-a",
            "x-user-id": "user@example.com",
            "x-rag-role-id": "finance-reviewer",
            "x-rag-agent-id": "answer-agent",
            "x-rag-thread-id": "thread-123",
        },
        request_id="request-1",
        settings=Settings(audit_context_hash_salt="salt-1"),
    )

    assert context.request_id == "request-1"
    assert context.tenant_id_hash
    assert context.user_id_hash
    assert context.role_id_hash
    assert context.agent_id_hash
    assert context.thread_id_hash
    assert len(context.tenant_id_hash) == 64
    assert len(context.user_id_hash) == 64
    assert len(context.role_id_hash) == 64
    assert len(context.agent_id_hash) == 64
    assert len(context.thread_id_hash) == 64
    assert "tenant-a" not in repr(context)
    assert "user@example.com" not in repr(context)
    assert "finance-reviewer" not in repr(context)
    assert "answer-agent" not in repr(context)
    assert "thread-123" not in repr(context)


def test_audit_context_hash_salt_changes_identifier_hashes() -> None:
    """salt を変えると同じ id でも hash が変わる。"""
    first = audit_request_context_from_headers(
        {"x-tenant-id": "tenant-a"},
        request_id="request-1",
        settings=Settings(audit_context_hash_salt="salt-1"),
    )
    second = audit_request_context_from_headers(
        {"x-tenant-id": "tenant-a"},
        request_id="request-1",
        settings=Settings(audit_context_hash_salt="salt-2"),
    )

    assert first.tenant_id_hash != second.tenant_id_hash


def test_authenticated_user_precedes_spoofable_header() -> None:
    """認証済み user は client 指定 header より優先する。"""
    settings = Settings(audit_context_hash_salt="salt-1")
    context = audit_request_context_from_headers(
        {"x-user-id": "spoofed-user"},
        request_id="request-1",
        settings=settings,
        authenticated_user_id="signed-session-user",
    )
    expected = audit_request_context_from_headers(
        {"x-user-id": "signed-session-user"},
        request_id="request-1",
        settings=settings,
    )

    assert context.user_id_hash == expected.user_id_hash


def test_user_header_is_ignored_when_authentication_is_enabled() -> None:
    """認証有効時は署名済みセッションがなければ user header を信頼しない。"""
    context = audit_request_context_from_headers(
        {"x-user-id": "spoofed-user"},
        request_id="request-1",
        settings=Settings(audit_context_hash_salt="salt-1"),
        allow_user_header=False,
    )

    assert context.user_id_hash is None


def test_default_user_is_used_only_without_header() -> None:
    """local mode は明示 header を保ち、未指定時だけ仮想 user を補う。"""
    settings = Settings(audit_context_hash_salt="salt-1")
    explicit = audit_request_context_from_headers(
        {"x-user-id": "service-user"},
        request_id="request-1",
        settings=settings,
        default_user_id="local-user",
    )
    fallback = audit_request_context_from_headers(
        {},
        request_id="request-2",
        settings=settings,
        default_user_id="local-user",
    )

    assert explicit.user_id_hash != fallback.user_id_hash
    assert fallback.user_id_hash is not None


def test_audit_context_ignores_invalid_or_oversized_header_values() -> None:
    """制御文字や過大な header は監査 context へ入れない。"""
    context = audit_request_context_from_headers(
        {
            "x-tenant-id": "tenant\nbad",
            "x-user-id": "u" * 257,
        },
        request_id="request-1",
        settings=Settings(),
    )

    assert context.tenant_id_hash is None
    assert context.user_id_hash is None


def test_audit_context_parses_access_scope_headers_without_repr_leakage() -> None:
    """アクセス範囲 header は request 内 filter として保持し、repr へ raw 値を出さない。"""
    context = audit_request_context_from_headers(
        {
            "x-rag-allowed-document-ids": "doc-1, doc-2, bad id",
            "x-rag-allowed-category-names": "契約書, FAQ",
        },
        request_id="request-1",
        settings=Settings(),
    )

    assert context.allowed_document_ids == frozenset({"doc-1", "doc-2"})
    assert context.allowed_category_names == frozenset({"契約書".casefold(), "faq"})
    assert "doc-1" not in repr(context)
    assert "契約書" not in repr(context)


def test_audit_context_parses_allowed_knowledge_base_ids_without_repr_leakage() -> None:
    """ナレッジベース scope header も request 内 filter として保持する。"""
    context = audit_request_context_from_headers(
        {
            "x-rag-allowed-knowledge-base-ids": "kb-1, kb_2, bad id",
        },
        request_id="request-1",
        settings=Settings(),
    )

    assert context.allowed_knowledge_base_ids == frozenset({"kb-1", "kb_2"})
    assert "kb-1" not in repr(context)


def test_audit_context_empty_access_scope_header_means_deny_all_scope() -> None:
    """アクセス範囲 header が存在するが有効値なしなら空 scope として扱う。"""
    context = audit_request_context_from_headers(
        {
            "x-rag-allowed-document-ids": "bad id, \n",
            "x-rag-allowed-category-names": "  ",
            "x-rag-allowed-knowledge-base-ids": "bad id",
        },
        request_id="request-1",
        settings=Settings(),
    )

    assert context.allowed_document_ids == frozenset()
    assert context.allowed_category_names == frozenset()
    assert context.allowed_knowledge_base_ids == frozenset()


def test_audit_request_context_is_scoped_to_current_context() -> None:
    """contextvars の設定・復元ができる。文脈が無ければ何も使えない context（#3250）。"""
    assert current_audit_request_context() is NO_ACCESS_AUDIT_REQUEST_CONTEXT

    token = set_audit_request_context(
        AuditRequestContext(
            request_id="request-1",
            tenant_id_hash="a" * 64,
            user_id_hash="b" * 64,
        )
    )
    try:
        context = current_audit_request_context()
        assert context.request_id == "request-1"
        assert context.tenant_id_hash == "a" * 64
        assert context.user_id_hash == "b" * 64
    finally:
        reset_audit_request_context(token)

    assert current_audit_request_context() is NO_ACCESS_AUDIT_REQUEST_CONTEXT


def test_missing_context_denies_every_access_scope() -> None:
    """文脈の無い処理の範囲は、制限なし（None）ではなく空の集合（拒否全件）。利用者も無い（#3250）。"""
    context = current_audit_request_context()
    assert context.allowed_document_ids == frozenset()
    assert context.allowed_category_names == frozenset()
    assert context.allowed_knowledge_base_ids == frozenset()
    assert context.allowed_search_answer_profile_ids == frozenset()
    assert context.user_id_hash is None
    assert not context.all_users_visible
    assert not context.answer_records_unrestricted
    assert not context.feedback_all_users
    # 範囲の制限を一時的に外す処理（既定データの確認）も、文脈の無いところでは外した範囲だけ。
    with unrestricted_access_scope():
        widened = current_audit_request_context()
        assert widened.allowed_search_answer_profile_ids is None
        assert widened.allowed_knowledge_base_ids is None
        assert widened.allowed_document_ids == frozenset()
    assert current_audit_request_context() is NO_ACCESS_AUDIT_REQUEST_CONTEXT


def test_system_access_is_explicit_and_restored() -> None:
    """範囲の制限の無い context は、信頼した処理の入口が明示したときだけ（#3250）。"""
    with system_access_context():
        context = current_audit_request_context()
        assert context == AuditRequestContext()
        assert context.allowed_document_ids is None
        assert context.allowed_knowledge_base_ids is None
        assert context.allowed_search_answer_profile_ids is None
        # 利用者は分からないので、利用者の物（会話など）は全員の分を見る権限を持たない。
        assert context.user_id_hash is None
        assert not context.all_users_visible
    assert current_audit_request_context() is NO_ACCESS_AUDIT_REQUEST_CONTEXT

    seen: list[AuditRequestContext] = []

    @with_system_access
    def main(value: int) -> int:
        seen.append(current_audit_request_context())
        return value + 1

    assert main(1) == 2
    assert seen == [AuditRequestContext()]
    assert current_audit_request_context() is NO_ACCESS_AUDIT_REQUEST_CONTEXT


@pytest.mark.parametrize("auth_mode", ["authenticated", "local"])
def test_request_without_a_principal_starts_with_no_access(
    monkeypatch: pytest.MonkeyPatch, auth_mode: str
) -> None:
    """認証の dependency を通らない要求（公開 path・404 など）の初期の context（#3250）。

    production は利用者が分からないので範囲をすべて空の集合（拒否全件）にし、client の
    `x-rag-allowed-*` も使わない。local は全権限のローカル利用者なので header のとおり（無ければ
    制限なし）。
    """
    monkeypatch.setattr(get_settings(), "auth_mode", auth_mode)
    probe_app = create_app()
    seen: list[AuditRequestContext] = []

    @probe_app.get("/probe-audit-context")
    async def probe() -> dict[str, str]:
        seen.append(current_audit_request_context())
        return {}

    response = AsgiTestClient(probe_app).get(
        "/probe-audit-context", headers={"x-rag-allowed-knowledge-base-ids": "kb-a"}
    )

    assert response.status_code == 200
    [context] = seen
    assert context.request_id
    if auth_mode == "authenticated":
        assert context.user_id_hash is None
        assert context.allowed_document_ids == frozenset()
        assert context.allowed_category_names == frozenset()
        assert context.allowed_knowledge_base_ids == frozenset()
        assert context.allowed_search_answer_profile_ids == frozenset()
    else:
        assert context.allowed_document_ids is None
        assert context.allowed_knowledge_base_ids == frozenset({"kb-a"})


def test_without_access_scope_keeps_audit_fields_and_denies_scopes() -> None:
    context = without_access_scope(
        AuditRequestContext(request_id="request-1", user_id_hash="b" * 64)
    )
    assert context.request_id == "request-1"
    assert context.user_id_hash == "b" * 64
    assert context.allowed_document_ids == frozenset()
    assert context.allowed_category_names == frozenset()
    assert context.allowed_knowledge_base_ids == frozenset()
    assert context.allowed_search_answer_profile_ids == frozenset()


def test_production_context_ignores_tenant_header() -> None:
    """production（利用者から範囲を決める経路）は X-Tenant-ID を使わない（#225）。"""
    from app.rag.request_context import audit_request_context_for_principal

    context = audit_request_context_for_principal(
        {"x-tenant-id": "other-tenant", "x-user-id": "spoofed"},
        request_id="req-1",
        user_uuid="user-1",
        allowed_search_answer_profile_ids=None,
        allowed_knowledge_base_ids=None,
    )
    assert context.tenant_id_hash is None
    local = audit_request_context_from_headers({"x-tenant-id": "other-tenant"}, request_id="req-2")
    assert local.tenant_id_hash is not None
