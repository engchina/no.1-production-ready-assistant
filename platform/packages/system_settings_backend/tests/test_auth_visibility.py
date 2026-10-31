"""「AI 活用」の下の機能のデータの見え方の共通の判定（#1889）。"""

from __future__ import annotations

from pr_system_settings.auth.domain import (
    CONFIGURED_SYSTEM_ADMIN_USER_UUID,
    LOCAL_DEBUG_USER_UUID,
    SYSTEM_ADMIN_ROLE_CODE,
    Principal,
)
from pr_system_settings.auth.visibility import (
    OTHERS_CONTENT_FORBIDDEN_STATUS,
    OWNER_NOT_FOUND_STATUS,
    RUN_EXECUTOR_FILTERS,
    can_access_run,
    can_access_user_data,
    can_add_user_content,
    can_view_all_users_data,
    owner_filter_user_uuid,
    run_executor_matches,
)


def _principal(user_uuid: str, *role_codes: str, permissions: set[str] | None = None) -> Principal:
    return Principal(
        user_uuid=user_uuid,
        login_user_id=user_uuid,
        display_name=user_uuid,
        status="ACTIVE",
        force_password_change=False,
        role_codes=list(role_codes),
        permissions=permissions or set(),
        session_id="s",
        csrf_token_hash="",  # nosec B106 - テスト用
    )


ADMIN = _principal(CONFIGURED_SYSTEM_ADMIN_USER_UUID, SYSTEM_ADMIN_ROLE_CODE)
LOCAL = _principal(LOCAL_DEBUG_USER_UUID, SYSTEM_ADMIN_ROLE_CODE)
ALICE = _principal("alice", "OPERATOR", permissions={"agent.admin", "menu.runs"})
BOB = _principal("bob", "OPERATOR")


def test_system_admin_sees_everyone() -> None:
    for principal in (ADMIN, LOCAL):
        assert can_view_all_users_data(principal)
        assert owner_filter_user_uuid(principal) is None
        assert can_access_user_data(principal, "alice")
        assert can_access_user_data(principal, None)


def test_other_users_see_only_their_own_data() -> None:
    # 製品の管理権限（agent.admin など）は全員を見る理由にしない。
    assert not can_view_all_users_data(ALICE)
    assert owner_filter_user_uuid(ALICE) == "alice"
    assert owner_filter_user_uuid(BOB) == "bob"
    assert can_access_user_data(ALICE, "alice")
    assert not can_access_user_data(ALICE, "bob")
    assert not can_access_user_data(BOB, "alice")


def test_unknown_owner_and_missing_principal_are_denied() -> None:
    assert not can_view_all_users_data(None)
    assert not can_access_user_data(None, "alice")
    assert not can_access_user_data(ALICE, None)
    assert not can_access_user_data(ALICE, "  ")
    assert OWNER_NOT_FOUND_STATUS == 404


def test_run_is_visible_to_executor_owner_and_system_admin() -> None:
    # system_admin が alice の会話で実行した物は alice（持ち主）にも見え、bob は見えない（#1954）。
    run = {"executor_user_uuid": CONFIGURED_SYSTEM_ADMIN_USER_UUID, "owner_user_uuid": "alice"}
    assert can_access_run(ADMIN, **run)
    assert can_access_run(LOCAL, **run)
    assert can_access_run(ALICE, **run)
    assert not can_access_run(BOB, **run)
    assert not can_access_run(None, **run)
    # 会話の無い実行は実行した人だけ。
    assert can_access_run(BOB, executor_user_uuid="bob", owner_user_uuid=None)
    assert not can_access_run(ALICE, executor_user_uuid="bob", owner_user_uuid=None)
    assert not can_access_run(ALICE, executor_user_uuid=None, owner_user_uuid="  ")


def test_run_executor_filter() -> None:
    assert RUN_EXECUTOR_FILTERS == ("all", "mine", "others")
    assert run_executor_matches(ALICE, "alice", "all")
    assert run_executor_matches(ALICE, "admin", "all")
    assert run_executor_matches(ALICE, "alice", "mine")
    assert not run_executor_matches(ALICE, "admin", "mine")
    assert run_executor_matches(ALICE, "admin", "others")
    assert run_executor_matches(ALICE, None, "others")
    assert not run_executor_matches(ALICE, "alice", "others")


def test_only_the_owner_adds_content_even_for_system_admin() -> None:
    # system_admin は他の利用者のデータを見て運用の操作はできるが、新しい実行・内容は
    # 足せない（#3244）。
    for principal in (ADMIN, LOCAL):
        assert can_access_user_data(principal, "alice")
        assert not can_add_user_content(principal, "alice")
        assert can_add_user_content(principal, principal.user_uuid)
    assert can_add_user_content(ALICE, "alice")
    assert not can_add_user_content(ALICE, "bob")
    # 持ち主の分からないデータ・利用者の無い要求には誰も足せない。
    assert not can_add_user_content(ADMIN, None)
    assert not can_add_user_content(ADMIN, " ")
    assert not can_add_user_content(None, "alice")
    assert OTHERS_CONTENT_FORBIDDEN_STATUS == 403
