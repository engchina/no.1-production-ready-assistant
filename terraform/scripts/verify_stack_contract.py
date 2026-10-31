#!/usr/bin/env python3
"""統合 OCI Resource Manager stack zip の入力契約と配備契約を検証する（#217）。

1つの ADB を全製品で共有し、選んだ製品（RAG / NL2SQL / Agent）をすべて 1 台の Compute に入れる stack（#1316）の、
Resource Manager フォーム・Terraform・cloud-init と、platform/deploy の suite の配備（Nginx の prefix・HTTPS）、
各製品の init_script.sh / Settings との整合を確かめる。
stdlib だけで動かす（CI と release workflow で追加依存なしに実行するため）。
"""

from __future__ import annotations

import argparse
import ast
import math
import re
import stat
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTS = ("rag", "nl2sql", "agent")
PRIVATE_ACCESS = "プライベート・エンドポイント・アクセスのみ"
ALLOWED_ACCESS = "許可されたIPおよびVCN限定のセキュア・アクセス"
EVERYWHERE_ACCESS = "すべての場所からのセキュア・アクセス"
IP_OR_CIDR = "IPアドレスまたはCIDRブロック"
# 製品と共有 platform は monorepo から1回の clone で取得する。
APPLICATION_GIT_URL = "https://github.com/engchina/no.1-production-ready-suite.git"
WALLET_DIR = "/u01/aipoc/wallet"

REQUIRED_ENTRIES = {
    "schema.yaml",
    "adb.tf",
    "compute.tf",
    "datasources.tf",
    "locals.tf",
    "output.tf",
    "provider.tf",
    "variables.tf",
    "versions.tf",
    "extract_wallet.sh",
    "cloud_init/bootstrap.template.yaml",
    "bastion.tf",
    "cloud_init/bastion.sh",
}
FORBIDDEN_NAMES = {".terraform.lock.hcl", "wallet_full.zip", "wallet_small.zip"}

HIDDEN_VARIABLES = [
    "application_git_url",
    "application_git_ref",
    "existing_oracle_wallet_password",
    "adb_is_mtls_connection_required",
    "platform_environment",
    "app_admin_login_user_id",
    "agent_runtime_repository_backend",
    # elastic pool は選ばせず既定値（無効）で配備する（#2801）。
    "adb_is_elastic_pool_enabled",
    "adb_resource_pool_size",
    "adb_resource_pool_storage_size_in_tbs",
]
SECRET_VARIABLES = [
    "adb_password",
    "adb_app_user_password",
    "existing_adb_admin_password",
    "existing_oracle_password",
    "existing_oracle_wallet_password",
    "app_admin_login_user_password",
    "nl2sql_oracle_deepsec_data_user_password",
    "bastion_vnc_password",
]
# 任意の bastion の Compute（#2670）の入力と既定値（利用者の決定。2026-10-20）。
BASTION_DEFAULTS = {
    "bastion_flex_shape_ocpus": "2",
    "bastion_flex_shape_memory": "16",
    "bastion_boot_volume_size": "100",
    "bastion_boot_volume_vpus": "10",
}
BASTION_VNC_PORT = "5901"
# 製品ごとの入力は、その製品を選んだときだけフォームに出す（group の visible と、group 内の変数の接頭辞）。
PRODUCT_GROUPS = {
    "RAG": "rag",
    "NL2SQL Deep Data Security": "nl2sql",
}
# 1 台の Compute（#1316）で廃止した入力・resource。製品ごとの Compute の大きさ、公開 port（80 / 443 に固定）、
# 製品ごとの Secure Cookie（https_enabled から決める）。stack に残さない。
REMOVED_SINGLE_COMPUTE_INPUTS = (
    "application_port",
    "rag_instance_",
    "nl2sql_instance_",
    "agent_instance_",
    "rag_app_auth_cookie_secure",
    "nl2sql_app_auth_cookie_secure",
    "agent_app_auth_cookie_secure",
    "oci_core_instance.product",
    "oci_core_instance.agent",
    # 製品ごとのログインの Cookie 名（共通 .env の PLATFORM_AUTH_*_COOKIE_NAME に統一。#1565）。
    "_APP_AUTH_SESSION_COOKIE_NAME",
    "_APP_AUTH_CSRF_COOKIE_NAME",
)
# 各製品の backend の port（127.0.0.1。1 台の Compute で衝突しない。#1316。ローカルの開発の port は別。#25）。
BACKEND_PORTS = {"rag": "8000", "nl2sql": "8010", "agent": "8020"}
# 証明書の発行者・サーバーの subject は固定（Terraform の変数にしない。#1316）。
TLS_CA_SUBJECT = "/C=JP/ST=Tokyo/L=Minato-ku/O=Oracle/OU=Production Ready Suite/CN=Production Ready Root CA"
TLS_SERVER_SUBJECT_PREFIX = "/C=JP/ST=Tokyo/L=Minato-ku/O=Oracle/OU=Production Ready Suite/CN="
SUITE_DEPLOY_DIR = REPO_ROOT / "platform" / "deploy"
# 3製品の構成管理者（system_admin。共通 .env の PLATFORM_ADMIN_*）のパスワードは共通の入力（#214 / #215）。
# 製品を選ばないとフォームから消えるため、Resource Manager では任意入力にして Terraform の precondition で必須にする。
ADMIN_PASSWORD_VARIABLE = "app_admin_login_user_password"
ADMIN_PASSWORD_PRODUCTS = ("rag", "nl2sql", "agent")
# Compute と ADB の配備を許可するリージョン。
SUPPORTED_REGIONS = ("ap-osaka-1", "ap-tokyo-1", "us-chicago-1")
# Compute の OS は Oracle Linux 9（platform image の Oracle-Linux-9.8-2026.09.18-0。#1611）。OCID は固定する。
ORACLE_LINUX_IMAGES = {
    "ap-osaka-1": "ocid1.image.oc1.ap-osaka-1.aaaaaaaabdp5adn6fy2h63s5iizgqv77wz4fqrjzosackszdo2gn5rn436ha",
    "ap-tokyo-1": "ocid1.image.oc1.ap-tokyo-1.aaaaaaaawxmm5rja45shcpoactn4avvfv2sty55ap5jnaxqmpzbgtdc7qcsa",
    "us-chicago-1": "ocid1.image.oc1.us-chicago-1.aaaaaaaaltnu52rsd7s6p7yepblc6cdq7b7orqkndiwlz2eggv5ja2ltn52a",
}
DEFAULT_IMAGE_REGION = "ap-osaka-1"
# Oracle Linux のログインのユーザー。3 製品のプロセスもこのユーザーで動かす。
COMPUTE_USER = "opc"
# 配備のスクリプト（cloud-init・platform/deploy・各製品の init_script.sh）に残さない Ubuntu 固有の command・path（#1611）。
UBUNTU_ONLY_TOKENS = (
    "apt-get",
    "apt-cache",
    "dpkg",
    "DEBIAN_FRONTEND",
    "sites-available",
    "sites-enabled",
    "www-data",
    "netfilter-persistent",
    "iptables",
    "update-ca-certificates",
    "/home/ubuntu",
    "nodesource",
)
# 廃止した入力（Agent の Nginx Basic 認証。#215）。stack と init_script.sh に残さない。
REMOVED_AGENT_BASIC_AUTH = (
    "agent_app_basic_auth_user",
    "agent_app_basic_auth_password",
    "basic_auth_user",
    "basic_auth_password",
    "configure_basic_auth",
    "auth_basic",
    "htpasswd",
)

# RAG の Compute が常に配備する前処理 / parser（CPU だけ）と、任意で足せる parser（#286）。
# backend と ingestion-worker は常に配備するため、この一覧には含めない。
RAG_BASE_SERVICES = [
    "preprocess-office-to-pdf",
    "preprocess-csv-to-jsonl",
    "preprocess-excel-to-jsonl",
    "preprocess-word-to-jsonl",
    "parser-docling",
]
# parser-unstructured は Docling が扱えない形式(テキスト・Office・メール など)を取り込むための parser(#286)。
RAG_OPTIONAL_SERVICES = [
    "parser-unstructured",
]
# systemd の unit の Environment（init_script.sh）と Settings の既定値が正本の key。RAG の .env に書くと、
# 配備が前提にする場所とずれるため、共通 .env にも書かない。
RAG_UNIT_OWNED_ENV_KEYS = {
    "PLATFORM_LOCAL_STORAGE_DIR",
    "PLATFORM_MODEL_SETTINGS_FILE",
    "PLATFORM_OCI_CONFIG_FILE",
}
# 以前の入力（Docling は既定の解析エンジンのため常に配備する。#286）。stack に残さない。
REMOVED_RAG_INPUTS = ("rag_enable_parser_docling", "rag_compose_services", "compose_services")

# 製品ごとの backend/.env（製品接頭辞。#211）に必ず書く値。
REQUIRED_BACKEND_ENV_LINES = {
    # RAG の backend/.env は OCI parser の unit も EnvironmentFile で読む。入力由来の値は single quote で囲む。
    # 既定の解析エンジンは Docling。サービス管理画面は sudoers で許可した unit の systemctl だけを実行する（#286）。
    "rag": [
        "RAG_AUDIT_CONTEXT_HASH_SALT=\n",
        "RAG_PARSER_ADAPTER_BACKEND=docling\n",
        "RAG_PARSER_DOCLING_ENABLED=true\n",
        # parser-unstructured を配備したら、文書解析の設定でも有効にする(#1559)。
        "RAG_PARSER_UNSTRUCTURED_ENABLED='${var.rag_enable_parser_unstructured}'\n",
        "RAG_SERVICE_CONTROL_ENABLED=true\n",
    ],
    "nl2sql": [
        "NL2SQL_ORACLE_DEEPSEC_ENABLED=${var.nl2sql_oracle_deepsec_enabled}\n",
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER=DEEPSEC_DATA_USER\n",
        'NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD=${var.nl2sql_oracle_deepsec_enabled ? '
        'var.nl2sql_oracle_deepsec_data_user_password : ""}\n',
        "NL2SQL_SELECT_AI_CREDENTIAL_NAME=NL2SQL_OCI_CRED\n",
    ],
    "agent": [
        "AGENT_RUNTIME_REPOSITORY_BACKEND=${var.agent_runtime_repository_backend}\n",
        "AGENT_RUNTIME_DISPATCH_MODE=in_process\n",
        # RAG / NL2SQL の MCP は、同じ Compute の backend（127.0.0.1）を直接呼ぶ（#233 / #1316）。
        'AGENT_EXTERNAL_RAG_MCP_URL=${local.product_mcp_urls["rag"]}\n',
        'AGENT_EXTERNAL_NL2SQL_MCP_URL=${local.product_mcp_urls["nl2sql"]}\n',
        # 画面（ブラウザ）から RAG の図の根拠を開く起点（#1311）。1 台の Compute では同じ origin の /rag（#1316）。
        'AGENT_EXTERNAL_RAG_PUBLIC_URL=${local.product_browser_urls["rag"]}\n',
    ],
}
# 全製品の Compute に置く共通 .env（platform/.env、PLATFORM_*。#211）に必ず書く値。
# python-dotenv の展開を避けるため、DB の入力由来の値は single quote で囲む。
REQUIRED_PLATFORM_ENV_LINES = [
    # 時間帯（#3255）: Terraform の timezone を共通の設定へ書く（suite-init.sh が OS の時間帯にも使う）。
    "PLATFORM_TIMEZONE=${var.timezone}\n",
    "PLATFORM_OCI_COMPARTMENT_ID=${var.compartment_ocid}\n",
    "PLATFORM_ORACLE_USER='${local.effective_oracle_user}'\n",
    "PLATFORM_ORACLE_PASSWORD='${local.effective_oracle_password}'\n",
    "PLATFORM_ORACLE_DSN='${local.effective_oracle_dsn}'\n",
    "PLATFORM_ORACLE_DRIVER_MODE=thin\n",
    "PLATFORM_ORACLE_CONNECTION_SECURITY=${local.nl2sql_oracle_connection_security}\n",
    "PLATFORM_ORACLE_CLIENT_LIB_DIR=\n",
    "PLATFORM_ORACLE_WALLET_DIR=${local.wallet_dir_host}\n",
    "PLATFORM_ORACLE_WALLET_PASSWORD='${local.effective_oracle_wallet_password}'\n",
    "PLATFORM_ORACLE_ADB_OCID=${local.effective_adb_ocid}\n",
    "PLATFORM_ADMIN_LOGIN_USER_ID=${var.app_admin_login_user_id}\n",
    "PLATFORM_ADMIN_LOGIN_USER_PASSWORD=${var.app_admin_login_user_password}\n",
    # 3製品で同じサービス間 token の署名鍵（#233）。stack が1つ生成して全 Compute に配る。
    "PLATFORM_SERVICE_TOKEN_SECRET=${random_password.service_token_secret.result}\n",
    # 1 台の Compute では platform/.env は 1 つ。ログインの Cookie は HTTPS が on なら Secure（#1316）。
    "PLATFORM_AUTH_COOKIE_SECURE=${var.https_enabled}\n",
    "PLATFORM_ENVIRONMENT=${var.platform_environment}\n",
    "PLATFORM_AUTH_MODE=authenticated\n",
    # ログインの Cookie は 3 製品で共有する（1 回のログインで全製品を使える。#1565）。
    "PLATFORM_AUTH_SESSION_COOKIE_NAME=pr_session\n",
    "PLATFORM_AUTH_CSRF_COOKIE_NAME=pr_csrf\n",
]
PRODUCT_ENV_PREFIXES = {"rag": "RAG_", "nl2sql": "NL2SQL_", "agent": "AGENT_"}
# 共通の属性名の正本（pr_backend_core.config.env.PLATFORM_SETTING_FIELDS）。stdlib だけで読むため AST で取り出す。
PLATFORM_ENV_MODULE = (
    REPO_ROOT / "platform" / "packages" / "backend_core" / "src" / "pr_backend_core" / "config" / "env.py"
)
SETTINGS_FILES = {
    "rag": REPO_ROOT / "rag" / "backend" / "app" / "config.py",
    "nl2sql": REPO_ROOT / "nl2sql" / "backend" / "app" / "settings.py",
    "agent": REPO_ROOT / "agent" / "backend" / "app" / "settings.py",
}
# pr_backend_core.BaseServiceSettings の共通 field（各製品の Settings には書かれていない）。
BASE_SETTINGS_FIELDS = {"app_version", "log_level", "environment", "cors_origins"}
# 3 製品の Settings が pr_backend_core.config の mixin から受け継ぐ共通の属性（時間帯の TimezoneSettings。#3255）。
MIXIN_SETTINGS_FIELDS = {"timezone", "auth_mode", "app_auth_cookie_secure"}

