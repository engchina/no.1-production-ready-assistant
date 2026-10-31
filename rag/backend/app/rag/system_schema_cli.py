"""RAG system schema の status / check / initialize / recreate / delete-orphans CLI。

`check` は配備の最後の確認（platform/deploy/suite-health.sh。#1648）が使う。DDL を実行せずに
状態を読み、ready なら 0、そうでなければ（読めないときも）1 で終わり、1 行の要約を標準出力に
出す。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence

from pr_backend_core.logging import configure_cli_logging
from pr_backend_core.oracle_dsn import enable_cli_connect_mode
from pr_system_settings.system_schema import (
    deployment_check_error_summary,
    deployment_check_summary,
    operation_failure_fields,
)

from app.rag.request_context import with_system_access
from app.rag.system_schema import (
    DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED,
    RECREATE_CONFIRMATION,
    SystemSchemaError,
    system_schema_manager,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAG Oracle system schema を管理します。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="DDL を実行せず現在状態を表示します。")
    subparsers.add_parser(
        "check",
        help="DDL を実行せず、ready なら 0、そうでなければ 1 で終わります（配備の確認）。",
    )
    initialize = subparsers.add_parser("initialize", help="不足 object を作成・更新します。")
    # データを消す未適用の migration（status の pending_destructive_migrations）は、
    # これが無ければ適用せずに止める（#619）。先に書き出し・バックアップを確かめてから付ける。
    initialize.add_argument(
        "--allow-destructive",
        action="store_true",
        help=(
            "データを削除する未適用の migration（status の pending_destructive_migrations）の"
            "適用を承認します。削除したデータは元に戻せないため、先に書き出してください。"
        ),
    )
    recreate = subparsers.add_parser(
        "recreate",
        help="管理対象 RAG object を削除して再作成します。",
    )
    recreate.add_argument(
        "--confirmation",
        required=True,
        help=f"確認値: {RECREATE_CONFIRMATION}",
    )
    # 参照先のない行の削除（#511）。status の orphaned_foreign_keys で件数を確認してから実行する。
    delete_orphans = subparsers.add_parser(
        "delete-orphans",
        help=(
            "外部キーの参照先のない行を削除し、外部キーを検査済み（VALIDATE）にします。"
            "削除した行は復元できません。"
        ),
    )
    delete_orphans.add_argument(
        "--constraint",
        required=True,
        help="外部キーの名前（status の orphaned_foreign_keys の name）。",
    )
    delete_orphans.add_argument(
        "--expected-rows",
        required=True,
        type=int,
        help=(
            "status で確認した参照先のない行の件数。実行時の件数がこれより多いときは削除しません。"
        ),
    )
    return parser


@with_system_access
def main(argv: Sequence[str] | None = None) -> int:
    configure_cli_logging("rag")
    # DB の停止中・届かないときに、Wallet の別名の TNS の再試行（retry_count=20）で 1〜2 分待たずに
    # 失敗を返す（配備が retry する。#1779）。常駐の server の共有 pool は変えない。
    enable_cli_connect_mode()
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return _check()
    try:
        if args.command == "status":
            result = system_schema_manager.status()
        elif args.command == "initialize":
            result = system_schema_manager.initialize(allow_destructive=args.allow_destructive)
        elif args.command == "delete-orphans":
            if args.expected_rows < 0:
                raise SystemSchemaError(
                    "SCHEMA_ORPHAN_ROWS_INVALID",
                    "--expected-rows には 0 以上の件数を指定してください。",
                    status_code=422,
                )
            result = system_schema_manager.delete_orphaned_rows(
                constraint_name=args.constraint,
                expected_orphan_rows=args.expected_rows,
            )
        else:
            result = system_schema_manager.initialize(
                recreate=True,
                confirmation=args.confirmation,
            )
    except SystemSchemaError as exc:
        error = {"error_code": exc.code, "error_message": exc.public_message}
        if exc.code == DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED:
            error["hint"] = (
                "status の pending_destructive_migrations で削除されるデータを確認し、必要な"
                "データを書き出してから、initialize --allow-destructive で再実行してください。"
            )
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    except Exception as exc:
        # 接続・権限の失敗（lease を取る前の制御テーブルの作成など）。SQL や資格情報は出さず、
        # ORA コードだけを出す（配備の log から原因を辿れるように。#1648）。ORA でない失敗
        # （設定の読み込みなど）は、secret を含まない手がかりを出す（#1701）。DB に接続できない
        # ときは、pool の時間切れでも本当の原因（ADB の停止・password の誤りなど）と対処を出す
        # （#1744）。
        fields = operation_failure_fields(exc, connect_kwargs=_connect_kwargs)
        error = {
            "error_code": fields.pop("code"),
            "error_message": f"システムテーブル操作に失敗しました（{type(exc).__name__}）。",
            **fields,
        }
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _check() -> int:
    try:
        ok, summary = deployment_check_summary(system_schema_manager.status())
    except Exception as exc:
        print(deployment_check_error_summary(exc, connect_kwargs=_connect_kwargs))
        return 1
    print(summary)
    return 0 if ok else 1


def _connect_kwargs() -> Mapping[str, object]:
    """接続の失敗の診断の直接の接続の引数（CLI の共有 pool と同じ。資格情報を含むため出さない）。"""
    from app.clients.oracle import shared_pool_connect_kwargs

    return shared_pool_connect_kwargs()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
