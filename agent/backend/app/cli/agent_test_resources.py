"""合成資料の業務 Agent を Agent の管理 API で作成・更新して公開する命令（#3545）。

    uv run python -m app.cli.agent_test_resources --agent-api <Agent の /api までの URL> \
        --rag-state <RAG の命令の state> [--state <結果の保存先>]
    uv run python -m app.cli.agent_test_resources --agent-api <URL> \
        --profile A=<ID> --profile B=<ID> [--namespace <区分>]

合成資料の検索・回答を確かめる業務 Agent（定義は `agent/examples/test-pdfs-ja/agents.json`）を、
RAG の命令（`app.rag.test_resources_cli`）が作った検索・回答プロファイルの ID で作る。RAG は
Agent を呼ばないので、業務 Agent の段は Agent の側のこの命令が持つ（手順は
rag/examples/test-pdfs-ja/README.md）。

- プロファイルの ID は、RAG の命令の `--state`（`complete: true` のもの。名前の区分も読む）か、
  `--profile <グループ>=<ID>` で渡す。この命令は RAG を呼ばない。
- Agent 自身の管理 API（画面と同じ `POST /auth/login` の Cookie / CSRF）で書く。保存先に直接は
  書かないので、動いている backend の再起動は要らない。データの範囲の確認（RAG の MCP で、利用者が
  使えるプロファイルか）と公開前の評価の門も、画面と同じ経路で backend が行う。
- 組織の業務 Agent を作るので、Agent 管理（`agent.admin`）か system_admin の利用者で実行する。
- 名前で既存の業務 Agent を探し、無ければ作る。あれば定義（版の項目と有効 / 無効）と違う項目だけ
  下書きを直し、公開していない変更があるときだけ公開する。同じ命令を何度実行してもよい（変更が
  無ければ GET だけ）。同名が複数ある・組み込み・個人の業務 Agent・移行が要るときは、変更せずに
  止める。

ログイン ID とパスワードは対話で入力する（自動実行では `SUITE_TEST_LOGIN_USER` /
`SUITE_TEST_PASSWORD`）。結果（業務 Agent の ID と公開中の版）は 1 行の JSON で標準出力に出し、
`--state` を渡せばそこにも保存する。認証情報・接続先は出さない。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import ssl
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.features.agent.data_scope import RAG_CONNECTION_ID, AgentDataScope
from app.features.agent.runtime import AGENT_VERSIONED_FIELDS, AgentProfile

# 定義の項目（版の項目と有効 / 無効）。データの範囲は定義に書かず、プロファイルの ID から作る。
DEFINITION_FIELDS: tuple[str, ...] = (*AGENT_VERSIONED_FIELDS, "enabled")
INPUT_FIELDS = frozenset(DEFINITION_FIELDS) - {"data_scopes"}
DEFAULT_DEFINITIONS = Path(__file__).resolve().parents[3] / "examples/test-pdfs-ja/agents.json"
PUBLISH_NOTE = "合成資料の検索・回答の確認用"
# グループ・名前の区分（RAG の命令と同じ形）。
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,32}")
NAME_PREFIX = "テスト_"
ADMIN_PERMISSION = "agent.admin"
# 利用者向けの文（画面と同じ検証の理由）を返す status。5xx の本文は出さない。
_CLIENT_ERRORS = frozenset({400, 403, 404, 409, 422})


class ProvisionError(RuntimeError):
    """値や応答の本文を漏らさず、止めた理由を説明する。"""


class AgentApi:
    """Cookie / CSRF で Agent の管理 API を呼ぶ。変更の自動再送はしない。"""

    def __init__(self, client: httpx.Client, base: str) -> None:
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProvisionError("API URL は http / https の絶対 URL にしてください。")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ProvisionError("API URL に認証情報・質問文字列・fragment は指定できません。")
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
            raise ProvisionError(
                "Agent の管理 API の通信が失敗しました。状態を確認して再実行してください。"
            ) from exc
        if response.is_error or response.is_redirect:
            raise ProvisionError(_error_message(response))
        try:
            result = response.json()
            if result.get("error_messages") or result.get("warning_messages"):
                raise ProvisionError("Agent の管理 API がエラーまたは警告を返しました。")
            return result["data"]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProvisionError("Agent の管理 API の応答形式を確認してください。") from exc


def _error_message(response: httpx.Response) -> str:
    """失敗の status と、検証の理由（画面に出す文。4xx だけ）。"""
    message = f"Agent の管理 API が HTTP {response.status_code} を返しました。"
    if response.status_code not in _CLIENT_ERRORS:
        return message
    try:
        reasons = response.json().get("error_messages") or []
    except (ValueError, AttributeError):
        return message
    if reasons and isinstance(reasons[0], str):
        return f"{message}{reasons[0][:300]}"
    return message


def read_definitions(path: Path) -> dict[str, dict[str, Any]]:
    """業務 Agent の定義（グループ → 項目）。形が違えば API を呼ぶ前に止める。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProvisionError("業務 Agent の定義の版・形式を確認してください。")
    agents = data.get("agents")
    if data.get("version") != 1 or not isinstance(agents, dict) or not agents:
        raise ProvisionError("業務 Agent の定義の版・形式を確認してください。")
    for key, definition in agents.items():
        if not KEY_PATTERN.fullmatch(key) or not isinstance(definition, dict):
            raise ProvisionError("業務 Agent の定義のグループを確認してください。")
        unknown = sorted(set(definition) - INPUT_FIELDS)
        if unknown:
            raise ProvisionError(
                f"業務 Agent の定義に使えない項目があります: {', '.join(unknown)}"
                "（データの範囲はプロファイルの ID から作ります）。"
            )
        try:
            AgentProfile.model_validate(definition)
        except ValidationError as exc:
            raise ProvisionError(
                f"業務 Agent の定義（グループ {key}）を確認してください。"
            ) from exc
    return agents


