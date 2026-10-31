"""旧 scope の読み捨てで対象範囲を拡大しない。"""

import pytest

from app.main import app
from app.mcp.tools import SearchInput
from tests.support import AsgiTestClient


@pytest.mark.parametrize(
    "field", ["business_view_id", "business_view_ids", "allowed_business_view_ids"]
)
def test_rest_rejects_removed_scope_in_query_and_json(field: str) -> None:
    client = AsgiTestClient(app)
    assert client.get(f"/api/search/answers?{field}=bv-1").status_code == 422
    response = client.post("/api/search", json={"query": "規程", field: "bv-1"})
    assert response.status_code == 422
    assert "旧指定" in response.text or "旧対象" in response.text


def test_mcp_rejects_removed_scope_even_when_current_scope_is_present() -> None:
    with pytest.raises(ValueError):
        SearchInput.model_validate(
            {"query": "規程", "business_view_id": "bv-1", "search_answer_profile_id": "bv-2"}
        )
