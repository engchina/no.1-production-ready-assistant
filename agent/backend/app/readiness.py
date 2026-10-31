"""依存設定の readiness チェック（/ready 用）。

ここに OCI/Oracle 等の前提チェックを追加する。各 status は "ok" / "missing" /
"missing_credentials" / "unreachable" 等（pr_backend_core.schemas.ReadinessStatus）。
"""

from app.settings import Settings


def readiness_checks(settings: Settings) -> dict[str, str]:
    """前提設定の readiness を返す。雛形では常に ok。"""
    _ = settings
    return {}
