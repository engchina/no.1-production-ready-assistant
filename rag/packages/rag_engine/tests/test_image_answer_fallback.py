"""説明生成OFFの画像保存・Embeddingと、文字不足後の画像補完を検証する。"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from rag_engine.config import get_settings
from rag_engine.generation.answering import AnswerContext, AnswerRecord, AnswerResponse


def response(*, missing=True, addressed=(), used_image=False):
    return AnswerResponse(
        answer_text="図で確認できる内容" if not missing else "本文で支持された部分回答",
        confidence="low" if missing else "high",
        insufficient_reason="詳細は本文にない" if missing else "",
        cited_evidence=({"evidence_id": "picture-1", "source": "guide.pdf", "page": "1"},) if used_image else (),
        generation_trace={
            "reviews": [
                {
                    "status": "completed",
                    "complete": not missing,
                    "reviews": [],
                    "request_reviews": [{"request_id": q, "status": "addressed"} for q in addressed],
                }
            ]
        },
    )


def setup_images(tmp_path, *, exists=True, provider="enterprise-ai-vision"):
    settings = replace(get_settings(environ={}, dotenv_path=None), output_dir=tmp_path, default_answer_llm=provider)
    crop = tmp_path / "abcdef" / "docling" / "visuals" / "picture-1.png"
    if exists:
        crop.parent.mkdir(parents=True)
        Image.new("RGB", (64, 64), "white").save(crop)
    record = AnswerRecord(
        id="c1",
        chunk_id="c1",
        source="guide.pdf",
        source_run_id="abcdef",
        engine="docling",
        engine_label="Docling",
        page="1",
        seq_no=1,
        category="Picture",
        text="Image evidence: 詳細は原図を参照",
        metadata={
            "image_evidence": [
                {"image_id": "picture-1", "source_run_id": "abcdef", "page": 1, "crop_path": "docling/visuals/picture-1.png"}
            ]
        },
    )
    return settings, AnswerContext(records=[record], text=record.text), crop


def test_vision_off_keeps_image_through_parsing_chunking_and_embedding(tmp_path):
    from test_adb_vector_store import _FakeConnection

    from rag_engine.adapters.oracle.store import EMBEDDING_MODALITY_IMAGE, save_chunk_run_embeddings
    from rag_engine.chunking import create_chunk_run, load_chunk_run_by_id
    from rag_engine.models.layout import LayoutRecord, PageImage
    from rag_engine.parsing.analysis import analyze_pdf

    source = tmp_path / "guide.pdf"
    source.write_bytes(b"synthetic pdf")
    page_path = tmp_path / "page.png"
    Image.new("RGB", (200, 200), "white").save(page_path)
    page = PageImage(1, 200, 200, 200, 200, str(page_path))
    picture = LayoutRecord(
        "picture-1", "docling", 1, 1, [10, 10, 180, 180], "image_top_left", 200, 200, "Picture", "", raw_type="picture", raw={}
    )
    adapter = SimpleNamespace(availability=lambda: SimpleNamespace(available=True, message=""), analyze=lambda context: [picture])
    settings = replace(
        get_settings(environ={}, dotenv_path=None),
        output_dir=tmp_path / "runs",
        image_embedding_enabled=True,
        embedding_output_dimensions=1536,
    )
    with (
        patch("rag_engine.parsing.analysis.build_adapters", return_value={"docling": adapter}),
        patch("rag_engine.parsing.analysis.get_source_page_count", return_value=1),
        patch("rag_engine.parsing.analysis.prepare_source_for_analysis", return_value=(str(source), [page])),
        patch("rag_engine.parsing.picture_descriptions.describe_docling_pictures") as describe,
    ):
        parsed = analyze_pdf(source, "all", ["docling"], settings, use_docling_vision=False, parse_entire_file=True)
    describe.assert_not_called()
    assert parsed.records[0].text == ""
    crop = Path(parsed.output_dir) / parsed.records[0].raw["crop_path"]
    assert crop.is_file() and not parsed.statuses[0].use_docling_vision
    chunks = create_chunk_run(output_dir=settings.output_dir, run_id=parsed.run_id, preferred_engine_ids=["docling"])
    restored = load_chunk_run_by_id(settings.output_dir, chunks.chunk_run_id)
    child = next(c for c in restored.chunks if c.chunk_level == "child")
    assert child.metadata["schema_version"] == 4
    assert child.metadata["image_evidence"][0]["crop_path"] == parsed.records[0].raw["crop_path"]
    connection = _FakeConnection()
    image_embedder = Mock(return_value=[[2.0] * 1536])
    saved = save_chunk_run_embeddings(
        restored,
        settings,
        connection=connection,
        embedder=lambda texts, *args, **kwargs: [[1.0] * 1536 for _ in texts],
        image_embedder=image_embedder,
    )
    assert saved.ok and saved.image_created_count == 1
    assert image_embedder.call_args.args[0] == [crop]
    assert any(r.get("embedding_modality") == EMBEDDING_MODALITY_IMAGE for r in connection.embedding_rows)
