"""ローカル起動の工程と失敗処理を外部サービスなしで検証する。"""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

SUITE = Path(__file__).resolve().parents[3]


# start-frontend.sh が探す CA bundle の場所（Debian / Ubuntu、RHEL / Oracle Linux の順）。
_SYSTEM_CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")


def _executable(path: Path, text: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -eu\n" + text, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture(params=["rag", "nl2sql", "agent"])
def startup(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, dict[str, str]]:
    source = SUITE / request.param / "scripts"
    scripts = tmp_path / request.param / "scripts"
    scripts.mkdir(parents=True)
    (scripts.parent / "backend").mkdir()
    for name in ("start-all.sh", "start-backend.sh", "start-frontend.sh"):
        shutil.copy2(source / name, scripts / name)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    _executable(binaries / "lsof", "exit 1\n")
    _executable(
        binaries / "uv",
        """
if [ "$1" = sync ]; then
  echo sync-start >> "$TEST_EVENTS"
  touch "$TEST_DIR/preparing"
  sleep "${TEST_SYNC_DELAY:-0}"
  if [ "${TEST_SYNC_FAIL:-0}" != 0 ]; then exit 23; fi
  echo sync-end >> "$TEST_EVENTS"
  exit 0
fi
[ "$1" = run ] && [ "$2" = --no-sync ]
printf '%s\n%s\n' "${NO_PROXY-<unset>}" "${no_proxy-<unset>}" > "$TEST_DIR/server-no-proxy"
echo server >> "$TEST_EVENTS"
sleep "${TEST_SERVER_DELAY:-0}"
if [ "${TEST_SERVER_FAIL:-0}" != 0 ]; then exit 24; fi
touch "$TEST_DIR/serving"
exec sleep 60
""",
    )
    _executable(
        binaries / "curl",
        """
echo "health:$*" >> "$TEST_EVENTS"
# 環境変数のプロキシを使うと、ローカルの backend へ届かず失敗する（502。#781）。
direct=0
previous=""
for arg in "$@"; do
  if [ "$previous" = --noproxy ] && [ "$arg" = "*" ]; then direct=1; fi
  previous="$arg"
done
if [ -n "${http_proxy:-}${HTTP_PROXY:-}" ] && [ "$direct" = 0 ]; then exit 22; fi
# DB readiness は失敗しても、アプリの health が成功すれば画面は起動できる。
[[ "$*" = *"/api/health" ]] || exit 1
[ "${TEST_NOT_READY:-0}" = 0 ] && [ -f "$TEST_DIR/serving" ]
""",
    )
    _executable(scripts / "start-frontend.sh", 'echo frontend >> "$TEST_EVENTS"\n')
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("BACKEND_", "FRONTEND_", "TEST_"))
    }
    env.update(
        PATH=f"{binaries}:{env['PATH']}",
        TEST_DIR=str(tmp_path),
        TEST_SOURCE=str(source),
        TEST_EVENTS=str(tmp_path / "events"),
        BACKEND_READY_TIMEOUT_SECONDS="2",
    )
    return scripts, env


def _run(startup: tuple[Path, dict[str, str]]) -> subprocess.CompletedProcess[str]:
    scripts, env = startup
    return subprocess.run(
        ["bash", str(scripts / "start-all.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_slow_dependency_preparation_does_not_consume_health_timeout(
    startup: tuple[Path, dict[str, str]],
) -> None:
    _, env = startup
    env["TEST_SYNC_DELAY"] = "3"
    result = _run(startup)
    assert result.returncode == 0, result.stdout + result.stderr
    events = Path(env["TEST_EVENTS"]).read_text().splitlines()
    assert events[:2] == ["sync-start", "sync-end"]
    assert events.count("sync-start") == 1
    assert events.index("server") < events.index("frontend")
    assert all("/api/ready" not in event for event in events)


def test_readiness_does_not_use_the_proxy_from_the_environment(
    startup: tuple[Path, dict[str, str]],
) -> None:
    """NO_PROXY に 127.0.0.1 が無いプロキシの環境でも、ローカルの backend を確かめる（#781）。"""
    _, env = startup
    for key in ("no_proxy", "NO_PROXY"):
        env.pop(key, None)
    env.update(http_proxy="http://proxy.invalid:80", HTTP_PROXY="http://proxy.invalid:80")
    result = _run(startup)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "backend ready を確認しました" in result.stdout


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({}, ["localhost,127.0.0.1,::1"] * 2),
        (
            {"NO_PROXY": "example.com,127.0.0.1"},
            ["example.com,127.0.0.1,localhost,::1"] * 2,
        ),
        (
            {"NO_PROXY": ".corp", "no_proxy": "localhost,10.0.0.1"},
            [".corp,localhost,127.0.0.1,::1", "localhost,10.0.0.1,127.0.0.1,::1"],
        ),
    ],
    ids=["unset", "upper-only", "both"],
)
def test_backend_does_not_send_loopback_calls_through_the_proxy(
    startup: tuple[Path, dict[str, str]], environment: dict[str, str], expected: list[str]
) -> None:
    """backend は既存の NO_PROXY を残したまま loopback を足して起動する（#852）。"""
    _, env = startup
    for key in ("no_proxy", "NO_PROXY"):
        env.pop(key, None)
    env.update(http_proxy="http://proxy.invalid:80", HTTP_PROXY="http://proxy.invalid:80")
    env.update(environment)
    result = _run(startup)
    assert result.returncode == 0, result.stdout + result.stderr
    no_proxy = (Path(env["TEST_DIR"]) / "server-no-proxy").read_text().splitlines()
    assert no_proxy == expected


