"""DB 停止/応答不良時に閲覧系 API を縮退表示するための共通ヘルパー。

ドキュメント一覧・取込ジョブ・ナレッジベース一覧など
「読み取り専用の一覧/集計」系エンドポイントを、DB が起動していなくても
500 を返さず空データ + warning で正常応答させるために使う。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import HTTPException
from pydantic import ValidationError

logger = logging.getLogger(__name__)

# 既定の縮退メッセージ。{timeout:g} で待機秒数を埋め込む。
DB_TIMEOUT_MESSAGE = (
    "データベースが {timeout:g} 秒以内に応答しませんでした。"
    "データベースの起動状態を確認して再試行してください。"
)
DB_ERROR_MESSAGE = (
    "データベースに接続できませんでした。データベースの起動状態を確認して再試行してください。"
)


@dataclass(frozen=True)
class DegradedRead:
    """縮退発生時の状況。warning メッセージと readiness check 用ステータスを持つ。"""

    message: str
    status: str  # "timeout" | "error"


# 縮退させずにそのまま投げる例外。意図した HTTP 応答と、DB の障害ではなく
# プログラムの誤りを示すもの。
# Oracle client は未設定などの DB 側の問題を RuntimeError でも投げるため、DB の例外だけに絞らない。
_NOT_DEGRADED_ERRORS: tuple[type[BaseException], ...] = (
    HTTPException,
    ValidationError,
    TypeError,
    AttributeError,
    NameError,
    AssertionError,
)


async def load_or_degrade[T](
    loader: Callable[[], Awaitable[T]],
    *,
    timeout_seconds: float,
    fallback: T,
    log_label: str,
    timeout_message: str = DB_TIMEOUT_MESSAGE,
    error_message: str = DB_ERROR_MESSAGE,
) -> tuple[T, DegradedRead | None]:
    """loader を timeout 付きで実行し、DB 不通時は fallback と縮退情報を返す。

    - 正常時: ``(値, None)``
    - timeout: ``(fallback, DegradedRead(..., "timeout"))``
    - その他例外: ``(fallback, DegradedRead(..., "error"))``

    例外を握りつぶして fallback を返すため、呼び出し側はステータス 200 のまま
    warning を添えて応答できる。
    """
    try:
        value = await asyncio.wait_for(loader(), timeout=timeout_seconds)
        return value, None
    except TimeoutError:
        logger.warning("%s_timeout", log_label, extra={"timeout_seconds": timeout_seconds})
        return fallback, DegradedRead(timeout_message.format(timeout=timeout_seconds), "timeout")
    except _NOT_DEGRADED_ERRORS:
        # 404 などの意図した応答と、プログラムの誤りは「DB に接続できない」にしない（#476）。
        raise
    except Exception as exc:  # noqa: BLE001 - DB 不通を縮退に正規化する境界
        logger.exception(
            "%s_failed",
            log_label,
            extra={"exception_type": type(exc).__name__},
        )
        return fallback, DegradedRead(error_message, "error")
