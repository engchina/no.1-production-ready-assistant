"""評価の合成の資料を取り込み、評価セットの文書の参照を実際の ID に置き換える CLI（#1231）。

評価セット（例: `rag/evaluation/business-support/business-support.json`）の
`relevant_document_ids` と `required_evidence[].document_id`（#1284）は、配備先ごとに違う文書 ID の
代わりに `file:<ファイル名>` で資料を指す。この CLI は次を行う。

1. ナレッジベースを作る（`--knowledge-base-id` を渡したときはそれを使う）。
2. 評価セットが参照するファイルを、評価セットと同じフォルダ（`--corpus-dir` で変更可）から
   アップロードし、取込を始める。Excel は前処理 `excel_to_jsonl` で読む。前の評価で同じ内容の
   ファイルを取り込んでいても（重複の文書。`duplicate_of_document_id`）、取込を進めてこの
   ナレッジベースの文書として索引を作る。自前の索引を持つ重複の検索は自分の chunk だけで、
   前の評価の文書（重複の元）は検索の範囲に入らない（#1381）。
3. 索引（INDEXED）まで待つ。確認待ち（REVIEW など）のゲートは承認して進める。
4. 評価セットに文書の版（`document_versions`。#1366）があれば、旧版の文書を新しい版に置き換えた
   文書として登録する（文書詳細の「版」と同じ。旧版は既定で回答の検索から外れる）。
   `--keep-superseded-active` を渡すと登録せず、旧版も今有効な文書のまま検索させる
   （旧版の紛らわしさの測定）。
5. `file:` の参照を文書 ID に置き換え、`knowledge_base_ids` を入れた評価セットを `--output` に書く。
6. 実体・別名索引は文書レシピの ``entity_index_enabled`` で選ぶ（全体の既定は OFF）。
   `--entity-index` は索引の構築を明示し、比較用の評価セットの `rag_overrides` に
   `entity_expansion_enabled: true` を入れる（既存の明示値は変えない）。
   `--no-entity-index` は構築を無効にし、未指定は全体の既定に従う。
   検索・回答プロファイルには拡張の個別設定を保存しない。
7. `--document-map-output` を渡したとき（#3240）は、取り込んだファイル名 → 文書 ID の対応表
   （`{"documents": {"<ファイル名>": "<document_id>"}}`）を書く。製品横断の
   評価（`suite-evaluation run --document-map`）が評価セットの `file:` を解決する。
8. `--guides` を渡したとき（#1289）は、そのナレッジベースを参照する検索・回答プロファイルを作り、
   業務ガイド（`support-guides.json`。参照の `file:` も文書 ID に置き換える）を取り込んで公開し、
   `search_answer_profile_id` を入れた評価セット（業務ガイドあり = C）を `--guided-output` に書く。

書き出した評価セットは `python -m app.rag.evaluation_cli <output> --api-base-url …` で実行できる。

    uv run python -m app.rag.evaluation_corpus_cli \\
        ../evaluation/business-support/business-support.json \\
        --api-base-url http://127.0.0.1:8000 --output /tmp/business-support.resolved.json \\
        --guides ../evaluation/business-support/support-guides.json \\
        --guided-output /tmp/business-support.guided.json
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

FILE_REFERENCE_PREFIX = "file:"
DEFAULT_API_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_SECONDS = 1800.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 120.0
# ファイルの拡張子ごとの文書レシピ（前処理）。無い拡張子は全体の既定のまま取り込む。
RECIPE_BY_EXTENSION: Mapping[str, Mapping[str, Any]] = {
    ".xlsx": {"preprocess_profile": "excel_to_jsonl"},
    ".xls": {"preprocess_profile": "excel_to_jsonl"},
}
_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
}
# 承認して先へ進めるゲートと、終わりの状態。
_GATE_STATUSES = frozenset({"PREPROCESSED", "REVIEW", "CHUNKED"})
_DONE_STATUS = "INDEXED"
_FAILED_STATUSES = frozenset({"ERROR", "FAILED"})


class CorpusError(RuntimeError):
    """利用者へ返す失敗（exit code 2）。"""


def recipe_for(path: Path, *, entity_index: bool | None = None) -> dict[str, Any]:
    """ファイルの文書レシピ（拡張子ごとの前処理と、指定したときは実体の抽出の有無。#1362）。

    ``entity_index`` が None ならレシピに書かず、全体の既定（ON。#1388）に従う。
    """
    recipe: dict[str, Any] = dict(RECIPE_BY_EXTENSION.get(path.suffix.lower(), {}))
    if entity_index is not None:
        recipe["entity_index_enabled"] = entity_index
    return recipe


def _file_reference_name(entry: object, key: str) -> str:
    """文書の版の 1 件の `file:<ファイル名>` の参照からファイル名を取り出す。"""
    value = entry.get(key) if isinstance(entry, Mapping) else None
    name = value.removeprefix(FILE_REFERENCE_PREFIX) if isinstance(value, str) else ""
    if not (isinstance(value, str) and value.startswith(FILE_REFERENCE_PREFIX) and name):
        raise CorpusError(f"document_versions の {key} は file:<ファイル名> で書いてください。")
    return name


def document_versions(golden_set: Mapping[str, Any]) -> list[tuple[str, str]]:
    """評価セットの文書の版（#1366）を（旧版のファイル名, 新しい版のファイル名）の列で返す。

    `document_versions` は `[{"document_id": "file:<旧版>", "superseded_by": "file:<新しい版>"}]`。
    """
    raw = golden_set.get("document_versions", [])
    if not isinstance(raw, list):
        raise CorpusError("document_versions は配列にしてください。")
    versions: list[tuple[str, str]] = []
    for entry in raw:
        old_name = _file_reference_name(entry, "document_id")
        new_name = _file_reference_name(entry, "superseded_by")
        if old_name == new_name:
            raise CorpusError(f"文書を自分自身の新しい版にはできません: {old_name}")
        if old_name in {name for name, _ in versions}:
            raise CorpusError(f"旧版の文書が重複しています: {old_name}")
        versions.append((old_name, new_name))
    return versions


def _document_references(case: Mapping[str, Any]) -> list[object]:
    """ケースの文書の参照（正解の文書と、必要な根拠の文書。#1284）。"""
    values: list[object] = list(case.get("relevant_document_ids", []))
    for evidence in case.get("required_evidence", []):
        if isinstance(evidence, Mapping):
            values.append(evidence.get("document_id"))
    return values


