"""前処理(Preprocess)ステージの共有契約。

backend と前処理マイクロサービス(`services/preprocess`)が共有する最小契約。
parse の **前** に原本を一度だけ canonical な中間物へ変換し(`先变换、再 parse`)、
原本を保全したまま「派生物→原本」の系譜(`SourceDerivation`)を残す。

- `PREPROCESS_PROFILES`: 変換プリセット名(backend config の `PreprocessProfile` と一致)。
- `SourceDerivation`: 派生系譜(溯源)。backend が保存先へ保存した後に確定し、
  既存 JSON(`StructuredExtraction.parser_artifacts["source_derivation"]`)へ格納する。
- `ConvertOutcome` / `ConvertResponse` / `ConvertHealth`: マイクロサービスの HTTP 契約。
- `create_preprocess_app`: 1 つの前処理サービス用 FastAPI app factory(converter 注入式)。

重い変換依存(LibreOffice 等)は本 package には持たせず、各サービス側へ隔離する。
core 本体の依存は pydantic + charset-normalizer のみに保つ。
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from rag_parser_core.source import SourceProfile

# 変換プリセット。backend の `app.config.PreprocessProfile` と同期させる。
# 重い変換(office_to_pdf / csv_to_jsonl / excel_to_jsonl)は各々独立した
# 前処理マイクロサービス(`services/preprocess/<name>`)へ HTTP 委譲する。
PREPROCESS_PROFILES: tuple[str, ...] = (
    "passthrough",
    "office_to_pdf",
    "csv_to_jsonl",
    "excel_to_jsonl",
    "word_to_jsonl",
)
DEFAULT_PREPROCESS_PROFILE = "passthrough"


def normalize_preprocess_profile(value: object) -> str:
    """未知のプロファイル名は既定 passthrough へ寄せる。"""
    normalized = str(value).strip().casefold()
    if normalized in PREPROCESS_PROFILES:
        return normalized
    return DEFAULT_PREPROCESS_PROFILE


class SourceDerivation(BaseModel):
    """派生系譜(溯源)。変換物がどの原本からどう生成されたかの追跡記録。

    既定(passthrough)では変換が行われないため `converted=False`、`derived_*` は
    原本と同一を指す。`page_map` は派生ページ→原本ページの対応(1 始まり)。
    """

    derivation_id: str
    preprocess_profile: str = DEFAULT_PREPROCESS_PROFILE
    converted: bool = False
    converter_name: str = "passthrough"
    converter_version: str = "v1"
    source_object_path: str | None = None
    source_content_type: str | None = None
    source_sha256: str | None = None
    derived_object_path: str | None = None
    derived_content_type: str | None = None
    derived_sha256: str | None = None
    page_map: dict[str, int] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    warnings: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class ConvertOutcome:
    """converter(各サービス実装)が返す変換結果。

    `derived_bytes` が None のときは変換せず原本を使う(passthrough / no-op)。
    """

    converted: bool
    converter_name: str
    converter_version: str
    derived_bytes: bytes | None = None
    derived_content_type: str | None = None
    page_map: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @classmethod
    def passthrough(cls, *, reason: str | None = None) -> ConvertOutcome:
        return cls(
            converted=False,
            converter_name="passthrough",
            converter_version="v1",
            warnings=(reason,) if reason else (),
        )


class ConvertResponse(BaseModel):
    """`POST /convert` のレスポンス本体(`ConvertOutcome` の wire 形式)。

    派生 bytes は base64 で運ぶ(内部サービス・timeout 付きの小〜中規模文書想定)。
    object path / sha256 は backend が保存先へ保存した後に確定するため含めない。
    """

    converted: bool = False
    converter_name: str = "passthrough"
    converter_version: str = "v1"
    derived_content_base64: str | None = None
    derived_content_type: str | None = None
    page_map: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_outcome(cls, outcome: ConvertOutcome) -> ConvertResponse:
        encoded: str | None = None
        if outcome.converted and outcome.derived_bytes is not None:
            encoded = base64.b64encode(outcome.derived_bytes).decode("ascii")
        return cls(
            converted=outcome.converted and encoded is not None,
            converter_name=outcome.converter_name,
            converter_version=outcome.converter_version,
            derived_content_base64=encoded,
            derived_content_type=outcome.derived_content_type,
            page_map=dict(outcome.page_map),
            warnings=list(outcome.warnings),
        )

    def derived_bytes(self) -> bytes | None:
        """base64 派生 content を bytes へ復号する。失敗時は None。"""
        if not self.converted or not self.derived_content_base64:
            return None
        try:
            return base64.b64decode(self.derived_content_base64, validate=True)
        except (ValueError, TypeError):
            return None


class ConvertHealth(BaseModel):
    """`GET /health` のレスポンス。readiness 表示の値ソース。"""

    status: str = "ok"
    backend: str = "preprocess"
    package_name: str | None = None
    package_version: str | None = None
    supported_profiles: list[str] = Field(default_factory=list)


# converter シグネチャ:
# (source_bytes, content_type, preprocess_profile, source_profile) -> ConvertOutcome
# 選択肢を持つ前処理（excel_to_jsonl。#1221）は keyword の `options`（Document Recipe の値）も
# 受け取る。受け取らない converter には渡さない（`create_preprocess_app` が signature で判定する）。
# app factory(create_preprocess_app)は fastapi 依存のため `preprocess_service` モジュールへ分離し、
# 本モジュール(契約 schema)は core 依存(pydantic + charset-normalizer)のみに保つ。
Converter = Callable[[bytes, str, str, SourceProfile | None], ConvertOutcome]
HealthProbe = Callable[[], ConvertHealth]


def supported_profiles_from(profiles: Sequence[str]) -> list[str]:
    """health 表示用に既知プロファイルへ正規化したリストを返す。"""
    seen: list[str] = []
    for profile in profiles:
        normalized = normalize_preprocess_profile(profile)
        if normalized not in seen:
            seen.append(normalized)
    return seen
