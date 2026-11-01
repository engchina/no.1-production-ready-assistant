"""配布物(ネイティブ配備: uv の venv + systemd。#286)の最低限の本番運用契約のテスト。"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# monorepo（#71）では GitHub workflow は suite root（rag/ の親）の .github に置く。
SUITE_ROOT = REPO_ROOT.parent


def _init_script() -> str:
    return (REPO_ROOT / "init_script.sh").read_text(encoding="utf-8")


def _function_body(script: str, name: str) -> str:
    """bash の関数 ``name() { ... }`` の本文を返す(行頭の ``}`` まで)。"""
    import re

    match = re.search(rf"(?ms)^{name}\(\) {{\n(.*?)^}}$", script)
    assert match is not None, f"{name} が見つからない"
    return match.group(1)


def test_frontend_build_uses_reproducible_install() -> None:
    """frontend は lockfile で再現可能に依存解決して build する(#177)。"""
    build = _function_body(_init_script(), "build_frontend")

    # 共有パッケージ(platform)を先に build し、frontend の file: 依存を解決する。
    steps = [
        '"${PLATFORM_DIR}" "npm ci"',
        '"${PLATFORM_DIR}" "npm run build"',
        '"${FRONTEND_DIR}" "npm ci"',
        # 1 台の Compute（#1316）では /rag/ を base に build する（単独の配備は /）。
        '"${FRONTEND_DIR}" "FRONTEND_BASE_PATH=\'${FRONTEND_BASE_PATH}\' npm run build"',
    ]
    positions = [build.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "npm install" not in _init_script()
    # suite で先の製品が共有 UI を build したときだけ build し直さない。
    assert 'if [ "${PR_SUITE_SKIP_PLATFORM_UI_BUILD}" = "true" ]; then' in build


def test_frontend_is_built_as_static_assets_without_reverse_proxy() -> None:
    """frontend は /rag/ を base に静的に build し、reverse proxy の site は書かない。

    RAG は単独では配備しない(#1316)。suite も Nginx を配備しない(#19)。
    前段の reverse proxy は利用者が置く。
    """
    deploy_dir = REPO_ROOT.parent / "platform" / "deploy"

    assert "configure_nginx" not in _init_script()
    assert not (deploy_dir / "suite-nginx.sh").exists()
    assert not (deploy_dir / "suite-tls.sh").exists()
    suite_init = (deploy_dir / "suite-init.sh").read_text(encoding="utf-8")
    body = suite_init.split("\nset -euo pipefail", 1)[1]
    assert "nginx" not in body.lower()


def test_services_run_as_dedicated_non_root_user() -> None:
    """backend・取込 worker・前処理 / parser は専用の非 root ユーザーで動く。"""
    init_script = _init_script()

    assert 'SERVICE_USER="${SERVICE_USER:-ragsvc}"' in init_script
    assert "User=${SERVICE_USER}" in _function_body(init_script, "write_backend_unit")
    assert "User=${user}" in _systemd_script()
    assert "User=root" not in init_script + _systemd_script()


def test_backend_venv_is_locked_and_excludes_heavy_parser_adapters() -> None:
    """backend の venv は lock どおり(開発用の依存なし)で、重い parser 依存を載せない。

    外部 parser は services/parsers/<name> の独立サービスへ切り出したため、backend は
    `--extra parser-adapters` などの parser の extra を同期しない(HTTP 委譲)。
    """
    venvs = _function_body(_init_script(), "install_service_venvs")

    assert '"${BACKEND_DIR}" "uv sync --locked --no-dev --python ${RAG_PYTHON_VERSION}"' in venvs
    assert "--extra" not in venvs
    assert "--extra" not in _function_body(_systemd_script(), "rag_uv_sync_args")
    # backend は共有 contract package を path 依存で取り込む(uv.lock に固定される)。
    pyproject = (REPO_ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    assert "rag-parser-core" in pyproject
    assert "../packages/rag_parser_core" in pyproject


def test_backend_unit_uses_gunicorn_uvicorn_worker() -> None:
    """backend の unit は Gunicorn で Uvicorn worker を管理する。"""
    pyproject = (REPO_ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    configure = _function_body(_init_script(), "configure_systemd")

    assert '"gunicorn>=26,<27"' in pyproject
    exec_start = next(
        line for line in configure.splitlines() if ".venv/bin/gunicorn app.main:app" in line
    )
    # gunicorn 26 の control socket は既定で /run/user/<uid> に作られる。system user の ragsvc には
    # 無い場所なので、使わない control interface は無効にする。
    assert "--no-control-socket" in exec_start
    assert "--worker-class uvicorn.workers.UvicornWorker" in exec_start
    assert "--workers 2" in exec_start
    assert "--timeout 60" in exec_start
    assert "--graceful-timeout 30" in exec_start
    assert "--bind ${BACKEND_HOST}:${BACKEND_PORT}" in exec_start


def test_own_code_has_no_container_image() -> None:
    """自前のコードは Docker イメージを作らない(#286 / #356)。配備は systemd の unit だけ。"""
    ignored = {"node_modules", ".venv", "dist"}
    leftovers = [
        path
        for pattern in ("Dockerfile*", ".dockerignore", "docker-compose*.yml")
        for path in REPO_ROOT.rglob(pattern)
        if not ignored.intersection(path.relative_to(REPO_ROOT).parts)
    ]
    assert leftovers == []


def test_nightly_rag_workflow_runs_search_load_gate() -> None:
    """nightly RAG gate は評価 trend と検索 p95 trend を同じ artifact に残す。"""
    workflow = (SUITE_ROOT / ".github" / "workflows" / "rag-evaluation-nightly.yml").read_text(
        encoding="utf-8"
    )

    assert "search_load_path:" in workflow
    assert "app.rag.evaluation_cli" in workflow
    assert "app.rag.search_load_cli" in workflow
    assert "--trend-output ../artifacts/evaluation-trend.json" in workflow
    assert "--trend-output ../artifacts/search-load-trend.json" in workflow
    assert "export RAG_SEARCH_LOAD_TENANT_ID" in workflow
    assert "if: always()" in workflow


def test_nightly_rag_workflow_runs_parser_adapter_contract_gate() -> None:
    """nightly RAG gate は外部 parser adapter の schema remap smoke を任意に厳格化できる。"""
    workflow = (SUITE_ROOT / ".github" / "workflows" / "rag-evaluation-nightly.yml").read_text(
        encoding="utf-8"
    )

    # 外部 parser はサービスで動くため、backend の venv に parser の extra を入れない(#343)。
    # adapter の可用性は parser サービスの /health で判定する。
    assert "install_parser_adapters" not in workflow
    assert "--extra" not in workflow
    assert "run: uv sync --locked --dev\n" in workflow
    assert 'RAG_PARSER_READINESS_PROBE_ENABLED: "true"' in workflow
    assert "adapter_contract_strict_enabled=false" in workflow
    assert "adapter_contract_strict_enabled=true" in workflow
    assert "run_parser_adapter_contract:" in workflow
    assert "parser_adapter_contract_strict:" in workflow
    assert "parser_adapter_contract_source_kinds:" in workflow
    assert "require_real_world_file_processing_manifest:" in workflow
    assert "app.rag.parser_adapter_contract_cli" in workflow
    assert "--output ../artifacts/parser-adapter-compatibility.json" in workflow
    assert "--require-real-world-policy" in workflow
    assert "parser_adapter_contract_args+=(--strict)" in workflow
    assert "staging_args+=(--parser-adapter-contract-strict)" in workflow
    assert "parser adapter contract gate failed" in workflow
    assert workflow.index("app.rag.parser_adapter_contract_cli") < workflow.index(
        "app.rag.file_processing_golden_cli"
    )


def test_nightly_rag_workflow_starts_docling_service_for_strict() -> None:
    """strict では job の中で Docling の parser サービスを起動してから gate を実行する(#366)。"""
    workflow = (SUITE_ROOT / ".github" / "workflows" / "rag-evaluation-nightly.yml").read_text(
        encoding="utf-8"
    )
    # step ごとの本文("      - name: <名前>" から次の step まで)。
    blocks = workflow.split("\n      - name: ")[1:]
    steps = {block.split("\n", 1)[0]: block for block in blocks}
    names = list(steps)
    strict = "if: steps.mode.outputs.adapter_contract_strict == 'true'"

    start = steps["Start Docling parser service"]
    assert strict in start
    # docling の lock が CPU 版の torch を持つので、置き換えの環境変数は要らない(#1650)。
    assert "RAG_SERVICES_TORCH" not in start
    assert "timeout-minutes:" in start
    assert "scripts/rag-services.sh sync parser-docling" in start
    assert "scripts/rag-services.sh run parser-docling > artifacts/" in start
    # backend の既定の URL(RAG_PARSER_DOCLING_SERVICE_URL)と unit のポートで待つ。
    _directory, port, _timeout = _script_microservices()["parser-docling"]
    assert f"http://127.0.0.1:{port}/health" in start
    config = (REPO_ROOT / "backend" / "app" / "config.py").read_text(encoding="utf-8")
    assert f'default="http://127.0.0.1:{port}"' in config
    assert strict in steps["Restore Docling models"]
    assert "hashFiles('rag/services/parsers/docling/uv.lock')" in steps["Restore Docling models"]
    assert "if: always()" in steps["Stop Docling parser service"]
    assert "if: always()" in steps["Save Docling models"]

    assert names.index("Resolve parser adapter contract mode") < names.index(
        "Start Docling parser service"
    )
    assert names.index("Start Docling parser service") < names.index(
        "Run evaluation and search load gates"
    )
    assert names.index("Run evaluation and search load gates") < names.index(
        "Stop Docling parser service"
    )
    gate = steps["Run evaluation and search load gates"]
    assert "ADAPTER_CONTRACT_STRICT: ${{ steps.mode.outputs.adapter_contract_strict }}" in gate


# --- ネイティブ配備(uv の venv + systemd。#286) --------------------------------


def _systemd_script() -> str:
    return (REPO_ROOT / "scripts" / "rag-systemd.sh").read_text(encoding="utf-8")


def _script_microservices() -> dict[str, tuple[str, int, int]]:
    """rag/scripts/rag-systemd.sh の RAG_MICROSERVICES(id|dir|port|timeout)を読む。"""
    import re

    block = re.search(r"(?ms)^RAG_MICROSERVICES=\(\n(.*?)^\)$", _systemd_script())
    assert block is not None, "RAG_MICROSERVICES が見つからない"
    services: dict[str, tuple[str, int, int]] = {}
    for line in block.group(1).splitlines():
        service_id, directory, port, timeout = line.strip().strip('"').split("|")
        services[service_id] = (directory, int(port), int(timeout))
    return services


def test_systemd_units_match_service_catalog() -> None:
    """unit を作るスクリプトのサービス・ディレクトリ・ポートは、backend のカタログと同じ。"""
    from app.services.catalog import SERVICE_CATALOG, SYSTEMD_UNIT_PREFIX

    services = _script_microservices()
    catalog = {entry.service_id: entry for entry in SERVICE_CATALOG}
    assert set(services) == set(catalog)
    for service_id, (directory, port, _timeout) in services.items():
        entry = catalog[service_id]
        assert directory == entry.working_dir, service_id
        assert port == entry.port, service_id
        assert (REPO_ROOT / directory / "pyproject.toml").is_file(), service_id
        assert (REPO_ROOT / directory / "uv.lock").is_file(), service_id
    assert f'RAG_UNIT_PREFIX="{SYSTEMD_UNIT_PREFIX}"' in _systemd_script()


def test_systemd_sudoers_matches_backend_argv() -> None:
    """sudoers は backend が sudo -n で実行する argv と引数まで同じ(違うと操作が拒否される)。"""
    from app.services.systemd import (
        JOURNAL_FETCH_LINES,
        JOURNALCTL,
        SYSTEMCTL,
        journalctl_argv,
        systemctl_action_argv,
    )

    script = _systemd_script()
    assert f'RAG_SYSTEMCTL="{SYSTEMCTL}"' in script
    assert f'RAG_JOURNALCTL="{JOURNALCTL}"' in script
    assert f"RAG_JOURNAL_FETCH_LINES={JOURNAL_FETCH_LINES}" in script
    unit = "production-ready-rag-parser-docling.service"
    templates = {
        '"${RAG_SYSTEMCTL} enable --now ${unit}"': systemctl_action_argv("start", unit),
        '"${RAG_SYSTEMCTL} disable --now ${unit}"': systemctl_action_argv("stop", unit),
        '"${RAG_SYSTEMCTL} restart ${unit}"': systemctl_action_argv("restart", unit),
        '"${RAG_JOURNALCTL} -u ${unit} -n ${RAG_JOURNAL_FETCH_LINES} --no-pager -o short-iso"': (
            journalctl_argv(unit)
        ),
    }
    for template, argv in templates.items():
        assert template in script
        # sudo -n の後ろが sudoers の 1 行と同じ。
        assert argv[:2] == ["/usr/bin/sudo", "-n"]
        rendered = (
            template.strip('"')
            .replace("${RAG_SYSTEMCTL}", SYSTEMCTL)
            .replace("${RAG_JOURNALCTL}", JOURNALCTL)
            .replace("${RAG_JOURNAL_FETCH_LINES}", str(JOURNAL_FETCH_LINES))
            .replace("${unit}", unit)
        )
        assert rendered == " ".join(argv[2:])


def test_native_deployment_defaults_match_backend_settings() -> None:
    """init_script.sh が前提にする既定値(upload 上限・実行用 env・保存先)は Settings と同じ。"""
    from app.config import BACKEND_ROOT, DEFAULT_LOCAL_STORAGE_DIR, Settings

    init_script = (REPO_ROOT / "init_script.sh").read_text(encoding="utf-8")
    fields = Settings.model_fields
    assert fields["rag_service_runtime_env_file"].default == str(
        BACKEND_ROOT / "service-runtime.env"
    )
    assert 'SERVICE_RUNTIME_ENV_FILE="${BACKEND_DIR}/service-runtime.env"' in init_script
    assert f'DATA_DIR="${{DATA_DIR:-{DEFAULT_LOCAL_STORAGE_DIR}}}"' in init_script
    # 配備は Docker を入れず、サービスごとの venv を Python 3.12 で作る。
    assert "docker-ce" not in init_script
    assert "docker compose" not in init_script
    assert 'RAG_PYTHON_VERSION="3.12"' in _systemd_script()
    assert "sync --locked --no-dev --python" in _systemd_script()