# 1 台の Compute の配備（platform/deploy/suite-init.sh が呼ぶ。#1316。製品は単独では配備しない）の契約。
# 製品の init_script.sh は Nginx の site を書かず、/<製品>/ を base に frontend を build し、backend/.env は製品ごとの元から作る。
SUITE_INIT_CONTRACT = [
    "FRONTEND_BASE_PATH='${FRONTEND_BASE_PATH}' npm run build",
    'BACKEND_ENV_SOURCE="${BACKEND_ENV_SOURCE:-',
    '"${BACKEND_ENV_SOURCE}"',
    'if [ "${PR_SUITE_SKIP_PLATFORM_UI_BUILD}" = "true" ]; then',
]
# 単独の配備（製品ごとの Nginx の site）は持たない。Nginx の location は platform/deploy/suite-nginx.sh の 1 か所だけ。
REMOVED_STANDALONE_DEPLOY = ("configure_nginx", "PR_SUITE_MODE", "sites-available", "proxy_pass", "APPLICATION_PORT")

# 各製品の init_script.sh が守る配備の契約。
INIT_SCRIPT_CONTRACTS = {
    "rag": [
        'WALLET_DIR="${APP_ROOT}/wallet"',
        'BACKEND_HOST="127.0.0.1"',
        'BACKEND_PORT="8000"',
        # 必ず配備する parser と前処理 Word→JSONL（stack の RAG_BASE_SERVICES にもある）。以前の stack の一覧に
        # 無くても足す（#2042・#2457）。
        "REQUIRED_RAG_SERVICES=(parser-docling preprocess-csv-to-jsonl preprocess-excel-to-jsonl preprocess-word-to-jsonl)",
        'source "${INIT_SCRIPT_DIR}/scripts/rag-systemd.sh"',
        'find "${WALLET_DIR}" -type f -exec chmod 0600 {} \\;',
        "python -m app.rag.system_schema_cli initialize",
        "openssl rand -hex 32",
        'uv sync --locked --no-dev --python ${RAG_PYTHON_VERSION}',
        "--bind ${BACKEND_HOST}:${BACKEND_PORT}",
        "python -m app.rag.ingestion_worker",
        "Environment=RAG_INGESTION_QUEUE_INPROCESS_WORKER_ENABLED=false",
        "KillMode=mixed",
        'systemctl enable --now "${unit}"',
        'elif systemctl is-enabled --quiet "${unit}"; then',
        'visudo -cf "${tmp}"',
        "/api/health",
        '"${PROPS_DIR}/platform.env" "${PLATFORM_ENV_FILE}"',
        'FRONTEND_BASE_PATH="${FRONTEND_BASE_PATH:-/rag/}"',
        *SUITE_INIT_CONTRACT,
    ],
    "nl2sql": [
        'WALLET_DIR="${APP_ROOT}/wallet"',
        'chown "root:${APP_GROUP}" "${APP_ROOT}"',
        'chmod 0775 "${APP_ROOT}"',
        'install -d -m 0700 -o "${APP_USER}" -g "${APP_GROUP}" "${WALLET_DIR}"',
        'find "${WALLET_DIR}" -type f -exec chmod 0600 {} \\;',
        "app.cli.nl2sql_system_schema --initialize",
        "app.cli.app_security_migrate --apply --skip-bootstrap",
        'if [ "${DATABASE_INITIALIZATION_READY}" = "true" ]; then',
        "production-ready-nl2sql-schema-refresh-worker.service",
        "production-ready-nl2sql-quality-evaluation-worker.service",
        "production-ready-nl2sql-ontology-worker.service",
        '"${APP_ROOT}/props/platform.env" "${PLATFORM_REPO_DIR}/.env"',
        'BACKEND_PORT="8010"',
        'FRONTEND_BASE_PATH="${FRONTEND_BASE_PATH:-/nl2sql/}"',
        *SUITE_INIT_CONTRACT,
    ],
    "agent": [
        'WALLET_DIR="${APP_ROOT}/wallet"',
        'BACKEND_PORT="8020"',
        'BACKEND_WORKERS="1"',
        'install -d -m 0700 -o "${APP_USER}" -g "${APP_GROUP}" "${WALLET_DIR}"',
        'find "${WALLET_DIR}" -type f -exec chmod 0600 {} \\;',
        "uv run python -m app.cli.agent_system_schema --initialize",
        "uv sync --locked --no-dev --python 3.12",
        '"${PROPS_DIR}/platform.env" "${PLATFORM_REPO_DIR}/.env"',
        'FRONTEND_BASE_PATH="${FRONTEND_BASE_PATH:-/agent/}"',
        *SUITE_INIT_CONTRACT,
    ],
}
INIT_SCRIPT_ORDER = {
    "rag": [
        "  load_rag_services\n",
        "  install_system_packages\n",
        "  install_nodejs\n",
        "  install_uv\n",
        "  prepare_service_user\n",
        "  prepare_filesystem\n",
        "  migrate_from_docker_compose\n",
        "  install_runtime_env\n",
        "  install_wallet\n",
        "  install_service_venvs\n",
        "  initialize_database_schema\n",
        "  build_frontend\n",
        "  configure_systemd\n",
        "  wait_for_backend\n",
    ],
    "nl2sql": [
        "app.cli.nl2sql_system_schema --initialize",
        "app.cli.app_security_migrate --apply --skip-bootstrap",
        "  configure_systemd\n",
        "  wait_for_backend\n",
    ],
    "agent": [
        "app.cli.agent_system_schema --initialize",
        "  install_runtime_env\n",
        "  install_backend\n",
        "  initialize_database_schema\n",
        "  build_frontend\n",
        "  configure_systemd\n",
        "  wait_for_backend\n",
    ],
}


def _require_all(source: str, expected: list[str], *, context: str) -> None:
    missing = [value for value in expected if value not in source]
    if missing:
        raise AssertionError(f"{context} is missing: {missing}")


def _require_in_order(source: str, expected: list[str], *, context: str) -> None:
    cursor = 0
    for value in expected:
        position = source.find(value, cursor)
        if position < 0:
            raise AssertionError(f"{context} is missing or out of order: {value}")
        cursor = position + len(value)


def _schema_variable(source: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [a-zA-Z0-9_]+:\n|^outputs:|\Z)",
        source,
    )
    if match is None:
        raise AssertionError(f"schema variable not found: {name}")
    return match.group(0)


def _terraform_variable(source: str, name: str) -> str:
    match = re.search(rf'(?ms)^variable "{re.escape(name)}" \{{.*?^\}}', source)
    if match is None:
        raise AssertionError(f"Terraform variable not found: {name}")
    return match.group(0)


def _schema_section(schema: str, name: str) -> str:
    match = re.search(rf"(?ms)^{name}:\n(.*?)(?=^[a-zA-Z]\w*:|\Z)", schema)
    if match is None:
        raise AssertionError(f"schema section not found: {name}")
    return match.group(1)


def _schema_groups(schema: str) -> dict[str, tuple[str, list[str]]]:
    """group title → (visible の記述, 変数名)。"""
    groups: dict[str, tuple[str, list[str]]] = {}
    section = _schema_section(schema, "variableGroups")
    for block in re.split(r"(?m)^  - title: ", section)[1:]:
        title = block.splitlines()[0].strip().strip('"')
        visible = block.split("    visible:", 1)[1].split("    variables:", 1)[0].strip()
        members = re.findall(r"(?m)^      - ([a-z0-9_]+)$", block)
        groups[title] = (visible, members)
    return groups


def _heredoc(locals_source: str, name: str) -> str:
    match = re.search(rf"(?ms)^  {name} = <<-EOT\n(.*?)^EOT$", locals_source)
    if match is None:
        raise AssertionError(f"locals.tf {name} heredoc not found")
    return match.group(1)


def _settings_fields(product: str) -> set[str]:
    source = SETTINGS_FILES[product].read_text(encoding="utf-8")
    body = source.split("\nclass Settings(", 1)[1]
    return set(re.findall(r"(?m)^    ([a-z][a-z0-9_]*): ", body)) | BASE_SETTINGS_FIELDS | MIXIN_SETTINGS_FIELDS


def _platform_setting_fields() -> frozenset[str]:
    tree = ast.parse(PLATFORM_ENV_MODULE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PLATFORM_SETTING_FIELDS" for target in node.targets
        ):
            return frozenset(ast.literal_eval(node.value.args[0]))  # type: ignore[attr-defined]
    raise AssertionError("PLATFORM_SETTING_FIELDS not found in pr_backend_core/config/env.py")


def _settings_env_names(product: str) -> tuple[set[str], set[str]]:
    """製品の Settings の (製品 .env の名前, 共通 .env の名前)。pr_backend_core の規則と同じ。"""
    platform_fields = _platform_setting_fields()
    prefix = PRODUCT_ENV_PREFIXES[product]
    product_names: set[str] = set()
    platform_names: set[str] = set()
    for field in _settings_fields(product):
        name = field.upper()
        if field in platform_fields:
            platform_names.add("PLATFORM_" + name.removeprefix("APP_"))
        else:
            product_names.add(name if name.startswith(prefix) else prefix + name)
    return product_names, platform_names


def _env_keys(env: str) -> list[str]:
    return re.findall(r"(?m)^([A-Z][A-Z0-9_]*)=", env)


def _verify_package_entries(archive: zipfile.ZipFile) -> None:
    names = set(archive.namelist())
    missing = REQUIRED_ENTRIES - names
    if missing:
        raise AssertionError(f"missing package entries: {sorted(missing)}")
    forbidden = {
        name
        for name in names
        if name in FORBIDDEN_NAMES
        or name.startswith(".terraform/")
        or name.endswith(".tfstate")
        or ".tfstate." in name
        or name.endswith((".tfvars", ".tfvars.json"))
        or (name.startswith("wallet") and name.endswith(".zip"))
    }
    if forbidden:
        raise AssertionError(f"forbidden package entries: {sorted(forbidden)}")
    mode = archive.getinfo("extract_wallet.sh").external_attr >> 16
    if not mode & stat.S_IXUSR:
        raise AssertionError("extract_wallet.sh is not executable in package")


