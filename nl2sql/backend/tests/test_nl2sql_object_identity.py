"""DB 管理画面で使う Oracle object identity のテスト。"""

from __future__ import annotations

import pytest

from app.features.nl2sql.object_identity import parse_object_identity, qualified_object_name


def test_parse_object_identity_preserves_quoted_identifier_parts() -> None:
    plain = parse_object_identity("app.orders")
    assert plain.owner == "APP"
    assert plain.object_name == "ORDERS"
    assert plain.qualified_name == "APP.ORDERS"
    assert plain.quoted_name == '"APP"."ORDERS"'

    lower = parse_object_identity('APP."lower"')
    assert lower.owner == "APP"
    assert lower.object_name == "lower"
    assert lower.qualified_name == 'APP."lower"'
    assert lower.quoted_name == '"APP"."lower"'

    japanese = parse_object_identity('"Mixed Owner"."売上.2026"')
    assert japanese.owner == "Mixed Owner"
    assert japanese.object_name == "売上.2026"
    assert japanese.qualified_name == '"Mixed Owner"."売上.2026"'

    escaped = parse_object_identity('APP."A""B"')
    assert escaped.object_name == 'A"B'
    assert escaped.qualified_name == 'APP."A""B"'


def test_parse_object_identity_rejects_malformed_quotes() -> None:
    with pytest.raises(ValueError, match="Oracle 識別子"):
        parse_object_identity('APP."BROKEN')

    with pytest.raises(ValueError, match="Oracle 識別子"):
        parse_object_identity('ADMIN"."SECRET')


def test_qualified_object_name_uppercases_simple_identifiers_only() -> None:
    assert parse_object_identity("app.orders").qualified_name == "APP.ORDERS"
    assert qualified_object_name("APP", "lower") == 'APP."lower"'
    assert qualified_object_name("APP", "売上") == 'APP."売上"'
