"""引用キーだけを移行し、本文・引用の順・画像メタデータを変えない。"""

import json
from copy import deepcopy

import pytest

from rag_engine.knowledge.answer_migration import main, migrate_answer_payload


def legacy_payload():
    return {
        "answer_text": "合成の回答",
        "used_image_ids": ["child-text", "picture-1"],
        "used_images": [
            {"image_id": "child-text", "answer_use": "confirmed", "page": "1"},
            {"image_id": "picture-1", "answer_use": "reference", "page": "2"},
        ],
        "evidence_items": [
            {
                "id": "parent-1",
                "child_ids": ["child-text"],
                "metadata": {"image_evidence": [{"image_id": "picture-1"}], "used_images": "元の文書の項目"},
            }
        ],
    }


def test_payload_migration_preserves_text_images_children_order_and_usage():
    before = legacy_payload()
    snapshot = deepcopy(before)
    result = migrate_answer_payload(before)
    assert before == snapshot
    assert result["cited_evidence_ids"] == ["child-text", "picture-1"]
    assert result["cited_evidence"] == [
        {"evidence_id": "child-text", "answer_use": "confirmed", "page": "1"},
        {"evidence_id": "picture-1", "answer_use": "reference", "page": "2"},
    ]
    assert result["evidence_items"] == before["evidence_items"]
    assert result["answer_text"] == before["answer_text"]
    assert "used_images" not in result and "used_image_ids" not in result
    assert migrate_answer_payload(result) == result


def test_payload_migration_rejects_collisions_without_modifying_input():
    before = {**legacy_payload(), "cited_evidence": []}
    snapshot = deepcopy(before)
    with pytest.raises(ValueError, match="新旧キー"):
        migrate_answer_payload(before)
    assert before == snapshot


def test_artifact_migration_is_dry_run_until_apply_and_is_idempotent(tmp_path, capsys):
    path = tmp_path / "synthetic-answer.json"
    before = json.dumps(legacy_payload(), ensure_ascii=False)
    path.write_text(before, encoding="utf-8")
    assert main([str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == 1
    assert path.read_text(encoding="utf-8") == before
    assert main([str(path), "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == 1
    assert json.loads(path.read_text(encoding="utf-8")) == migrate_answer_payload(legacy_payload())
    assert main([str(path), "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == 0