def referenced_files(golden_set: Mapping[str, Any]) -> list[str]:
    """評価セットが `file:` で参照するファイル名（出てきた順・重複なし）。"""
    names: list[str] = []
    for case in golden_set.get("cases", []):
        for value in _document_references(case):
            if isinstance(value, str) and value.startswith(FILE_REFERENCE_PREFIX):
                name = value.removeprefix(FILE_REFERENCE_PREFIX)
                if name and name not in names:
                    names.append(name)
    return names


def _resolve_reference(value: object, document_ids: Mapping[str, str]) -> object:
    """`file:<ファイル名>` を取り込んだ文書の ID にする（それ以外の値はそのまま）。"""
    if not (isinstance(value, str) and value.startswith(FILE_REFERENCE_PREFIX)):
        return value
    name = value.removeprefix(FILE_REFERENCE_PREFIX)
    if name not in document_ids:
        raise CorpusError(f"取り込んでいないファイルを参照しています: {name}")
    return document_ids[name]


def resolve_golden_set(
    golden_set: Mapping[str, Any],
    document_ids: Mapping[str, str],
    knowledge_base_id: str,
    *,
    versions_registered: bool = True,
) -> dict[str, Any]:
    """`file:` の参照を文書 ID に置き換え、`knowledge_base_ids` を入れた評価セット。

    文書の版（#1366）は、登録したときは文書 ID に置き換えて残し、登録しなかったとき
    （`--keep-superseded-active`）は外す（書き出した評価セットが取り込んだ状態を表すように）。
    """
    resolved: dict[str, Any] = json.loads(json.dumps(golden_set))
    if versions_registered and "document_versions" in resolved:
        resolved["document_versions"] = [
            {
                key: _resolve_reference(f"{FILE_REFERENCE_PREFIX}{name}", document_ids)
                for key, name in (("document_id", old), ("superseded_by", new))
            }
            for old, new in document_versions(golden_set)
        ]
    else:
        resolved.pop("document_versions", None)
    for case in resolved.get("cases", []):
        case["relevant_document_ids"] = [
            _resolve_reference(value, document_ids)
            for value in case.get("relevant_document_ids", [])
        ]
        for evidence in case.get("required_evidence", []):
            if isinstance(evidence, dict) and "document_id" in evidence:
                evidence["document_id"] = _resolve_reference(evidence["document_id"], document_ids)
    resolved["knowledge_base_ids"] = [knowledge_base_id]
    return resolved


