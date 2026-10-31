"""NL2SQL system table の状態・確認・初期化・全再作成 CLI。

`--check` は配備の最後の確認（platform/deploy/suite-health.sh。#1648）が使う。DDL を実行せずに、
system table が ready で、`app_security_migrate --apply` が作る認証・権限の表がそろっているかを
読み、そろっていれば 0、そうでなければ（読めないときも）1 で終わり、1 行の要約を標準出力に出す。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from typing import Any

from pr_backend_core.logging import configure_cli_logging
from pr_backend_core.oracle_dsn import apply_cli_connect_mode, enable_cli_connect_mode
from pr_system_settings.system_schema import (
    deployment_check_error_summary,
    deployment_check_summary,
    operation_failure_fields,
)

from app.features.settings.system_schema import SystemSchemaError, system_schema_manager
from app.features.settings.system_schema_runtime import reset_system_schema_runtime


def _stable_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main() -> int:
    configure_cli_logging("nl2sql")
    # DB の停止中・届かないときに、接続記述子の TNS の再試行で長く待たずに失敗を返す（配備が retry
    # する。#1779）。常駐の server の pool は変えない。
    enable_cli_connect_mode()
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--status", action="store_true", help="read-only status")
    actions.add_argument(
        "--check",
        action="store_true",
        help="exit 0 only when system and security tables are ready (deployment check)",
    )
    actions.add_argument("--initialize", action="store_true", help="create or migrate")
    actions.add_argument("--recreate", action="store_true", help="drop core allowlist and recreate")
    parser.add_argument("--confirmation", default=None, help="required confirmation for --recreate")
    args = parser.parse_args()
    if args.check:
        return _check()

    try:
        if args.status:
            result = system_schema_manager.status()
        else:
            result = system_schema_manager.initialize(
                recreate=bool(args.recreate),
                confirmation=args.confirmation,
            )
            reset_system_schema_runtime(schema_epoch=int(result["operation_state"]["schema_epoch"]))
    except SystemSchemaError as exc:
        print(  # noqa: T201
            _stable_json(
                {
                    "error": {"code": exc.code, "message": exc.public_message},
                    "ok": False,
                }
            )
        )
        return 2
    except Exception as exc:
        # 権限の不足などは ORA コードを出す（配備の log から辿れる。#1648）。ORA でない失敗
        # （設定の読み込みなど）は secret を含まない手がかりを出す（#1701）。DB に接続できない
        # ときは、pool の時間切れでも本当の原因（ADB の停止・password の誤りなど）と対処を出す
        # （#1744）。
        error: dict[str, Any] = {
            "message": "システムテーブル操作に失敗しました。",
            **operation_failure_fields(exc, connect_kwargs=_connect_kwargs),
        }
        print(_stable_json({"error": error, "ok": False}))  # noqa: T201
        return 1

    print(_stable_json({"data": result, "ok": True}))  # noqa: T201
    return 0


def _check() -> int:
    from app.cli.app_security_migrate import missing_security_tables
    from app.clients.oracle_runtime import get_oracle_pool_manager

    try:
        ok, summary = deployment_check_summary(system_schema_manager.status())
        with get_oracle_pool_manager().control_connection() as connection:
            missing = missing_security_tables(connection)
    except Exception as exc:
        print(deployment_check_error_summary(exc, connect_kwargs=_connect_kwargs))  # noqa: T201
        return 1
    if missing:
        ok = False
        shown = ", ".join(missing[:5]) + (", …" if len(missing) > 5 else "")
        summary = (
            f"{summary}・認証・権限の表が不足 {len(missing)} 件（{shown}。"
            "app_security_migrate --apply --skip-bootstrap で作成）"
        )
    print(summary)  # noqa: T201
    return 0 if ok else 1


def _connect_kwargs() -> Mapping[str, object]:
    """接続の失敗の診断の直接の接続の引数（CLI の control の pool と同じ）。

    資格情報を含むため出さない。
    """
    from app.features.nl2sql.oracle_adapter import oracle_connect_kwargs
    from app.settings import get_settings

    return apply_cli_connect_mode(oracle_connect_kwargs(get_settings()))


if __name__ == "__main__":  # pragma: no cover - CLI boundary
    raise SystemExit(main())
