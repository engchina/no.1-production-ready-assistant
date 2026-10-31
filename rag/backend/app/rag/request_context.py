"""監査ログ用のリクエストコンテキスト。"""

import hashlib
import logging
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from functools import wraps

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

TENANT_ID_HEADER = "x-tenant-id"
USER_ID_HEADER = "x-user-id"
ROLE_ID_HEADER = "x-rag-role-id"
AGENT_ID_HEADER = "x-rag-agent-id"
THREAD_ID_HEADER = "x-rag-thread-id"
ALLOWED_DOCUMENT_IDS_HEADER = "x-rag-allowed-document-ids"
ALLOWED_CATEGORY_NAMES_HEADER = "x-rag-allowed-category-names"
ALLOWED_KNOWLEDGE_BASE_IDS_HEADER = "x-rag-allowed-knowledge-base-ids"
MAX_CONTEXT_VALUE_CHARS = 256
MAX_ACCESS_SCOPE_VALUES = 200
DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class AuditRequestContext:
    """監査ログに付与する非機密リクエスト情報。"""

    request_id: str | None = None
    tenant_id_hash: str | None = None
    user_id_hash: str | None = None
    role_id_hash: str | None = None
    agent_id_hash: str | None = None
    thread_id_hash: str | None = None
    allowed_document_ids: frozenset[str] | None = field(default=None, repr=False)
    allowed_category_names: frozenset[str] | None = field(default=None, repr=False)
    allowed_knowledge_base_ids: frozenset[str] | None = field(default=None, repr=False)
    # 利用できる検索・回答プロファイル（None は制限なし）。pr
    # oduction では認証済みの利用者から決める（#214）。
    #
    allowed_search_answer_profile_ids: frozenset[str] | None = field(default=None, repr=False)
    # 保存済みの回答（`rag_answer_records`）を持ち主にかかわらず扱えるか（#304）。False のときは
    # `user_id_hash` の利用者の回答だけ。SYSTEM_ADMIN と `rag.feedback.manage` を持つ利用者は True。
    answer_records_unrestricted: bool = False
    # 利用者フィードバックを、送った利用者にかかわらず見られるか（#408。NL2SQL の実行履歴と同じ）。
    # SYSTEM_ADMIN（構成管理者・local の利用者を含む）だけ True。False のときは `user_id_hash` の
    # 利用者が送ったものだけ（`rag.feedback.manage` を持っていても同じ）。
    feedback_all_users: bool = False
    # 利用者の UUID（監査ログには書かない）。実行履歴（#1890）が実行した利用者として保存し、
    # system_admin の一覧で表示名を引く。local では `X-User-ID`（無ければローカル利用者）。
    user_uuid: str | None = field(default=None, repr=False)
    # 「AI 活用」の機能のデータを全員の分まで見られるか（#1889 の規則。system_admin だけ True。
    # 判定は `pr_system_settings.auth.visibility.can_view_all_users_data`）。
    # 実行履歴（#1890）が使う。False のときは `user_id_hash` の利用者の分だけ。
    all_users_visible: bool = False


_AUDIT_REQUEST_CONTEXT: ContextVar[AuditRequestContext | None] = ContextVar(
    "audit_request_context",
    default=None,
)

# 文脈を作らずに動いた処理（文脈を引き継がない thread・起動時の task など）の context。範囲は
# すべて空の集合（拒否全件）で、利用者も分からない（fail-closed。#3250）。範囲の制限の無い
# context は、利用者の要求の外の信頼した処理だけが `system_access_context` で明示して作る。
NO_ACCESS_AUDIT_REQUEST_CONTEXT = AuditRequestContext(
    allowed_document_ids=frozenset(),
    allowed_category_names=frozenset(),
    allowed_knowledge_base_ids=frozenset(),
    allowed_search_answer_profile_ids=frozenset(),
)


def without_access_scope(context: AuditRequestContext) -> AuditRequestContext:
    """`context` の対象範囲を、すべて空の集合（拒否全件）にする（利用者の分からない要求）。"""
    return replace(
        context,
        allowed_document_ids=frozenset(),
        allowed_category_names=frozenset(),
        allowed_knowledge_base_ids=frozenset(),
        allowed_search_answer_profile_ids=frozenset(),
    )


