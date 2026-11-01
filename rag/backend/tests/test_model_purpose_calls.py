"""実際の画像読取・条件認識・再帰要約の呼出点で固定用途を引き継ぐ。"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pr_backend_core.observability.model_purpose import (
    model_call_purpose,
    model_purpose_attributes,
)
from rag_engine.adapters import model_api as engine_api

from app.config import Settings
from app.rag.chunking import Chunk
from app.rag.extraction_field_adapter import FieldDefinition
from app.rag.field_filter_reader import read_field_conditions
from app.rag.ingestion import IngestionPipeline


@pytest.mark.parametrize("multimodal", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_picture_reading_sets_purpose_for_both_image_paths_and_restores_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multimodal: bool, failed: bool
) -> None:
    target, context = tmp_path / "target.png", tmp_path / "context.png"
    target.write_bytes(b"synthetic")
    context.write_bytes(b"synthetic")
    provider = object()
    settings = SimpleNamespace(default_vision_llm="model-a")
    result = {"retrieval_text": "合成の画像の説明"}
    failure = RuntimeError("合成の失敗")
    calls: list[tuple[object, ...]] = []

    def parse(
        system: str, prompt: str, images: object, active: object, schema: object, selected: object
    ) -> Any:
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": "document.read"}
        calls.append((system, prompt, images, active, schema, selected))
        if failed:
            raise failure
        return SimpleNamespace(model_dump=lambda **kwargs: result)

    monkeypatch.setattr(engine_api, "_validated_llm_provider", lambda *args, **kwargs: provider)
    monkeypatch.setattr(engine_api, "_parse_image_with_retry", parse)
    monkeypatch.setattr(engine_api, "_parse_multimodal_with_retry", parse)
    with model_call_purpose("answer.audit"):
        kwargs: dict[str, Any] = {"context_image_paths": [context]} if multimodal else {}
        if failed:
            with pytest.raises(RuntimeError) as raised:
                engine_api.describe_picture(
                    target, {}, cast(Any, settings), rendered_prompt="固定の合成指示", **kwargs
                )
            assert raised.value is failure
        else:
            assert (
                engine_api.describe_picture(
                    target, {}, cast(Any, settings), rendered_prompt="固定の合成指示", **kwargs
                )
                is result
            )
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": "answer.audit"}
    assert len(calls) == 1
    assert calls[0][1] == "固定の合成指示" and calls[0][3] is settings and calls[0][5] is provider
    assert calls[0][2] == ([target, context] if multimodal else target)
    assert model_purpose_attributes() == {}


async def test_metadata_condition_reader_records_purpose_at_actual_generate() -> None:
    observed: list[dict[str, str]] = []

    class Llm:
        async def generate(self, question: str, context: str, *, system_prompt: str) -> str:
            observed.append(model_purpose_attributes())
            assert question == "合成の条件を確認する" and "項目 A" in context
            assert system_prompt
            return "[]"

    assert (
        await read_field_conditions(
            "合成の条件を確認する",
            [FieldDefinition(name="項目 A", value_type="string")],
            cast(Any, Llm()),
        )
        == []
    )
    assert observed == [{"pr.gen_ai.purpose": "query.conditions"}]
    assert model_purpose_attributes() == {}


async def test_raptor_records_summary_purpose_for_each_actual_cluster_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, str]] = []

    class Llm:
        async def generate(self, prompt: str, source: str, *, system_prompt: str) -> str:
            observed.append(model_purpose_attributes())
            assert prompt == "" and source and system_prompt
            return "合成の要約"

    async def stage(self: object, key: str, fallback: str) -> str:
        return fallback

    monkeypatch.setattr(IngestionPipeline, "_stage_prompt", stage)
    pipeline = IngestionPipeline(
        vlm=cast(Any, Llm()),
        genai=cast(Any, object()),
        oracle=cast(Any, object()),
        object_storage=cast(Any, object()),
        settings=Settings(
            rag_raptor_enabled=True, rag_raptor_cluster_size=2, rag_raptor_max_levels=1
        ),
    )
    leaves = [
        Chunk(text=f"合成の本文 {i}", index=i, start_offset=0, end_offset=10) for i in range(4)
    ]
    result = await pipeline._augment_with_raptor("trace-a", leaves, None)
    assert len(result) == 6
    assert observed == [{"pr.gen_ai.purpose": "document.summarize"}] * 2
    assert model_purpose_attributes() == {}
