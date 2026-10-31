"""配備で、ADMIN からアプリ用の DB ユーザーを作り、権限を付ける（#1668）。

platform/deploy/suite-init.sh が、製品の配備（各製品の system schema の初期化）の前に root で実行する。
3 製品は platform/.env の PLATFORM_ORACLE_USER（このユーザー）で接続し、システムの表はこのユーザーの schema に作る。

  python suite-db-user.py apply --admin-env <props/adb_admin.env> --app-env <props/platform.env> \
      --wallet-dir <Wallet を展開した directory> [--current-env <repository の platform/.env>]
  python suite-db-user.py plan [--app-user ADB_APP_USER]   # 実行する SQL を表示する（パスワードは出さない。DB に繋がない）

- 冪等: ユーザーが無ければ CREATE USER、あれば profile・quota を合わせ、アプリのパスワードで接続できなければ
  パスワードを合わせて lock を外す。grant は毎回付け直す（付いていれば何も変わらない）。
- 付ける権限は APP_USER_GRANTS（理由は terraform/README.md「アプリ用の DB ユーザー」と #1668 の調査）。
  Autonomous AI Database では ADMIN が DBA を持たず、GRANT DBA は PDB_DBA に読み替えられるため、PDB_DBA を付ける。
  PDB_DBA に無い Deep Data Security の 3 つの system privilege（Data Grant の作成・DATA ROLE の付与・
  USE DATA GRANTS ONLY）を足す。表領域は QUOTA UNLIMITED ON DATA。
- パスワードは環境変数・引数で受け取らず、root だけが読める props のファイルから読む（process の一覧に出さない）。
  SQL の log・失敗の表示にはパスワードを出さない。
- 失敗したら ORA の番号と手がかりを出して 1 で終わる（suite-init.sh が配備を失敗にする）。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any

LOG_PREFIX = "[suite-db-user]"
# アプリ用のユーザーの名前（Oracle の引用符なしの識別子。terraform/stack/variables.tf の adb_app_user_name と同じ規則）。
APP_USER_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,29}$")
# アプリ用に使えない名前（Oracle の管理のユーザー・ADMIN・NL2SQL の Deep Data Security の END USER）。
RESERVED_APP_USERS = frozenset(
    {"ADMIN", "SYS", "SYSTEM", "PUBLIC", "DEEPSEC_DATA_USER", "GGADMIN", "ADBSNMP", "C##CLOUD$SERVICE"}
)
APP_TABLESPACE = "DATA"
# Autonomous AI Database のアプリ用の profile（PASSWORD_LIFE_TIME UNLIMITED・FAILED_LOGIN_ATTEMPTS UNLIMITED）。
# DEFAULT（360 日でパスワードが切れ、10 回の失敗で lock）だと、3 製品の接続が 1 年で止まる。無ければ DEFAULT のまま。
APP_PROFILE = "ORA_APP_PROFILE"
APP_USER_GRANTS = (
    # ADB の DBA 相当。表・索引・sequence・view・package・SQL ドメイン・Oracle Text（CTXAPP）・Vector の索引・
    # DBA_* / V$ の参照（SELECT ANY DICTIONARY）・Select AI（DBMS_CLOUD / DBMS_CLOUD_AI）・role / END USER /
    # DATA ROLE / CONTEXT の作成（Deep Data Security）を含む。
    "GRANT PDB_DBA TO {user}",
    # Deep Data Security（NL2SQL）: PDB_DBA に無い。無いと ORA-01031。
    "GRANT GRANT ANY DATA ROLE TO {user}",  # GRANT DATA ROLE … TO <END USER>
    "GRANT ADMINISTER ANY DATA GRANT TO {user}",  # CREATE / DROP DATA GRANT
    "GRANT SET USE DATA GRANTS ONLY TO {user}",  # SET USE DATA GRANTS ONLY ON <表> ENABLED / DISABLED
)
# 付けた後に、アプリのユーザーの session で効いていることを確かめる権限（PDB_DBA の代表と、足した 3 つ）。
REQUIRED_SESSION_PRIVS = (
    "CREATE TABLE",
    "CREATE END USER",
    "GRANT ANY DATA ROLE",
    "ADMINISTER ANY DATA GRANT",
    "SET USE DATA GRANTS ONLY",
)
# 1 回の接続の TCP の接続の上限（秒）。DB の停止中・届かないときに長く待たない（#1779）。待てば直りうる失敗は
# connect_admin が connect_wait_seconds の間隔で試し直す。
CONNECT_TCP_TIMEOUT_SECONDS = 10.0
_TNS_ALIAS_RE = re.compile(r"(?im)^\s*([A-Za-z0-9_.-]+)\s*=\s*")
_TNS_RETRY_RE = re.compile(r"\(\s*retry_(?:count|delay)\s*=\s*\d+\s*\)", re.IGNORECASE)
# 接続の失敗のうち、待てば直りうるもの（ADB の起動の直後・network の一時の失敗）。
RETRYABLE_CONNECT_ERRORS = ("DPY-6005", "DPY-6000", "DPY-4011", "ORA-12506", "ORA-12514", "ORA-12541", "ORA-12170")
# アプリのパスワードで接続できないときに、パスワード・lock を合わせれば直るもの。
LOGIN_REPAIRABLE_ERRORS = ("ORA-01017", "ORA-28000", "ORA-28001", "ORA-28002")
HINTS = {
    "ORA-01017": "ADMIN のパスワード（新規の ADB は adb_password、既存の ADB は existing_adb_admin_password）が違います。",
    "ORA-28000": "ADMIN が lock されています。OCI のコンソールで ADMIN のパスワードを設定し直してください。",
    "ORA-01031": "権限が足りません。ADMIN で接続しているか確かめてください。",
    "ORA-01920": "同じ名前の role があります。adb_app_user_name を変えてください。",
    "ORA-28003": "パスワードが ADB の規則（12〜30 文字・大文字・小文字・数字を含む・ユーザー名を含まない）を満たしていません。",
    "ORA-28007": "以前に使ったパスワードは使えません。adb_app_user_password を変えてください。",
    "ORA-01950": "表領域 DATA の quota がありません。",
    "ORA-00959": "表領域 DATA がありません（Autonomous AI Database ではない DB です）。",
    "DPY-6005": "ADB に接続できません。ADB が起動しているか、ACL・プライベート・エンドポイント（TCP 1522）で Compute から到達できるか確かめてください。",
}


class SuiteDbUserError(Exception):
    """配備を失敗にする誤り（ORA の番号と手がかりを持つ）。"""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


def log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", flush=True)


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE の file（props・platform/.env）を読む。値の前後の single / double quote は外す。"""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def normalize_user(value: str, *, label: str) -> str:
    user = value.strip().upper()
    if not APP_USER_PATTERN.fullmatch(user):
        raise SuiteDbUserError(f"{label} は英字で始まる 30 文字以内の英大文字・数字・_ にしてください（{value!r}）。")
    if user in RESERVED_APP_USERS or user.startswith(("ORA_", "SYS")):
        raise SuiteDbUserError(f"{label} に {user} は使えません（Oracle の管理のユーザー・ADMIN と同じ名前）。")
    return user