def _verify_schema(schema: str, variables: str) -> None:
    _require_all(
        schema,
        ["schemaVersion: 1.1.0", "Production Ready Suite"],
        context="Resource Manager schema header",
    )
    terraform_names = set(re.findall(r'(?m)^variable "([a-z0-9_]+)" \{', variables))
    schema_names = set(re.findall(r"(?m)^  ([a-z0-9_]+):\n", _schema_section(schema, "variables")))
    if terraform_names - schema_names:
        raise AssertionError(f"Terraform variables missing from schema: {sorted(terraform_names - schema_names)}")
    if schema_names - terraform_names:
        raise AssertionError(f"schema variables missing from Terraform: {sorted(schema_names - terraform_names)}")

    groups = _schema_groups(schema)
    grouped = [name for _, members in groups.values() for name in members]
    duplicated = sorted({name for name in grouped if grouped.count(name) > 1})
    if duplicated:
        raise AssertionError(f"variables listed in multiple groups: {duplicated}")
    ungrouped = sorted((terraform_names - {"region"}) - set(grouped))
    if ungrouped:
        raise AssertionError(f"variables not listed in any variable group: {ungrouped}")

    hidden_visible, hidden = groups.get("Hidden", ("", []))
    if hidden_visible != "false":
        raise AssertionError("hidden Resource Manager variable group not found")
    _require_all("\n".join(hidden), HIDDEN_VARIABLES, context="hidden Resource Manager variables")
    for name in HIDDEN_VARIABLES:
        _require_all(_schema_variable(schema, name), ["visible: false"], context=f"{name} schema")
    for name, default in {"application_git_url": APPLICATION_GIT_URL, "application_git_ref": "main"}.items():
        _require_all(
            _schema_variable(schema, name),
            ["required: true", f'default: "{default}"'],
            context=f"{name} hidden schema",
        )
        _require_all(
            _terraform_variable(variables, name),
            [f'default     = "{default}"'],
            context=f"{name} Terraform default",
        )

    # 配備する製品の選択は画面に出さず、3 製品をすべて配備する（#1562。Suite はセットで配備する）。
    selection_visible, selection = groups.get("配備する製品", ("", []))
    if selection_visible != "false":
        raise AssertionError("product selection group must be hidden")
    if selection != [f"deploy_{product}" for product in PRODUCTS]:
        raise AssertionError(f"product selection group must list deploy_* in order: {selection}")
    for product in PRODUCTS:
        _require_all(
            _schema_variable(schema, f"deploy_{product}"),
            ["type: boolean", "required: true", "visible: false", "default: true"],
            context=f"deploy_{product} schema",
        )
        _require_all(
            _terraform_variable(variables, f"deploy_{product}"),
            ["type        = bool", "default     = true"],
            context=f"deploy_{product} Terraform default",
        )
    for title, product in PRODUCT_GROUPS.items():
        visible, members = groups.get(title, ("", []))
        if visible != f"deploy_{product}":
            raise AssertionError(f"{title} group must be visible only when deploy_{product} is selected")
        foreign = [name for name in members if not name.startswith(f"{product}_")]
        if foreign:
            raise AssertionError(f"{title} group must hold only {product}_* variables: {foreign}")
    admin_group_visible = "or:\n" + "".join(f"        - deploy_{product}\n" for product in ADMIN_PASSWORD_PRODUCTS)
    visible, members = groups.get("アプリケーション管理者", ("", []))
    if visible + "\n" != admin_group_visible or members != [ADMIN_PASSWORD_VARIABLE]:
        raise AssertionError("the administrator group must hold only the shared admin password for every product")
    _require_all(
        _schema_variable(schema, ADMIN_PASSWORD_VARIABLE),
        ["type: password", "required: false", "visible:\n      " + admin_group_visible,
         "confirmation: true", "pattern: '^$|"],
        context=f"{ADMIN_PASSWORD_VARIABLE} schema",
    )
    _require_all(
        _schema_variable(schema, "nl2sql_oracle_deepsec_data_user_password"),
        ["- deploy_nl2sql", "- nl2sql_oracle_deepsec_enabled", "confirmation: true"],
        context="DeepSec password conditional schema",
    )
    # Deep Data Security は使う環境だけが有効にする（既定は無効。#1707）。
    _require_all(
        _schema_variable(schema, "nl2sql_oracle_deepsec_enabled"),
        ["type: boolean", "required: true", "visible: deploy_nl2sql", "default: false"],
        context="nl2sql_oracle_deepsec_enabled schema default",
    )
    _require_all(
        _terraform_variable(variables, "nl2sql_oracle_deepsec_enabled"),
        ["type        = bool", "default     = false"],
        context="nl2sql_oracle_deepsec_enabled Terraform default",
    )
    compute_visible, compute = groups.get("主 Compute の構成", ("", []))
    if compute_visible != "true" or any(name.startswith(PRODUCTS) for name in compute):
        raise AssertionError(f"shared Compute group must hold only shared inputs: {compute}")

    expected_groups = [
        "配備先", "Autonomous AI Database", "Existing Autonomous AI Database",
        "New Autonomous AI Database", "ADB のネットワーク", "アプリ用の DB ユーザー",
        "主 Compute の構成", "主 Compute のネットワークと SSH", "HTTPS", "Bastion", "bastion の構成",
        "bastion のネットワークと接続", "アプリケーション管理者", "RAG",
        "NL2SQL Deep Data Security", "配備する製品", "Hidden",
    ]
    if list(groups) != expected_groups:
        raise AssertionError(f"Resource Manager groups must follow the deployment flow: {list(groups)}")
    if groups["主 Compute のネットワークと SSH"] != (
        "true", ["availability_domain", "vcn_ai_vcn_id", "subnet_ai_subnet_id", "ssh_authorized_keys"]
    ):
        raise AssertionError("Main Compute network and SSH inputs must be grouped in dependency order")
    _require_all(
        _schema_variable(schema, "adb_compartment_ocid"),
        ["required: true", 'title: "ADBのコンパートメント"', "default: compartment_ocid"],
        context="adb_compartment_ocid schema",
    )
    _require_all(
        _schema_variable(schema, "adb_deployment_mode"),
        [
            'title: "ADBの利用方法"',
            '- "新規 Autonomous AI Database の作成"',
            '- "既存の Autonomous AI Database を選択"',
            'default: "新規 Autonomous AI Database の作成"',
        ],
        context="adb_deployment_mode schema",
    )
    _require_all(
        _schema_variable(schema, "existing_adb_ocid"),
        ["compartmentId: ${adb_compartment_ocid}"],
        context="existing ADB compartment dependency",
    )
    _require_all(
        _schema_variable(schema, "adb_network_access_type"),
        [f'- "{EVERYWHERE_ACCESS}"', f'- "{ALLOWED_ACCESS}"', f'- "{PRIVATE_ACCESS}"', f'default: "{PRIVATE_ACCESS}"'],
        context="adb_network_access_type schema",
    )
    for name in (
        "adb_private_endpoint_vcn_compartment_id",
        "adb_private_endpoint_subnet_compartment_id",
        "adb_acl_vcn_compartment_id",
        "adb_acl_subnet_compartment_id",
    ):
        _require_all(_schema_variable(schema, name), ["default: compartment_ocid"], context=f"{name} schema")
    _require_all(
        _schema_variable(schema, "adb_subnet_id"),
        ["compartmentId: ${adb_private_endpoint_subnet_compartment_id}", "vcnId: ${adb_private_endpoint_vcn_id}"],
        context="private endpoint subnet dependencies",
    )
    _require_all(
        _schema_variable(schema, "adb_acl_cidr_blocks"),
        ["and:", f'- "{ALLOWED_ACCESS}"', f'- "{IP_OR_CIDR}"'],
        context="ACL IP/CIDR visibility",
    )
    workload = _schema_variable(schema, "adb_workload")
    _require_all(
        workload,
        ['- "OLTP"', '- "AJD"', '- "LH"', 'default: "LH"'],
        context="adb_workload schema",
    )
    if any(f'- "{value}"' in workload for value in ("DW", "APEX")):
        raise AssertionError("Resource Manager workload options must not include DW or APEX")
    _require_all(
        _schema_variable(schema, "adb_is_mtls_connection_required"),
        ["type: boolean", "visible: false", "default: true"],
        context="mTLS hidden schema",
    )
    _require_all(
        _schema_variable(schema, "ssh_authorized_keys"),
        ["type: oci:core:ssh:publickey", "required: true", "visible: true", 'title: "SSHキーの追加"'],
        context="SSH public key schema",
    )
    # 対応する各リージョンの Compute image を選べるようにする。
    image_regions = set(
        re.findall(r"ocid1\.image\.oc1\.([a-z0-9-]+)\.", _schema_variable(schema, "instance_image_source_id"))
    )
    if image_regions != set(SUPPORTED_REGIONS):
        raise AssertionError(
            f"Resource Manager Compute image regions must be {sorted(SUPPORTED_REGIONS)}: {sorted(image_regions)}"
        )

    # HTTPS（#1316）: on / off だけを入力にし、既定は on。port と証明書の発行者は入力にしない。
    https_visible, https_members = groups.get("HTTPS", ("", []))
    if https_visible != "true" or https_members != ["https_enabled", "https_port", "http_port"]:
        raise AssertionError(f"the HTTPS group must hold https_enabled and the ports: {https_members}")
    _require_all(
        _schema_variable(schema, "https_enabled"),
        ["type: boolean", "required: true", "visible: true", "default: true", "443"],
        context="https_enabled schema",
    )
    _require_all(
        _terraform_variable(variables, "https_enabled"),
        ["type        = bool", "default     = true"],
        context="https_enabled Terraform default",
    )
    for name, default in {
        "instance_flex_shape_ocpus": "4",
        "instance_flex_shape_memory": "32",
        "instance_boot_volume_size": "200",
    }.items():
        _require_all(_schema_variable(schema, name), [f"default: {default}"], context=f"{name} schema default")
        _require_all(
            _terraform_variable(variables, name), [f"default     = {default}"], context=f"{name} Terraform default"
        )
    stack_inputs = " ".join(sorted(set(re.findall(r'(?m)^variable "([a-z0-9_]+)" \{', variables))))
    if re.search(r"(?:^| )(?:tls|ssl|cert|certificate|ca)_", stack_inputs):
        raise AssertionError("the certificate subject / validity must not be stack inputs")
    # 公開の port は変数（#1316）。HTTPS が on なら https_port、off なら http_port をフォームに出す。
    for name, default, visible in (
        ("https_port", "443", "visible: https_enabled"),
        ("http_port", "80", "visible:\n      not:\n        - https_enabled"),
    ):
        _require_all(
            _schema_variable(schema, name),
            ["type: number", "required: true", f"default: {default}", visible],
            context=f"{name} schema",
        )
        _require_all(
            _terraform_variable(variables, name),
            [f"default     = {default}", f"var.{name} >= 1 && var.{name} <= 65535"],
            context=f"{name} Terraform",
        )

    outputs = _schema_section(schema, "outputs")
    for product in PRODUCTS:
        block = re.search(rf"(?ms)^  {product}_application_url:\n.*?(?=^  [a-z]|\Z)", outputs)
        if block is None or f"visible: deploy_{product}" not in block.group(0):
            raise AssertionError(f"{product}_application_url output must be shown only for deploy_{product}")
    block = re.search(r"(?ms)^  ca_certificate_url:\n.*?(?=^  [a-z]|\Z)", outputs)
    if block is None or "visible: https_enabled" not in block.group(0):
        raise AssertionError("ca_certificate_url output must be shown only when https_enabled")
    for name in ("application_url", "ssh_to_instance"):
        if not re.search(rf"(?m)^  {name}:\n", outputs):
            raise AssertionError(f"output {name} is missing from the schema")


