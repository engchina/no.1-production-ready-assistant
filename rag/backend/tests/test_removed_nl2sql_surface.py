"""RAG app に SQL 専用プロダクト面が公開されていないことを固定する。"""

from app.main import app
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


def test_nl2sql_api_surface_is_not_registered() -> None:
    """SQL 専用 API は RAG repo の FastAPI router に載せない。"""
    for method, path in [
        ("post", "/api/search/select-ai"),
        ("post", "/api/nl2sql/generate"),
        ("post", "/api/nl2sql/execute"),
        ("get", "/api/settings/nl2sql/router"),
        ("get", "/api/settings/nl2sql/guardrail"),
        ("get", "/api/settings/nl2sql/cache"),
        ("get", "/api/settings/nl2sql/pipeline"),
    ]:
        response = getattr(client, method)(path)
        assert response.status_code == 404
