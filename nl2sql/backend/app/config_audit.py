"""`.env` / `.env.example` / model-settings.json の read-only audit。

3製品共通の設定（`PLATFORM_*`）は platform の共通 `.env`（雛形 `platform/.env.example`）、
NL2SQL 固有の設定（`NL2SQL_*`）は `backend/.env`（雛形 `backend/.env.example`）に置く（#211）。
既知の key は Settings の属性から `settings_env_names` で求める。
"""

from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pr_backend_core.config import (
    PLATFORM_SETTING_FIELDS,
    RuntimeSettings,
    platform_env_file,
    platform_env_name,
    settings_env_names,
)
from pr_system_settings.model import MODEL_SETTINGS_SECRET_FREE_DOCUMENT_VERSION

from app.settings import Settings

Severity = Literal["error", "warning", "info"]
ENV_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$"
)
EXAMPLE_EMPTY_KEYS = frozenset(
    {
        "PLATFORM_ADMIN_LOGIN_USER_PASSWORD",
        "PLATFORM_ENTERPRISE_AI_API_KEY",
        "PLATFORM_ENTERPRISE_AI_PROJECT_ID",
        "PLATFORM_ORACLE_DSN",
        "PLATFORM_ORACLE_PASSWORD",
        "PLATFORM_ORACLE_WALLET_PASSWORD",
    }
)
EXAMPLE_PLACEHOLDER_VALUES = frozenset({"TODO"})
# 共通 `.env` に置いてよい key（3製品共通の全 key。NL2SQL が使わない key も含む）。
PLATFORM_ENV_KEYS = frozenset(platform_env_name(name) for name in PLATFORM_SETTING_FIELDS)


def settings_env_keys() -> tuple[frozenset[str], frozenset[str]]:
    """NL2SQL の Settings が読む (共通 `.env` の key, 製品 `.env` の key)。"""
    names = frozenset(settings_env_names(Settings).values())
    platform_keys = names & PLATFORM_ENV_KEYS
    return platform_keys, names - platform_keys


@dataclass(frozen=True)
class AuditPaths:
    """audit の対象ファイル。"""

    example_path: Path
    env_path: Path
    platform_example_path: Path
    platform_env_path: Path
    model_settings_path: Path


@dataclass(frozen=True)
class EnvDocument:
    """値を外へ公開せず audit 内だけで扱う env document。"""

    values: dict[str, str]
    duplicates: tuple[str, ...] = ()
    malformed_lines: tuple[int, ...] = ()


@dataclass(frozen=True)
class AuditFinding:
    severity: Severity
    code: str
    keys: tuple[str, ...] = ()
    lines: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code, "severity": self.severity}
        if self.keys:
            result["keys"] = list(self.keys)
        if self.lines:
            result["lines"] = list(self.lines)
        return result


@dataclass
class ConfigAuditResult:
    findings: list[AuditFinding] = field(default_factory=list)
    actual_env_present: bool = False
    model_settings_present: bool = False
    overridden_keys: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.findings)

    def to_dict(self) -> dict[str, object]:
        ordered = sorted(
            self.findings,
            key=lambda item: (item.severity, item.code, item.keys, item.lines),
        )
        return {
            "actual_env_present": self.actual_env_present,
            "findings": [item.to_dict() for item in ordered],
            "model_settings_present": self.model_settings_present,
            "ok": self.ok,
            "overridden_keys": list(self.overridden_keys),
        }


def parse_env_document(path: Path) -> EnvDocument:
    """dotenv の assignment key/value と構文エラー位置だけを取り出す。"""
    values: dict[str, str] = {}
    duplicates: set[str] = set()
    malformed: list[int] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ENV_ASSIGNMENT_RE.match(line)
        if match is None:
            malformed.append(line_number)
            continue
        key = match.group("key")
        if key in values:
            duplicates.add(key)
        values[key] = match.group("value").strip()
    return EnvDocument(
        values=values,
        duplicates=tuple(sorted(duplicates)),
        malformed_lines=tuple(malformed),
    )


def audit_configuration(paths: AuditPaths) -> ConfigAuditResult:
    """設定ファイルを変更せず、secret value を返さずに検査する。"""
    result = ConfigAuditResult(
        actual_env_present=paths.env_path.is_file(),
        model_settings_present=paths.model_settings_path.is_file(),
    )
    if not paths.example_path.is_file():
        result.findings.append(AuditFinding("error", "ENV_EXAMPLE_MISSING"))
        return result
    if not paths.platform_example_path.is_file():
        result.findings.append(AuditFinding("error", "PLATFORM_ENV_EXAMPLE_MISSING"))
        return result

    platform_keys, product_keys = settings_env_keys()
    example = parse_env_document(paths.example_path)
    _audit_env_document(result, example, product_keys, prefix="ENV_EXAMPLE")
    platform_example = parse_env_document(paths.platform_example_path)
    _audit_env_document(result, platform_example, PLATFORM_ENV_KEYS, prefix="PLATFORM_ENV_EXAMPLE")

    missing_template_keys = tuple(sorted(product_keys - example.values.keys()))
    if missing_template_keys:
        result.findings.append(
            AuditFinding("error", "ENV_EXAMPLE_FIELDS_MISSING", missing_template_keys)
        )
    missing_platform_keys = tuple(sorted(platform_keys - platform_example.values.keys()))
    if missing_platform_keys:
        result.findings.append(
            AuditFinding("error", "PLATFORM_ENV_EXAMPLE_FIELDS_MISSING", missing_platform_keys)
        )
    example_values = {**platform_example.values, **example.values}
    populated_sensitive_keys = tuple(
        sorted(
            key
            for key in EXAMPLE_EMPTY_KEYS
            if key in example_values
            and _unquote(example_values[key])
            and _unquote(example_values[key]) not in EXAMPLE_PLACEHOLDER_VALUES
        )
    )
    if populated_sensitive_keys:
        result.findings.append(
            AuditFinding(
                "error",
                "ENV_EXAMPLE_SENSITIVE_VALUE_NONEMPTY",
                populated_sensitive_keys,
            )
        )

    actual_values: dict[str, str] = {}
    for env_path, known_keys, prefix in (
        (paths.platform_env_path, PLATFORM_ENV_KEYS, "PLATFORM_ENV_ACTUAL"),
        (paths.env_path, product_keys, "ENV_ACTUAL"),
    ):
        if not env_path.is_file():
            continue
        actual = parse_env_document(env_path)
        _audit_env_document(result, actual, known_keys, prefix=prefix)
        _audit_env_permissions(result, env_path, code=f"{prefix}_PERMISSIONS_NOT_0600")
        actual_values.update(actual.values)
    result.overridden_keys = tuple(
        sorted(
            key
            for key, value in actual_values.items()
            if key in example_values and value != example_values[key]
        )
    )

    effective = {**example_values, **actual_values}
    _audit_security_combinations(result, effective)
    _audit_model_settings(result, paths.model_settings_path)
    return result