def _verify_terraform(variables: str, adb: str, compute: str, locals_source: str, outputs: str) -> None:
    _require_all(
        _terraform_variable(variables, "adb_workload"),
        ['default     = "LH"', 'contains(["OLTP", "AJD", "LH"], var.adb_workload)'],
        context="adb_workload",
    )
    _require_all(_terraform_variable(variables, "adb_db_version"), ['default     = "26ai"'], context="adb_db_version")
    _require_all(
        _terraform_variable(variables, "region"),
        ["contains([" + ", ".join(f'"{region}"' for region in SUPPORTED_REGIONS) + "], var.region)"],
        context="region validation",
    )
    for name in SECRET_VARIABLES:
        _require_all(_terraform_variable(variables, name), ["sensitive   = true"], context=f"{name} sensitive flag")
    # DB の接続情報は全製品の backend/.env に入り、RAG の env_file は single quote で囲むため、全製品で single quote を許さない。
    for name in ("adb_password", "existing_oracle_password", "existing_oracle_user", "existing_oracle_dsn"):
        _require_all(
            _terraform_variable(variables, name),
            [f"!can(regex(\"[\\r\\n']\", var.{name}))"],
            context=f"{name} single quote validation",
        )

    _require_all(
        adb,
        [
            f'var.adb_network_access_type == "{PRIVATE_ACCESS}"',
            f'var.adb_network_access_type == "{ALLOWED_ACCESS}"',
            f'var.adb_network_access_type == "{EVERYWHERE_ACCESS}"',
            "subnet_id                                      = local.adb_private_endpoint_enabled ? var.adb_subnet_id : null",
            "whitelisted_ips                                = local.adb_secure_acl_enabled ? local.adb_whitelisted_ips : null",
            "compartment_id                                 = var.adb_compartment_ocid",
            "self.compartment_id == trimspace(var.adb_compartment_ocid)",
            'resource "oci_database_autonomous_database_wallet"',
            # ADB の入力と製品の選択は、常に1つ作る Wallet の precondition で1回だけ検証する。
            "condition     = length(local.selected_products) > 0",
            "プライベート・エンドポイントを作成する場合は、サブネットを選択してください。",
            "data.oci_core_subnet.adb_private_endpoint_subnet[0].vcn_id",
            "existing_adb_ocid, existing_oracle_user, and existing_oracle_password must be configured",
        ],
        context="ADB and product selection",
    )
    if "is_access_control_enabled" in adb:
        raise AssertionError("serverless Autonomous AI Database must not configure is_access_control_enabled")
    # Wallet のパスワードは接続のユーザーのパスワード（アプリ用の DB ユーザーなら ADMIN のパスワードにしない。#1668）。
    if not re.search(r"effective_oracle_wallet_password\s*=\s*local\.effective_oracle_password\n", adb):
        raise AssertionError("wallet password must reuse the connection password (never the ADMIN password of the app user mode)")
    if len(re.findall(r'(?m)^resource "oci_database_autonomous_database" ', adb)) != 1:
        raise AssertionError("the stack must create at most one Autonomous AI Database")

    _require_all(
        compute,
        [
            # 選んだ製品はすべて 1 台の Compute に入れる（#1316）。
            'resource "oci_core_instance" "suite" {',
            "compartment_id      = var.compartment_ocid",
            'user_data"           = local.cloud_init_user_data',
            "display_name = var.instance_display_name",
            "memory_in_gbs             = var.instance_flex_shape_memory",
            "ocpus                     = var.instance_flex_shape_ocpus",
            "boot_volume_size_in_gbs = var.instance_boot_volume_size",
            'trimspace(var.app_admin_login_user_password) != ""',
            # 公開の port に SSH・よく使われる service・Compute の中で使う port を使わせない（#1316）。
            "condition     = length(local.public_ports_reserved) == 0",
            'var.platform_environment == "local" || var.https_enabled',
            "!var.deploy_nl2sql || !var.nl2sql_oracle_deepsec_enabled",
            # 3製品で同じサービス間 token の署名鍵。
            'resource "random_password" "service_token_secret" {',
            "special = false",
        ],
        context="single Compute",
    )
    # 通信は subnet の security list（stack の外）で許可する。stack は NSG を作らない（#259）。
    if "oci_core_network_security_group" in compute or "nsg_ids" in compute:
        raise AssertionError("The stack must not create NSGs; allow 443 / 80 in the subnet security list")
    instance_resources = re.findall(r'(?m)^resource "oci_core_instance" "([a-z_]+)"', compute)
    if instance_resources != ["suite"] or "for_each" in compute or re.search(r"(?m)^\s*count\s*=", compute):
        raise AssertionError("every selected product must run on the single Compute instance oci_core_instance.suite")

    normalized = re.sub(r"[ \t]+", " ", locals_source)
    _require_all(
        normalized,
        [
            f'wallet_dir_host = "{WALLET_DIR}"',
            "rag = var.deploy_rag",
            "nl2sql = var.deploy_nl2sql",
            "agent = var.deploy_agent",
            'selected_products = [for product in ["rag", "nl2sql", "agent"] : product if local.product_enabled[product]]',
            "backend_envs = { for product in local.selected_products : product => base64gzip(local.backend_envs[product]) }",
            "platform_env = base64gzip(local.platform_env)",
            'products = join(" ", local.selected_products)',
            "https_enabled = tostring(var.https_enabled)",
            "assign_public_ip = tostring(!local.compute_subnet_prohibits_public_ip)",
            "http_port = tostring(var.http_port)",
            "https_port = tostring(var.https_port)",
            "timezone = var.timezone",
            'rag_services = var.deploy_rag ? join(" ", local.rag_services) : ""',
            "application_git_ref = var.application_git_ref",
            "application_git_url = var.application_git_url",
            # MCP は同じ Compute の backend を 127.0.0.1 で直接呼ぶ（Nginx を通さない）。
            f'rag = var.deploy_rag ? "http://127.0.0.1:{BACKEND_PORTS["rag"]}/api/mcp" : ""',
            f'nl2sql = var.deploy_nl2sql ? "http://127.0.0.1:{BACKEND_PORTS["nl2sql"]}/api/mcp" : ""',
            # 画面から RAG の図を開く起点は同じ origin の /rag（#1311 / #1316）。
            'rag = var.deploy_rag ? "/rag" : ""',
        ],
        context="product selection and cloud-init rendering",
    )
    for product in PRODUCTS:
        _require_all(
            outputs,
            [f'output "{product}_application_url"', f'"${{local.application_base_url}}/{product}/"'],
            context="outputs",
        )
    _require_all(
        outputs,
        [
            'output "application_url"',
            'output "ca_certificate_url"',
            '"${local.application_base_url}/platform/ca.crt"',
            'output "ssh_to_instance"',
            # 既定でない port は URL に付ける（#1316）。
            'public_port          = var.https_enabled ? var.https_port : var.http_port',
            'local.public_port == local.default_public_port ? "" : ":${local.public_port}"',
        ],
        context="outputs",
    )

    platform_common = _heredoc(locals_source, "platform_env")
    _require_all(platform_common, REQUIRED_PLATFORM_ENV_LINES, context="platform/.env (common)")
    all_platform_names: set[str] = set()
    for product in PRODUCTS:
        all_platform_names |= _settings_env_names(product)[1]
    unknown = sorted(key for key in _env_keys(platform_common) if key not in all_platform_names)
    if unknown:
        raise AssertionError(f"platform/.env keys are not PLATFORM_ Settings names of any product: {unknown}")
    if re.search(r"var\.(?:rag|nl2sql|agent)_", platform_common):
        raise AssertionError("the common platform/.env must not use product inputs")

    for product in PRODUCTS:
        product_names, _platform_names = _settings_env_names(product)
        env = _heredoc(locals_source, f"{product}_backend_env")
        _require_all(env, REQUIRED_BACKEND_ENV_LINES[product], context=f"{product} backend/.env")
        keys = _env_keys(env)
        unknown = sorted(key for key in keys if key not in product_names)
        if unknown:
            raise AssertionError(f"{product} backend/.env keys are not {product} Settings names: {unknown}")
        duplicated = sorted({key for key in keys if keys.count(key) > 1})
        if duplicated:
            raise AssertionError(f"{product} backend/.env keys are duplicated: {duplicated}")
        # 他製品の入力が紛れ込んでいないこと。
        foreign = re.findall(rf"var\.((?!{product}_)(?:rag|nl2sql|agent)_[a-z0-9_]+)", env)
        if foreign:
            raise AssertionError(f"{product} .env uses another product's inputs: {sorted(set(foreign))}")

    platform_keys = _env_keys(platform_common)
    duplicated = sorted({key for key in platform_keys if platform_keys.count(key) > 1})
    if duplicated:
        raise AssertionError(f"platform/.env keys are duplicated: {duplicated}")
    for line in platform_common.splitlines():
        key, _, value = line.partition("=")
        if re.search(r"\$\{local\.effective_oracle_(user|password|dsn|wallet)", value):
            if not (value.startswith("'") and value.endswith("'")):
                raise AssertionError(f"platform/.env value must be single-quoted: {key}")

    rag_env = _heredoc(locals_source, "rag_backend_env")
    rag_keys = _env_keys(rag_env) + _env_keys(platform_common)
    unit_owned = sorted(set(rag_keys) & RAG_UNIT_OWNED_ENV_KEYS)
    if unit_owned:
        raise AssertionError(f"RAG .env must not set keys owned by the systemd units / Settings defaults: {unit_owned}")
    # RAG の backend/.env は python-dotenv と systemd の EnvironmentFile が読む。入力値を書くなら single quote で囲む。
    for line in rag_env.splitlines():
        key, _, value = line.partition("=")
        if re.search(r"\$\{var\.", value) and not (value.startswith("'") and value.endswith("'")):
            raise AssertionError(f"RAG backend/.env value must be single-quoted: {key}")

    _verify_rag_services(locals_source)


def _rag_systemd_services() -> dict[str, str]:
    """rag/scripts/rag-systemd.sh の RAG_MICROSERVICES（id|dir|port|timeout）の id → dir。"""
    source = (REPO_ROOT / "rag" / "scripts" / "rag-systemd.sh").read_text(encoding="utf-8")
    block = re.search(r"(?ms)^RAG_MICROSERVICES=\(\n(.*?)^\)$", source)
    if block is None:
        raise AssertionError("rag/scripts/rag-systemd.sh RAG_MICROSERVICES not found")
    services: dict[str, str] = {}
    for line in block.group(1).splitlines():
        service_id, directory, _port, _timeout = line.strip().strip('"').split("|")
        services[service_id] = directory
    return services


def _verify_rag_services(locals_source: str) -> None:
    match = re.search(r"(?ms)^  rag_services = concat\(\n(.*?)^  \)$", locals_source)
    if match is None:
        raise AssertionError("locals.tf rag_services concat() not found")
    services_local = match.group(1)
    listed = re.findall(r'"([a-z0-9-]+)"', services_local)
    # 入力（var.*）で選ぶ parser と、それ以外（常に配備する）を分ける。
    optional = [
        name
        for group in re.findall(r"var\.[a-z0-9_]+ \? \[([^\]]*)\]", services_local)
        for name in re.findall(r'"([a-z0-9-]+)"', group)
    ]
    base = [name for name in listed if name not in optional]
    if base != RAG_BASE_SERVICES:
        raise AssertionError(f"RAG base services changed: {base}")
    if sorted(optional) != sorted(RAG_OPTIONAL_SERVICES):
        raise AssertionError(f"RAG optional services changed: {optional}")
    _require_all(
        services_local,
        [
            'var.rag_enable_parser_unstructured ? ["parser-unstructured"] : []',
        ],
        context="RAG optional services",
    )
    # 各サービスは rag/scripts/rag-systemd.sh に unit の定義（venv のディレクトリ・port）がある。
    defined = _rag_systemd_services()
    missing = sorted(name for name in listed if name not in defined)
    if missing:
        raise AssertionError(f"RAG services not defined in rag/scripts/rag-systemd.sh: {missing}")
    # 並びはサービス管理画面と同じ（rag-systemd.sh の RAG_MICROSERVICES の順）。
    defined_order = [name for name in defined if name in listed]
    if listed != defined_order:
        raise AssertionError(f"RAG services must follow the order of RAG_MICROSERVICES: {listed}")
    for name in listed:
        directory = REPO_ROOT / "rag" / defined[name]
        if not (directory / "uv.lock").is_file():
            raise AssertionError(f"RAG service {name} has no uv.lock for `uv sync --locked`: {directory}")


