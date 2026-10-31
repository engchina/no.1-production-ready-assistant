"""Agent のシステムテーブルの状態・作成 / 更新・全再作成の CLI（#751。NL2SQL と同じ形）。

    uv run python -m app.cli.agent_system_schema --status
    uv run python -m app.cli.agent_system_schema --check
    uv run python -m app.cli.agent_system_schema --initialize [--allow-destructive]
    uv run python -m app.cli.agent_system_schema --recreate \
        --confirmation RECREATE_AGENT_SYSTEM_TABLES

共通 `.env`（platform/.env）の `PLATFORM_ORACLE_*` で接続する。作成・更新は、3 製品共通の
`PLATFORM_*` と組み込み SYSTEM_ADMIN ロールを先に冪等に用意してから、`AGENT_*` の不足分と
未適用の migration を当てる。データを消す migration は `--allow-destructive` が無ければ止める
（画面の確認ダイアログと同じ。#619）。何度実行してもよい。ユーザーは作らない。
保存済みのデータを書き換えたとき（出力の `rewritten_saved_data`）は、動いている backend のメモリが
古いままなので、出力の `restart_required` のプロセスを再起動する（#1844）。

`--check` は配備の最後の確認（platform/deploy/suite-health.sh。#1648）が使う。DDL を実行せずに状態を
読み、ready なら 0、そうでなければ（読めないときも）1 で終わり、1 行の要約を標準出力に出す。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pr_backend_core.logging import configure_cli_logging
from pr_backend_core.oracle_dsn import apply_cli_connect_mode, enable_cli_connect_mode
from pr_system_settings.system_schema import (
    deployment_check_error_summary,
    deployment_check_summary,
    operation_failure_fields,
)

from app.system_schema import SystemSchemaError, system_schema_manager


def _stable_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main(argv: Sequence[str] | None = None) -> int:
    configure_cli_logging("agent")
    # DB の停止中・届かないときに、接続記述子の TNS の再試行で長く待たずに失敗を返す（配備が retry
    # する。#1779）。常駐の server の pool は変えない。
    enable_cli_connect_mode()
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--status", action="store_true", help="状態だけを読む（DDL なし）")
    actions.add_argument(
        "--check", action="store_true", help="ready なら 0、それ以外は 1 で終わる（配備の確認）"
    )
    actions.add_argument("--initialize", action="store_true", help="作成・更新する")
    actions.add_argument("--recreate", action="store_true", help="管理対象を削除して作り直す")
    parser.add_argument("--confirmation", default=None, help="--recreate の確認語")
    parser.add_argument(
        "--allow-destructive",
        action="store_true",
        help="データを消す未適用の migration を当てることを承認する",
    )
    args = parser.parse_args(argv)
    if args.check:
        return _check()

    try:
        if args.status:
            result = system_schema_manager.status()
        else:
            result = system_schema_manager.initialize(
                recreate=bool(args.recreate),
                confirmation=args.confirmation,
                allow_destructive=bool(args.allow_destructive),
            )
    except SystemSchemaError as exc:
        print(  # noqa: T201
            _stable_json({"error": {"code": exc.code, "message": exc.public_message}, "ok": False})
        )
        return 2
    except Exception as exc:  # 接続・DDL の失敗は SQL や資格情報を出さずに非 0 で終わる
        # 権限の不足などは ORA コードを出す（配備の log から辿れる。#1648）。ORA でない失敗
        # （設定の読み込みなど）は secret を含まない手がかりを出す（#1701）。DB に接続できない
        # ときは、pool の時間切れでも本当の原因（ADB の停止・password の誤りなど）と対処を出す
        # （#1744）。
        error: dict[str, Any] = {
            "message": f"システムテーブル操作に失敗しました（{type(exc).__name__}）。",
            **operation_failure_fields(exc, connect_kwargs=_connect_kwargs),
        }
        print(_stable_json({"error": error, "ok": False}))  # noqa: T201
        return 1
    if not args.status and result.get("rewritten_saved_data"):
        result = {**result, "restart_required": _restart_required(result.get("restart_required"))}
    print(_stable_json({"data": result, "ok": True}))  # noqa: T201
    return 0


def _restart_required(current: object) -> dict[str, object] | None:
    """保存済みのデータを書き換えたときの再起動の案内に、動いている backend を加える（#1844）。

    CLI は別のプロセスなので、動いている backend のメモリを読み直せない（配備の
    `init_script.sh` は CLI の後に backend を再起動する）。
    """
    from app.features.agent.storage_bootstrap import BACKEND_PROCESS, SavedDataReload

    processes = [BACKEND_PROCESS]
    if isinstance(current, Mapping):
        processes += [str(item) for item in current.get("processes") or [] if item not in processes]
    return SavedDataReload(restart_processes=tuple(processes)).restart_required()


def _check() -> int:
    try:
        ok, summary = deployment_check_summary(system_schema_manager.status())
    except Exception as exc:
        print(deployment_check_error_summary(exc, connect_kwargs=_connect_kwargs))  # noqa: T201
        return 1
    print(summary)  # noqa: T201
    return 0 if ok else 1


def _connect_kwargs() -> Mapping[str, object]:
    """接続の失敗の診断の直接の接続の引数（CLI の共通の pool と同じ）。

    資格情報を含むため出さない。
    """
    from app.oracle_connection import platform_oracle_connect_kwargs
    from app.settings import get_settings

    return apply_cli_connect_mode(platform_oracle_connect_kwargs(get_settings()))


if __name__ == "__main__":  # pragma: no cover - CLI boundary
    raise SystemExit(main())
