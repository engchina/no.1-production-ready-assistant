"""Oracle 接続障害のログ診断。例外の生値や接続設定はログ項目へ転記しない。"""

from __future__ import annotations

import re

_ERROR_CODE_RE = re.compile(r"\b(?:ORA-\d{5}|(?:DPY|DPI)-\d{4})\b", re.IGNORECASE)

# 外側の DPY-6005 より、内側の具体的な原因を優先する。
_DIAGNOSTICS = (
    (
        {"DPY-6001", "ORA-12514", "ORA-12505"},
        "service_not_registered",
        "要求したデータベースサービスが listener に登録されていません。",
        "OCI Console で ADB の稼働状態を確認し、"
        "PLATFORM_ORACLE_DSN のサービス名を現在の接続文字列と"
        "照合してください。Wallet 利用時は tnsnames.ora が対象 ADB のものか確認してください。",
    ),
    (
        {"ORA-01017"},
        "invalid_credentials",
        "データベースのユーザー名またはパスワードが拒否されました。",
        "PLATFORM_ORACLE_USER と PLATFORM_ORACLE_PASSWORD を確認してください。"
        "DB パスワードと Wallet "
        "パスワードは別の設定です。",
    ),
    (
        {"ORA-28000", "ORA-28001"},
        "account_unavailable",
        "データベースアカウントがロックされているか、パスワードが期限切れです。",
        "DB 管理者にアカウント状態とパスワードの有効期限を確認してください。",
    ),
    (
        {"ORA-12154", "DPY-4000"},
        "dsn_resolution_failed",
        "接続先サービス名を解決できませんでした。",
        "PLATFORM_ORACLE_DSN と PLATFORM_ORACLE_WALLET_DIR の tnsnames.ora を確認してください。"
        "Walletless TLS では host:port/service_name または接続記述子を指定してください。",
    ),
    (
        {"ORA-12506"},
        "network_access_denied",
        "データベースへの接続がアクセス制御で拒否されました。",
        "ADB の Network Access / ACL が実行ホストの接続元 IP または VCN 経路を"
        "許可しているか確認してください。",
    ),
    (
        {"DPI-1047", "DPI-1072"},
        "client_unavailable",
        "Oracle Client ライブラリを利用できません。",
        "PLATFORM_ORACLE_DRIVER_MODE を確認してください。"
        "標準の Thin mode では Instant Client は不要です。"
        "Thick mode 利用時は PLATFORM_ORACLE_CLIENT_LIB_DIR と"
        "ライブラリの互換性を確認してください。",
    ),
    (
        {"DPY-4011", "ORA-03113", "ORA-03114"},
        "connection_closed",
        "データベースまたはネットワークによって接続が切断されました。",
        "DB の再起動・障害とネットワーク経路を確認してください。"
        "接続確立時の失敗なら TLS / Wallet とサーバー側の接続設定も確認してください。",
    ),
)


def oracle_connection_diagnostics(exc: Exception) -> dict[str, object]:
    """例外チェーンからコードだけを抽出し、安全な日本語の原因・確認事項を返す。"""
    codes: list[str] = []
    timed_out = False
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        error_text = str(current)
        codes.extend(code.upper() for code in _ERROR_CODE_RE.findall(error_text))
        timed_out |= isinstance(current, TimeoutError) or "timed out" in error_text.lower()
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    codes = list(dict.fromkeys(codes))
    code_set = set(codes)
    for matching_codes, *diagnostic in _DIAGNOSTICS:
        if code_set & matching_codes:
            category, summary, action = diagnostic
            break
    else:
        if timed_out or code_set & {"ORA-12170", "ORA-12535"}:
            category = "connection_timeout"
            summary = "データベース接続の応答が制限時間内に返りませんでした。"
            action = (
                "ADB の稼働状態、接続先ホスト・ポートへの到達性と VCN / VPN / Firewall を確認し、"
                "PLATFORM_ORACLE_DB_TEST_TIMEOUT_SECONDS と "
                "PLATFORM_ORACLE_TCP_CONNECT_TIMEOUT_SECONDS "
                "を確認してください。"
            )
        elif code_set & {"ORA-12541", "DPY-6000", "DPY-6005"}:
            category = "connection_unavailable"
            summary = "データベースへの接続を確立できませんでした。"
            action = (
                "ADB / listener の稼働状態と PLATFORM_ORACLE_DSN のホスト・ポートを確認し、"
                "DNS、VCN / VPN / Firewall、ADB のアクセス制御を切り分けてください。"
                "DPY-6005 だけでは原因を特定できないため、併記されたエラーも確認してください。"
            )
        else:
            category = "unknown"
            summary = "データベース操作に失敗しました。接続障害かどうかは未特定です。"
            action = (
                "同じログの operation / exception_type / traceback と Oracle エラーコードを確認し、"
                "接続設定・DB の稼働状態・実行処理を切り分けてください。"
            )
    return {
        "diagnostic_category": category,
        "summary": summary,
        "suggested_action": action,
        "oracle_error_codes": codes,
        "exception_type": type(exc).__name__,
    }