def _verify_bootstrap(bootstrap: str) -> None:
    _require_all(
        bootstrap,
        [
            'APP_ROOT="/u01/aipoc"',
            'SUITE_REPO_DIR="$${APP_ROOT}/no.1-production-ready-suite"',
            'clone_or_update_repo "${application_git_url}" "${application_git_ref}" "$${SUITE_REPO_DIR}"',
            # 1 台の Compute の配備は platform/deploy/suite-init.sh が行う（#1316）。
            'local init_script="$${SUITE_REPO_DIR}/platform/deploy/suite-init.sh"',
            'bash "$${init_script}"',
            'path: "/u01/aipoc/bootstrap-suite.sh"',
            "nohup bash /u01/aipoc/bootstrap-suite.sh",
            # 再実行では repo の所有者（opc）で git を実行する（#1994）。
            'runuser -u "$${owner}" -- git -C "$${dest}" fetch --depth 1 origin "$${ref}"',
            "%{ for product, env in backend_envs ~}",
            'path: "/u01/aipoc/props/${product}.backend.env"',
            'path: "/u01/aipoc/props/products.txt"',
            'path: "/u01/aipoc/props/https_enabled.txt"',
            'path: "/u01/aipoc/props/assign_public_ip.txt"',
            'path: "/u01/aipoc/props/http_port.txt"',
            'path: "/u01/aipoc/props/https_port.txt"',
            # OS の時間帯（#3255。cloud-init の timezone。suite-init.sh も PLATFORM_TIMEZONE で合わせ直す）。
            'timezone: "${timezone}"',
            "- openssl",
            # 運用の道具（#1566）: 最新のコードの取得（platform/deploy/suite-pull.sh）と、長い作業を切れない端末で。
            "- git",
            "- tmux",
        ],
        context="Compute bootstrap",
    )
    for secret_path in (
        "/u01/aipoc/props/${product}.backend.env",
        "/u01/aipoc/props/platform.env",
        "/u01/aipoc/props/wallet.zip",
    ):
        if not re.search(
            rf'(?ms)path: "{re.escape(secret_path)}"\n    permissions: "0600"\n    owner: "root:root"\n',
            bootstrap,
        ):
            raise AssertionError(f"{secret_path} must be written root-only (0600)")
    # RAG の前処理 / parser の一覧は、RAG を配備するときだけ書く。
    rag_block = re.search(r'(?ms)^%\{ if rag_services != "" ~\}\n(.*?)^%\{ endif ~\}', bootstrap)
    if rag_block is None or 'path: "/u01/aipoc/props/rag_services.txt"' not in rag_block.group(1):
        raise AssertionError("rag_services.txt must be written only when RAG is deployed")
    if "basic_auth" in bootstrap:
        raise AssertionError("the Agent uses the backend login; cloud-init must not write Basic authentication files")
    _require_in_order(
        bootstrap,
        ["clone_or_update_repo ", "run_suite_init\n"],
        context="bootstrap order",
    )


def _verify_app_db_user(schema: str, variables: str, adb: str, locals_source: str, bootstrap: str) -> None:
    """アプリ用の DB ユーザー（#1668）: 入力・接続のユーザー・ADMIN のパスワードの置き場所・Compute での作成。"""
    _require_all(
        _terraform_variable(variables, "adb_app_user_enabled"),
        ["type        = bool", "default     = true"],
        context="adb_app_user_enabled",
    )
    _require_all(
        _terraform_variable(variables, "adb_app_user_name"),
        ['default     = "ADB_APP_USER"', 'can(regex("^[A-Za-z][A-Za-z0-9_]{0,29}$", var.adb_app_user_name))', '"ADMIN"'],
        context="adb_app_user_name",
    )
    _require_all(
        _terraform_variable(variables, "adb_app_user_password"),
        ["length(var.adb_app_user_password) >= 12", "length(var.adb_app_user_password) <= 30"],
        context="adb_app_user_password",
    )
    # フォームの並び（#1675）: 使うかどうかは ADB の利用方法の直後、名前とパスワードは新規 / 既存の ADB の入力
    # （ADMIN のパスワード）とADB のネットワークの後の「アプリ用の DB ユーザー」の group。
    groups = _schema_groups(schema)
    _, adb_members = groups.get("Autonomous AI Database", ("", []))
    if adb_members[-2:] != ["adb_deployment_mode", "adb_app_user_enabled"]:
        raise AssertionError(f"adb_app_user_enabled must follow adb_deployment_mode: {adb_members}")
    visible, members = groups.get("アプリ用の DB ユーザー", ("", []))
    if visible != "adb_app_user_enabled" or members != ["adb_app_user_name", "adb_app_user_password"]:
        raise AssertionError(f"the application database user group must show the name and password when enabled: {members}")
    titles = list(groups)
    order = ["Autonomous AI Database", "Existing Autonomous AI Database", "New Autonomous AI Database", "ADB のネットワーク", "アプリ用の DB ユーザー"]
    if [title for title in titles if title in order] != order:
        raise AssertionError(f"ADB groups must be ordered as {order}: {titles}")
    _, existing = groups.get("Existing Autonomous AI Database", ("", []))
    if "existing_adb_admin_password" not in existing:
        raise AssertionError("existing_adb_admin_password must be in the existing ADB group")
    # 既定はオン（#1668・#1675）。名前の既定は ADB_APP_USER。
    _require_all(_schema_variable(schema, "adb_app_user_enabled"), ["type: boolean", "default: true"], context="adb_app_user_enabled schema")
    _require_all(_schema_variable(schema, "adb_app_user_name"), ['default: "ADB_APP_USER"'], context="adb_app_user_name schema")
    for name in ("adb_app_user_name", "adb_app_user_password", "existing_adb_admin_password"):
        _require_all(_schema_variable(schema, name), ["visible: adb_app_user_enabled"], context=f"{name} schema")
    for name in ("existing_oracle_user", "existing_oracle_password"):
        _require_all(
            _schema_variable(schema, name), ["visible:\n      not:\n        - adb_app_user_enabled"], context=f"{name} schema"
        )

    normalized = re.sub(r"[ \t]+", " ", adb)
    _require_all(
        normalized,
        [
            "adb_app_user = upper(trimspace(var.adb_app_user_name))",
            "effective_adb_admin_password = local.create_new_adb ? var.adb_password : var.existing_adb_admin_password",
            "effective_oracle_user = var.adb_app_user_enabled ? local.adb_app_user : (",
            "effective_oracle_password = var.adb_app_user_enabled ? var.adb_app_user_password : (",
            # 使わないとき（adb_app_user_enabled=false）は #1668 の前と同じ: 新規の ADB は ADMIN / adb_password、
            # 既存の ADB は existing_oracle_user / existing_oracle_password。
            'local.create_new_adb ? "ADMIN" : var.existing_oracle_user',
            "local.create_new_adb ? var.adb_password : var.existing_oracle_password",
            "!var.adb_app_user_enabled || trimspace(var.adb_app_user_password) != \"\"",
            "!var.adb_app_user_enabled || local.create_new_adb || trimspace(var.existing_adb_admin_password) != \"\"",
        ],
        context="application database user (adb.tf)",
    )
    # ADMIN のパスワードは platform/.env・各製品の backend/.env に書かない（props の adb_admin.env だけ）。
    for heredoc in ("platform_env", "rag_backend_env", "nl2sql_backend_env", "agent_backend_env"):
        body = _heredoc(locals_source, heredoc)
        for forbidden in ("adb_password", "existing_adb_admin_password", "effective_adb_admin_password"):
            if forbidden in body:
                raise AssertionError(f"{heredoc} must not contain the ADMIN password ({forbidden})")
    _require_all(
        _heredoc(locals_source, "adb_admin_env"),
        [
            "SUITE_DB_ADMIN_USER=ADMIN",
            "SUITE_DB_ADMIN_PASSWORD='${local.effective_adb_admin_password}'",
            "SUITE_DB_APP_USER='${local.adb_app_user}'",
        ],
        context="adb_admin.env",
    )
    _require_all(
        re.sub(r"[ \t]+", " ", locals_source),
        ['adb_admin_env = var.adb_app_user_enabled ? base64gzip(local.adb_admin_env) : ""'],
        context="adb_admin.env only with the application database user",
    )
    block = re.search(r'(?ms)^%\{ if adb_admin_env != "" ~\}\n(.*?)^%\{ endif ~\}', bootstrap)
    if block is None or not re.search(
        r'path: "/u01/aipoc/props/adb_admin.env"\n    permissions: "0600"\n    owner: "root:root"\n', block.group(1)
    ):
        raise AssertionError("adb_admin.env must be written root-only (0600) and only with the application database user")

    # ユーザーの作成と grant は Compute の上（suite-init.sh が製品の配備の前に suite-db-user.py を実行）。ADB には
    # Compute からしか接続できないため、Terraform（provider・local-exec）では DB に接続しない。
    suite_init = (SUITE_DEPLOY_DIR / "suite-init.sh").read_text(encoding="utf-8")
    main_body = re.search(r"(?ms)^main\(\) \{\n(.*?)^\}", suite_init)
    if main_body is None:
        raise AssertionError("suite-init.sh main not found")
    # 失敗しても製品の配備と起動は続ける（#1756。`if ! provision_db_app_user; then` の形）。
    _require_in_order(
        main_body.group(1), ["if ! provision_db_app_user; then\n", "deploy_products\n"], context="suite-init main"
    )
    _require_all(
        suite_init,
        ['"${PROPS_DIR}/adb_admin.env"', 'python "${SUITE_DEPLOY_DIR}/suite-db-user.py" apply'],
        context="suite-init provision_db_app_user",
    )
    script = (SUITE_DEPLOY_DIR / "suite-db-user.py").read_text(encoding="utf-8")
    _require_all(
        script,
        [
            '"GRANT PDB_DBA TO {user}"',
            '"GRANT GRANT ANY DATA ROLE TO {user}"',
            '"GRANT ADMINISTER ANY DATA GRANT TO {user}"',
            '"GRANT SET USE DATA GRANTS ONLY TO {user}"',
            'APP_PROFILE = "ORA_APP_PROFILE"',
        ],
        context="suite-db-user.py grants",
    )


def _verify_reserved_ports(locals_source: str) -> None:
    """公開の port に使えない port の一覧を Terraform（locals.tf）と配備のスクリプトでそろえる（#1316）。"""
    suite_nginx = (SUITE_DEPLOY_DIR / "suite-nginx.sh").read_text(encoding="utf-8")
    tf_ports = re.search(r"(?m)^  reserved_ports = \[([0-9, ]+)\]$", locals_source)
    tf_ranges = re.findall(r"\[(\d+), (\d+)\]", (re.search(r"(?m)^  reserved_port_ranges = (.*)$", locals_source) or [""])[0])
    sh_ports = re.search(r"(?m)^SUITE_RESERVED_PORTS=\(([0-9 ]+)\)$", suite_nginx)
    sh_ranges = re.findall(r'"(\d+)-(\d+)"', (re.search(r"(?m)^SUITE_RESERVED_PORT_RANGES=\((.*)\)$", suite_nginx) or [""])[0])
    if tf_ports is None or sh_ports is None:
        raise AssertionError("reserved ports are missing from locals.tf or platform/deploy/suite-nginx.sh")
    # 公開する port は 1 つだけ（HTTPS が on なら https_port、off なら http_port。#1638）。
    if "public_ports = var.https_enabled ? [var.https_port] : [var.http_port]" not in locals_source:
        raise AssertionError("locals.tf public_ports must be [https_port] when HTTPS is on and [http_port] when off (#1638)")
    tf_set = {int(port) for port in tf_ports.group(1).split(",")}
    sh_set = {int(port) for port in sh_ports.group(1).split()}
    if tf_set != sh_set or sorted(tf_ranges) != sorted(sh_ranges) or not tf_ranges:
        raise AssertionError(f"reserved ports differ: Terraform {sorted(tf_set)} {tf_ranges} / suite {sorted(sh_set)} {sh_ranges}")
    # frontend の開発・プレビューの port（vite。RAG 3000・NL2SQL 3001・Agent 3002）も拒む（#1747）。
    required = {22, 3000, 3001, 3002, *(int(port) for port in BACKEND_PORTS.values())}
    if not required <= tf_set:
        raise AssertionError(
            f"reserved ports must include SSH, the frontend dev ports and the backend ports: {sorted(required - tf_set)}"
        )
    rag_systemd = (REPO_ROOT / "rag" / "scripts" / "rag-systemd.sh").read_text(encoding="utf-8")
    block = re.search(r"(?ms)^RAG_MICROSERVICES=\(\n(.*?)^\)$", rag_systemd)
    assert block is not None
    for line in block.group(1).splitlines():
        port = int(line.strip().strip('"').split("|")[2])
        if not any(int(low) <= port <= int(high) for low, high in tf_ranges):
            raise AssertionError(f"RAG service port {port} must be in a reserved range")


