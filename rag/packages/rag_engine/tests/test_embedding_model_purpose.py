"""SDK 直接経路も、宣言済み入力型と明示用途だけを記録する。"""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pr_backend_core.observability.model_purpose import model_call_purpose, model_purpose_attributes

from rag_engine.adapters import oci
from rag_engine.resources.tracing import bind_trace_observer


@pytest.mark.parametrize("image", [False, True])
@pytest.mark.parametrize("input_type,expected", [("QUERY", "query.embed"), ("SEARCH_DOCUMENT", "document.embed"), ("IMAGE", None)])
@pytest.mark.parametrize("probe", [False, True])
def test_sdk_embedding_records_declared_purpose_without_changing_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    image: bool,
    input_type: str,
    expected: str | None,
    probe: bool,
) -> None:
    class Details(SimpleNamespace):
        INPUT_TYPE_SEARCH_QUERY = "SEARCH_QUERY"
        INPUT_TYPE_SEARCH_DOCUMENT = "SEARCH_DOCUMENT"
        INPUT_TYPE_IMAGE = "IMAGE"
        EMBEDDING_TYPES_FLOAT = "FLOAT"
        TRUNCATE_NONE = "NONE"

    requests = []
    recorded = []
    snapshots = []

    def embed_text(*, embed_text_details, retry_strategy):
        requests.append(embed_text_details)
        return SimpleNamespace(data=object())

    @contextmanager
    def observer(operation, model):
        assert operation == "embeddings" and model == "model-a"
        recorded.append(model_purpose_attributes())
        yield SimpleNamespace(
            request=lambda value, **kwargs: snapshots.append(("request", value)),
            response=lambda value: snapshots.append(("response", value)),
        )

    runtime = SimpleNamespace(
        models=SimpleNamespace(
            EmbedTextDetails=Details, EmbedTextContent=SimpleNamespace, EmbedImageContent=SimpleNamespace, ImageUrl=SimpleNamespace
        ),
        compartment_id="synthetic",
        client=SimpleNamespace(embed_text=embed_text),
        oci=SimpleNamespace(retry=SimpleNamespace(NoneRetryStrategy=lambda: None)),
    )
    monkeypatch.setattr(oci, "_runtime", lambda *args: runtime)
    monkeypatch.setattr(oci, "_embedding_endpoint", lambda settings: "synthetic")
    monkeypatch.setattr(oci, "_embedding_output_dimensions", lambda settings: 3)
    monkeypatch.setattr(oci, "_serving_mode", lambda *args: "synthetic")
    monkeypatch.setattr(oci, "_embedding_vectors", lambda *args, **kwargs: [[1.0, 0.0, 0.0]])
    monkeypatch.setattr(oci, "_image_data_url", lambda path: "synthetic-image")
    settings = SimpleNamespace(
        embedding_model="model-a",
        oci_compartment_id="synthetic",
        oci_config_file="synthetic",
        oci_profile="synthetic",
        llm_request_timeout_seconds=1,
    )
    with bind_trace_observer(observer), model_call_purpose("connection.check" if probe else None):
        if image:
            path = tmp_path / "image.png"
            path.write_bytes(b"synthetic")
            result = oci._embed_image_once(path, "", settings, input_type=input_type)
            assert result == [1.0, 0.0, 0.0]
        else:
            result = oci._embed_text_batch_once(["合成の入力"], settings, input_type=input_type)
            assert result == [[1.0, 0.0, 0.0]]
    purpose = "connection.check" if probe else expected
    assert recorded == ([{"pr.gen_ai.purpose": purpose}] if purpose else [{}])
    assert [item[0] for item in snapshots] == ["request", "response"]
    assert snapshots[0][1] is requests[0]
    assert len(requests) == 1
    assert requests[0].input_type == ("SEARCH_QUERY" if input_type == "QUERY" else input_type)
    assert model_purpose_attributes() == {}
