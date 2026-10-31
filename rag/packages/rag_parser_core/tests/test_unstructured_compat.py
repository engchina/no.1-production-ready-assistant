"""pi-heif を入れない環境の unstructured の互換(#3351)。"""

from __future__ import annotations

import importlib.util
import sys
import types

import pytest

from rag_parser_core import unstructured_compat
from rag_parser_core.source import is_plain_text_markup


def test_stub_is_installed_when_pi_heif_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """pi-heif が無ければ、何もしない register_heif_opener を持つ stub を置く。"""
    monkeypatch.setitem(sys.modules, "pi_heif", types.ModuleType("placeholder"))
    monkeypatch.delitem(sys.modules, "pi_heif")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *args: None)

    assert unstructured_compat.ensure_pi_heif_absent_stub() is True
    stub = sys.modules["pi_heif"]
    assert stub.register_heif_opener() is None  # type: ignore[attr-defined]
    # 2 回目は置き直さない。
    assert unstructured_compat.ensure_pi_heif_absent_stub() is False
    assert sys.modules["pi_heif"] is stub
    # unstructured の partition/pdf.py と同じ import が通る。
    namespace: dict[str, object] = {}
    exec("from pi_heif import register_heif_opener", namespace)
    assert callable(namespace["register_heif_opener"])


def test_stub_does_not_shadow_installed_pi_heif(monkeypatch: pytest.MonkeyPatch) -> None:
    """pi-heif が入っていれば(find_spec が見つける)stub を置かない。"""
    monkeypatch.setitem(sys.modules, "pi_heif", types.ModuleType("placeholder"))
    monkeypatch.delitem(sys.modules, "pi_heif")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *args: object())

    assert unstructured_compat.ensure_pi_heif_absent_stub() is False
    assert "pi_heif" not in sys.modules


@pytest.mark.parametrize(
    ("extension", "content_type", "expected"),
    [
        (".rst", "application/octet-stream", True),
        (".ORG", "", True),
        (None, "text/x-rst; charset=utf-8", True),
        (None, "text/org", True),
        (".txt", "text/plain", False),
        (".md", "text/markdown", False),
    ],
    ids=["rst", "org-upper", "rst-mime", "org-mime", "txt", "md"],
)
def test_plain_text_markup(extension: str | None, content_type: str, expected: bool) -> None:
    assert is_plain_text_markup(extension, content_type) is expected
