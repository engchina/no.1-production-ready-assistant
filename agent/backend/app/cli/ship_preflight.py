"""Agent と RAG / NL2SQL の連携の出荷前点検（#64）。

    uv run python -m app.cli.ship_preflight                    # 構成と稼働中のサービスを読むだけ
    uv run python -m app.cli.ship_preflight --as-user <uuid>   # 実在の利用者で MCP の契約も確かめる
    uv run python -m app.cli.ship_preflight --run-stub-tests   # 障害注入のスタブ検証も実行する
    uv run python -m app.cli.ship_preflight --json             # 結果を JSON で出す

稼働中の配備の設定を読むときは、その `.env` を指す（例: `PLATFORM_ENV_FILE=<repo>/platform/.env
AGENT_ENV_FILE=<repo>/agent/backend/.env`）。Agent の backend と同じ解決（共通 `.env` → 製品の
`.env` → 環境変数）で設定を読み、その値で RAG / NL2SQL を呼ぶ。

点検は読み取りだけで、DB への書き込み・worker の起動・Run の回復をしない（本番の bootstrap を
import しない）。RAG / NL2SQL へは `initialize` / `tools/list` だけを送り、`tools/call` は送らない。

項目ごとの状態:

- pass: 確かめた。blocker: 出荷を止める。warning: 出荷できるが理由を記録する。
- not_applicable: この構成では当てはまらない。not_run: 実行していない（証拠が無いので pass に
  しない）。manual: 人の演練・確認が要る（runbook の手順）。error: 点検自体が失敗した。

判定は blocker / error があれば NOT_READY（終了コード 1）、not_run があれば INCOMPLETE（2）、
それ以外は READY（0）。READY でも manual の項目は人が確かめる。単一のコマンドの成功は、
ここで確かめた範囲を超える本番の保証ではない。手順と保証の対応は
agent/docs/agent-rag-ship-preflight.md。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess  # nosec B404 - 固定の引数で git / pytest を呼ぶだけ
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from pr_backend_core.config import product_env_file
from pr_system_settings.auth.service_token import (
    SERVICE_TOKEN_MIN_SECRET_LENGTH,
    issue_service_token,
)
from pr_system_settings.database import database_readiness

from app.settings import BACKEND_DIR, PLATFORM_ENV_FILE, Settings, get_settings

PASS = "pass"  # nosec B105 - 点検の状態の名前（秘密ではない）
BLOCKER = "blocker"
WARNING = "warning"
NOT_APPLICABLE = "not_applicable"
NOT_RUN = "not_run"
MANUAL = "manual"
ERROR = "error"

RUNBOOK = "agent/docs/agent-rag-ship-preflight.md"
CONTRACTS_DIR = BACKEND_DIR.parents[1] / "platform/contracts/mcp"
# 障害注入のスタブ検証（CI でも実行する）。
STUB_TESTS = (
    "tests/test_rag_fault_injection.py",
    "tests/test_tool_call_boundary.py",
    "tests/test_untrusted_content_authority.py",
    "tests/test_run_claim_fencing.py",
    "tests/test_interrupted_runs.py",
)
# 存在しない利用者（署名の鍵の一致と、未知の主体を通さないことの確認に使う。実在の ID ではない）。
SYNTHETIC_SUBJECT = "00000000-0000-4000-8000-000000000064"
HTTP_TIMEOUT_SECONDS = 5.0
_PYTEST_SUMMARY = re.compile(r"\d+ (passed|failed|error)")


@dataclass
class Check:
    area: str
    name: str
    status: str
    detail: str
    remedy: str = ""
    evidence: str = ""


@dataclass
class Report:
    commit: str
    config_fingerprint: str
    env_files: list[str]
    checked_at: str
    checks: list[Check] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        statuses = {check.status for check in self.checks}
        if statuses & {BLOCKER, ERROR}:
            return "NOT_READY"
        if NOT_RUN in statuses:
            return "INCOMPLETE"
        return "READY"


Http = Callable[..., httpx.Response]


def _products(settings: Settings) -> dict[str, str]:
    return {
        "rag": (settings.agent_external_rag_mcp_url or "").strip(),
        "nl2sql": (settings.agent_external_nl2sql_mcp_url or "").strip(),
    }


def _health_url(mcp_url: str, path: str) -> str:
    # `http://host:port/api/mcp` → `http://host:port/api/<path>`。
    return f"{mcp_url.rstrip('/').removesuffix('/mcp')}/{path}"


def check_config(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    if settings.local_debug_enabled:
        checks.append(
            Check(
                "config",
                "auth_mode",
                BLOCKER,
                "PLATFORM_ENVIRONMENT=local かつ PLATFORM_AUTH_MODE=local: RAG / NL2SQL の MCP は"
                " サービストークンを検査せず、全権限のローカル利用者として動く。",
                "PLATFORM_AUTH_MODE=authenticated にして 3 製品を再起動する。",
            )
        )
    else:
        checks.append(Check("config", "auth_mode", PASS, f"auth_mode={settings.auth_mode}"))
    if settings.is_deployed:
        checks.append(Check("config", "environment", PASS, f"environment={settings.environment}"))
    else:
        checks.append(
            Check(
                "config",
                "environment",
                WARNING,
                f"environment={settings.environment}: 配備用の安全検査（is_deployed）の対象外。",
                "検証環境・本番は PLATFORM_ENVIRONMENT=staging / production にする。",
            )
        )
    secret = settings.app_service_token_secret or ""
    if len(secret) < SERVICE_TOKEN_MIN_SECRET_LENGTH:
        checks.append(
            Check(
                "config",
                "service_token_secret",
                BLOCKER,
                f"PLATFORM_SERVICE_TOKEN_SECRET が {SERVICE_TOKEN_MIN_SECRET_LENGTH} 文字未満か"
                "未設定。",
                "platform/.env に 3 製品で同じ値を設定する（値はログ・PR に書かない）。",
            )
        )
    else:
        checks.append(
            Check("config", "service_token_secret", PASS, f"設定あり（{len(secret)} 文字）")
        )
    for product, url in _products(settings).items():
        if url:
            checks.append(Check("config", f"{product}_mcp_url", PASS, url))
        else:
            checks.append(
                Check(
                    "config",
                    f"{product}_mcp_url",
                    BLOCKER,
                    f"AGENT_EXTERNAL_{product.upper()}_MCP_URL が未設定（画面の保存値に依存し、"
                    "配備の値として固定されない）。",
                    "agent/backend/.env に URL を設定し、Agent を再起動する。",
                )
            )
    for name, enabled in (
        ("tool_grounding_required", settings.agent_tool_grounding_required),
        ("final_validation_enabled", settings.agent_final_validation_enabled),
    ):
        checks.append(
            Check(
                "config",
                name,
                PASS if enabled else WARNING,
                f"AGENT_{name.upper()}={enabled}",
                "" if enabled else "無効にした理由と代わりの確認を出荷の記録に残す。",
            )
        )
    backend = settings.agent_runtime_repository_backend.strip().lower() or "auto"
    if backend == "auto":
        backend = "oracle_checkpoint" if database_readiness(settings) == "ok" else "memory"
    if backend in {"memory", "in_memory"}:
        checks.append(
            Check(
                "config",
                "run_store",
                BLOCKER,
                "Run・承認の保存先が memory: 再起動で承認待ち・取消の記録が消え、停止と復帰の手順が"
                "成り立たない。",
                "PLATFORM_ORACLE_* を設定するか、"
                "AGENT_RUNTIME_REPOSITORY_BACKEND=oracle_checkpoint にする。",
            )
        )
    else:
        checks.append(
            Check(
                "config",
                "run_store",
                PASS,
                f"{backend}（dispatch={settings.agent_runtime_dispatch_mode}）",
            )
        )
    return checks


def _get(http: Http, area: str, name: str, url: str) -> Check:
    try:
        response = http("GET", url, timeout=HTTP_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        return Check(
            area,
            name,
            BLOCKER,
            f"{url} に接続できない（{type(exc).__name__}）。",
            "サービスが起動しているか（systemctl status production-ready-<製品>-backend /"
            " start-backend.sh）と URL を確かめる。",
        )
    if response.status_code == 200:
        return Check(area, name, PASS, f"{url} → 200")
    detail = f"{url} → {response.status_code}"
    try:
        checks = response.json().get("data", {}).get("checks") or {}
        failing = {key: value for key, value in checks.items() if value != "ok"}
        if failing:
            detail += f"（{', '.join(f'{k}={v}' for k, v in sorted(failing.items()))}）"
    except (ValueError, AttributeError):
        pass
    return Check(
        area,
        name,
        BLOCKER,
        detail,
        "/api/ready の checks の項目（DB・モデルの設定など）を直してから再点検する。",
    )


def check_services(settings: Settings, agent_url: str, http: Http) -> list[Check]:
    targets = {"agent": f"{agent_url.rstrip('/')}/api"}
    for product, url in _products(settings).items():
        if url:
            targets[product] = url.rstrip("/").removesuffix("/mcp")
    checks: list[Check] = []
    for path in ("health", "ready"):
        for product, base in targets.items():
            checks.append(_get(http, path, product, f"{base}/{path}"))
    return checks


def _mcp_post(http: Http, url: str, token: str | None) -> httpx.Response:
    headers = {"accept": "application/json, text/event-stream"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": "preflight", "method": "tools/list"}
    return http("POST", url, json=body, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)


def check_auth_boundary(settings: Settings, http: Http) -> list[Check]:
    """認証の境界を、稼働中の RAG / NL2SQL に拒否されるべき呼び出しを送って確かめる。

    - token なし・別の製品の audience の token は 401。
    - Agent の鍵で署名した、存在しない利用者の token は 403（鍵は一致し、未知の主体は通さない）。
      401 なら Agent と呼び先の鍵が食い違っている（Agent のすべての呼び出しが失敗する）。
    """
    secret = settings.app_service_token_secret or ""
    checks: list[Check] = []
    for product, url in _products(settings).items():
        if not url:
            checks.append(Check("auth", product, NOT_RUN, "MCP の URL が未設定。"))
            continue
        if len(secret) < SERVICE_TOKEN_MIN_SECRET_LENGTH:
            checks.append(Check("auth", product, NOT_RUN, "サービストークンの鍵が未設定。"))
            continue
        probes = {
            "no_token": (None, 401),
            "wrong_audience": (
                issue_service_token(
                    secret,
                    subject=SYNTHETIC_SUBJECT,
                    audience=f"{product}-other",
                    issuer="preflight",
                ),
                401,
            ),
            "unknown_subject": (
                issue_service_token(
                    secret, subject=SYNTHETIC_SUBJECT, audience=product, issuer="preflight"
                ),
                403,
            ),
        }
        for probe, (token, expected) in probes.items():
            name = f"{product}.{probe}"
            try:
                status = _mcp_post(http, url, token).status_code
            except httpx.HTTPError as exc:
                checks.append(
                    Check("auth", name, ERROR, f"{url} に接続できない（{type(exc).__name__}）。")
                )
                continue
            if status == expected:
                checks.append(Check("auth", name, PASS, f"{status}（期待どおり拒否）"))
            elif probe == "unknown_subject" and status == 401:
                checks.append(
                    Check(
                        "auth",
                        name,
                        BLOCKER,
                        f"Agent の鍵で署名した token を {product} が 401 で拒否した。",
                        "PLATFORM_SERVICE_TOKEN_SECRET が Agent と呼び先で一致しているか確かめ、"
                        "揃えてから両方を再起動する。",
                    )
                )
            else:
                checks.append(
                    Check(
                        "auth",
                        name,
                        BLOCKER,
                        f"期待は {expected}、実際は {status}"
                        "（拒否されるべき呼び出しが通った可能性）。",
                        "PLATFORM_AUTH_MODE と MCP の認証の設定を確かめる。",
                    )
                )
    return checks


def _contract_tool_names(product: str) -> set[str]:
    data = json.loads((CONTRACTS_DIR / f"{product}-tools.json").read_text(encoding="utf-8"))
    return {tool["name"] for tool in data["tools"]}


def check_contract(user_uuid: str | None) -> list[Check]:
    """実在の利用者として tools/list を呼び、契約のツールが公開されているかを確かめる。"""
    if not user_uuid:
        return [
            Check(
                "contract",
                "tools_list",
                NOT_RUN,
                "--as-user が無いので、MCP の契約（tools/list）を確かめていない。",
                "Agent を使う実在の利用者の user_uuid を --as-user に渡して再点検する。",
            )
        ]
    from app.features.agent.config import _product_connections
    from app.features.agent.tools import (
        ExternalToolError,
        McpConnectionClient,
        ToolInvocationContext,
    )

    checks: list[Check] = []
    context = ToolInvocationContext(user_uuid=user_uuid)
    for config in _product_connections():
        product = config.server_id
        if not config.base_url:
            checks.append(Check("contract", product, NOT_RUN, "MCP の URL が未設定。"))
            continue
        try:
            listed = McpConnectionClient(config, context=context).list_tools().tools
        except ExternalToolError as exc:
            checks.append(
                Check(
                    "contract",
                    product,
                    BLOCKER,
                    f"tools/list が失敗した（{exc.code}: {exc.message}）。",
                    "利用者が有効か、呼び先の権限・認証を確かめる。",
                )
            )
            continue
        names = {tool.name for tool in listed}
        # 一覧に出さない内部のツール（rag_readable_documents など）は Control Plane だけが呼ぶ。
        missing = sorted(_contract_tool_names(product) - names - {"rag_readable_documents"})
        if missing:
            checks.append(
                Check(
                    "contract",
                    product,
                    WARNING,
                    f"契約のツールが一覧に無い: {', '.join(missing)}"
                    "（利用者の権限で絞られた可能性）。",
                    "利用者の権限と呼び先の版（platform/contracts/mcp）を確かめる。",
                )
            )
        else:
            checks.append(
                Check("contract", product, PASS, f"{len(names)} 件（契約のツールがそろう）")
            )
    return checks


def check_stub_tests(run: bool) -> list[Check]:
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *STUB_TESTS]
    shown = "uv run pytest -q " + " ".join(STUB_TESTS)
    if not run:
        return [
            Check(
                "fault_injection",
                "stub_tests",
                NOT_RUN,
                "障害注入のスタブ検証を実行していない"
                "（CI の結果を証拠にするなら commit を照合する）。",
                f"--run-stub-tests を付けるか、agent/backend で {shown}",
            )
        ]
    completed = subprocess.run(  # nosec B603 - 固定の引数
        command, cwd=BACKEND_DIR, capture_output=True, text=True, check=False
    )
    outcome = [line for line in completed.stdout.splitlines() if _PYTEST_SUMMARY.search(line)]
    summary = outcome[-1].strip("= ") if outcome else f"pytest の終了コード {completed.returncode}"
    return [
        Check(
            "fault_injection",
            "stub_tests",
            PASS if completed.returncode == 0 else BLOCKER,
            summary,
            "" if completed.returncode == 0 else f"agent/backend で {shown} を実行して直す。",
            evidence=" ".join(STUB_TESTS),
        )
    ]


def check_operations() -> list[Check]:
    return [
        Check(
            "operations",
            "stop_and_recovery_drill",
            MANUAL,
            "受付停止・能力の禁止・実行の取消・全体停止と復帰を、隔離した環境で演練したか。",
            f"{RUNBOOK} の「停止と復帰」を実施し、日時・担当・結果を出荷の記録に残す。",
        ),
        Check(
            "operations",
            "approval_backlog_owner",
            MANUAL,
            "承認待ちの滞留の確認者と連絡経路が決まっているか（期限超過で自動承認しない）。",
            f"{RUNBOOK} の「承認の滞留」の役割を出荷の記録に書く。",
        ),
    ]


def _commit() -> str:
    try:
        return subprocess.run(  # nosec B603 B607 - 固定の引数
            ["git", "rev-parse", "HEAD"],
            cwd=BACKEND_DIR,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _fingerprint(settings: Settings) -> str:
    # 秘密の値を含めない、解決した構成の識別。
    values = {
        "environment": settings.environment,
        "auth_mode": settings.auth_mode,
        "rag_mcp_url": settings.agent_external_rag_mcp_url,
        "nl2sql_mcp_url": settings.agent_external_nl2sql_mcp_url,
        "repository_backend": settings.agent_runtime_repository_backend,
        "dispatch_mode": settings.agent_runtime_dispatch_mode,
        "max_retries": settings.agent_external_mcp_max_retries,
        "grounding_required": settings.agent_tool_grounding_required,
        "final_validation": settings.agent_final_validation_enabled,
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:12]


def run_preflight(
    settings: Settings,
    *,
    agent_url: str,
    as_user: str | None = None,
    run_stub_tests: bool = False,
    http: Http | None = None,
) -> Report:
    client = httpx.Client()
    request: Http = http or client.request
    report = Report(
        commit=_commit(),
        config_fingerprint=_fingerprint(settings),
        env_files=[str(PLATFORM_ENV_FILE), str(product_env_file("AGENT_", BACKEND_DIR))],
        checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    steps: list[Callable[[], list[Check]]] = [
        lambda: check_config(settings),
        lambda: check_services(settings, agent_url, request),
        lambda: check_auth_boundary(settings, request),
        lambda: check_contract(as_user),
        lambda: check_stub_tests(run_stub_tests),
        check_operations,
    ]
    try:
        for step in steps:
            try:
                report.checks.extend(step())
            except Exception as exc:  # 点検自体の失敗は error として残し、残りを続ける
                report.checks.append(Check("preflight", "internal", ERROR, repr(exc)))
    finally:
        client.close()
    return report


def render(report: Report) -> str:
    lines = [
        f"preflight: commit={report.commit} config={report.config_fingerprint}"
        f" checked_at={report.checked_at}",
        f"preflight: env_files={', '.join(report.env_files)}",
    ]
    for check in report.checks:
        line = f"[{check.status:<14}] {check.area}/{check.name}: {check.detail}"
        if check.remedy and check.status != PASS:
            line += f"\n                 修復: {check.remedy}"
        lines.append(line)
    counts: dict[str, int] = {}
    for check in report.checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    lines.append(f"verdict: {report.verdict}（{summary}）")
    return "\n".join(lines)


EXIT_CODES = {"READY": 0, "NOT_READY": 1, "INCOMPLETE": 2}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--agent-url", default="http://127.0.0.1:8120", help="Agent の backend の起点"
    )
    parser.add_argument("--as-user", default=None, help="契約の確認に使う実在の利用者の user_uuid")
    parser.add_argument(
        "--run-stub-tests", action="store_true", help="障害注入のスタブ検証を実行する"
    )
    parser.add_argument("--json", action="store_true", help="結果を JSON で出す")
    args = parser.parse_args(argv)
    report = run_preflight(
        get_settings(),
        agent_url=args.agent_url,
        as_user=args.as_user,
        run_stub_tests=args.run_stub_tests,
    )
    if args.json:
        payload: dict[str, Any] = {**asdict(report), "verdict": report.verdict}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return EXIT_CODES[report.verdict]


if __name__ == "__main__":
    raise SystemExit(main())
