"""外部 taxonomy を読まず、保存済み分類と汎用のファイル名処理を保つ。"""

import json

from rag_engine.knowledge.classification import (
    classification_filter_from_values,
    classification_from_metadata,
    classification_from_selection,
    classification_options_from_share,
    infer_classification_from_path,
    normalize_classification_payload,
    small_categories_from_saved_metadata,
)


def test_path_only_infers_topic_not_external_parent_categories():
    value = infer_classification_from_path("share/10_分類A/20_分類B/Ver3.0/6_項目A_41.pdf")
    assert value.large_category == value.middle_category == ""
    assert value.small_category == "項目A"
    assert value.source == "path_default"


def test_manual_and_stored_classifications_keep_names_without_taxonomy_aliases():
    value = classification_from_selection(
        source_path="share/10_分類A/項目A.pdf", large_category="分類A", middle_category="分類B", small_category="分類C"
    )
    assert value.source == "manual"
    restored = classification_from_metadata(value.to_metadata())
    assert restored == value
    assert restored.large_category == "分類A"
    assert restored.middle_category == "分類B"
    filtered = classification_filter_from_values(large_category="分類A", middle_category="分類B")
    assert filtered.to_metadata() == {"large_category": "分類A", "middle_category": "分類B"}


def test_saved_numbered_names_are_not_rewritten():
    value = classification_from_metadata({"large_category": "10_分類A", "middle_category": "20_分類B", "small_category": "未分類"})
    assert value.large_category == "10_分類A"
    assert value.middle_category == "20_分類B"
    assert value.small_category == ""


def test_optional_blank_and_unknown_values():
    assert not classification_filter_from_values(large_category="未分類").active
    value = infer_classification_from_path("/var/manual.pdf", source_root="/var/share")
    assert value.large_category == value.middle_category == ""
    assert value.small_category == "manual"


def test_nested_normalization_keeps_saved_categories_and_is_idempotent():
    payload = {
        "classification": {"large_category": "分類A", "middle_category": "分類B", "small_category": "未分類"},
        "chunks": [{"metadata": {"classification": {"large_category": "10_分類C", "middle_category": "20_分類D"}}}],
        "classification_filter": {"large_category": "分類E", "middle_category": "分類F"},
    }
    assert normalize_classification_payload(payload) == 2
    assert payload["classification"]["large_category"] == "分類A"
    assert payload["classification"]["middle_category"] == "分類B"
    assert payload["chunks"][0]["metadata"]["classification"]["large_category"] == "10_分類C"
    assert payload["classification_filter"]["middle_category"] == "分類F"
    assert normalize_classification_payload(payload) == 0


def test_share_options_only_collect_topics(tmp_path):
    root = tmp_path / "share"
    target = root / "10_分類A" / "20_分類B"
    target.mkdir(parents=True)
    (target / "（２）項目A.pdf").write_bytes(b"%PDF")
    value = classification_options_from_share(root)
    assert value.large_categories == value.middle_categories == ()
    assert value.middle_categories_by_large == {}
    assert value.small_categories == ("項目A",)


def test_saved_metadata_topics_are_collected(tmp_path):
    root = tmp_path / "runs"
    target = root / "abcdef"
    target.mkdir(parents=True)
    (target / "viewer-data.json").write_text(
        json.dumps({"classification": {"large_category": "分類A", "small_category": "項目A"}}), encoding="utf-8"
    )
    assert small_categories_from_saved_metadata(root) == ("項目A",)