def audit_request_context_from_headers(
    headers: Mapping[str, str],
    *,
    request_id: str,
    settings: Settings | None = None,
    authenticated_user_id: str | None = None,
    default_user_id: str | None = None,
    allow_user_header: bool = True,
    trust_scope_headers: bool = True,
) -> AuditRequestContext:
    """HTTP header から監査用 context を作る。raw id は保存しない。

    `trust_scope_headers=False`（production）では、client が指定する対象範囲の header
    （`x-rag-allowed-*`）を使わない。対象範囲は認証済みの利用者から決める（#214）。
    `X-Tenant-ID` も使わない（tenant なし＝単一 tenant で動かす。client が tenant を
    切り替えて別の tenant のデータを指定できないようにする。#225）。
    """
    resolved_settings = settings or get_settings()
    header_user_id = headers.get(USER_ID_HEADER) if allow_user_header else None
    user_id = authenticated_user_id or header_user_id or default_user_id
    if not trust_scope_headers:
        return AuditRequestContext(
            request_id=request_id,
            # ponytail: 単一 tenant 前提。複数 tenant が必要になったら、
            # tenant を利用者 / ロールに割り当てて principal から決める。
            tenant_id_hash=None,
            user_id_hash=_header_hash(user_id, resolved_settings),
            role_id_hash=_header_hash(headers.get(ROLE_ID_HEADER), resolved_settings),
            agent_id_hash=_header_hash(headers.get(AGENT_ID_HEADER), resolved_settings),
            thread_id_hash=_header_hash(headers.get(THREAD_ID_HEADER), resolved_settings),
        )
    return AuditRequestContext(
        request_id=request_id,
        tenant_id_hash=_header_hash(headers.get(TENANT_ID_HEADER), resolved_settings),
        user_id_hash=_header_hash(user_id, resolved_settings),
        role_id_hash=_header_hash(headers.get(ROLE_ID_HEADER), resolved_settings),
        agent_id_hash=_header_hash(headers.get(AGENT_ID_HEADER), resolved_settings),
        thread_id_hash=_header_hash(headers.get(THREAD_ID_HEADER), resolved_settings),
        allowed_document_ids=_access_scope_values(
            headers.get(ALLOWED_DOCUMENT_IDS_HEADER),
            normalizer=_normalize_document_id,
        ),
        allowed_category_names=_access_scope_values(
            headers.get(ALLOWED_CATEGORY_NAMES_HEADER),
            normalizer=_normalize_category_name,
        ),
        allowed_knowledge_base_ids=_access_scope_values(
            headers.get(ALLOWED_KNOWLEDGE_BASE_IDS_HEADER),
            normalizer=_normalize_knowledge_base_id,
        ),
    )


def audit_request_context_for_principal(
    headers: Mapping[str, str],
    *,
    request_id: str,
    user_uuid: str,
    allowed_search_answer_profile_ids: frozenset[str] | None,
    allowed_knowledge_base_ids: frozenset[str] | None,
    settings: Settings | None = None,
    service_token_claims: Mapping[str, object] | None = None,
    answer_records_unrestricted: bool = False,
    feedback_all_users: bool = False,
    all_users_visible: bool = False,
) -> AuditRequestContext:
    """認証済みの利用者から監査・対象範囲の context を作る（production。#214）。

    利用者（`user_id_hash`）と対象範囲は利用者から決め、client の `X-User-ID` と
    `x-rag-allowed-*` と `X-Tenant-ID` は使わない（#225）。memory の分割キー（role / agent /
    thread）は従来どおり header から読む（絞り込み方向にしか働かない）。

    サービストークン（MCP。#232）の呼び出しでは、agent / thread を header ではなく token の
    claims（`agent_id` / `run_id`）から決める（署名済みの値だけを使う）。
    """
    resolved_settings = settings or get_settings()
    base = audit_request_context_from_headers(
        headers,
        request_id=request_id,
        settings=resolved_settings,
        authenticated_user_id=user_uuid,
        allow_user_header=False,
        trust_scope_headers=False,
    )
    if service_token_claims is not None:
        base = replace(
            base,
            agent_id_hash=_claim_hash(service_token_claims, "agent_id", resolved_settings),
            thread_id_hash=_claim_hash(service_token_claims, "run_id", resolved_settings),
        )
    return replace(
        base,
        allowed_search_answer_profile_ids=allowed_search_answer_profile_ids,
        allowed_knowledge_base_ids=allowed_knowledge_base_ids,
        answer_records_unrestricted=answer_records_unrestricted,
        feedback_all_users=feedback_all_users,
        user_uuid=user_uuid,
        all_users_visible=all_users_visible,
    )


def _claim_hash(claims: Mapping[str, object], key: str, settings: Settings) -> str | None:
    value = claims.get(key)
    return _header_hash(value, settings) if isinstance(value, str) else None


@contextmanager
def unrestricted_access_scope() -> Iterator[None]:
    """検索・回答プロファイル・ナレッジベースの対象範囲を一時的に外す（tenant は保つ）。

    既定データ（DEFAULT）の存在確認や、権限管理の ID 検証など、利用者の範囲と無関係に
    tenant 内の全件を見る必要がある処理だけで使う。
    """
    current = current_audit_request_context()
    token = set_audit_request_context(
        replace(current, allowed_search_answer_profile_ids=None, allowed_knowledge_base_ids=None)
    )
    try:
        yield
    finally:
        reset_audit_request_context(token)


@contextmanager
def system_access_context() -> Iterator[None]:
    """利用者の要求の外の信頼した処理（取込の worker・CLI）を、範囲の制限なしで動かす（#3250）。

    文脈の無い処理は何も使えない（`NO_ACCESS_AUDIT_REQUEST_CONTEXT`）ので、tenant 内の全件を
    扱う process の入口（`main`・worker の loop）だけがこれで明示する。利用者は分からないので、
    利用者の物（会話・保存済みの回答・フィードバック・実行履歴）は見えない。
    """
    token = set_audit_request_context(AuditRequestContext())
    try:
        yield
    finally:
        reset_audit_request_context(token)