def _scope(profile_ids: object, default_profile_id: object) -> AgentDataScope:
    if (
        not isinstance(profile_ids, list)
        or not profile_ids
        or not all(isinstance(item, str) and item.strip() for item in profile_ids)
        or not isinstance(default_profile_id, str)
    ):
        raise ProvisionError("検索・回答プロファイルの ID を確認してください。")
    try:
        return AgentDataScope(profile_ids=profile_ids, default_profile_id=default_profile_id)
    except ValidationError as exc:
        raise ProvisionError("検索・回答プロファイルの ID と既定を確認してください。") from exc


def read_rag_state(path: Path) -> tuple[dict[str, AgentDataScope], str]:
    """RAG の命令の state から、グループごとのプロファイルの ID と名前の区分を読む。"""
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("complete") is not True:
        raise ProvisionError(
            "RAG の再構築が完了していません（state の complete が true ではありません）。"
            "先に RAG の命令を完了してください。"
        )
    groups = state.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise ProvisionError("RAG の state にグループがありません。")
    scopes = {
        str(key): _scope(row.get("profile_ids"), row.get("default_profile_id"))
        for key, row in groups.items()
        if isinstance(row, dict)
    }
    if len(scopes) != len(groups):
        raise ProvisionError("RAG の state のグループを確認してください。")
    namespace = state.get("namespace", "")
    if not isinstance(namespace, str):
        raise ProvisionError("RAG の state の名前の区分を確認してください。")
    return scopes, namespace


def parse_profiles(values: Sequence[str]) -> dict[str, AgentDataScope]:
    """`<グループ>=<プロファイルの ID>` の並び（グループごとに 1 件。既定も同じ）。"""
    scopes: dict[str, AgentDataScope] = {}
    for value in values:
        key, separator, profile_id = value.partition("=")
        if not separator or not KEY_PATTERN.fullmatch(key) or key in scopes:
            raise ProvisionError(
                "--profile は <グループ>=<ID> の形で、グループごとに 1 回にしてください。"
            )
        scopes[key] = _scope([profile_id.strip()], profile_id.strip())
    return scopes


