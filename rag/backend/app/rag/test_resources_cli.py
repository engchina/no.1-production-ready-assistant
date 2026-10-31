"""合成 PDF の KB・検索・回答プロファイルを管理 API で再構築し、文書を取り込む。

RAG が持つもの（KB・検索・回答プロファイル・文書の取込）だけを作る。RAG は Agent を呼ばない
（#3545）。業務 Agent は、この命令が `--state` に残す検索・回答プロファイルの ID を使い、Agent の
backend の命令で作成・公開する（手順は rag/examples/test-pdfs-ja/README.md）。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import math
import os
import re
import ssl
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pypdf import PdfReader

from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
from app.schemas.document import DocumentProcessingConfig


class RebuildError(RuntimeError):
    """値や応答本文を漏らさず、停止した工程を説明する。"""


def read_fixture(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    resources = json.loads((root / "resources.json").read_text(encoding="utf-8"))
    if manifest.get("synthetic") is not True or resources.get("version") != 1:
        raise RebuildError("合成資料の manifest / 構成の版を確認してください。")
    DocumentProcessingConfig.model_validate(resources["recipe"])
    # グループは RAG が持つもの（KB・検索・回答プロファイル）だけを定義する。業務 Agent の定義は
    # Agent の側に置く（#3545）。知らない項目は黙って無視せずに止める。
    if any(set(group) != {"knowledge_base", "profile"} for group in resources["groups"].values()):
        raise RebuildError("構成のグループは knowledge_base と profile だけを定義してください。")
    names: set[str] = set()
    for row in manifest["documents"]:
        path = root / row["file"]
        if path.resolve().parent != root.resolve() or row["file"] in names:
            raise RebuildError("PDF のパスまたは重複を確認してください。")
        names.add(row["file"])
        if row["group"] not in resources["groups"]:
            raise RebuildError("文書のグループが構成にありません。")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise RebuildError("PDF の SHA-256 が manifest と一致しません。")
        if len(PdfReader(path).pages) != row["pages"]:
            raise RebuildError("PDF のページ数が manifest と一致しません。")
    if set(resources["groups"]) != {row["group"] for row in manifest["documents"]}:
        raise RebuildError("文書のないグループがあります。")
    return manifest, resources


class ManagementApi:
    """Cookie / CSRF で管理 API を呼ぶ。変更の自動再送はしない。"""

    def __init__(self, client: httpx.Client, base: str) -> None:
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RebuildError("API URL は http / https の絶対 URL にしてください。")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RebuildError("API URL に認証情報・質問文字列・fragment は指定できません。")
        self.client = client
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        headers: dict[str, str] = {}
        for cookie in self.client.cookies.jar:
            if "csrf" in cookie.name.lower() and cookie.value:
                headers["X-CSRF-Token"] = cookie.value
        try:
            response = self.client.request(method, self.base + path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise RebuildError(
                "管理 API の通信が失敗しました。状態を確認して再実行してください。"
            ) from exc
        if response.is_error or response.is_redirect:
            raise RebuildError(f"管理 API が HTTP {response.status_code} を返しました。")
        try:
            result = response.json()
            if result.get("error_messages") or result.get("warning_messages"):
                raise RebuildError("管理 API がエラーまたは警告を返しました。")
            return result["data"]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RebuildError("管理 API の応答形式を確認してください。") from exc

    def rows(self, path: str, **params: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.call("GET", path, params={**params, "limit": 200, "offset": offset})
            items = page["items"]
            rows.extend(items)
            offset += len(items)
            if offset >= page["total"]:
                return rows
            if not items:
                raise RebuildError("管理 API のページングが進みません。")


def _only(rows: list[dict[str, Any]], name: str, field: str = "name") -> dict[str, Any] | None:
    found = [row for row in rows if row.get(field) == name]
    if len(found) > 1:
        raise RebuildError("同じ名前の対象が複数あります。自動では選択しません。")
    return found[0] if found else None


def _matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


class Rebuilder:
    """名前と定義で既存対象を確認し、作成直後の切断でも次回の一覧から再開する。"""

    def __init__(
        self,
        rag: ManagementApi,
        root: Path,
        state_path: Path,
        *,
        timeout: float = 3600,
        interval: float = 5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
        namespace: str = "",
    ) -> None:
        if not all(math.isfinite(v) and v > 0 for v in (timeout, interval)):
            raise RebuildError("待機の上限と間隔は正の有限値にしてください。")
        if namespace and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", namespace):
            raise RebuildError("名前の区分は英数字・_・- の 32 字以内にしてください。")
        self.namespace = namespace
        self.rag, self.root, self.state_path = rag, root, state_path
        self.timeout, self.interval, self.clock, self.sleep, self.log = (
            timeout,
            interval,
            clock,
            sleep,
            log,
        )
        self.state: dict[str, Any] = {
            "complete": False,
            "namespace": namespace,
            "groups": {},
            "documents": {},
        }

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".pending")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.state_path)

    def named(self, api: ManagementApi, path: str, definition: dict[str, Any]) -> dict[str, Any]:
        found = _only(api.rows(path), definition["name"])
        if found is None:
            found = api.call("POST", path, json=definition)
            self.log("対象を作成しました。")
        else:
            found = api.call("GET", f"{path}/{found['id']}")
            if found.get("status") == "ARCHIVED" or not _matches(
                found, {k: v for k, v in definition.items() if k != "config"}
            ):
                raise RebuildError("同名の既存定義が異なります。上書きせず停止します。")
            self.log("一致する既存の対象を再利用します。")
        return dict(found)

    def document(self, row: dict[str, Any], kb_id: str, recipe: dict[str, Any]) -> dict[str, Any]:
        path = "/documents"
        remote_name = row.get("remote_file", row["file"])
        found = _only(self.rag.rows(path, q=remote_name), remote_name, "file_name")
        if found is None:
            with (self.root / row["file"]).open("rb") as stream:
                uploaded = self.rag.call(
                    "POST",
                    path + "/upload",
                    files={"file": (remote_name, stream, "application/pdf")},
                    data={"knowledge_base_ids": kb_id},
                )
            found = self.rag.call("GET", f"{path}/{uploaded['id']}")
        if (
            found.get("content_sha256") != row["sha256"]
            or {ref["id"] for ref in found["knowledge_bases"]} != {kb_id}
            or found.get("superseded_by_document_id")
        ):
            raise RebuildError("同名文書の原本または所属が異なります。上書きせず停止します。")
        document_id = found["id"]
        recipes = self.rag.call("GET", f"{path}/{document_id}/recipes")
        if len(recipes) != 1:
            raise RebuildError("文書の処理レシピが 1 件ではありません。変更せず停止します。")
        current = recipes[0]
        jobs_path = f"{path}/{document_id}/ingestion-jobs"
        jobs = self.rag.call("GET", jobs_path)
        if not jobs and found["status"] == "UPLOADED":
            current = self.rag.call(
                "PUT", f"{path}/{document_id}/recipes/{current['recipe_id']}", json=recipe
            )
        elif not _matches(current["effective_processing_config"], recipe):
            raise RebuildError("既存文書の処理設定が異なります。上書きせず停止します。")
        if not jobs and not current["searchable"]:
            self.rag.call(
                "POST", f"{path}/{document_id}/recipes/{current['recipe_id']}/ingestion-jobs"
            )
        self.state["documents"][row["file"]] = {"id": document_id, "group": row["group"]}
        self.save()
        return dict(current)

    def wait_documents(self) -> None:
        deadline = self.clock() + self.timeout
        pending = dict(self.state["documents"])
        while pending:
            for name, row in list(pending.items()):
                current = self.rag.call("GET", f"/documents/{row['id']}/recipes")[0]
                if (
                    current["status"] == "INDEXED"
                    and current["searchable"]
                    and not current["needs_reprocessing"]
                ):
                    chunks, vectors = current["chunk_count"], current["vector_count"]
                    if chunks > 0 and chunks == vectors and current["active_chunk_set_id"]:
                        self.state["documents"][name].update(
                            chunk_count=chunks, vector_count=vectors, indexed=True
                        )
                        del pending[name]
                        self.save()
                        continue
                if current["status"] in {"ERROR", "FAILED"}:
                    raise RebuildError("取込が失敗しました。文書の工程を確認してください。")
                jobs = self.rag.call("GET", f"/documents/{row['id']}/ingestion-jobs")
                if (
                    jobs
                    and not any(job["status"] in {"QUEUED", "RUNNING"} for job in jobs)
                    and any(job["status"] in {"FAILED", "CANCELLED"} for job in jobs)
                ):
                    raise RebuildError(
                        "取込 job が失敗または取消済みです。工程を確認してください。"
                    )
                if self.clock() >= deadline:
                    raise RebuildError(
                        "索引の完了を待つ上限に達しました。確認待ちを含む文書の工程を確認してください。"
                        "取込は取り消していません。"
                    )
            if pending:
                self.sleep(min(self.interval, max(0, deadline - self.clock())))

    def run(self) -> dict[str, Any]:
        manifest, config = read_fixture(self.root)
        if self.namespace:
            prefix = f"テスト_{self.namespace}_"
            for definition in config["groups"].values():
                for kind in ("knowledge_base", "profile"):
                    definition[kind]["name"] = prefix + definition[kind]["name"].removeprefix(
                        "テスト_"
                    )
            for row in manifest["documents"]:
                row["remote_file"] = prefix + row["file"].removeprefix("テスト_")
        self.save()
        for key, definition in config["groups"].items():
            kb = self.named(self.rag, "/knowledge-bases", definition["knowledge_base"])
            profile_payload = {
                **definition["profile"],
                "config": {
                    **definition["profile"].get("config", {}),
                    "knowledge_base_ids": [kb["id"]],
                },
            }
            profile = self.named(self.rag, "/search-answer-profiles", profile_payload)
            if SearchAnswerProfileConfig.model_validate(
                profile["config"]
            ) != SearchAnswerProfileConfig.model_validate(profile_payload["config"]):
                raise RebuildError("同名プロファイルの設定が異なります。変更せず停止します。")
            # 業務 Agent のデータの範囲に渡す ID（グループごとに 1 件で、既定も同じ）。
            self.state["groups"][key] = {
                "kb_id": kb["id"],
                "profile_ids": [profile["id"]],
                "default_profile_id": profile["id"],
            }
            self.save()
            for row in manifest["documents"]:
                if row["group"] == key:
                    self.document(row, kb["id"], config["recipe"])
            members = self.rag.rows("/documents", knowledge_base_id=kb["id"])
            expected = {
                row["id"] for row in self.state["documents"].values() if row["group"] == key
            }
            if {row["id"] for row in members} != expected:
                raise RebuildError("KB の文書の所属が構成と異なります。既存文書は外しません。")
        self.wait_documents()
        self.state["complete"] = True
        self.save()
        return self.state


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--rag-api", required=True, help="RAG の /api までの URL")
    parser.add_argument("--state", type=Path, required=True, help="環境固有 ID のローカル保存先")
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--poll-interval", type=float, default=5)
    parser.add_argument("--ca-file", help="私設 CA の PEM ファイル")
    parser.add_argument("--namespace", default="", help="別の合成テスト一式を作る名前の区分")
    args = parser.parse_args(argv)
    try:
        read_fixture(args.fixture_dir)
        user = os.environ.get("SUITE_TEST_LOGIN_USER") or input("ログイン ID: ")
        password = os.environ.get("SUITE_TEST_PASSWORD") or getpass.getpass("パスワード: ")
        verify = ssl.create_default_context(cafile=args.ca_file)
        with httpx.Client(verify=verify, timeout=120, follow_redirects=False) as client:
            rag = ManagementApi(client, args.rag_api)
            rag.call("POST", "/auth/login", json={"login_user_id": user, "password": password})
            result = Rebuilder(
                rag,
                args.fixture_dir,
                args.state,
                timeout=args.timeout,
                interval=args.poll_interval,
                namespace=args.namespace,
            ).run()
            print(
                f"完了: KB・検索・回答プロファイル {len(result['groups'])} 組、"
                f"索引済み文書 {len(result['documents'])} 件"
            )
        return 0
    except (RebuildError, OSError, ValueError, KeyError, TypeError) as exc:
        message = (
            str(exc)
            if isinstance(exc, RebuildError)
            else "構成またはローカルファイルを確認してください。"
        )
        print(f"未完了: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
