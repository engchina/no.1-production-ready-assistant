"""systemd(systemctl / journalctl)の実行層(#286)。

サービス管理画面の起動/停止・状態・ログは、カタログの allowlist にある unit だけを対象にする。

セキュリティ要件:
- unit 名は ``catalog.is_allowed_systemd_unit`` で検証し、それ以外(任意の unit・文字列)は
  argv を組み立てる前に ``ValueError`` にする。
- argv は固定の絶対パスとサブコマンドだけで組み立て、shell を通さない(``create_subprocess_exec``)。
- root が要る操作(起動/停止/再起動とログ)は ``sudo -n`` で実行する。sudoers は同じ argv だけを
  許可する(``rag/init_script.sh`` / ``rag/scripts/rag-services.sh`` が書く)。そのため
  ``journalctl`` の行数は固定(``JOURNAL_FETCH_LINES``)で取り、画面が求める行数へは
  Python で切り詰める。
- 状態の読み取り(``systemctl show``)は root が要らないため sudo を使わない。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from typing import Literal

from app.services.catalog import is_allowed_systemd_unit

SUDO = "/usr/bin/sudo"
SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
# sudoers で許可する journalctl の行数(固定)。API の上限(1000 行)と同じにする。
JOURNAL_FETCH_LINES = 1000
# ``systemctl show`` で読む property。
SHOW_PROPERTIES = ("LoadState", "ActiveState", "SubState", "UnitFileState")

SystemdAction = Literal["start", "stop", "restart"]

# sudo / systemd / journald が権限不足で拒否したときの出力(英語。ロケールは C で実行する)。
_PERMISSION_PATTERNS = (
    re.compile(r"a password is required", re.IGNORECASE),
    re.compile(r"is not allowed to (?:execute|run)", re.IGNORECASE),
    re.compile(r"not in the sudoers file", re.IGNORECASE),
    re.compile(r"a terminal is required", re.IGNORECASE),
    re.compile(r"access denied", re.IGNORECASE),
    re.compile(r"interactive authentication required", re.IGNORECASE),
    re.compile(r"permission denied", re.IGNORECASE),
    re.compile(r"no journal files were opened due to insufficient permissions", re.IGNORECASE),
    re.compile(r"not seeing messages from other users and the system", re.IGNORECASE),
)
# systemd が PID 1 でない(コンテナ・systemd なしの WSL など)ときの出力。
_UNAVAILABLE_PATTERNS = (
    re.compile(r"has not been booted with systemd", re.IGNORECASE),
    re.compile(r"failed to connect to bus", re.IGNORECASE),
    re.compile(r"system has not been booted", re.IGNORECASE),
)
# journalctl の情報行(本文ではない)。``-- No entries --`` はログが 0 行のとき、
# ``-- Logs begin at ... --`` / ``-- Boot <id> --`` は区切り。
_JOURNAL_META_LINE = re.compile(r"^-- .* --$")


def validate_unit(unit: str) -> str:
    """allowlist の unit 名だけを通す(それ以外は ``ValueError``)。"""
    if not is_allowed_systemd_unit(unit):
        raise ValueError(f"許可されていない systemd の unit です: {unit!r}")
    return unit


def systemctl_action_argv(action: SystemdAction, unit: str) -> list[str]:
    """起動/停止/再起動の argv(``sudo -n`` 付き・固定)。

    起動は ``enable --now``、停止は ``disable --now`` にして、利用者が最後に操作した状態を
    systemd の enable / disable として残す(再起動・再配備でもその状態に戻る。#286)。
    再起動は enable / disable を変えない。
    """
    validate_unit(unit)
    if action == "start":
        return [SUDO, "-n", SYSTEMCTL, "enable", "--now", unit]
    if action == "stop":
        return [SUDO, "-n", SYSTEMCTL, "disable", "--now", unit]
    if action == "restart":
        return [SUDO, "-n", SYSTEMCTL, "restart", unit]
    raise ValueError(f"未知の操作です: {action!r}")


def systemctl_show_argv(unit: str) -> list[str]:
    """unit の状態を読む argv(sudo なし)。"""
    validate_unit(unit)
    return [SYSTEMCTL, "show", unit, f"--property={','.join(SHOW_PROPERTIES)}", "--no-pager"]


def journalctl_argv(unit: str) -> list[str]:
    """unit のログ末尾を読む argv(``sudo -n`` 付き・行数固定)。"""
    validate_unit(unit)
    return [
        SUDO,
        "-n",
        JOURNALCTL,
        "-u",
        unit,
        "-n",
        str(JOURNAL_FETCH_LINES),
        "--no-pager",
        "-o",
        "short-iso",
    ]


@dataclass(frozen=True)
class UnitState:
    """``systemctl show`` の結果。"""

    load_state: str
    active_state: str
    sub_state: str
    unit_file_state: str

    @property
    def installed(self) -> bool:
        """unit ファイルが登録されているか(``LoadState=not-found`` は未登録)。"""
        return self.load_state not in {"", "not-found"}

    @property
    def enabled(self) -> bool:
        """起動時に自動で起動するか(利用者が最後に「起動」した状態)。"""
        return self.unit_file_state in {"enabled", "enabled-runtime", "linked", "alias"}


def parse_systemctl_show(stdout: str) -> UnitState:
    """``systemctl show --property=...`` の ``Key=Value`` 行を読む。"""
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return UnitState(
        load_state=values.get("LoadState", ""),
        active_state=values.get("ActiveState", ""),
        sub_state=values.get("SubState", ""),
        unit_file_state=values.get("UnitFileState", ""),
    )


def parse_journal(stdout: str, lines: int) -> str:
    """journalctl の出力から情報行(``-- No entries --`` 等)を除き、末尾 ``lines`` 行を返す。

    ログが 0 行なら空文字を返す。
    """
    body = [line for line in stdout.splitlines() if not _JOURNAL_META_LINE.match(line.strip())]
    return "\n".join(body[-lines:]) if lines > 0 else ""


def is_permission_error(output: str) -> bool:
    """sudo / systemd / journald の権限不足の出力か。"""
    return any(pattern.search(output) for pattern in _PERMISSION_PATTERNS)


def is_systemd_unavailable(output: str) -> bool:
    """systemd が使えない(PID 1 でない等)ときの出力か。"""
    return any(pattern.search(output) for pattern in _UNAVAILABLE_PATTERNS)


@dataclass(frozen=True)
class CommandOutput:
    """subprocess の終了コードと出力(decode 済み)。"""

    returncode: int
    stdout: str
    stderr: str

    @property
    def message(self) -> str:
        return self.stderr or self.stdout


class CommandUnavailableError(Exception):
    """コマンドを実行できなかった(コマンドが無い / timeout)。メッセージは利用者向け。"""


async def run_command(argv: list[str], timeout: float) -> CommandOutput:
    """argv を shell なし・timeout 付きで実行する。

    メッセージで権限不足などを判定するため、ロケールは C に固定する
    (利用者向けの文言は日本語で別に作る)。
    """
    env = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "SYSTEMD_PAGER": ""}
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise CommandUnavailableError(f"{argv[0]} が見つかりません。") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        with contextlib.suppress(ProcessLookupError):
            await process.wait()
        raise CommandUnavailableError(f"timeout({timeout:g} 秒)で打ち切りました。") from exc
    return CommandOutput(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=(stdout or b"").decode("utf-8", "replace").strip(),
        stderr=(stderr or b"").decode("utf-8", "replace").strip(),
    )