def test_readiness_waits_for_slow_backend_and_reports_progress(
    startup: tuple[Path, dict[str, str]],
) -> None:
    """負荷が高く backend の起動が遅くても、生きている間は経過を出して待ち切る（#1935）。"""
    _, env = startup
    env.update(
        TEST_SERVER_DELAY="3",
        BACKEND_READY_TIMEOUT_SECONDS="10",
        BACKEND_READY_PROGRESS_SECONDS="1",
    )
    result = _run(startup)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "backend は起動中です" in result.stdout
    assert "上限 10s" in result.stdout
    assert "backend ready を確認しました" in result.stdout
    events = Path(env["TEST_EVENTS"]).read_text().splitlines()
    assert events[-1] == "frontend"


def test_readiness_default_timeout_is_long_enough_for_loaded_machines() -> None:
    """3 製品の start-all.sh は同じ既定（上限 300 秒・経過は 15 秒ごと）を持つ（#1935）。"""
    for product in ("rag", "nl2sql", "agent"):
        text = (SUITE / product / "scripts" / "start-all.sh").read_text(encoding="utf-8")
        assert 'BACKEND_READY_TIMEOUT_SECONDS="${BACKEND_READY_TIMEOUT_SECONDS:-300}"' in text
        assert 'BACKEND_READY_PROGRESS_SECONDS="${BACKEND_READY_PROGRESS_SECONDS:-15}"' in text


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("TEST_SERVER_FAIL", "backend プロセスが ready 前に終了しました"),
        ("TEST_NOT_READY", "backend readiness の待機がタイムアウトしました (2s)"),
    ],
    ids=["exited", "timeout"],
)
def test_readiness_fails_only_when_backend_exits_or_cap_passes(
    startup: tuple[Path, dict[str, str]], failure: str, message: str
) -> None:
    _, env = startup
    env[failure] = "1"
    result = _run(startup)
    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.parametrize("failure", ["TEST_SYNC_FAIL", "TEST_SERVER_FAIL", "TEST_NOT_READY"])
def test_failed_stage_does_not_start_frontend(
    startup: tuple[Path, dict[str, str]], failure: str
) -> None:
    _, env = startup
    env[failure] = "1"
    result = _run(startup)
    assert result.returncode != 0
    events = Path(env["TEST_EVENTS"]).read_text().splitlines()
    assert "frontend" not in events
    if failure == "TEST_SYNC_FAIL":
        assert result.returncode == 23
        assert "server" not in events
        assert "readiness" not in result.stdout