def with_entity_expansion(golden_set: Mapping[str, Any]) -> dict[str, Any]:
    """実体の 1 段の拡張を選んだ評価セット（#1388）。

    拡張は検索方法の全体設定（`RAG_ENTITY_EXPANSION_ENABLED`。既定 OFF）で決め、検索・回答
    プロファイルでは上書きしない。全体設定に頼らず、評価（A）は評価の `rag_overrides` で
    明示する。評価セットが明示した `entity_expansion_enabled` は変えない。
    """
    resolved: dict[str, Any] = json.loads(json.dumps(dict(golden_set)))
    overrides = resolved.get("rag_overrides")
    overrides = dict(overrides) if isinstance(overrides, Mapping) else {}
    overrides.setdefault("entity_expansion_enabled", True)
    resolved["rag_overrides"] = overrides
    return resolved


def resolve_guides(
    guides: Sequence[Mapping[str, Any]], document_ids: Mapping[str, str]
) -> list[dict[str, Any]]:
    """業務ガイドの参照（`references[].document_id`）の `file:` を文書 ID に置き換える（#1289）。"""
    resolved: list[dict[str, Any]] = json.loads(json.dumps(list(guides)))
    for guide in resolved:
        for reference in guide.get("references", []):
            if isinstance(reference, dict) and "document_id" in reference:
                reference["document_id"] = _resolve_reference(
                    reference["document_id"], document_ids
                )
    return resolved


def guide_referenced_files(guides: Sequence[Mapping[str, Any]]) -> list[str]:
    """業務ガイドが `file:` で参照するファイル名（出てきた順・重複なし）。"""
    names: list[str] = []
    for guide in guides:
        for reference in guide.get("references", []):
            value = reference.get("document_id") if isinstance(reference, Mapping) else None
            if isinstance(value, str) and value.startswith(FILE_REFERENCE_PREFIX):
                name = value.removeprefix(FILE_REFERENCE_PREFIX)
                if name and name not in names:
                    names.append(name)
    return names


def guided_golden_set(resolved: Mapping[str, Any], search_answer_profile_id: str) -> dict[str, Any]:
    """検索・回答プロファイル（参照 KB と業務ガイド）で評価する評価セット（C。#1249 / #1289）。

    参照 KB はプロファイルが決めるため、`knowledge_base_ids` は外す。
    """
    guided: dict[str, Any] = json.loads(json.dumps(dict(resolved)))
    guided.pop("knowledge_base_ids", None)
    guided["search_answer_profile_id"] = search_answer_profile_id
    return guided