def desired_agent(
    definition: Mapping[str, Any], scope: AgentDataScope, namespace: str
) -> dict[str, Any]:
    """定義とデータの範囲から、目標の項目（API の JSON と同じ形）を作る。"""
    name = str(definition["name"])
    if namespace:
        name = f"{NAME_PREFIX}{namespace}_{name.removeprefix(NAME_PREFIX)}"
    profile = AgentProfile.model_validate(
        {**definition, "name": name, "data_scopes": {RAG_CONNECTION_ID: scope.model_dump()}}
    )
    return profile.model_dump(mode="json", include=set(DEFINITION_FIELDS))


def _differences(actual: Mapping[str, Any], target: Mapping[str, Any]) -> list[str]:
    return [field for field in DEFINITION_FIELDS if actual.get(field) != target[field]]


def _published(agent: Mapping[str, Any]) -> Mapping[str, Any] | None:
    version = agent.get("published_version")
    return next(
        (item for item in agent.get("versions") or [] if item.get("version") == version), None
    )


def build_targets(
    definitions: Mapping[str, Mapping[str, Any]],
    scopes: Mapping[str, AgentDataScope],
    namespace: str,
) -> dict[str, dict[str, Any]]:
    """グループごとの目標の項目。足りない・形の違う入力は、API を呼ぶ前に止める。"""
    if namespace and not KEY_PATTERN.fullmatch(namespace):
        raise ProvisionError("名前の区分は英数字・_・- の 32 字以内にしてください。")
    missing = sorted(set(definitions) - set(scopes))
    if missing:
        raise ProvisionError(
            f"検索・回答プロファイルの ID が無いグループがあります: {', '.join(missing)}"
        )
    return {
        key: desired_agent(definitions[key], scopes[key], namespace) for key in sorted(definitions)
    }


class Provisioner:
    """名前で照合し、作成・下書きの修正・公開を、要るときだけ管理 API で行う（冪等）。"""

    def __init__(self, api: AgentApi, *, log: Callable[[str], None] = print) -> None:
        self.api, self.log = api, log

    def require_admin(self) -> None:
        """組織の業務 Agent を作るので、Agent 管理か system_admin を求める（個人の業務 Agent に
        しない）。"""
        me = self.api.call("GET", "/auth/me")
        if not me.get("is_system_admin") and ADMIN_PERMISSION not in (me.get("permissions") or []):
            raise ProvisionError(
                "Agent 管理（agent.admin）か system_admin の利用者で実行してください"
                "（組織の業務 Agent を作るため）。"
            )

    def run(self, targets: Mapping[str, Mapping[str, Any]], *, namespace: str) -> dict[str, Any]:
        """`targets` は `build_targets` の結果（グループ → 目標の項目）。"""
        self.require_admin()
        result: dict[str, Any] = {"namespace": namespace, "groups": {}}
        for key, target in targets.items():
            agent = self._ensure(key, target)
            if agent.get("unpublished_changes", True):
                agent = self.api.call(
                    "POST", f"/agents/{agent['id']}/publish", json={"note": PUBLISH_NOTE}
                )
                self.log(
                    f"業務 Agent を公開しました（グループ {key}・v{agent['published_version']}）。"
                )
            published = _published(agent)
            if (
                published is None
                or _differences(agent, target)
                or any(published.get(field) != target[field] for field in AGENT_VERSIONED_FIELDS)
            ):
                raise ProvisionError(
                    f"業務 Agent（グループ {key}）の公開中の版が定義と異なります。公開の状態を"
                    "確認してください。"
                )
            result["groups"][key] = {
                "agent_id": agent["id"],
                "published_version": agent["published_version"],
            }
        return result

    def _ensure(self, key: str, target: Mapping[str, Any]) -> dict[str, Any]:
        """無ければ作り、あれば下書きの違う項目だけを直す（同じなら書かない）。"""
        agents = self.api.call("GET", "/agents")["agents"]
        found = [agent for agent in agents if agent.get("name") == target["name"]]
        if len(found) > 1:
            raise ProvisionError(
                f"同じ名前の業務 Agent が複数あります（グループ {key}）。自動では選びません。"
            )
        if not found:
            # ID は送らない（backend が `agent_<uuid>` を作る。画面の作成と同じ下書きから）。
            created = self.api.call("POST", "/agents", json=dict(target))
            if created.get("owner_user_uuid"):
                raise ProvisionError(
                    f"業務 Agent（グループ {key}）が個人の業務 Agent として作られました。"
                    "画面で削除し、Agent 管理の利用者で実行し直してください。"
                )
            self.log(f"業務 Agent を作成しました（グループ {key}）。")
            return dict(created)
        agent = found[0]
        if (
            agent.get("source") != "runtime"
            or agent.get("owner_user_uuid")
            or agent.get("migration_required")
        ):
            raise ProvisionError(
                f"同名の業務 Agent（グループ {key}）が組み込み・個人の業務 Agent か、移行が要る"
                "状態です。変更せずに停止します。"
            )
        changes = _differences(agent, target)
        if not changes:
            return dict(agent)
        patched = self.api.call(
            "PATCH", f"/agents/{agent['id']}", json={field: target[field] for field in changes}
        )
        self.log(
            f"業務 Agent の下書きを定義に合わせました（グループ {key}: {', '.join(changes)}）。"
        )
        return dict(patched)