def _audit_env_document(
    result: ConfigAuditResult,
    document: EnvDocument,
    known_keys: frozenset[str],
    *,
    prefix: str,
) -> None:
    if document.duplicates:
        result.findings.append(
            AuditFinding("error", f"{prefix}_DUPLICATE_KEYS", document.duplicates)
        )
    if document.malformed_lines:
        result.findings.append(
            AuditFinding("error", f"{prefix}_MALFORMED_LINES", lines=document.malformed_lines)
        )
    unknown_keys = tuple(sorted(document.values.keys() - known_keys))
    if unknown_keys:
        result.findings.append(AuditFinding("error", f"{prefix}_UNKNOWN_KEYS", unknown_keys))


def _audit_security_combinations(
    result: ConfigAuditResult,
    effective: dict[str, str],
) -> None:
    from pydantic import ValidationError

    try:
        RuntimeSettings.model_validate(
            {
                "environment": _unquote(effective.get("PLATFORM_ENVIRONMENT", "staging")),
                "auth_mode": _unquote(effective.get("PLATFORM_AUTH_MODE", "authenticated")),
                "app_auth_cookie_secure": _as_bool(
                    effective.get("PLATFORM_AUTH_COOKIE_SECURE", "true")
                ),
            }
        )
    except ValidationError:
        result.findings.append(
            AuditFinding(
                "error",
                "INVALID_RUNTIME_SETTINGS",
                ("PLATFORM_ENVIRONMENT", "PLATFORM_AUTH_MODE", "PLATFORM_AUTH_COOKIE_SECURE"),
            )
        )


def _audit_model_settings(result: ConfigAuditResult, path: Path) -> None:
    if not path.is_file():
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result.findings.append(AuditFinding("error", "MODEL_SETTINGS_INVALID_JSON"))
        return
    if not isinstance(document, dict):
        result.findings.append(AuditFinding("error", "MODEL_SETTINGS_INVALID_ROOT"))
        return
    version = document.get("version")
    enterprise = document.get("enterprise_ai")
    legacy_secret = isinstance(enterprise, dict) and "api_key" in enterprise
    if legacy_secret:
        result.findings.append(
            AuditFinding("error", "MODEL_SETTINGS_LEGACY_SECRET", ("enterprise_ai.api_key",))
        )
    # 3 以降は secret を持たない形で、そのまま読める
    # （4 は登録モデルの推論の量を足しただけ。#1425）。
    if not isinstance(version, int) or version < MODEL_SETTINGS_SECRET_FREE_DOCUMENT_VERSION:
        result.findings.append(AuditFinding("warning", "MODEL_SETTINGS_VERSION_LEGACY"))
    _audit_env_permissions(result, path, code="MODEL_SETTINGS_PERMISSIONS_NOT_0600")


def _audit_env_permissions(
    result: ConfigAuditResult,
    path: Path,
    *,
    code: str = "ENV_ACTUAL_PERMISSIONS_NOT_0600",
) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        result.findings.append(AuditFinding("error", f"{code}_UNREADABLE"))
        return
    if mode != 0o600:
        result.findings.append(AuditFinding("error", code))


def _unquote(value: str) -> str:
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        return normalized[1:-1].strip()
    return normalized


def _as_bool(value: str) -> bool:
    return _unquote(value).lower() in {"1", "true", "yes", "on"}


def stable_audit_json(result: ConfigAuditResult) -> str:
    """CI と運用で diff しやすい stable JSON。"""
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def default_audit_paths(backend_dir: Path) -> AuditPaths:
    """backend root 基準の既定 audit target。共通 `.env` は `PLATFORM_ENV_FILE` で変えられる。"""
    platform_env_path = platform_env_file(backend_dir)
    return AuditPaths(
        example_path=backend_dir / ".env.example",
        env_path=backend_dir / ".env",
        platform_example_path=backend_dir.resolve().parents[1] / "platform" / ".env.example",
        platform_env_path=platform_env_path,
        model_settings_path=platform_env_path.parent / "model-settings.json",
    )