class CorpusLoader:
    """API を呼んで資料を取り込む。HTTP の client と待ち方を差し替えられる（テスト用）。"""

    def __init__(
        self,
        client: httpx.Client,
        api_base_url: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = print,
        entity_index: bool | None = None,
    ) -> None:
        self._client = client
        # すべての文書のレシピで実体の抽出を有効 / 無効にする（#1362。None は全体の既定）。
        self._entity_index = entity_index
        self._api = api_base_url.rstrip("/") + "/api"
        self._timeout = timeout_seconds
        self._interval = poll_interval_seconds
        self._sleep = sleep
        self._clock = clock
        self._log = log

    def _data(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            raise CorpusError(
                f"{response.request.method} {response.request.url.path} が失敗しました"
                f"（HTTP {response.status_code}）: {response.text[:300]}"
            )
        return response.json().get("data")

    def create_knowledge_base(self, name: str) -> str:
        data = self._data(
            self._client.post(
                f"{self._api}/knowledge-bases",
                json={"name": name, "description": "業務支援の評価の合成の資料（評価用）"},
            )
        )
        return str(data["id"])

    def create_search_answer_profile(self, name: str, knowledge_base_id: str) -> str:
        # --guides で明示的に作る評価用プロファイルだけ、ガイドの使用を ON にする。
        config: dict[str, Any] = {
            "knowledge_base_ids": [knowledge_base_id],
            "query": {"support_guide_enabled": True},
        }
        data = self._data(
            self._client.post(
                f"{self._api}/search-answer-profiles",
                json={
                    "name": name,
                    "description": "業務支援の評価の業務ガイドあり（評価用）",
                    "config": config,
                },
            )
        )
        return str(data["id"])

    def import_guides(
        self, search_answer_profile_id: str, guides: Sequence[Mapping[str, Any]]
    ) -> list[str]:
        """業務ガイドを下書きとして取り込み、検証して公開する。公開した guide_id を返す。"""
        base = f"{self._api}/search-answer-profiles/{search_answer_profile_id}/support-guides"
        created = self._data(
            self._client.post(f"{base}/import", json={"guides": [dict(g) for g in guides]})
        )
        published: list[str] = []
        for summary in created.get("created", []):
            guide_id = str(summary["guide_id"])
            self._data(
                self._client.post(
                    f"{base}/{guide_id}/publish",
                    json={"base_revision": int(summary["draft_revision"])},
                )
            )
            self._log(f"published guide {summary.get('title') or guide_id}")
            published.append(guide_id)
        return published

    def ingest(self, path: Path, knowledge_base_id: str) -> str:
        """1 ファイルをアップロードして取込を始め、文書 ID を返す。"""
        content_type = _CONTENT_TYPES.get(path.suffix.lower()) or (
            mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        uploaded = self._data(
            self._client.post(
                f"{self._api}/documents/upload",
                files={"file": (path.name, path.read_bytes(), content_type)},
                data={"knowledge_base_ids": [knowledge_base_id]},
            )
        )
        document_id = str(uploaded.get("document_id") or uploaded.get("id"))
        duplicate_of = uploaded.get("duplicate_of_document_id")
        if duplicate_of:
            # 取込を進めて自前の索引を作るので、重複の元（前の評価の文書）は検索に入らない。
            self._log(
                f"duplicate {path.name}: 既存の文書 {duplicate_of} と同じ内容です"
                "（このナレッジベースの文書として索引を作ります）"
            )
        recipe_id = self._recipe(document_id)["recipe_id"]
        recipe = recipe_for(path, entity_index=self._entity_index)
        if recipe:
            self._data(
                self._client.put(
                    f"{self._api}/documents/{document_id}/recipes/{recipe_id}", json=dict(recipe)
                )
            )
        self._data(
            self._client.post(
                f"{self._api}/documents/{document_id}/recipes/{recipe_id}/ingestion-jobs", json={}
            )
        )
        return document_id

    def register_document_version(self, document_id: str, superseded_by_document_id: str) -> None:
        """文書を新しい版に置き換えた文書（旧版）として登録する（#1366。文書詳細の「版」と同じ）。"""
        self._data(
            self._client.put(
                f"{self._api}/documents/{document_id}/superseded-by",
                json={"superseded_by_document_id": superseded_by_document_id},
            )
        )

    def _recipe(self, document_id: str) -> Mapping[str, Any]:
        recipes = self._data(self._client.get(f"{self._api}/documents/{document_id}/recipes"))
        if not recipes:
            raise CorpusError(f"文書の処理レシピがありません: {document_id}")
        recipe: Mapping[str, Any] = recipes[0]
        return recipe

    def wait_indexed(self, documents: Mapping[str, str]) -> None:
        """すべての文書が索引まで進むのを待つ（確認待ちのゲートは承認する）。"""
        pending = dict(documents)
        approved: set[tuple[str, str]] = set()
        deadline = self._clock() + self._timeout
        while pending:
            for name, document_id in list(pending.items()):
                recipe = self._recipe(document_id)
                status = str(recipe.get("status") or "")
                if status == _DONE_STATUS:
                    self._log(f"indexed {name}")
                    pending.pop(name)
                elif status in _FAILED_STATUSES:
                    raise CorpusError(
                        f"取込に失敗しました: {name}（{recipe.get('error_message') or status}）"
                    )
                elif status in _GATE_STATUSES and (document_id, status) not in approved:
                    # 承認の後、次の工程の job が状態を変えるまでは同じゲートを承認し直さない。
                    approved.add((document_id, status))
                    self._log(f"approve {name} ({status})")
                    response = self._client.post(
                        f"{self._api}/documents/{document_id}/recipes/"
                        f"{recipe['recipe_id']}/approve",
                        json={},
                    )
                    if response.status_code == 409:
                        # 読んだ後に自動の進行で次の工程へ進んだ（確認待ちでなくなった）。次の
                        # 読み取りで新しい状態を見る（#1362 の評価で、前処理の後のゲートで起きた）。
                        self._log(f"skip approve {name} ({status}: 状態が進んだ)")
                        continue
                    self._data(response)
            if not pending:
                return
            if self._clock() >= deadline:
                raise CorpusError(f"索引まで進みませんでした: {', '.join(sorted(pending))}")
            self._sleep(self._interval)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("golden_set", type=Path, help="file: で資料を参照する評価セット")
    parser.add_argument("--corpus-dir", type=Path, help="資料のフォルダ（既定は評価セットと同じ）")
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    parser.add_argument("--knowledge-base-id", help="既存のナレッジベースへ取り込む")
    parser.add_argument(
        "--knowledge-base-name", default=f"業務支援の評価 {time.strftime('%Y%m%d-%H%M%S')}"
    )
    parser.add_argument("--output", type=Path, required=True, help="置き換えた評価セットの出力先")
    parser.add_argument(
        "--document-map-output",
        type=Path,
        help="取り込んだファイル名 → 文書 ID の対応表の出力先（製品横断の評価が使う。#3240）",
    )
    parser.add_argument(
        "--guides",
        type=Path,
        help=(
            '業務ガイド（{"guides": [...]}）。渡すと検索・回答プロファイルを作って'
            "取り込み、公開する"
        ),
    )
    parser.add_argument(
        "--guided-output",
        type=Path,
        help=(
            "検索・回答プロファイルで評価する評価セット（業務ガイドあり）の出力先"
            "（--guides と一緒に）"
        ),
    )
    parser.add_argument(
        "--search-answer-profile-name",
        default=f"業務支援の評価 業務ガイドあり {time.strftime('%Y%m%d-%H%M%S')}",
    )
    parser.add_argument(
        "--entity-index",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "すべての文書のレシピで実体・別名索引の構築を明示して取り込み、評価セットと"
            "作る検索・回答プロファイルで実体の 1 段の拡張を選ぶ（#1388）。--no-entity-index は"
            "実体・別名索引の構築を無効にする。省略時は全体の既定（OFF）に従う"
        ),
    )
    parser.add_argument(
        "--keep-superseded-active",
        action="store_true",
        help=(
            "評価セットの文書の版（document_versions）を登録せず、旧版も今有効な文書のまま"
            "検索させる（旧版の紛らわしさを測る。#1366）"
        ),
    )
    parser.add_argument("--tenant-id")
    parser.add_argument("--user-id")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    headers = {"Accept": "application/json"}
    if args.tenant_id:
        headers["X-Tenant-ID"] = args.tenant_id
    if args.user_id:
        headers["X-User-ID"] = args.user_id
    try:
        if bool(args.guides) != bool(args.guided_output):
            raise CorpusError("--guides と --guided-output は一緒に渡してください。")
        golden_set = json.loads(args.golden_set.read_text(encoding="utf-8"))
        guides: list[Mapping[str, Any]] = []
        if args.guides:
            guides = list(json.loads(args.guides.read_text(encoding="utf-8")).get("guides", []))
            if not guides:
                raise CorpusError(f"業務ガイドがありません: {args.guides}")
        corpus_dir = args.corpus_dir or args.golden_set.parent
        versions = document_versions(golden_set)
        names = referenced_files(golden_set)
        names += [name for name in guide_referenced_files(guides) if name not in names]
        for name in (name for pair in versions for name in pair):
            if name not in names:
                names.append(name)
        missing = [name for name in names if not (corpus_dir / name).is_file()]
        if missing:
            raise CorpusError(f"資料が見つかりません: {', '.join(missing)}")
        with httpx.Client(
            headers=headers, timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False
        ) as client:
            loader = CorpusLoader(
                client,
                args.api_base_url,
                timeout_seconds=args.timeout,
                entity_index=args.entity_index,
            )
            knowledge_base_id = args.knowledge_base_id or loader.create_knowledge_base(
                args.knowledge_base_name
            )
            print(f"knowledge base {knowledge_base_id}")
            documents = {
                name: loader.ingest(corpus_dir / name, knowledge_base_id) for name in names
            }
            loader.wait_indexed(documents)
            register_versions = bool(versions) and not args.keep_superseded_active
            if register_versions:
                for old, new in versions:
                    loader.register_document_version(documents[old], documents[new])
                    print(f"superseded {old} by {new}")
            profile_id: str | None = None
            if guides:
                profile_id = loader.create_search_answer_profile(
                    args.search_answer_profile_name, knowledge_base_id
                )
                print(f"search answer profile {profile_id}")
                loader.import_guides(profile_id, resolve_guides(guides, documents))
        resolved = resolve_golden_set(
            golden_set,
            documents,
            knowledge_base_id,
            versions_registered=register_versions,
        )
        if args.entity_index is True:
            resolved = with_entity_expansion(resolved)
        _write(args.output, resolved)
        if args.document_map_output:
            _write(args.document_map_output, {"documents": dict(documents)})
        if profile_id is not None:
            _write(args.guided_output, guided_golden_set(resolved, profile_id))
        return 0
    except (CorpusError, OSError, json.JSONDecodeError, httpx.HTTPError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    raise SystemExit(main())