def _verify_suite_deploy(init_sources: dict[str, str]) -> None:
    """platform/deploy の 1 台の Compute の配備（Nginx の prefix・HTTPS・証明書。#1316）。"""
    suite_init = (SUITE_DEPLOY_DIR / "suite-init.sh").read_text(encoding="utf-8")
    suite_nginx = (SUITE_DEPLOY_DIR / "suite-nginx.sh").read_text(encoding="utf-8")
    suite_tls = (SUITE_DEPLOY_DIR / "suite-tls.sh").read_text(encoding="utf-8")
    # backend の port は製品の init_script.sh・Nginx・Agent の MCP の URL で同じ値（衝突しない）。
    if len(set(BACKEND_PORTS.values())) != len(BACKEND_PORTS):
        raise AssertionError("backend ports must differ on the single Compute")
    for product, port in BACKEND_PORTS.items():
        if f'BACKEND_PORT="{port}"' not in init_sources[product]:
            raise AssertionError(f"{product}/init_script.sh BACKEND_PORT must be {port}")
        name = f"SUITE_{product.upper()}_BACKEND_PORT"
        if f'{name}="${{{name}:-{port}}}"' not in suite_nginx:
            raise AssertionError(f"platform/deploy/suite-nginx.sh must proxy {product} to 127.0.0.1:{port}")
    # RAG の前処理 / parser の port（127.0.0.1:18010〜）は backend の port と重ならない。
    rag_systemd = (REPO_ROOT / "rag" / "scripts" / "rag-systemd.sh").read_text(encoding="utf-8")
    block = re.search(r"(?ms)^RAG_MICROSERVICES=\(\n(.*?)^\)$", rag_systemd)
    if block is None:
        raise AssertionError("rag/scripts/rag-systemd.sh RAG_MICROSERVICES not found")
    service_ports = [line.strip().strip('"').split("|")[2] for line in block.group(1).splitlines()]
    overlap = sorted(set(service_ports) & set(BACKEND_PORTS.values()))
    if overlap or len(set(service_ports)) != len(service_ports):
        raise AssertionError(f"RAG service ports must be unique and differ from the backend ports: {overlap}")
    # Nginx の upload の上限は RAG の backend の RAG_MAX_UPLOAD_BYTES の既定値（rag/backend/app/config.py）+ 余白。
    rag_config = (REPO_ROOT / "rag" / "backend" / "app" / "config.py").read_text(encoding="utf-8")
    default = re.search(r"max_upload_bytes: int = Field\(default=([0-9 *]+),", rag_config)
    default_bytes = math.prod(int(part) for part in default.group(1).split("*")) if default else None
    if default_bytes is None or f"RAG_DEFAULT_MAX_UPLOAD_BYTES={default_bytes}" not in suite_init:
        raise AssertionError("platform/deploy/suite-init.sh RAG_DEFAULT_MAX_UPLOAD_BYTES must match rag/backend/app/config.py")
    # 単独の配備（製品ごとの Nginx の site）は製品の init_script.sh に残さない。
    for product, source in init_sources.items():
        leftovers = [name for name in REMOVED_STANDALONE_DEPLOY if name in source]
        if leftovers:
            raise AssertionError(f"{product}/init_script.sh must not deploy standalone (#1316): {leftovers}")
    _require_all(
        suite_nginx,
        [
            "listen ${https_listen} ssl http2 default_server;",
            "ssl_protocols TLSv1.2 TLSv1.3;",
            "location = /platform/ca.crt {",
            "default_type application/x-x509-ca-cert;",
            'filename="production-ready-root-ca.crt"',
            "location /platform/ {",
            "return 302 ${root_target};",
            "printf '/agent/\\n'",
            "limit_req zone=pr_login burst=30 nodelay;",
            "proxy_buffering off;",
        ],
        context="platform/deploy/suite-nginx.sh",
    )
    if "Strict-Transport-Security" in suite_nginx:
        raise AssertionError("HSTS must not be sent (IP certificate signed by a private CA)")
    # HTTPS が on のときは http_port を listen せず、HTTP から HTTPS へ転送しない（#1638）。
    if "return 301 https://" in suite_nginx:
        raise AssertionError("platform/deploy/suite-nginx.sh must not redirect http_port to HTTPS (#1638)")
    if "ca.key" in suite_nginx:
        raise AssertionError("the CA private key must never be served")
    _require_all(
        suite_tls,
        [
            f'SUITE_TLS_CA_SUBJECT="{TLS_CA_SUBJECT}"',
            f'SUITE_TLS_SERVER_SUBJECT_PREFIX="{TLS_SERVER_SUBJECT_PREFIX}"',
            "SUITE_TLS_KEY_BITS=3072",
            "SUITE_TLS_CA_DAYS=3650",
            "SUITE_TLS_SERVER_DAYS=397",
            'chmod 0600 "${dir}/ca.key" "${dir}/server.key"',
        ],
        context="platform/deploy/suite-tls.sh",
    )
    if "No.1" in TLS_CA_SUBJECT + TLS_SERVER_SUBJECT_PREFIX:
        raise AssertionError("the certificate subject must not contain No.1")
    _require_all(
        suite_init,
        [
            '"FRONTEND_BASE_PATH=/${product}/"',
            '"BACKEND_ENV_SOURCE=${PROPS_DIR}/${product}.backend.env"',
            '"SERVICE_USER=${APP_USER}"',
            "suite_tls_ensure",
            "suite_nginx_site",
            'local site="${NGINX_CONF_DIR}/${SUITE_SITE_NAME}.conf"',
            'install -m 0644 "${template_dir}/nginx.conf" "${NGINX_MAIN_CONF}"',
        ],
        context="platform/deploy/suite-init.sh",
    )


def _shell_scalar(source: str, name: str, *, context: str) -> str:
    """`NAME=value` か `NAME="${NAME:-value}"` の値（既定値）。"""
    match = re.search(rf'(?m)^{name}=(?:"\$\{{{name}:-([^}}]+)\}}"|([^\s#]+))$', source)
    if match is None:
        raise AssertionError(f"{context}: {name} not found")
    return match.group(1) or match.group(2)


def _shell_array(source: str, name: str, *, context: str) -> list[str]:
    match = re.search(rf"(?ms)^{name}=\((.*?)^\)$", source)
    if match is None:
        raise AssertionError(f"{context}: {name} not found")
    return re.findall(r"'([^']+)'", match.group(1))


def _python_number(source: str, pattern: str, *, context: str) -> float:
    match = re.search(pattern, source)
    if match is None:
        raise AssertionError(f"{context}: {pattern} not found")
    return float(math.prod(float(part.replace("_", "")) for part in match.group(1).split("*")))


def _route_paths(module: Path, prefix: str = "") -> set[str]:
    """FastAPI の route の path（`@router.<method>(path)`。module の str 定数と APIRouter の prefix を解く）。"""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    constants: dict[str, str] = {}

    def value(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return constants.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = value(node.left), value(node.right)
            return None if left is None or right is None else left + right
        return None

    router_prefix = ""
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "APIRouter":
                if name == "router":
                    router_prefix = next((value(k.value) or "" for k in node.value.keywords if k.arg == "prefix"), "")
            elif (text := value(node.value)) is not None:
                constants[name] = text
    paths: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and getattr(decorator.func.value, "id", "") == "router"
                and decorator.args
                and (path := value(decorator.args[0])) is not None
            ):
                paths.add(prefix + router_prefix + path)
    return paths


def _rag_route_paths() -> set[str]:
    """RAG の /api の下の route（app/api/router.py の include_router の prefix を付ける）。"""
    api_dir = REPO_ROOT / "rag" / "backend" / "app" / "api"
    tree = ast.parse((api_dir / "router.py").read_text(encoding="utf-8"))
    paths: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "include_router" and node.args):
            continue
        target = node.args[0]
        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)):
            continue
        module = api_dir / "routes" / f"{target.value.id}.py"
        prefix = next(
            (k.value.value for k in node.keywords if k.arg == "prefix" and isinstance(k.value, ast.Constant)), ""
        )
        if module.exists():
            paths |= _route_paths(module, prefix)
    return paths