def test_cancellation_during_dependency_preparation(startup: tuple[Path, dict[str, str]]) -> None:
    scripts, env = startup
    env["TEST_SYNC_DELAY"] = "60"
    with subprocess.Popen(
        ["bash", str(scripts / "start-all.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            deadline = time.monotonic() + 5
            while not (Path(env["TEST_DIR"]) / "preparing").exists():
                assert time.monotonic() < deadline
                time.sleep(0.05)
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=8)
            assert process.returncode == 143
        finally:
            if process.poll() is None:
                process.kill()
    assert Path(env["TEST_EVENTS"]).read_text().splitlines() == ["sync-start"]


@pytest.mark.parametrize("custom_ca", [False, True])
def test_frontend_uses_system_ca_and_preserves_explicit_ca(
    startup: tuple[Path, dict[str, str]], custom_ca: bool
) -> None:
    scripts, env = startup
    shutil.copy2(Path(env["TEST_SOURCE"]) / "start-frontend.sh", scripts / "start-frontend.sh")
    platform = scripts.parents[1] / "platform"
    ui = platform / "packages" / "ui"
    (ui / "dist").mkdir(parents=True)
    (platform / "node_modules").mkdir()
    for path in [platform / "package.json", ui / "package.json"]:
        path.write_text("{}")
    for name in ("index.js", "index.d.ts", "tokens.css"):
        (ui / "dist" / name).touch()
    (scripts.parent / "frontend").mkdir()
    # 明示が無いときは、この OS で読める最初の CA bundle（無ければ未設定）。
    expected = next((path for path in _SYSTEM_CA_BUNDLES if os.access(path, os.R_OK)), "")
    env.pop("NODE_EXTRA_CA_CERTS", None)
    if custom_ca:
        expected = str(platform / "custom-ca.pem")
        env["NODE_EXTRA_CA_CERTS"] = expected
    _executable(
        Path(env["TEST_DIR"]) / "bin" / "npm",
        'printf "%s\\n" "${NODE_EXTRA_CA_CERTS:-}" >> "$TEST_EVENTS"\n',
    )
    result = subprocess.run(
        ["bash", str(scripts / "start-frontend.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # 共有 UI の成果物がソースより新しいので build を省き、npm install と npm run dev だけ（#816）。
    assert Path(env["TEST_EVENTS"]).read_text().splitlines() == [expected] * 2


def _frontend_with_shared_ui(startup: tuple[Path, dict[str, str]]) -> tuple[Path, Path]:
    scripts, env = startup
    shutil.copy2(Path(env["TEST_SOURCE"]) / "start-frontend.sh", scripts / "start-frontend.sh")
    platform = scripts.parents[1] / "platform"
    ui = platform / "packages" / "ui"
    (ui / "src").mkdir(parents=True)
    (ui / "dist").mkdir()
    (platform / "node_modules").mkdir()
    for path in [platform / "package.json", ui / "package.json", ui / "src" / "button.tsx"]:
        path.write_text("{}")
    for name in ("index.js", "index.d.ts", "tokens.css"):
        (ui / "dist" / name).touch()
    linked_ui = scripts.parent / "frontend" / "node_modules" / "@production-ready" / "ui"
    linked_ui.mkdir(parents=True)
    (linked_ui / "package.json").write_text("{}")
    _executable(Path(env["TEST_DIR"]) / "bin" / "npm", 'printf "%s\\n" "$*" >> "$TEST_EVENTS"\n')
    return scripts, ui


def _run_frontend(scripts: Path, env: dict[str, str]) -> list[str]:
    result = subprocess.run(
        ["bash", str(scripts / "start-frontend.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return Path(env["TEST_EVENTS"]).read_text().splitlines()


@pytest.mark.parametrize("script_name", ["start-backend.sh", "start-frontend.sh"])
def test_port_cleanup_stops_listener_but_preserves_connected_client(
    startup: tuple[Path, dict[str, str]], script_name: str
) -> None:
    """宛先 port が同じ client を停止せず、既存 listener だけを置き換える（#880）。"""
    scripts, env = startup
    if script_name == "start-frontend.sh":
        scripts, _ = _frontend_with_shared_ui(startup)
    listener = subprocess.Popen(["sleep", "60"])
    client = subprocess.Popen(["sleep", "60"])
    try:
        env.update(TEST_LISTENER_PID=str(listener.pid), TEST_CLIENT_PID=str(client.pid))
        _executable(
            Path(env["TEST_DIR"]) / "bin" / "lsof",
            """
# TCP の port 指定だけでは接続側も一致する。LISTEN の絞り込みなら listener だけ。
if [[ " $* " = *" -sTCP:LISTEN "* ]]; then
  echo "$TEST_LISTENER_PID"
else
  printf '%s\\n%s\\n' "$TEST_LISTENER_PID" "$TEST_CLIENT_PID"
fi
""",
        )
        args = ["bash", str(scripts / script_name)]
        if script_name == "start-backend.sh":
            # HTTP サーバを起動せず、port の掃除が終わったところまでを確認する。
            args.append("--no-sync")
            env["TEST_SERVER_FAIL"] = "1"
        result = subprocess.run(
            args, env=env, capture_output=True, text=True, timeout=10, check=False
        )
        assert result.returncode == (24 if script_name == "start-backend.sh" else 0), (
            result.stdout + result.stderr
        )
        assert listener.wait(timeout=2) == -signal.SIGTERM
        assert client.poll() is None, "port の宛先が一致する client は停止しない"
    finally:
        for process in (listener, client):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=2)


def test_frontend_builds_shared_ui_only_when_sources_changed(
    startup: tuple[Path, dict[str, str]],
) -> None:
    """共有 UI は成果物より新しいソースがあるときだけ build する。

    3 製品の同時起動で、起動中の別の製品が読む dist を消さない（#816）。
    """
    _, env = startup
    scripts, ui = _frontend_with_shared_ui(startup)
    events = Path(env["TEST_EVENTS"])

    assert not any(line.startswith("run build") for line in _run_frontend(scripts, env))

    # dist より新しいソースがあれば build する。
    later = time.time() + 5
    os.utime(ui / "src" / "button.tsx", (later, later))
    events.write_text("")
    assert "run build" in _run_frontend(scripts, env)

    # FORCE_SHARED_UI_BUILD=1 なら常に build する。
    for name in ("index.js", "index.d.ts", "tokens.css"):
        os.utime(ui / "dist" / name, (later + 5, later + 5))
    events.write_text("")
    assert "run build" in _run_frontend(scripts, {**env, "FORCE_SHARED_UI_BUILD": "1"})
