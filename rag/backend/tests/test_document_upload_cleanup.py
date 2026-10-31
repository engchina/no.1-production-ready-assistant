"""文書アップロードの失敗時の後始末と保存名の回帰テスト（#280）。

- 文書行を作れなかったとき（範囲外・アーカイブ済みの KB、DB の失敗）に、保存済みの原本を残さない
- 保存先への保存に失敗したときは、原因の分かる 503 を返す（一括アップロードでは失敗 item）
- 日本語の長いファイル名を `rag_documents.file_name`（VARCHAR2(512) BYTE）に収まる長さへ切り詰める
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.api.routes import documents as documents_route
from app.clients.object_storage import ObjectStorageClient
from app.config import get_settings
from app.main import app
from tests.support import AsgiTestClient
from tests.test_document_workspace import FakeWorkspaceOracle

client = AsgiTestClient(app)
lenient_client = AsgiTestClient(app, raise_app_exceptions=False)


class _FailingCreateOracle(FakeWorkspaceOracle):
    """`create_document` だけ指定の例外で失敗する fake。"""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def create_document(self, **kwargs: object) -> object:  # type: ignore[override]
        raise self.error


def _stored_objects() -> list[Path]:
    root = Path(get_settings().local_storage_dir) / "objects"
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


def _use_oracle(monkeypatch: pytest.MonkeyPatch, oracle: FakeWorkspaceOracle) -> None:
    monkeypatch.setattr(documents_route, "OracleClient", lambda: oracle)


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        # 利用者の範囲外、または存在しない KB（`_require_active_knowledge_base` が KeyError）。
        (KeyError("knowledge_base_id=kb-other は存在しません。"), 404),
        # アーカイブ済みの KB（ValueError）。
        (ValueError("アーカイブ済みナレッジベースは変更できません。"), 409),
    ],
)
def test_upload_removes_stored_object_when_document_row_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status_code: int,
) -> None:
    """KB の検証で文書行を作れなかった原本は、保存先から消してから返す。"""
    _use_oracle(monkeypatch, _FailingCreateOracle(error))

    response = client.post(
        "/api/documents/upload",
        data={"knowledge_base_ids": "kb-other"},
        files={"file": ("policy.txt", "経費規程".encode(), "text/plain")},
    )

    assert response.status_code == status_code
    assert _stored_objects() == []


def test_upload_removes_stored_object_when_database_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB の失敗（ORA- など）でも保存済みの原本を残さない。"""
    _use_oracle(monkeypatch, _FailingCreateOracle(RuntimeError("ORA-12899: value too large")))

    response = lenient_client.post(
        "/api/documents/upload",
        files={"file": ("policy.txt", "経費規程".encode(), "text/plain")},
    )

    assert response.status_code == 500
    assert _stored_objects() == []


def test_batch_upload_removes_stored_object_of_failed_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一括アップロードでも、失敗した item の原本を残さない。"""
    _use_oracle(monkeypatch, _FailingCreateOracle(KeyError("kb-other")))

    response = client.post(
        "/api/documents/batch-upload",
        data={"knowledge_base_ids": "kb-other"},
        files=[
            ("files", ("a.txt", b"A", "text/plain")),
            ("files", ("b.txt", b"B", "text/plain")),
        ],
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["failed_count"] == 2
    assert [item["status_code"] for item in data["failed_items"]] == [404, 404]
    assert _stored_objects() == []


def test_upload_keeps_stored_object_when_document_row_is_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功したアップロードの原本は消さない（後始末が成功経路に及ばない）。"""
    _use_oracle(monkeypatch, FakeWorkspaceOracle())

    response = client.post(
        "/api/documents/upload",
        files={"file": ("policy.txt", "経費規程".encode(), "text/plain")},
    )

    assert response.status_code == 200
    assert len(_stored_objects()) == 1


def test_upload_reports_storage_failure_with_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存先に保存できないときは、500 の汎用文言ではなく設定の確認を促す 503 を返す。"""
    oracle = FakeWorkspaceOracle()
    _use_oracle(monkeypatch, oracle)

    async def failing_put(
        self: ObjectStorageClient, key: str, data: bytes, content_type: str
    ) -> str:
        raise ValueError("OCI Object Storage の namespace / bucket が未設定です。")

    monkeypatch.setattr(ObjectStorageClient, "put", failing_put)

    single = client.post(
        "/api/documents/upload",
        files={"file": ("policy.txt", "経費規程".encode(), "text/plain")},
    )
    assert single.status_code == 503
    assert single.json()["error_messages"] == [documents_route.UPLOAD_STORAGE_FAILED_MESSAGE]

    batch = client.post(
        "/api/documents/batch-upload",
        files=[("files", ("a.txt", b"A", "text/plain"))],
    )
    assert batch.status_code == 200
    failed = batch.json()["data"]["failed_items"]
    assert [(item["status_code"], item["message"]) for item in failed] == [
        (503, documents_route.UPLOAD_STORAGE_FAILED_MESSAGE)
    ]
    assert oracle.documents == {}


def test_upload_reports_missing_bucket_with_bucket_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OCI の bucket が無い・権限が無い（404）ときは、bucket 名と権限の確認を案内する（#2093）。"""
    oracle = FakeWorkspaceOracle()
    _use_oracle(monkeypatch, oracle)

    class BucketNotFoundError(Exception):
        status = 404
        code = "BucketNotFound"

    async def failing_put(
        self: ObjectStorageClient, key: str, data: bytes, content_type: str
    ) -> str:
        raise BucketNotFoundError("bucket not found")

    monkeypatch.setattr(ObjectStorageClient, "put", failing_put)

    response = client.post(
        "/api/documents/upload",
        files={"file": ("policy.txt", "経費規程".encode(), "text/plain")},
    )

    assert response.status_code == 503
    [message] = response.json()["error_messages"]
    assert "bucket が見つからないか" in message
    assert "IAM ポリシー" in message
    assert oracle.documents == {}


def test_long_japanese_file_name_fits_document_column_and_keeps_extension() -> None:
    """255 文字以内でも 512 バイトを超える日本語名は、拡張子を残して切り詰める。"""
    name = "報" * 200 + ".pdf"  # 204 文字・604 バイト

    safe = documents_route._safe_display_filename(name)

    assert safe.endswith(".pdf")
    assert len(safe.encode("utf-8")) <= documents_route.MAX_UPLOAD_FILE_NAME_BYTES
    assert len(safe) <= documents_route.MAX_UPLOAD_FILE_NAME_CHARS
    assert safe.startswith("報" * 100)


def test_long_ascii_file_name_keeps_extension() -> None:
    """文字数の上限で切るときも拡張子（形式の判定に使う）を落とさない。"""
    safe = documents_route._safe_display_filename("a" * 300 + ".docx")

    assert safe == "a" * 250 + ".docx"


def test_short_file_name_is_unchanged() -> None:
    assert documents_route._safe_display_filename("経費規程 2026.pdf") == "経費規程 2026.pdf"


def test_long_japanese_upload_is_saved_with_truncated_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """長い日本語名のアップロードは、切り詰めた名前で文書行を作る。"""
    oracle = FakeWorkspaceOracle()
    _use_oracle(monkeypatch, oracle)

    response = client.post(
        "/api/documents/upload",
        files={"file": ("規" * 200 + ".txt", "本文".encode(), "text/plain")},
    )

    assert response.status_code == 200
    saved_name = response.json()["data"]["file_name"]
    assert saved_name.endswith(".txt")
    assert len(saved_name.encode("utf-8")) <= 512
