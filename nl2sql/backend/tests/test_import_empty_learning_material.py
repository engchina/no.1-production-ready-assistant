"""見出しだけの取込で登録内容を空に戻せる。"""

import importlib
import io

import pytest

from app.features.nl2sql.models import Nl2SqlProfile
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore


def workbook(header: str, row: list[str] | None = None) -> bytes:
    book = importlib.import_module("openpyxl").Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(header.split(","))
    if row is not None:
        sheet.append(row)
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


@pytest.mark.parametrize("kind,header", [("terms", "TERM,DEFINITION"), ("rules", "RULE")])
def test_header_only_import_clears_saved_material(kind: str, header: str) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    importer = getattr(service, f"import_legacy_{kind}")
    row = ["label", "meaning"] if kind == "terms" else ["合成の生成規則"]
    importer(filename="synthetic.xlsx", content=workbook(header, row))
    importer(filename="empty.xlsx", content=workbook(header))
    material = service.get_legacy_learning_material()
    assert not (material.glossary if kind == "terms" else material.rules)


@pytest.mark.parametrize("kind", ["terms", "rules"])
def test_missing_header_is_rejected(kind: str) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    with pytest.raises(ValueError):
        getattr(service, f"import_legacy_{kind}")(filename="bad.xlsx", content=workbook("OTHER"))


def test_select_ai_uses_current_rules_without_profile_resync() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    profile = Nl2SqlProfile(id="synthetic", name="プロファイル A")
    service.import_legacy_rules(filename="rules.xlsx", content=workbook("RULE", ["合成の規則"]))
    attributes = service._select_ai_generate_attributes(profile, None)
    assert attributes is not None
    assert "合成の規則" in attributes["additional_instructions"]
    service.import_legacy_rules(filename="empty.xlsx", content=workbook("RULE"))
    attributes = service._select_ai_generate_attributes(profile, None)
    assert attributes is not None
    assert attributes["additional_instructions"] == ""
