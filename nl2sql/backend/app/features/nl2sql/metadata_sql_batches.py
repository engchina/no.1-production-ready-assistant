"""コメント・アノテーション・ドメインの SQL 生成の分割と、fallback の理由（#1719）。

表・列が多いと 1 回の Structured Outputs の応答が大きくなり、推論のあるモデルは推論の token も
出力の上限に数えるため未完了（`incomplete` / `max_output_tokens`）になりやすい。選んだ表を、
表の数と列の数で区切った組に分けて呼び、1 回の出力を小さくする。

入力の文（構造・主キー・外部キー・サンプル）は、画面が `OBJECT: <OWNER.NAME>` で始まる塊を
空行で区切って作る（`nl2sql/frontend/src/features/nl2sql/metadataSql.ts`）。組ごとに、その組の
表の塊と、どの表にも当たらない塊だけを残す。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .enterprise_ai_client import (
    EnterpriseAiDirectError,
    enterprise_ai_configuration_failure_message,
)

# 1 回の呼び出しに入れる表の数と列の数の上限。2026-10-13 の実測（5 表・32 列のコメント）で、
# 応答の本文は列あたり 30〜50 token、推論は 1500〜2700 token（medium）。アノテーションは
# 列あたりの本文がコメントの 2〜3 倍になるため、列 100 で本文 15000 token 前後に収め、既定の
# 出力の上限（32768）に推論の余地を残す。
METADATA_SQL_BATCH_MAX_OBJECTS = 10
METADATA_SQL_BATCH_MAX_COLUMNS = 100
# 同時に呼ぶ組の数は設定 `NL2SQL_METADATA_SQL_CONCURRENCY`（既定 4。#2004）。
# 機能が要る出力の量の見積もり（推論の余地 + 列ごとの本文）。送る上限は共通の
# `enterprise_ai_output_token_budget` で既定（32768）を下回らず、モデルの上限で抑える。
METADATA_SQL_OUTPUT_TOKENS_BASE = 8192
METADATA_SQL_OUTPUT_TOKENS_PER_COLUMN = 200
METADATA_SQL_OUTPUT_TOKENS_PER_OBJECT = 400

OBJECT_PREFIX = "OBJECT:"


def plan_metadata_sql_batches(
    column_counts: Sequence[int],
    *,
    max_objects: int = METADATA_SQL_BATCH_MAX_OBJECTS,
    max_columns: int = METADATA_SQL_BATCH_MAX_COLUMNS,
) -> list[list[int]]:
    """表ごとの列の数から、組（表の index の並び）を作る。表の順は変えない。

    1 つの表の列が上限を超えても、その表は 1 つの組にする（表の途中では分けない）。
    """
    batches: list[list[int]] = []
    current: list[int] = []
    current_columns = 0
    for index, count in enumerate(column_counts):
        columns = max(0, int(count))
        if current and (
            len(current) >= max_objects or current_columns + columns > max(1, max_columns)
        ):
            batches.append(current)
            current, current_columns = [], 0
        current.append(index)
        current_columns += columns
    if current:
        batches.append(current)
    return batches


def metadata_sql_output_tokens(object_count: int, column_count: int) -> int:
    """組の出力の量の見積もり（`max_output_tokens` の要る量。共通の既定を下回らない）。"""
    return (
        METADATA_SQL_OUTPUT_TOKENS_BASE
        + METADATA_SQL_OUTPUT_TOKENS_PER_OBJECT * max(0, object_count)
        + METADATA_SQL_OUTPUT_TOKENS_PER_COLUMN * max(0, column_count)
    )


def split_object_blocks(text: str) -> list[tuple[str, str]]:
    """入力の文を空行で区切った塊にし、(`OBJECT:` の名前（無ければ空）, 塊) で返す。"""
    blocks: list[tuple[str, str]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        first = next((line.strip() for line in block.splitlines() if line.strip()), "")
        name = first.removeprefix(OBJECT_PREFIX).strip() if first.startswith(OBJECT_PREFIX) else ""
        blocks.append((name, block))
    return blocks


def filter_object_blocks(text: str, keep: Callable[[str], bool]) -> str:
    """`keep(名前)` が真の塊と、`OBJECT:` の無い塊だけを残す。"""
    return "\n\n".join(block for name, block in split_object_blocks(text) if not name or keep(name))


def metadata_fallback_reason(exc: BaseException) -> str:
    """AI で作れずに規則ベースの SQL にしたときの、利用者向けの理由（1 文。詳細は別に出す）。"""
    if isinstance(exc, EnterpriseAiDirectError):
        if exc.incomplete is not None and exc.incomplete.output_limit_reached:
            return (
                "OCI Enterprise AI の応答が出力の上限に達して未完了でした。"
                "システム設定 › モデル で既定のテキストモデルの「出力の上限（tokens）」を"
                "大きくするか、対象の表を減らしてやり直してください。"
            )
        if exc.code == "incomplete":
            return "OCI Enterprise AI の応答が未完了でした。時間をおいてやり直してください。"
        if exc.configuration_error:
            return (
                "OCI Enterprise AI の呼び出しが設定の誤りで失敗しました"
                "（時間をおいても直りません）。"
            )
        if exc.code == "timeout":
            return (
                "OCI Enterprise AI の応答が時間内に返りませんでした。"
                "時間をおいてやり直してください。"
            )
        return "OCI Enterprise AI の呼び出しに失敗しました。時間をおいてやり直してください。"
    return "OCI Enterprise AI が返した SQL をそのまま使えませんでした。"


def metadata_fallback_detail(exc: BaseException) -> str:
    """「詳細」に出す文（失敗の元の文。設定の誤りは確かめる所も。secret は含まない）。"""
    if isinstance(exc, EnterpriseAiDirectError) and exc.configuration_error:
        return enterprise_ai_configuration_failure_message(exc, "")
    return str(exc) or type(exc).__name__
