"""埋め込みの直接経路も、宣言済み入力型と明示用途だけを記録する（#8 で OpenAI 互換の HTTP に移行）。"""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pr_backend_core.observability.model_purpose import model_call_purpose, model_purpose_attributes

from rag_engine.adapters import oci
from rag_engine.resources.tracing import bind_trace_observer


@pytest.mark.parametrize("input_type,expected", [("QUERY", "query.embed"), ("SEARCH_DOCUMENT", "document.embed"), ("IMAGE", None)])
@pytest.mark.parametrize("probe", [False, True])
def test_embedding_records_declared_purpose_without_changing_request(
    monkeypatch: pytest.MonkeyPatch,
    input_type: str,
    expected: str | None,
    probe: bool,
) -> None:
    requests = []
    clients = []
    recorded = []
    snapshots = []
    response = SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0, 0.0, 0.0])])

    def create(**request):
        requests.append(request)
        return response

    def openai_client(*args):
        clients.append(args)
        return SimpleNamespace(embeddings=SimpleNamespace(create=create))

    @contextmanager
    def observer(operation, model):
        assert operation == "embeddings" and model == "model-a"
        recorded.append(model_purpose_attributes())
        yield SimpleNamespace(
            request=lambda value, **kwargs: snapshots.append(("request", value)),
            response=lambda value: snapshots.append(("response", value)),
        )

    monkeypatch.setattr(oci, "_openai_client", openai_client)
    monkeypatch.setattr(oci, "_embedding_base_url", lambda settings: "https://synthetic.test/compatible-mode/v1")
    monkeypatch.setattr(oci, "_embedding_api_key", lambda settings: "synthetic-key")
    monkeypatch.setattr(oci, "_embedding_output_dimensions", lambda settings: 3)
    settings = SimpleNamespace(embedding_model="model-a", llm_request_timeout_seconds=1)
    with bind_trace_observer(observer), model_call_purpose("connection.check" if probe else None):
        result = oci._embed_text_batch_once(["合成の入力"], settings, input_type=input_type)
    assert result == [[1.0, 0.0, 0.0]]
    purpose = "connection.check" if probe else expected
    assert recorded == ([{"pr.gen_ai.purpose": purpose}] if purpose else [{}])
    assert [item[0] for item in snapshots] == ["request", "response"]
    assert snapshots[0][1] == requests[0] and snapshots[1][1] is response
    # 用途は観測だけに載せ、要求は変えない。`dimensions` は必ず送る（#8）。
    assert requests == [{"model": "model-a", "input": ["合成の入力"], "dimensions": 3}]
    assert clients[0][1:3] == ("https://synthetic.test/compatible-mode/v1", "synthetic-key")
    assert model_purpose_attributes() == {}


def test_image_embedding_is_not_supported(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    path.write_bytes(b"synthetic")
    with pytest.raises(RuntimeError, match="画像の埋め込みは未対応です"):
        oci._embed_image_once(path, "", SimpleNamespace(), input_type="IMAGE")


def test_default_embedding_dimensions_match_oracle_schema() -> None:
    """rag_engine の既定の次元は Oracle の列（ADB_EMBEDDING_DIMENSIONS）と同じ（#46）。"""
    from rag_engine.adapters.oci import SUPPORTED_EMBEDDING_OUTPUT_DIMENSIONS
    from rag_engine.adapters.oracle.store import ADB_EMBEDDING_DIMENSIONS
    from rag_engine.config import DEFAULT_EMBEDDING_OUTPUT_DIMENSIONS

    assert DEFAULT_EMBEDDING_OUTPUT_DIMENSIONS == 1024
    assert ADB_EMBEDDING_DIMENSIONS == DEFAULT_EMBEDDING_OUTPUT_DIMENSIONS
    assert DEFAULT_EMBEDDING_OUTPUT_DIMENSIONS in SUPPORTED_EMBEDDING_OUTPUT_DIMENSIONS