def _verify_nginx_limits() -> None:
    """Nginx の時間切れ・本文の上限・ストリームの値を、3 製品の backend の値と照合する（#1597）。"""
    context = "platform/deploy/suite-nginx.sh"
    suite_nginx = (SUITE_DEPLOY_DIR / "suite-nginx.sh").read_text(encoding="utf-8")
    suite_init = (SUITE_DEPLOY_DIR / "suite-init.sh").read_text(encoding="utf-8")
    rag_config = (REPO_ROOT / "rag" / "backend" / "app" / "config.py").read_text(encoding="utf-8")
    nl2sql_settings = (REPO_ROOT / "nl2sql" / "backend" / "app" / "settings.py").read_text(encoding="utf-8")
    agent_settings = (REPO_ROOT / "agent" / "backend" / "app" / "settings.py").read_text(encoding="utf-8")
    agent_router_path = REPO_ROOT / "agent" / "backend" / "app" / "features" / "agent" / "router.py"
    agent_router = agent_router_path.read_text(encoding="utf-8")
    nl2sql_router_path = REPO_ROOT / "nl2sql" / "backend" / "app" / "features" / "nl2sql" / "router.py"

    # 本文の上限: backend の <製品>_MAX_UPLOAD_BYTES の既定値 + 余白（suite-init.sh と suite-nginx.sh の既定）。
    margin_mib = int(_shell_scalar(suite_init, "NGINX_UPLOAD_MARGIN_MIB", context="platform/deploy/suite-init.sh"))
    upload_defaults = {
        "rag": _python_number(rag_config, r"max_upload_bytes: int = Field\(default=([0-9 *]+),", context="rag config"),
        "nl2sql": _python_number(nl2sql_settings, r"max_upload_bytes: int = ([0-9 *]+)\n", context="nl2sql settings"),
        "agent": _python_number(agent_settings, r"max_upload_bytes: int = ([0-9 *]+)\n", context="agent settings"),
    }
    for product, default_bytes in upload_defaults.items():
        name = f"{product.upper()}_DEFAULT_MAX_UPLOAD_BYTES"
        if f"{name}={int(default_bytes)}" not in suite_init:
            raise AssertionError(f"platform/deploy/suite-init.sh {name} must match the {product} backend default")
        expected = f"{math.ceil(default_bytes / 1048576) + margin_mib}M"
        actual = _shell_scalar(suite_nginx, f"SUITE_{product.upper()}_CLIENT_MAX_BODY_SIZE", context=context)
        if actual != expected:
            raise AssertionError(f"{context}: {product} client_max_body_size must be {expected} (backend + margin): {actual}")

    # 時間切れ: 既定は 600 秒。SSE の heartbeat はそれより十分短い（間が空かない）。
    default_timeout = float(_shell_scalar(suite_nginx, "SUITE_PROXY_TIMEOUT_SECONDS", context=context))
    chat_progress = (
        REPO_ROOT / "platform" / "packages" / "backend_core" / "src" / "pr_backend_core" / "chat_progress.py"
    ).read_text(encoding="utf-8")
    heartbeats = {
        "pr_backend_core.chat_progress": _python_number(
            chat_progress, r"(?m)^CHAT_PROGRESS_SSE_HEARTBEAT_SECONDS = ([0-9.]+)", context="chat_progress"
        ),
        "rag chat SSE": _python_number(
            (REPO_ROOT / "rag" / "backend" / "app" / "api" / "routes" / "chat.py").read_text(encoding="utf-8"),
            r"(?m)^SSE_HEARTBEAT_SECONDS = ([0-9.]+)",
            context="rag chat",
        ),
        "agent run SSE": _python_number(
            (REPO_ROOT / "agent" / "backend" / "app" / "features" / "agent" / "runtime.py").read_text(encoding="utf-8"),
            r"idle_timeout_seconds: float = ([0-9.]+)",
            context="agent runtime",
        ),
    }
    for name, seconds in heartbeats.items():
        if not 0 < seconds * 4 <= default_timeout:
            raise AssertionError(f"{name} heartbeat {seconds}s must be well below proxy_read_timeout {default_timeout}s")

    # RAG: 回答の通しの上限（上限 600 秒）< 画面の待ち時間（630 秒）< Nginx。
    rag_timeout = float(_shell_scalar(suite_nginx, "SUITE_RAG_LONG_API_TIMEOUT_SECONDS", context=context))
    rag_answer_max = _python_number(
        rag_config, r"(?m)^OCI_ENTERPRISE_AI_TIMEOUT_MAX_SECONDS = ([0-9.]+)", context="rag config"
    )
    rag_api = (REPO_ROOT / "rag" / "frontend" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    for name in ("ANSWER_GENERATION_TIMEOUT_MS",):
        frontend_seconds = _python_number(rag_api, rf"export const {name} = ([0-9_]+);", context="rag api.ts") / 1000
        if not rag_answer_max < frontend_seconds < rag_timeout:
            raise AssertionError(
                f"RAG {name} ({frontend_seconds}s) must be between the answer limit ({rag_answer_max}s) "
                f"and Nginx ({rag_timeout}s)"
            )
    # NL2SQL: LLM 1 回の timeout × 試行の回数（再試行 + 1。timeout も再試行する）+ 60 秒。
    nl2sql_timeout = float(_shell_scalar(suite_nginx, "SUITE_NL2SQL_LONG_API_TIMEOUT_SECONDS", context=context))
    llm_seconds = _python_number(
        nl2sql_settings, r"oci_enterprise_ai_timeout_seconds: float = ([0-9.]+)", context="nl2sql settings"
    )
    llm_retries = _python_number(
        nl2sql_settings, r"oci_enterprise_ai_max_retries: int = ([0-9]+)", context="nl2sql settings"
    )
    if nl2sql_timeout != llm_seconds * (llm_retries + 1) + 60:
        raise AssertionError(
            f"{context}: SUITE_NL2SQL_LONG_API_TIMEOUT_SECONDS must be {llm_seconds} x {llm_retries + 1} + 60"
        )
    # Agent: 外部 MCP の timeout の上限 + 60 秒（tools/invoke はツールの応答を同期で待つ）。
    agent_timeout = float(_shell_scalar(suite_nginx, "SUITE_AGENT_LONG_API_TIMEOUT_SECONDS", context=context))
    mcp_max = _python_number(agent_router, r"(?m)^_MCP_TIMEOUT_MAX_SECONDS = ([0-9.]+)", context="agent router")
    if agent_timeout != mcp_max + 60:
        raise AssertionError(f"{context}: SUITE_AGENT_LONG_API_TIMEOUT_SECONDS must be {mcp_max} + 60")

    # 長い待ち時間の API は、今の backend の route にある（route の名前が変わったら気づく）。
    routes = {
        "rag": _rag_route_paths(),
        "nl2sql": _route_paths(nl2sql_router_path),
        "agent": _route_paths(agent_router_path),
    }
    for product, paths in routes.items():
        patterns = {re.sub(r"\{[^}]+\}", "[^/]+", path) for path in paths}
        for path in _shell_array(suite_nginx, f"SUITE_{product.upper()}_LONG_API_PATHS", context=context):
            if f"/{path}" not in patterns:
                raise AssertionError(f"{context}: {product} long API path /api/{path} is not a backend route")


def _verify_init_scripts(init_sources: dict[str, str]) -> None:
    for product, source in init_sources.items():
        _require_all(source, INIT_SCRIPT_CONTRACTS[product], context=f"{product}/init_script.sh")
        _require_in_order(source, INIT_SCRIPT_ORDER[product], context=f"{product}/init_script.sh main order")
    rag = init_sources["rag"]
    allowed_block = re.search(r"(?ms)^ALLOWED_RAG_SERVICES=\(\n(.*?)^\)$", rag)
    if allowed_block is None:
        raise AssertionError("rag/init_script.sh ALLOWED_RAG_SERVICES not found")
    if sorted(allowed_block.group(1).split()) != sorted(RAG_BASE_SERVICES + RAG_OPTIONAL_SERVICES):
        raise AssertionError("rag/init_script.sh allowed services differ from the stack")
    for product in ("rag", "agent"):
        if "auth_basic" in init_sources[product]:
            raise AssertionError(f"{product} uses the backend login; Nginx must not add Basic authentication")
    # 3 製品とも Docker を入れず、systemd で直接動かす（RAG は #286。以前の compose の配備を止める処理だけ残す）。
    if re.search(r"docker-ce|docker-compose-plugin|download\.docker\.com|docker (?:run|compose)\b", rag):
        raise AssertionError("rag is deployed natively with systemd and must not install or run Docker")
    for product in PRODUCTS:
        if re.search(r"(?im)^\s*(?:docker|docker-compose)\b", init_sources[product]):
            raise AssertionError(f"{product} is deployed directly with systemd and must not require Docker")


# 自前のコードは Docker イメージを作らない（#286 / #356）。Agent の外部 Runtime の compose も #754 で削除し、
# リポジトリに compose は無い。
_IGNORED_SOURCE_DIRS = {"node_modules", ".venv", "dist", ".git"}


def _repo_files(pattern: str) -> list[str]:
    files = []
    for top in (*PRODUCTS, "platform", "terraform"):
        for path in (REPO_ROOT / top).rglob(pattern):
            relative = path.relative_to(REPO_ROOT)
            if not _IGNORED_SOURCE_DIRS.intersection(relative.parts):
                files.append(relative.as_posix())
    return sorted(files)


def _verify_no_own_container_images() -> None:
    leftovers = _repo_files("Dockerfile*") + _repo_files(".dockerignore") + [
        path
        for pattern in ("docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml")
        for path in _repo_files(pattern)
    ]
    if (REPO_ROOT / ".dockerignore").exists():
        leftovers.append(".dockerignore")
    if leftovers:
        raise AssertionError(f"own code must not build Docker images or compose (#356 / #754): {', '.join(leftovers)}")
    rag_systemd = (REPO_ROOT / "rag" / "scripts" / "rag-systemd.sh").read_text(encoding="utf-8")
    if re.search(r"\bdocker\b", rag_systemd):
        raise AssertionError("rag/scripts/rag-systemd.sh must not use Docker")


def _verify_boundaries(sources: dict[str, str]) -> None:
    combined = "\n".join(sources.values())
    for name, source in sources.items():
        if "platform_git_" in source or "no.1-production-ready-platform" in source:
            raise AssertionError(f"{name} must not clone the shared platform separately")
    if re.search(r"(?i)\b(CREATE|ALTER|DROP)\s+(TABLE|INDEX|USER)\b|DBMS_CLOUD", combined):
        raise AssertionError("ADB DDL must stay in the application, not in the stack")
    if re.search(r"--profile[ =]gpu|nvidia", combined):
        raise AssertionError("GPU services must not be part of the stack")
    for legacy in REMOVED_AGENT_BASIC_AUTH:
        if legacy in combined:
            raise AssertionError(f"removed Agent Basic authentication remains: {legacy}")
    for legacy in (
        "PUBLIC_ENDPOINT",
        "SECURE_ACCESS_FROM_ALLOWED_IPS_AND_VCNS",
        "PRIVATE_ENDPOINT_ONLY",
        "CIDR_BLOCK",
        "adb_use_private_subnet",
    ):
        if legacy in combined:
            raise AssertionError(f"legacy ADB network input remains: {legacy}")


def verify(package_path: Path) -> None:
    with zipfile.ZipFile(package_path) as archive:
        _verify_package_entries(archive)
        sources = {
            name: archive.read(name).decode()
            for name in ("schema.yaml", "variables.tf", "adb.tf", "compute.tf", "locals.tf", "output.tf", "bastion.tf")
        }
        bootstrap = archive.read("cloud_init/bootstrap.template.yaml").decode()
        bastion_script = archive.read("cloud_init/bastion.sh").decode()
    init_sources = {
        product: (REPO_ROOT / product / "init_script.sh").read_text(encoding="utf-8") for product in PRODUCTS
    }

    _verify_schema(sources["schema.yaml"], sources["variables.tf"])
    _verify_timezone(sources["schema.yaml"], sources["variables.tf"])
    _verify_terraform(
        sources["variables.tf"],
        sources["adb.tf"],
        sources["compute.tf"],
        sources["locals.tf"],
        sources["output.tf"],
    )
    _verify_bootstrap(bootstrap)
    _verify_bastion(sources["schema.yaml"], sources["variables.tf"], sources["bastion.tf"], sources["output.tf"], bastion_script)
    _verify_app_db_user(sources["schema.yaml"], sources["variables.tf"], sources["adb.tf"], sources["locals.tf"], bootstrap)
    stack_sources = "\n".join([*sources.values(), bootstrap, bastion_script])
    # ADB には Compute からしか接続できない（プライベート・エンドポイント・ACL）。アプリ用の DB ユーザーの作成などの
    # DB の操作は Compute の suite-init.sh が行い、Terraform の provisioner（local-exec など）では行わない（#1668）。
    if re.search(r'(?m)^\s*provisioner\s+"', stack_sources):
        raise AssertionError("the stack must not run provisioners; database work runs on the Compute instance (#1668)")
    for removed in REMOVED_RAG_INPUTS:
        if removed in stack_sources:
            raise AssertionError(f"removed RAG stack input / output remains: {removed}")
    for removed in REMOVED_SINGLE_COMPUTE_INPUTS:
        if removed in stack_sources:
            raise AssertionError(f"removed per-product Compute input / resource remains (#1316): {removed}")
    _verify_init_scripts(init_sources)
    _verify_suite_deploy(init_sources)
    _verify_nginx_limits()
    _verify_reserved_ports(sources["locals.tf"])
    _verify_no_own_container_images()
    deploy_sources = {
        f"platform/deploy/{path.name}": path.read_text(encoding="utf-8") for path in sorted(SUITE_DEPLOY_DIR.glob("*.sh"))
    }
    _verify_boundaries(
        {
            **sources,
            "bootstrap": bootstrap,
            "cloud_init/bastion.sh": bastion_script,
            **{f"{p}/init_script.sh": s for p, s in init_sources.items()},
            **deploy_sources,
        }
    )
    _verify_oracle_linux(
        sources["schema.yaml"],
        sources["variables.tf"],
        sources["output.tf"],
        bootstrap,
        {
            **{f"{p}/init_script.sh": s for p, s in init_sources.items()},
            **deploy_sources,
            "cloud_init/bastion.sh": bastion_script,
        },
    )


def _verify_bastion(schema: str, variables: str, bastion: str, outputs: str, script: str) -> None:
    """任意の bastion の Compute（#2670）。既定は作らず、配置先・shape・image・SSH キーを主 Compute と独立に設定する。"""
    groups = _schema_groups(schema)
    if groups.get("Bastion") != ("true", ["bastion_enabled"]):
        raise AssertionError(f"the Bastion group must hold only bastion_enabled: {groups.get('Bastion')}")
    for title in ("bastion の構成", "bastion のネットワークと接続"):
        visible, members = groups.get(title, ("", []))
        if visible != "bastion_enabled" or not members or any(not name.startswith("bastion_") for name in members):
            raise AssertionError(f"{title} must contain bastion inputs shown only when enabled")
    _require_all(
        _schema_variable(schema, "bastion_enabled"),
        ["type: boolean", "required: true", "visible: true", "default: false"],
        context="bastion_enabled schema",
    )
    _require_all(
        _terraform_variable(variables, "bastion_enabled"),
        ["type        = bool", "default     = false"],
        context="bastion_enabled Terraform default",
    )
    for name in ("bastion_shape", "bastion_image_source_id", "bastion_ssh_authorized_keys", "bastion_compartment_ocid", "bastion_availability_domain", "bastion_vcn_id", "bastion_subnet_id"):
        _require_all(
            _schema_variable(schema, name), ["required: true", "visible: bastion_enabled"],
            context=f"{name} dedicated schema",
        )
    for name, default in {"bastion_shape": "VM.Standard.E5.Flex", "bastion_image_source_id": ORACLE_LINUX_IMAGES[DEFAULT_IMAGE_REGION]}.items():
        _require_all(_schema_variable(schema, name), [f'default: "{default}"'], context=f"{name} schema default")
        _require_all(_terraform_variable(variables, name), [f'default     = "{default}"'], context=f"{name} Terraform default")
    _require_all(_schema_variable(schema, "bastion_ssh_authorized_keys"), ["type: oci:core:ssh:publickey"], context="bastion SSH control")
    _require_all(_schema_variable(schema, "bastion_subnet_id"), ["compartmentId: ${bastion_compartment_ocid}", "vcnId: ${bastion_vcn_id}"], context="bastion subnet dependencies")
    for name, default in BASTION_DEFAULTS.items():
        _require_all(_schema_variable(schema, name), [f"default: {default}"], context=f"{name} schema default")
        _require_all(
            _terraform_variable(variables, name), [f"default     = {default}"], context=f"{name} Terraform default"
        )
    _require_all(
        _schema_variable(schema, "bastion_vnc_password"),
        ["type: password", "required: false", "visible: bastion_enabled", "confirmation: true", "pattern: '^$|^.{8}$'"],
        context="bastion_vnc_password schema",
    )
    # TigerVNC は先頭の 8 文字だけを使うため、パスワードはちょうど 8 文字に限る（#2673）。
    _require_all(
        _terraform_variable(variables, "bastion_vnc_password"),
        ["sensitive   = true", "length(var.bastion_vnc_password) == 8"],
        context="bastion_vnc_password Terraform validation",
    )
    _require_all(
        re.sub(r"[ \t]+", " ", bastion),
        [
            'resource "oci_core_instance" "bastion" {',
            "count = var.bastion_enabled ? 1 : 0",
            "shape = var.bastion_shape",
            "source_id = var.bastion_image_source_id",
            '"ssh_authorized_keys" = var.bastion_ssh_authorized_keys',
            'base64encode(file("${path.module}/cloud_init/bastion.sh"))',
            '"bastion_vnc_password_b64" = base64encode(var.bastion_vnc_password)',
            "memory_in_gbs = var.bastion_flex_shape_memory",
            "ocpus = var.bastion_flex_shape_ocpus",
            "boot_volume_size_in_gbs = var.bastion_boot_volume_size",
            "boot_volume_vpus_per_gb = var.bastion_boot_volume_vpus",
            "length(var.bastion_vnc_password) == 8",
        ],
        context="bastion.tf",
    )
    for reference in ("var.instance_", "var.ssh_authorized_keys", "var.subnet_ai_subnet_id", "var.availability_domain", "var.compartment_ocid", "var.vcn_ai_vcn_id"):
        if reference in bastion:
            raise AssertionError(f"bastion must not inherit main Compute input: {reference}")
    _require_all(re.sub(r"[ \t]+", " ", bastion), ["availability_domain = var.bastion_availability_domain", "compartment_id = var.bastion_compartment_ocid", "subnet_id = var.bastion_subnet_id"], context="bastion placement")
    # bastion に 3 製品の配備・ADB の情報を渡さない（Suite の Compute の cloud-init とは別）。
    for leaked in ("cloud_init_user_data", "wallet", "platform_env", "backend_envs", "adb_password"):
        if leaked in bastion:
            raise AssertionError(f"bastion.tf must not pass the suite deployment inputs: {leaked}")
    if "oci_core_network_security_group" in bastion or "nsg_ids" in bastion:
        raise AssertionError("The stack must not create NSGs; allow the bastion ports in the subnet security list")
    if not script.startswith("#!/bin/bash\n"):
        raise AssertionError("cloud_init/bastion.sh must start with the shebang (cloud-init runs it as a shell script)")
    _require_all(
        script,
        [
            "/usr/libexec/oci-growfs -y",
            'groupinstall "Server with GUI"',
            "tigervnc-server",
            "/opc/v2/instance/metadata",
            "bastion_vnc_password_b64",
            'firewall-cmd --permanent --add-port="${VNC_PORT}/tcp"',
            "baseurl=https://dl.google.com/linux/chrome/rpm/stable/x86_64",
            "gpgcheck=1",
            "dnf -y install google-chrome-stable",
            'systemctl enable "vncserver@:${VNC_DISPLAY}.service"',
        ],
        context="cloud_init/bastion.sh",
    )
    if 'VNC_PORT="$((5900 + VNC_DISPLAY))"' not in script or 'VNC_DISPLAY="1"' not in script:
        raise AssertionError(f"the bastion VNC must listen on TCP {BASTION_VNC_PORT} (display :1)")
    # VNC のパスワードはスクリプトに書かない（metadata から読む）。
    if re.search(r'(?m)^\s*VNC_PASS(?:WORD)?="[^$]', script):
        raise AssertionError("cloud_init/bastion.sh must not hard-code the VNC password")
    for name, value in (
        ("bastion_vnc_address", f':{BASTION_VNC_PORT}" : null'),
        ("ssh_to_bastion", "ssh -o ServerAliveInterval=10 opc@${local.bastion_access_ip}"),
    ):
        block = re.search(rf'(?ms)^output "{name}" \{{.*?^\}}', outputs)
        if block is None or value not in block.group(0) or "var.bastion_enabled ?" not in block.group(0):
            raise AssertionError(f"output {name} must be shown only when bastion_enabled")
        if not re.search(rf"(?ms)^  {name}:\n(?:    .*\n)*?    visible: bastion_enabled", _schema_section(schema, "outputs")):
            raise AssertionError(f"output {name} must be shown only when bastion_enabled in the schema")


# 時間帯（#3255・#3274）: stack で選べるのは OCI と同じ IANA の地域名の 2 つだけ（利用者の決定）。既定は UTC。
# 配備した後の platform/.env の PLATFORM_TIMEZONE は任意の IANA の地域名を受ける（入口だけを絞る）。
TIMEZONE_CHOICES = ["UTC", "Asia/Tokyo"]
DEFAULT_TIMEZONE = "UTC"


def _verify_timezone(schema: str, variables: str) -> None:
    timezone_schema = _schema_variable(schema, "timezone")
    choices = re.findall(r'(?m)^      - "([^"]+)"$', timezone_schema)
    if "    type: enum\n" not in timezone_schema or choices != TIMEZONE_CHOICES:
        raise AssertionError(f"the timezone input must be an enum of {TIMEZONE_CHOICES}: {choices}")
    if f'    default: "{DEFAULT_TIMEZONE}"\n' not in timezone_schema:
        raise AssertionError(f"the timezone input must default to {DEFAULT_TIMEZONE}")
    # 画面には出さない（#3274。変数は残す）。
    if "    visible: false\n" not in timezone_schema:
        raise AssertionError("the timezone input must be hidden in the Resource Manager form")
    variable = _terraform_variable(variables, "timezone")
    _require_all(
        variable,
        [
            f'default     = "{DEFAULT_TIMEZONE}"',
            "contains([" + ", ".join(f'"{value}"' for value in TIMEZONE_CHOICES) + "], var.timezone)",
        ],
        context="Terraform variable timezone",
    )


def _verify_oracle_linux(
    schema: str, variables: str, outputs: str, bootstrap: str, scripts: dict[str, str]
) -> None:
    """Compute の OS（Oracle Linux 9）・ユーザー（opc）と、配備のスクリプトの OS の前提（#1611）。"""
    image_schema = _schema_variable(schema, "instance_image_source_id")
    enum = re.findall(r'(?m)^      - "(ocid1\.image\.[^"]+)"$', image_schema)
    if enum != list(ORACLE_LINUX_IMAGES.values()):
        raise AssertionError(f"the Compute image must be the pinned Oracle Linux 9 images: {enum}")
    bastion_image = _schema_variable(schema, "bastion_image_source_id")
    bastion_enum = re.findall(r'(?m)^      - "(ocid1\.image\.[^"]+)"$', bastion_image)
    if bastion_enum != list(ORACLE_LINUX_IMAGES.values()):
        raise AssertionError("Bastion image options must match the pinned Oracle Linux 9 images")
    default_image = ORACLE_LINUX_IMAGES[DEFAULT_IMAGE_REGION]
    if f'default: "{default_image}"' not in image_schema or f'default     = "{default_image}"' not in variables:
        raise AssertionError("the default Compute image must be the Oracle Linux 9 image of ap-osaka-1")
    if "Oracle Linux 9" not in image_schema:
        raise AssertionError("the Compute image description must say Oracle Linux 9")
    if f"ssh -o ServerAliveInterval=10 {COMPUTE_USER}@" not in outputs:
        raise AssertionError(f"ssh_to_instance must log in as {COMPUTE_USER}")
    for name, source in {"schema.yaml": schema, "variables.tf": variables, "output.tf": outputs, **scripts}.items():
        if re.search(r"(?i)ubuntu", source):
            raise AssertionError(f"{name} must not assume Ubuntu (#1611)")
    for name, source in {"bootstrap": bootstrap, **scripts}.items():
        leftovers = [token for token in UBUNTU_ONLY_TOKENS if token in source]
        if leftovers:
            raise AssertionError(f"{name} must not use Ubuntu-only commands or paths (#1611): {leftovers}")
    for product in PRODUCTS:
        _require_all(
            scripts[f"{product}/init_script.sh"],
            [f'APP_USER="${{APP_USER:-{COMPUTE_USER}}}"', 'retry_command 5 dnf -y "$@"'],
            context=f"{product}/init_script.sh (Oracle Linux)",
        )
    _require_all(
        scripts["platform/deploy/suite-init.sh"],
        [
            f'APP_USER="${{APP_USER:-{COMPUTE_USER}}}"',
            'NGINX_MODULE_STREAM="1.24"',
            'suite_dnf module enable "nginx:${NGINX_MODULE_STREAM}"',
            "systemctl enable --now firewalld",
            'firewall-cmd --permanent --add-port="${port}/tcp"',
            # HTTPS が on なら、以前に開けた http_port を閉じる（#1638）。
            'firewall-cmd --permanent --query-port="${http}/tcp"',
            'firewall-cmd --permanent --remove-port="${http}/tcp"',
            "firewall-cmd --reload",
            "setsebool -P httpd_can_network_connect 1",
            'semanage fcontext -a -t usr_t "${SUITE_REPO_DIR}(/.*)?"',
            'semanage fcontext -a -t httpd_sys_content_t "${SUITE_REPO_DIR}/${product}/frontend/dist(/.*)?"',
            'semanage fcontext -a -t cert_t "${SSL_DIR}(/.*)?"',
            'semanage port -a -t http_port_t -p tcp "${port}"',
            # 開発用の worktree と repository の中の一時ファイルは辿らない（#2258）。
            'restorecon -R "${excludes[@]}" "${SUITE_REPO_DIR}"',
            'source "${SUITE_DEPLOY_DIR}/suite-repo.sh"',
        ],
        context="platform/deploy/suite-init.sh (Oracle Linux)",
    )
    # 製品の venv・systemd の unit を作る前に、SELinux の context の規則を置く。
    _require_in_order(
        scripts["platform/deploy/suite-init.sh"],
        ["  install_os_packages\n", "  configure_firewall\n", "  configure_selinux\n", "  deploy_products\n"],
        context="platform/deploy/suite-init.sh main order (Oracle Linux)",
    )
    template = (REPO_ROOT / "platform" / "templates" / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    _require_all(
        template,
        ["user nginx;", "include /usr/share/nginx/modules/*.conf;", "include /etc/nginx/conf.d/*.conf;"],
        context="platform/templates/nginx/nginx.conf",
    )
    if re.search(r"(?m)^\s*(?:server|listen)\b", template):
        raise AssertionError("platform/templates/nginx/nginx.conf must not keep the package default server")
    packages = re.search(r"(?ms)^packages:\n(.*?)^\S", bootstrap)
    if packages is None or "  - curl\n" in packages.group(1):
        raise AssertionError("cloud-init packages must not list curl (curl-minimal of the Oracle Linux image conflicts)")
    for name in ("firewalld", "policycoreutils-python-utils"):
        if f"  - {name}\n" not in packages.group(1):
            raise AssertionError(f"cloud-init must install {name}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="Path to the packaged OCI Resource Manager stack ZIP.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    verify(args.package)
    print("Suite Terraform stack contract verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
