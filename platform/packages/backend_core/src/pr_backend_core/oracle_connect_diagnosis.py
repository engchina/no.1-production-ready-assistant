"""DB に接続できないときの本当の原因の診断（3 製品共通。#1744）。

接続 pool（`POOL_GETMODE_TIMEDWAIT`）は、接続の作成の失敗を背景のスレッドで握り、`acquire` には
時間切れ（`DPY-4005`）しか返さない。ADB の停止・password の誤り・account lock・network・Wallet の
どれでも同じ `DPY-4005` になり、本当の原因が分からない。

- `diagnose_oracle_connect_failure(exc, connect_kwargs=…)`：接続の失敗なら、ORA / DPY のコードから
  平易な日本語の理由と対処を返す。pool の時間切れのときは、pool を使わずに 1 回だけ直接 `connect`
  して（`probe_oracle_connect`）、本当の例外から判定する。接続の失敗でなければ None
- `probe_oracle_connect(connect_kwargs)`：pool を使わない 1 回だけの接続。Wallet の別名の長い再試行
  （`(retry_count=20)(retry_delay=3)`）を外し（`dsn_without_retry`）、`tcp_connect_timeout`
  を付ける（DB の停止中に 1 分以上待たない）
- `install_oracledb_thread_excepthook()`：python-oracledb の pool の背景のスレッドの例外を、
  traceback ではなく 1 行の log にまとめる（CLI だけで使う。`configure_cli_logging` から）

secret（password・Wallet の password・DSN の資格情報）は、結果・log に出さない。接続引数の値と
`password=…` のような代入・`user/password@host` の資格情報は伏せる。

python-oracledb に直接依存しない（`oracledb` は使うときに import する。テストは loader で
差し替える）。
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .oracle_dsn import dsn_without_retry
from .oracle_errors import (
    is_oracle_connection_error,
    is_oracle_pool_wait_timeout,
    oracle_error_codes,
)

logger = logging.getLogger(__name__)

# 直接の接続の TCP の接続の上限（秒）。接続引数に `tcp_connect_timeout` が無いとき。
DEFAULT_PROBE_TCP_CONNECT_TIMEOUT_SECONDS = 10.0
# 結果に出す本当の例外の文の上限（文字）。
DETAIL_MAX_CHARS = 300

OracleConnectCategory = Literal[
    "unreachable",
    "service_unavailable",
    "acl_rejected",
    "invalid_credentials",
    "account_locked",
    "password_expired",
    "wallet",
    "dsn",
    "pool_timeout",
    "unknown",
]

OracledbLoader = Callable[[], Any]
ConnectKwargsFactory = Callable[[], Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class OracleConnectDiagnosis:
    """接続の失敗の診断（理由と対処は利用者向けの日本語。secret を含まない）。"""

    category: OracleConnectCategory
    # 本当の例外の ORA / DPY のコード（分類に使ったもの。無ければ None）。
    code: str | None
    reason: str
    action: str
    # 本当の例外の 1〜2 行（secret を伏せたもの。無ければ None）。
    detail: str | None = None
    # pool を使わずに直接接続して確かめたか。
    probed: bool = False

    @property
    def hint(self) -> str:
        """理由と対処を 1 つの文にしたもの（CLI の `hint`）。"""
        return f"{self.reason}{self.action}"

    def summary(self) -> str:
        """1 行の要約（配備の確認の出力。`DPY-6005: ADB が…`）。"""
        return f"{self.code}: {self.reason}" if self.code else self.reason

    def as_fields(self) -> dict[str, str]:
        """CLI の JSON に足す項目（`code` / `hint` / `detail` / `cause`）。"""
        fields = {"cause": self.category, "hint": self.hint}
        if self.code:
            fields["code"] = self.code
        if self.detail:
            fields["detail"] = self.detail
        return fields


@dataclass(frozen=True, slots=True)
class _Rule:
    category: OracleConnectCategory
    codes: frozenset[str]
    reason: str
    action: str


_START_ADB_ACTION = (
    "OCI コンソールか、データベース設定の「Autonomous Database 管理」で ADB の状態を確かめ、"
    "停止していれば起動して（完了まで数分かかります）から再実行してください。"
)

# 上から順に当てる（1 つの例外が複数のコードを持つときは、より具体的なものを先に）。
_RULES: tuple[_Rule, ...] = (
    _Rule(
        "account_locked",
        frozenset({"ORA-28000"}),
        "DB のユーザーがロックされています（password の誤りが続いた・管理者がロックした など）。",
        "ADMIN で `ALTER USER <ユーザー> ACCOUNT UNLOCK` を実行し、platform/.env の "
        "PLATFORM_ORACLE_PASSWORD が正しいことを確かめてから再実行してください。",
    ),
    _Rule(
        "password_expired",
        frozenset({"ORA-28001"}),
        "DB のユーザーの password の期限が切れています。",
        "ADMIN で `ALTER USER <ユーザー> IDENTIFIED BY <新しい password>` を実行し、"
        "platform/.env の PLATFORM_ORACLE_PASSWORD を同じ値にしてから再実行してください。",
    ),
    _Rule(
        "invalid_credentials",
        frozenset({"ORA-01017", "ORA-01005", "ORA-01004", "DPY-4001"}),
        "DB のユーザー名か password が違います。",
        "platform/.env の PLATFORM_ORACLE_USER / PLATFORM_ORACLE_PASSWORD を確かめてください。"
        "誤った password で何度も試すと、ユーザーがロックされます（ORA-28000）。",
    ),
    _Rule(
        "acl_rejected",
        frozenset({"ORA-12506"}),
        "ADB のアクセス制御リスト（ACL）が、この接続元を許可していません"
        "（ADB が停止しているときも、この理由で拒否されることがあります）。",
        "ADB のネットワークの設定で、接続元（Compute の IP・VCN）が ACL に入っているかを確かめ、"
        f"入っていれば ADB の状態を確かめてください。{_START_ADB_ACTION}",
    ),
    _Rule(
        "wallet",
        frozenset(
            {
                "ORA-28759",
                "ORA-28860",
                "ORA-28865",
                "ORA-29002",
                "ORA-29003",
                "ORA-29024",
                "ORA-29106",
                "DPY-4026",
                "DPY-4027",
                "DPY-6002",
                "DPY-6006",
            }
        ),
        "Wallet（mTLS の証明書）で DB に接続できません（Wallet のファイルの不足・"
        "別の ADB の Wallet・Wallet の password の誤り・証明書の期限切れ）。",
        "PLATFORM_ORACLE_WALLET_DIR に、接続先の ADB からダウンロードした Wallet "
        "（tnsnames.ora・ewallet.pem・cwallet.sso など）があるか、"
        "PLATFORM_ORACLE_WALLET_PASSWORD が Wallet の password と同じかを確かめてください。"
        "ADB の Wallet を作り直した（rotate した）ときは、新しい Wallet に置き換えてください。",
    ),
    _Rule(
        "dsn",
        frozenset({"DPY-4000", "DPY-4017", "DPY-4018", "DPY-4021", "ORA-12154", "ORA-12262"}),
        "DSN（接続先の指定）が読めないか、Wallet の tnsnames.ora に見つかりません。",
        "platform/.env の PLATFORM_ORACLE_DSN が、Wallet の tnsnames.ora の別名（例: "
        "<DB 名>_high）と同じかを確かめてください。",
    ),
    _Rule(
        "service_unavailable",
        frozenset(
            {
                "ORA-12514",
                "ORA-12505",
                "ORA-12516",
                "ORA-12518",
                "ORA-12519",
                "ORA-12520",
                "ORA-12526",
                "ORA-12527",
                "ORA-12528",
                "ORA-12529",
                "ORA-01033",
                "ORA-01034",
                "ORA-01089",
                "ORA-01090",
                "DPY-6000",
                "DPY-6001",
                "DPY-6003",
            }
        ),
        "DB のサービスが接続を受け付けていません（ADB の停止・起動中・再起動中、"
        "または DSN のサービス名の誤り）。",
        f"{_START_ADB_ACTION}起動している場合は、PLATFORM_ORACLE_DSN のサービス名を"
        "確かめてください。",
    ),
    _Rule(
        "unreachable",
        frozenset(
            {
                "DPY-6005",
                "DPY-4011",
                "ORA-12170",
                "ORA-12535",
                "ORA-12541",
                "ORA-12543",
                "ORA-12545",
                "ORA-12547",
                "ORA-12560",
                "ORA-03113",
                "ORA-03135",
                "ORA-28547",
            }
        ),
        "ADB が停止しているか、この接続元から DB に届きません。",
        f"{_START_ADB_ACTION}起動している場合は、ADB のネットワーク（ACL・プライベート"
        "エンドポイント・NSG / セキュリティリスト）、DSN のホスト名、接続元からの外向きの通信"
        "（port 1522）を確かめてください。",
    ),
)

# `DPY-6005`（接続の失敗）の下の原因の文に、これがあれば Wallet / TLS の失敗とみなす。
# ただし時間切れ・拒否・名前の解決の失敗は、TLS の途中でも「届かない」とする。
_WALLET_TEXT_RE = re.compile(r"\bssl\b|certificate|\bpem\b|wallet|pkcs|decrypt", re.IGNORECASE)
_NETWORK_TEXT_RE = re.compile(
    r"timed out|timeout|refused|unreachable|no route|name or service not known|"
    r"nodename nor servname|getaddrinfo|temporary failure in name resolution",
    re.IGNORECASE,
)

_POOL_TIMEOUT_REASON = (
    "接続 pool が時間内に接続を返しませんでしたが、pool を使わない直接の接続はできました"
    "（DB の起動の直後・同時の接続の多さによる一時的なものと考えられます）。"
)
_POOL_TIMEOUT_ACTION = (
    "少し待ってから再実行してください。続くときは、同じ DB を使うほかの処理（3 製品の backend）"
    "が接続を使い切っていないかを確かめてください。"
)
_POOL_TIMEOUT_UNPROBED_REASON = "接続 pool が時間内に DB への接続を返しませんでした。"
_POOL_TIMEOUT_UNPROBED_ACTION = (
    f"ADB が停止しているか、届かない・資格情報の誤りが考えられます。{_START_ADB_ACTION}"
)
_UNKNOWN_REASON = "DB に接続できませんでした。"
_UNKNOWN_ACTION = (
    f"{_START_ADB_ACTION}起動している場合は、platform/.env の PLATFORM_ORACLE_* "
    "（ユーザー・password・DSN・Wallet）を確かめてください。"
)

# 伏せる代入（`password=…` / `wallet_password: …`）と、`user/password@host` の資格情報。
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([a-z_]*(?:password|passwd|pwd|secret|token)[a-z_]*)(\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;)\"']+)"
)
_EASY_CONNECT_CREDENTIAL_RE = re.compile(r"(?<![\w/])([\w.$#-]+)/[^\s@/]+@")
_URL_CREDENTIAL_RE = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/@]+@", re.IGNORECASE)
# 接続引数のうち、値そのものを伏せるもの。
_SECRET_KWARGS = ("password", "wallet_password", "newpassword", "access_token", "token")
_MIN_SECRET_LENGTH = 3


def _default_oracledb_loader() -> Any:
    return importlib.import_module("oracledb")


def redact_connect_secrets(text: str, connect_kwargs: Mapping[str, object] | None = None) -> str:
    """文から secret を伏せる（接続引数の secret の値・代入の値・DSN の資格情報）。"""
    redacted = text
    for key in _SECRET_KWARGS:
        value = (connect_kwargs or {}).get(key)
        secret = _secret_text(value)
        if secret and len(secret) >= _MIN_SECRET_LENGTH:
            redacted = redacted.replace(secret, "***")
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1\2***", redacted)
    redacted = _URL_CREDENTIAL_RE.sub(r"\1***@", redacted)
    return _EASY_CONNECT_CREDENTIAL_RE.sub(r"\1/***@", redacted)


def _secret_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    # python-oracledb の `SecretValue` などは文字列にしない（中身を取り出さない）。
    return None


def _exception_detail(exc: BaseException, connect_kwargs: Mapping[str, object] | None) -> str:
    """例外とその原因の文の、空でない最初の 2 行（secret を伏せ、長さを切る）。"""
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < 3:
        seen.add(id(current))
        lines = [line.strip() for line in str(current).splitlines() if line.strip()][:2]
        text = " ".join(lines) or type(current).__name__
        # 原因の文が前の文に含まれる（python-oracledb は原因の文を自分の文の 2 行目に入れる）
        # ときは重ねない。
        if not any(text in part for part in parts):
            parts.append(text)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    detail = redact_connect_secrets(" ← ".join(parts), connect_kwargs)
    if len(detail) > DETAIL_MAX_CHARS:
        detail = f"{detail[:DETAIL_MAX_CHARS]}…"
    return detail


def _full_text(exc: BaseException) -> str:
    texts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(texts) < 10:
        seen.add(id(current))
        texts.append(f"{type(current).__name__} {current}")
        current = current.__cause__ or current.__context__
    return "\n".join(texts)


def explain_oracle_connect_error(
    exc: BaseException,
    *,
    connect_kwargs: Mapping[str, object] | None = None,
    probed: bool = False,
) -> OracleConnectDiagnosis | None:
    """接続の失敗の例外から、理由と対処を返す（接続の失敗でなければ None）。

    pool の時間切れ（`DPY-4005`）は本当の原因が分からないため、`unknown` と同じ扱いの文を返す
    （本当の原因は `diagnose_oracle_connect_failure` が直接の接続で確かめる）。
    """
    codes = oracle_error_codes(exc)
    detail = _exception_detail(exc, connect_kwargs)
    for rule in _RULES:
        matched = next((code for code in codes if code in rule.codes), None)
        if matched is None:
            continue
        category = rule.category
        full_text = _full_text(exc)
        if (
            category == "unreachable"
            and _WALLET_TEXT_RE.search(full_text)
            and not _NETWORK_TEXT_RE.search(full_text)
        ):
            wallet = next(item for item in _RULES if item.category == "wallet")
            return OracleConnectDiagnosis(
                "wallet", matched, wallet.reason, wallet.action, detail, probed
            )
        return OracleConnectDiagnosis(category, matched, rule.reason, rule.action, detail, probed)
    if is_oracle_pool_wait_timeout(exc):
        return OracleConnectDiagnosis(
            "pool_timeout",
            "DPY-4005",
            _POOL_TIMEOUT_UNPROBED_REASON,
            _POOL_TIMEOUT_UNPROBED_ACTION,
            detail,
            probed,
        )
    if is_oracle_connection_error(exc):
        code = codes[0] if codes else None
        return OracleConnectDiagnosis(
            "unknown", code, _UNKNOWN_REASON, _UNKNOWN_ACTION, detail, probed
        )
    return None


def probe_connect_kwargs(
    connect_kwargs: Mapping[str, object],
    *,
    tcp_connect_timeout_seconds: float = DEFAULT_PROBE_TCP_CONNECT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """直接の接続の引数（pool の引数を除き、TNS の再試行を外し、TCP の接続の上限を付ける）。"""
    pool_only = {
        "min",
        "max",
        "increment",
        "getmode",
        "wait_timeout",
        "timeout",
        "ping_interval",
        "ping_timeout",
        "max_lifetime_session",
        "max_sessions_per_shard",
        "soda_metadata_cache",
        "homogeneous",
        "session_callback",
    }
    kwargs = {key: value for key, value in connect_kwargs.items() if key not in pool_only}
    dsn = kwargs.get("dsn")
    if isinstance(dsn, str) and dsn.strip():
        wallet_dir = kwargs.get("config_dir") or kwargs.get("wallet_location")
        kwargs["dsn"] = dsn_without_retry(dsn, wallet_dir if isinstance(wallet_dir, str) else None)
    kwargs["retry_count"] = 0
    kwargs["retry_delay"] = 0
    timeout = kwargs.get("tcp_connect_timeout")
    if not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0:
        kwargs["tcp_connect_timeout"] = float(tcp_connect_timeout_seconds)
    return kwargs


def probe_oracle_connect(
    connect_kwargs: Mapping[str, object],
    *,
    oracledb_loader: OracledbLoader | None = None,
    tcp_connect_timeout_seconds: float = DEFAULT_PROBE_TCP_CONNECT_TIMEOUT_SECONDS,
) -> BaseException | None:
    """pool を使わずに 1 回だけ接続する。失敗したらその例外、接続できたら None を返す。

    誤った password で何度も試して account をロックしないよう、再試行はしない。
    """
    kwargs = probe_connect_kwargs(
        connect_kwargs, tcp_connect_timeout_seconds=tcp_connect_timeout_seconds
    )
    try:
        oracledb = (oracledb_loader or _default_oracledb_loader)()
        connection = oracledb.connect(**kwargs)
    except Exception as exc:  # noqa: BLE001 - 本当の接続エラーを診断に使う
        return exc
    try:
        connection.close()
    except Exception:  # noqa: BLE001 - 接続はできた。閉じる失敗は診断に影響しない
        logger.debug("oracle_connect_probe_close_failed")
    return None


def diagnose_oracle_connect_failure(
    exc: BaseException,
    *,
    connect_kwargs: ConnectKwargsFactory | Mapping[str, object] | None = None,
    oracledb_loader: OracledbLoader | None = None,
    tcp_connect_timeout_seconds: float = DEFAULT_PROBE_TCP_CONNECT_TIMEOUT_SECONDS,
) -> OracleConnectDiagnosis | None:
    """接続の失敗の理由と対処を返す（接続の失敗でなければ None）。

    pool の時間切れ（`DPY-4005`）で接続引数が分かるときは、pool を使わずに 1 回だけ直接接続して、
    本当の例外から判定する。直接の接続ができたら、pool の一時的な時間切れとして返す。
    """
    if not is_oracle_pool_wait_timeout(exc):
        diagnosis = explain_oracle_connect_error(
            exc, connect_kwargs=_mapping_or_none(connect_kwargs)
        )
        _log_diagnosis(diagnosis)
        return diagnosis
    kwargs = _resolve_kwargs(connect_kwargs)
    if kwargs is None:
        diagnosis = explain_oracle_connect_error(exc)
        _log_diagnosis(diagnosis)
        return diagnosis
    probe_error = probe_oracle_connect(
        kwargs,
        oracledb_loader=oracledb_loader,
        tcp_connect_timeout_seconds=tcp_connect_timeout_seconds,
    )
    if probe_error is None:
        diagnosis = OracleConnectDiagnosis(
            "pool_timeout",
            "DPY-4005",
            _POOL_TIMEOUT_REASON,
            _POOL_TIMEOUT_ACTION,
            _exception_detail(exc, kwargs),
            probed=True,
        )
    else:
        diagnosis = explain_oracle_connect_error(
            probe_error, connect_kwargs=kwargs, probed=True
        ) or OracleConnectDiagnosis(
            "unknown",
            next(iter(oracle_error_codes(probe_error)), None),
            _UNKNOWN_REASON,
            _UNKNOWN_ACTION,
            _exception_detail(probe_error, kwargs),
            probed=True,
        )
    _log_diagnosis(diagnosis)
    return diagnosis


def _mapping_or_none(
    connect_kwargs: ConnectKwargsFactory | Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    return connect_kwargs if isinstance(connect_kwargs, Mapping) else None


def _resolve_kwargs(
    connect_kwargs: ConnectKwargsFactory | Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    if connect_kwargs is None:
        return None
    if isinstance(connect_kwargs, Mapping):
        return connect_kwargs
    try:
        return connect_kwargs()
    except Exception as exc:  # noqa: BLE001 - 接続引数を作れなければ直接の接続はしない
        logger.warning(
            "oracle_connect_probe_skipped",
            extra={"exception_type": type(exc).__name__},
        )
        return None


def _log_diagnosis(diagnosis: OracleConnectDiagnosis | None) -> None:
    if diagnosis is None:
        return
    logger.info(
        "oracle_connect_diagnosed",
        extra={
            "cause": diagnosis.category,
            "error_code": diagnosis.code,
            "probed": diagnosis.probed,
        },
    )


# ---- python-oracledb の背景のスレッドの例外 ---------------------------------------------------
#
# python-oracledb の thin の pool の背景のタスク（`_bg_task_func`）は、接続の作成に失敗した要求を
# 片づけるとき、待ちの時間切れで既に外された要求を `list.remove` して `ValueError` を出すことがある
# （4.0.2 と 26.0.1・main で同じコード。#1744）。CLI の出力に traceback が毎回出て、本当の原因と
# 取り違えやすいため、CLI ではそのスレッドの例外を 1 行の log にまとめる。

_ORACLEDB_HOOK_INSTALLED = threading.Event()


def _is_oracledb_thread_exception(args: threading.ExceptHookArgs) -> bool:
    for frame, _line in traceback.walk_tb(args.exc_traceback):
        # python-oracledb の package（`…/oracledb/…`・Cython の `src/oracledb/impl/…`）の frame。
        if "/oracledb/" in "/" + frame.f_code.co_filename.replace("\\", "/"):
            return True
    name = getattr(args.thread, "name", "") or ""
    return "_bg_task_func" in name


def install_oracledb_thread_excepthook() -> None:
    """python-oracledb のスレッドの例外を 1 行の warning の log にする（それ以外は今までどおり）。

    プロセスで 1 回だけ設定する。CLI だけで使う（常駐の server の挙動は変えない）。
    """
    if _ORACLEDB_HOOK_INSTALLED.is_set():
        return
    _ORACLEDB_HOOK_INSTALLED.set()
    previous = threading.excepthook

    def hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit or not _is_oracledb_thread_exception(args):
            previous(args)
            return
        message = str(args.exc_value or "").splitlines()
        logger.warning(
            "oracledb_background_thread_error",
            extra={
                "thread_name": getattr(args.thread, "name", None),
                "exception_type": getattr(args.exc_type, "__name__", "Exception"),
                "error_message": redact_connect_secrets(message[0][:200]) if message else None,
                "note": (
                    "python-oracledb の pool の背景のスレッドの例外（原因は接続の失敗の診断を見る）"
                ),
            },
        )

    threading.excepthook = hook


def _reset_oracledb_thread_excepthook_for_tests(previous: Callable[..., None]) -> None:
    """テスト用：hook を戻す。"""
    threading.excepthook = previous
    _ORACLEDB_HOOK_INSTALLED.clear()


__all__ = [
    "DEFAULT_PROBE_TCP_CONNECT_TIMEOUT_SECONDS",
    "OracleConnectCategory",
    "OracleConnectDiagnosis",
    "diagnose_oracle_connect_failure",
    "explain_oracle_connect_error",
    "install_oracledb_thread_excepthook",
    "probe_connect_kwargs",
    "probe_oracle_connect",
    "redact_connect_secrets",
]
