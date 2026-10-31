"""parser マイクロサービスと backend 間の HTTP 契約スキーマと共有 contract 型。

サービスは `ParserRegistryResult`(dataclass)を `ParseResponse`(Pydantic)へ変換して
JSON で返し、backend はそれを `ParserRegistryResult` へ戻して既存パイプラインへ渡す。
これにより外部 adapter の出力忠実度(StructuredExtraction)をネットワーク越しに維持する。

`ParserRegistryResult` / 各 backend 集合 / `ExternalAdapterRunner` 型は本モジュール(軽量)に
置く。重い `registry`(local 解析実装)を読み込まずに backend が型・定数を参照できるようにし、
backend では解析実体を import/実行しない方針を成立させる。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rag_parser_core.extraction import StructuredExtraction

if TYPE_CHECKING:
    from rag_parser_core.source import SourceProfile

# ローカル(in-process)parser の版。実行は backend から撤去済みだが、結果タグの既定値・
# 後方互換のため契約値として残す。
LOCAL_PARSER_VERSION = "local_partition_v1"

# Unstructured の本文変換と、保存済み抽出の再利用が共有する契約の版。
UNSTRUCTURED_TEXT_CONTRACT = "html_structural_roles_v3"

# 外部 Python package を持つ adapter とその配布 package 名(registry の package 検出に使う)。
EXTERNAL_ADAPTER_PACKAGES = {
    "docling": "docling",
    "unstructured": "unstructured",
}

# 外部 adapter 実行の注入点。backend は HTTP runner を、service/test は in-process を渡す。
ExternalAdapterRunner = Callable[[str, bytes, "SourceProfile | None", str], "ParserRegistryResult"]


@dataclass(frozen=True)
class ParserRegistryResult:
    """parser registry の結果。None extraction は Enterprise AI fallback を意味する。"""

    extraction: StructuredExtraction | None
    parser_backend: str
    parser_version: str = LOCAL_PARSER_VERSION
    fallback_used: bool = False
    template: str = "enterprise_ai_fallback"
    warnings: tuple[str, ...] = ()
    unsupported_reason: str | None = None


class ParseResponse(BaseModel):
    """`POST /parse` のレスポンス本体(ParserRegistryResult の wire 形式)。"""

    extraction: StructuredExtraction | None = None
    parser_backend: str
    parser_version: str
    fallback_used: bool = False
    template: str = "enterprise_ai_fallback"
    warnings: list[str] = Field(default_factory=list)
    unsupported_reason: str | None = None

    @classmethod
    def from_result(cls, result: ParserRegistryResult) -> ParseResponse:
        return cls(
            extraction=result.extraction,
            parser_backend=result.parser_backend,
            parser_version=result.parser_version,
            fallback_used=result.fallback_used,
            template=result.template,
            warnings=list(result.warnings),
            unsupported_reason=result.unsupported_reason,
        )

    def to_result(self) -> ParserRegistryResult:
        return ParserRegistryResult(
            extraction=self.extraction,
            parser_backend=self.parser_backend,
            parser_version=self.parser_version,
            fallback_used=self.fallback_used,
            template=self.template,
            warnings=tuple(self.warnings),
            unsupported_reason=self.unsupported_reason,
        )


def service_failure_warning(backend: str, exc: BaseException) -> str:
    """service parser の失敗を別経路へ縮退させず、actionable な理由を warning に残す。

    `safe_for_user` を持つ例外(timeout / 不完全応答 / schema 不一致 / 入力非対応 等)は
    そのメッセージを上流へ届け、利用者が container ログを開かずに原因を直せるようにする。
    それ以外は情報漏えいを避けて例外型名だけを返す。warning は backend の
    `_selected_parser_failure_message` で `エラーコード:` として表示される。
    """
    if getattr(exc, "safe_for_user", False):
        detail = " ".join(str(exc).split())[:400]
    else:
        detail = type(exc).__name__
    return f"{backend}_unavailable: {detail}"


class ParseHealth(BaseModel):
    """`GET /health` のレスポンス。readiness 表示の値ソース。"""

    status: str = "ok"
    backend: str
    package_name: str | None = None
    package_version: str | None = None
