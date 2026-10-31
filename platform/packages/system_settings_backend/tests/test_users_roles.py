"""ユーザー管理・ロール管理の API 契約（#206）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pr_system_settings.users_roles import (
    AssignedRoleData,
    PasswordPolicyError,
    RoleCreateRequest,
    RoleUpdateRequest,
    UserCreateRequest,
    UserUpdateRequest,
    generate_temporary_password,
    validate_password,
)


def test_login_user_id_is_trimmed_and_validated() -> None:
    created = UserCreateRequest(login_user_id=" a.b-c_1 ", display_name="x", role_ids=["r"])
    assert created.login_user_id == "a.b-c_1"
    with pytest.raises(ValidationError):
        UserCreateRequest(login_user_id="._-", display_name="x", role_ids=["r"])
    with pytest.raises(ValidationError):
        UserCreateRequest(login_user_id="has space", display_name="x", role_ids=["r"])


def test_user_status_is_normalized() -> None:
    updated = UserUpdateRequest(version=1, display_name="x", status=" disabled ", role_ids=["r"])
    assert updated.status == "DISABLED"
    with pytest.raises(ValidationError):
        UserUpdateRequest(version=1, display_name="x", status="LOCKED", role_ids=["r"])


@pytest.mark.parametrize("role_ids", [None, [], ["", "  "]], ids=["missing", "empty", "blank"])
def test_user_requests_require_a_role(role_ids: list[str] | None) -> None:
    """ユーザーのロールは画面と同じく必須（#540）。省略・空・空白だけは 422 の日本語の文言。"""
    extra = {} if role_ids is None else {"role_ids": role_ids}
    for model, payload in (
        (UserCreateRequest, {"login_user_id": "alice", "display_name": "x"}),
        (UserUpdateRequest, {"version": 1, "display_name": "x", "status": "ACTIVE"}),
    ):
        with pytest.raises(ValidationError) as exc:
            model.model_validate({**payload, **extra})
        errors = exc.value.errors()
        assert [error["loc"] for error in errors] == [("role_ids",)]
        assert "ロールを選択してください。" in errors[0]["msg"]
    assert UserCreateRequest(
        login_user_id="alice", display_name="x", role_ids=[" r1 ", ""]
    ).role_ids == ["r1"]


def test_role_code_is_uppercased_and_role_requests_carry_no_permissions() -> None:
    created = RoleCreateRequest(role_code=" sales_viewer ", display_name="営業閲覧")
    assert created.role_code == "SALES_VIEWER"
    with pytest.raises(ValidationError):
        RoleCreateRequest(role_code="1ROLE", display_name="x")


@pytest.mark.parametrize(
    ("role_code", "message"),
    [
        ("", "ロールコードを入力してください。"),
        ("  ", "ロールコードを入力してください。"),
        ("a", "ロールコードは 2 文字以上で入力してください。"),
        (" a ", "ロールコードは 2 文字以上で入力してください。"),
        ("1ROLE", "ロールコードは英大文字で始め"),
        ("A" * 65, "String should have at most 64 characters"),
    ],
    ids=["empty", "blank", "one-char", "one-char-padded", "digit-first", "too-long"],
)
def test_role_code_errors_match_the_screen(role_code: str, message: str) -> None:
    """ロールコードの文言は画面の検証（system-settings の validation.ts）と同じ（#540）。"""
    with pytest.raises(ValidationError) as exc:
        RoleCreateRequest(role_code=role_code, display_name="x")
    assert message in exc.value.errors()[0]["msg"]
    # 権限は製品の権限管理 API で扱う。旧 client が送っても共通契約は受け取らない。
    assert "permissions" not in RoleCreateRequest.model_fields
    assert "permissions" not in RoleUpdateRequest.model_fields


def test_unresolved_assigned_role_is_archived_without_exposing_its_id() -> None:
    """参照できないロールは削除済みとして返し、コード・表示名に ID を入れない（#1682）。"""
    role_id = "00000000-0000-0000-0000-00000000dead"
    role = AssignedRoleData.unresolved(role_id)
    assert (role.role_code, role.display_name, role.archived, role.deleted) == ("", "", True, True)
    assert role.role_id == role_id
    assert role_id not in (role.role_code, role.display_name)


def test_password_policy_reports_every_violation() -> None:
    validate_password("Str0ng!Passw0rd", login_user_id="alice", min_length=12, max_length=128)
    with pytest.raises(PasswordPolicyError) as exc:
        validate_password("short", login_user_id="alice", min_length=12, max_length=128)
    message = str(exc.value)
    assert "12～128" in message and "英大文字" in message and "数字" in message
    with pytest.raises(PasswordPolicyError, match="推測されやすい"):
        validate_password("Alice!Pass1234", login_user_id="alice", min_length=12, max_length=128)


def test_temporary_password_satisfies_policy() -> None:
    for _ in range(20):
        password = generate_temporary_password()
        assert len(password) == 20
        validate_password(password, login_user_id="alice", min_length=12, max_length=128)