def _stable_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _save_state(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def main(argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--agent-api", required=True, help="Agent の /api までの URL")
    parser.add_argument(
        "--definitions",
        type=Path,
        default=DEFAULT_DEFINITIONS,
        help="業務 Agent の定義（既定は agent/examples/test-pdfs-ja/agents.json）",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--rag-state", type=Path, help="RAG の命令の --state のファイル")
    source.add_argument(
        "--profile",
        action="append",
        metavar="GROUP=ID",
        help="グループの検索・回答プロファイルの ID（グループごとに 1 回）",
    )
    parser.add_argument(
        "--namespace", default="", help="名前の区分（--profile のときだけ。state は state の区分）"
    )
    parser.add_argument("--state", type=Path, help="結果（業務 Agent の ID と公開中の版）の保存先")
    parser.add_argument("--ca-file", help="私設 CA の PEM ファイル")
    args = parser.parse_args(argv)
    try:
        definitions = read_definitions(args.definitions)
        if args.rag_state is not None:
            if args.namespace:
                raise ProvisionError("--rag-state のときは state の名前の区分を使います。")
            scopes, namespace = read_rag_state(args.rag_state)
        else:
            scopes, namespace = parse_profiles(args.profile), args.namespace
        targets = build_targets(definitions, scopes, namespace)
        user = os.environ.get("SUITE_TEST_LOGIN_USER") or input("ログイン ID: ")
        password = os.environ.get("SUITE_TEST_PASSWORD") or getpass.getpass("パスワード: ")
        options: dict[str, Any] = (
            {"transport": transport}
            if transport is not None
            else {"verify": ssl.create_default_context(cafile=args.ca_file)}
        )
        with httpx.Client(timeout=120, follow_redirects=False, **options) as client:
            api = AgentApi(client, args.agent_api)
            api.call("POST", "/auth/login", json={"login_user_id": user, "password": password})
            result = Provisioner(
                api,
                log=lambda message: print(message, file=sys.stderr),  # noqa: T201
            ).run(targets, namespace=namespace)
        if args.state is not None:
            _save_state(args.state, result)
    except ProvisionError as exc:
        print(_stable_json({"error": {"message": str(exc)}, "ok": False}))  # noqa: T201
        return 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        message = f"定義・state のファイルを確認してください（{type(exc).__name__}）。"
        print(_stable_json({"error": {"message": message}, "ok": False}))  # noqa: T201
        return 1
    print(_stable_json({"data": result, "ok": True}))  # noqa: T201
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI boundary
    raise SystemExit(main())