def with_system_access[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """CLI の `main` を `system_access_context` の中で動かす（`asyncio.run` は文脈を引き継ぐ）。"""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with system_access_context():
            return function(*args, **kwargs)

    return wrapped


def set_audit_request_context(
    context: AuditRequestContext,
) -> Token[AuditRequestContext | None]:
    """現在の async context へ監査 context を設定する。"""
    return _AUDIT_REQUEST_CONTEXT.set(context)


def reset_audit_request_context(token: Token[AuditRequestContext | None]) -> None:
    """監査 context を以前の状態へ戻す。"""
    _AUDIT_REQUEST_CONTEXT.reset(token)


def current_audit_request_context() -> AuditRequestContext:
    """現在の監査 context を返す。文脈が無ければ何も使えない context（fail-closed。#3250）。"""
    context = _AUDIT_REQUEST_CONTEXT.get()
    return NO_ACCESS_AUDIT_REQUEST_CONTEXT if context is None else context


def access_scope_context(reader: str) -> AuditRequestContext:
    """対象範囲で絞って DB を読む SQL の条件に使う context（`current_audit_request_context`）。

    文脈を作らずに範囲の条件に来た（要求の外の入口が `system_access_context` も利用者の context も
    作っていない）ときは、黙って 0 件にせず WARNING `no_access_context_read` を出す（`reader`
    ごとに process で 1 回。#3334）。要求は middleware が必ず context を作るので、ここに来ない。
    """
    context = _AUDIT_REQUEST_CONTEXT.get()
    if context is None:
        _warn_no_access_context_read(reader)
        return NO_ACCESS_AUDIT_REQUEST_CONTEXT
    return context


_NO_ACCESS_READ_WARNED: set[str] = set()
_NO_ACCESS_READ_WARNED_LOCK = threading.Lock()


def _warn_no_access_context_read(reader: str) -> None:
    with _NO_ACCESS_READ_WARNED_LOCK:
        if reader in _NO_ACCESS_READ_WARNED:
            return
        _NO_ACCESS_READ_WARNED.add(reader)
    logger.warning(
        "no_access_context_read",
        extra={
            "reader": reader,
            "advice": (
                "a scoped DB read ran without a request context and returns nothing; "
                "wrap the entry point in system_access_context / with_system_access"
            ),
        },
    )


def caller_owns(owner_user_id_hash: str | None) -> bool:
    """呼んだ利用者が、`owner_user_id_hash` の会話の持ち主か（#3244）。

    会話に新しい内容（続きの質問・添付・名前の変更・評価）を足せるのは持ち主だけで、platform の
    `can_add_user_content` と同じ規則にする: 持ち主の分からない会話（`user_id_hash` が NULL の
    古い行）には誰も足せない。利用者の分からない context（`user_id_hash` が None）は誰の持ち主
    でもない（fail-closed。会話の条件も会話を返さない。#3250）。
    """
    caller = current_audit_request_context().user_id_hash
    if caller is None:
        return False
    return bool(owner_user_id_hash) and owner_user_id_hash == caller


def user_id_hash_for(user_id: str, settings: Settings | None = None) -> str | None:
    """利用者の ID（UUID・local の利用者）から、保存に使う `user_id_hash` を作る。

    会話は利用者を hash でしか持たないので、system_admin の一覧の利用者の表示名は、利用者の UUID を
    同じ hash にして引き当てる（#1889）。
    """
    return _header_hash(user_id, settings or get_settings())


def _header_hash(value: str | None, settings: Settings) -> str | None:
    normalized = _normalize_context_value(value)
    if normalized is None:
        return None
    salt = settings.audit_context_hash_salt
    payload = f"{salt}\0{normalized}" if salt else normalized
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_context_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_CONTEXT_VALUE_CHARS:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        return None
    return normalized


def _access_scope_values(
    raw_header: str | None,
    *,
    normalizer: Callable[[str], str | None],
) -> frozenset[str] | None:
    if raw_header is None:
        return None
    values: list[str] = []
    for item in raw_header.split(","):
        normalized = normalizer(item)
        if normalized is None:
            continue
        values.append(normalized)
        if len(values) >= MAX_ACCESS_SCOPE_VALUES:
            break
    return frozenset(values)


def _normalize_document_id(value: str) -> str | None:
    normalized = _normalize_context_value(value)
    if normalized is None or not DOCUMENT_ID_PATTERN.fullmatch(normalized):
        return None
    return normalized


def _normalize_knowledge_base_id(value: str) -> str | None:
    normalized = _normalize_context_value(value)
    if normalized is None or not DOCUMENT_ID_PATTERN.fullmatch(normalized):
        return None
    return normalized


def _normalize_category_name(value: str) -> str | None:
    normalized = _normalize_context_value(value)
    if normalized is None:
        return None
    return normalized.casefold()