def check_password(password: str, *, label: str, user: str = "") -> None:
    if not password:
        raise SuiteDbUserError(f"{label} が空です。")
    if any(ch in password for ch in "\"'\r\n"):
        raise SuiteDbUserError(f"{label} に引用符・改行は使えません。")
    if user and user.lower() in password.lower():
        raise SuiteDbUserError(f"{label} にユーザー名（{user}）を含めないでください（ADB のパスワードの規則）。")


def error_code(exc: BaseException) -> str:
    """oracledb の例外から ORA-xxxxx / DPY-xxxx を取り出す（無ければ空）。"""
    text = str(exc)
    match = re.search(r"\b(ORA-\d{5}|DPY-\d{4}|PLS-\d{5})\b", text)
    return match.group(1) if match else ""


def describe_error(exc: BaseException, *secrets: str) -> str:
    code = error_code(exc)
    message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    hint = HINTS.get(code, "")
    return f"{message}{'（' + hint + '）' if hint else ''}"


def mask(sql: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            sql = sql.replace(secret, "***")
    return sql


def dsn_without_tns_retry(dsn: str, wallet_dir: str) -> str:
    """Wallet の別名・接続記述子から、TNS の再試行（`(retry_count=20)(retry_delay=3)`）を外す（#1779）。

    python-oracledb は記述子の値を `connect()` の `retry_count` より優先するため、外さないと DB の停止中に
    1 回の接続が 1 分以上（届かないときは数分）続く。`pr_backend_core.oracle_dsn.dsn_without_retry` と同じ
    規則（この script は製品の venv を使わずに動くため、ここに持つ）。別名が無ければ元の DSN のまま。
    """
    descriptor = dsn
    if not dsn.lstrip().startswith("(") and wallet_dir:
        descriptor = _tns_alias_descriptor(Path(wallet_dir) / "tnsnames.ora", dsn) or dsn
    if descriptor.lstrip().startswith("("):
        return _TNS_RETRY_RE.sub("", descriptor)
    return descriptor


def _tns_alias_descriptor(tnsnames: Path, alias: str) -> str | None:
    try:
        content = tnsnames.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for match in _TNS_ALIAS_RE.finditer(content):
        if match.group(1).lower() != alias.strip().lower():
            continue
        start = content.find("(", match.end())
        if start < 0:
            return None
        depth = 0
        for index in range(start, len(content)):
            if content[index] == "(":
                depth += 1
            elif content[index] == ")":
                depth -= 1
                if depth == 0:
                    return content[start : index + 1]
        return None
    return None


def plan_statements(user: str, *, create: bool, profile: str = APP_PROFILE) -> list[str]:
    """実行する DDL / GRANT（パスワードは <password>）。apply と plan で同じ一覧を使う。"""
    statements = []
    if create:
        statements.append(
            f'CREATE USER {user} IDENTIFIED BY "<password>" DEFAULT TABLESPACE {APP_TABLESPACE} '
            f"QUOTA UNLIMITED ON {APP_TABLESPACE} PROFILE {profile}"
        )
    else:
        statements.append(
            f"ALTER USER {user} DEFAULT TABLESPACE {APP_TABLESPACE} QUOTA UNLIMITED ON {APP_TABLESPACE} PROFILE {profile}"
        )
    statements.extend(grant.format(user=user) for grant in APP_USER_GRANTS)
    return statements


class Provisioner:
    def __init__(
        self,
        oracledb: Any,
        *,
        dsn: str,
        wallet_dir: str,
        wallet_password: str,
        connect_attempts: int = 5,
        connect_wait_seconds: float = 20.0,
    ) -> None:
        self.oracledb = oracledb
        self.dsn = dsn
        self.wallet_dir = wallet_dir
        self.wallet_password = wallet_password
        self.connect_attempts = connect_attempts
        self.connect_wait_seconds = connect_wait_seconds
        # 試し直しは connect_admin の間隔に任せ、1 回の接続では TNS の再試行をしない（#1779）。
        self.connect_dsn = dsn_without_tns_retry(dsn, wallet_dir)

    def connect(self, user: str, password: str) -> Any:
        kwargs: dict[str, Any] = {
            "user": user,
            "password": password,
            "dsn": self.connect_dsn,
            "retry_count": 0,
            "retry_delay": 0,
            "tcp_connect_timeout": CONNECT_TCP_TIMEOUT_SECONDS,
        }
        if self.wallet_dir:
            kwargs["config_dir"] = self.wallet_dir
            kwargs["wallet_location"] = self.wallet_dir
        if self.wallet_password:
            kwargs["wallet_password"] = self.wallet_password
        return self.oracledb.connect(**kwargs)

    def connect_admin(self, user: str, password: str) -> Any:
        """ADMIN で接続する。ADB の起動の直後などの一時の失敗は待って試し直す。"""
        last: BaseException | None = None
        for attempt in range(1, self.connect_attempts + 1):
            try:
                return self.connect(user, password)
            except Exception as exc:  # noqa: BLE001 - oracledb の例外を ORA の番号で分ける
                last = exc
                code = error_code(exc)
                if code not in RETRYABLE_CONNECT_ERRORS or attempt == self.connect_attempts:
                    break
                log(
                    f"{user} で接続できません（{code}）。{self.connect_wait_seconds:g} 秒後に試し直します"
                    f"（{attempt}/{self.connect_attempts}）。"
                )
                time.sleep(self.connect_wait_seconds)
        assert last is not None
        raise SuiteDbUserError(
            f"{user} で ADB（{self.dsn}）に接続できません: {describe_error(last, password)}",
            code=error_code(last),
        )

    def try_login(self, user: str, password: str) -> str:
        """アプリのパスワードで接続できるか。できれば空、できなければ ORA の番号。"""
        try:
            connection = self.connect(user, password)
        except Exception as exc:  # noqa: BLE001
            return error_code(exc) or "unknown"
        connection.close()
        return ""


def execute(cursor: Any, sql: str, *secrets: str, binds: dict[str, Any] | None = None) -> None:
    try:
        cursor.execute(sql, binds or {})
    except Exception as exc:  # noqa: BLE001
        shown = mask(sql, *secrets)
        raise SuiteDbUserError(
            f"{shown} が失敗しました: {describe_error(exc, *secrets)}", code=error_code(exc)
        ) from exc


def fetch_one(cursor: Any, sql: str, binds: dict[str, Any]) -> tuple[Any, ...] | None:
    cursor.execute(sql, binds)
    return cursor.fetchone()


def apply(args: argparse.Namespace, oracledb: Any) -> int:
    admin_env = read_env_file(Path(args.admin_env))
    app_env = read_env_file(Path(args.app_env))
    admin_user = normalize_admin(admin_env.get("SUITE_DB_ADMIN_USER", "ADMIN"))
    admin_password = admin_env.get("SUITE_DB_ADMIN_PASSWORD", "")
    check_password(admin_password, label="ADMIN のパスワード（SUITE_DB_ADMIN_PASSWORD）")
    app_user = normalize_user(admin_env.get("SUITE_DB_APP_USER", ""), label="アプリ用のユーザー（SUITE_DB_APP_USER）")
    env_user = app_env.get("PLATFORM_ORACLE_USER", "").strip().upper()
    if env_user != app_user:
        raise SuiteDbUserError(
            f"{args.app_env} の PLATFORM_ORACLE_USER（{env_user or '空'}）がアプリ用のユーザー（{app_user}）と違います。"
        )
    app_password = app_env.get("PLATFORM_ORACLE_PASSWORD", "")
    # 画面（システム設定 > データベース）で保存したパスワードを優先する（3 製品はそれで接続する）。
    # ユーザーが別に変わっていれば、stack の入力（props）のパスワードに合わせる。
    if args.current_env and Path(args.current_env).is_file():
        current = read_env_file(Path(args.current_env))
        if current.get("PLATFORM_ORACLE_USER", "").strip().upper() == app_user and current.get(
            "PLATFORM_ORACLE_PASSWORD"
        ):
            if current["PLATFORM_ORACLE_PASSWORD"] != app_password:
                log(f"{args.current_env} のパスワード（画面で保存した値）に合わせます。")
            app_password = current["PLATFORM_ORACLE_PASSWORD"]
    check_password(app_password, label="アプリ用のユーザーのパスワード（PLATFORM_ORACLE_PASSWORD）", user=app_user)
    dsn = app_env.get("PLATFORM_ORACLE_DSN", "").strip()
    if not dsn:
        raise SuiteDbUserError(f"{args.app_env} に PLATFORM_ORACLE_DSN がありません。")
    secrets = (admin_password, app_password)

    provisioner = Provisioner(
        oracledb,
        dsn=dsn,
        wallet_dir=args.wallet_dir,
        wallet_password=app_env.get("PLATFORM_ORACLE_WALLET_PASSWORD", "") or app_password,
        connect_attempts=args.connect_attempts,
        connect_wait_seconds=args.connect_wait_seconds,
    )
    log(f"{admin_user} で接続し、アプリ用のユーザー {app_user} を準備します（DSN {dsn}）。")
    connection = provisioner.connect_admin(admin_user, admin_password)
    try:
        cursor = connection.cursor()
        current_user = (fetch_one(cursor, "SELECT USER FROM DUAL", {}) or ("",))[0]
        if str(current_user).upper() != admin_user:
            raise SuiteDbUserError(f"接続したユーザーが {current_user} です（{admin_user} のはず）。")
        has_profile = fetch_one(
            cursor, "SELECT COUNT(*) FROM DBA_PROFILES WHERE PROFILE = :profile", {"profile": APP_PROFILE}
        )
        profile = APP_PROFILE if has_profile and int(has_profile[0]) > 0 else "DEFAULT"
        if profile != APP_PROFILE:
            log(f"profile {APP_PROFILE} がありません。DEFAULT のまま作ります（パスワードの期限に注意）。")
        existing = fetch_one(
            cursor,
            "SELECT ACCOUNT_STATUS, ORACLE_MAINTAINED FROM DBA_USERS WHERE USERNAME = :username",
            {"username": app_user},
        )
        if existing and str(existing[1]).upper() == "Y":
            raise SuiteDbUserError(f"{app_user} は Oracle の管理のユーザーです。adb_app_user_name を変えてください。")
        statements = plan_statements(app_user, create=existing is None, profile=profile)
        first = statements[0].replace('"<password>"', f'"{app_password}"')
        execute(cursor, first, *secrets)
        log(mask(first, *secrets) + "（" + ("作成" if existing is None else f"既存。状態 {existing[0]}") + "）")
        if existing is not None:
            failure = provisioner.try_login(app_user, app_password)
            if failure in LOGIN_REPAIRABLE_ERRORS:
                execute(cursor, f'ALTER USER {app_user} IDENTIFIED BY "{app_password}" ACCOUNT UNLOCK', *secrets)
                # ORA_APP_PROFILE は PASSWORD_ROLLOVER_TIME（既定 90 日）のあいだ古いパスワードでも接続できる。
                # 合わせた後は新しいパスワードだけにする（古い値が漏れて変えた場合に備える）。
                if profile == APP_PROFILE:
                    execute(cursor, f"ALTER USER {app_user} EXPIRE PASSWORD ROLLOVER PERIOD", *secrets)
                log(f"{app_user} のパスワードを PLATFORM_ORACLE_PASSWORD に合わせ、lock を外しました（{failure}）。")
            elif failure:
                raise SuiteDbUserError(f"{app_user} の接続の確認が失敗しました（{failure}）。", code=failure)
        for statement in statements[1:]:
            execute(cursor, statement, *secrets)
            log(statement)
    finally:
        connection.close()

    # アプリのパスワードで接続でき、権限が効いていることを確かめる（3 製品と同じ接続）。
    failure = provisioner.try_login(app_user, app_password)
    if failure:
        raise SuiteDbUserError(f"{app_user} で接続できません（{failure}）。{HINTS.get(failure, '')}", code=failure)
    check = provisioner.connect(app_user, app_password)
    try:
        cursor = check.cursor()
        cursor.execute("SELECT PRIVILEGE FROM SESSION_PRIVS", {})
        granted = {str(row[0]) for row in cursor.fetchall()}
    finally:
        check.close()
    missing = sorted(set(REQUIRED_SESSION_PRIVS) - granted)
    if missing:
        raise SuiteDbUserError(f"{app_user} に権限が付いていません: {', '.join(missing)}。")
    log(f"OK: {app_user} で接続でき、権限が付いています（profile {profile}、quota UNLIMITED ON {APP_TABLESPACE}）。")
    return 0


def normalize_admin(value: str) -> str:
    user = value.strip().upper() or "ADMIN"
    if not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", user):
        raise SuiteDbUserError(f"SUITE_DB_ADMIN_USER が不正です（{value!r}）。")
    return user


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    apply_parser = sub.add_parser("apply", help="ADMIN で接続し、アプリ用のユーザーを作って権限を付ける")
    apply_parser.add_argument("--admin-env", required=True, help="SUITE_DB_ADMIN_USER / _PASSWORD / SUITE_DB_APP_USER")
    apply_parser.add_argument("--app-env", required=True, help="PLATFORM_ORACLE_*（cloud-init の platform.env）")
    apply_parser.add_argument("--current-env", default="", help="repository の platform/.env（画面で保存した値）")
    apply_parser.add_argument("--wallet-dir", default="", help="Wallet（tnsnames.ora・ewallet.pem）の directory")
    apply_parser.add_argument("--connect-attempts", type=int, default=5)
    apply_parser.add_argument("--connect-wait-seconds", type=float, default=20.0)
    plan_parser = sub.add_parser("plan", help="実行する SQL を表示する（DB に繋がない）")
    plan_parser.add_argument("--app-user", default="ADB_APP_USER")
    plan_parser.add_argument("--existing", action="store_true", help="既にユーザーがあるときの SQL")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "plan":
            user = normalize_user(args.app_user, label="--app-user")
            for statement in plan_statements(user, create=not args.existing):
                print(statement + ";")
            return 0
        import oracledb  # noqa: PLC0415 - plan では要らない（uv run --with oracledb で渡す）

        return apply(args, oracledb)
    except SuiteDbUserError as exc:
        log(f"NG{'（' + exc.code + '）' if exc.code else ''}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
