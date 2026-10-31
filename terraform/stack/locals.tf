locals {
  compute_subnet_prohibits_public_ip = coalesce(
    data.oci_core_subnet.selected_compute_subnet.prohibit_public_ip_on_vnic,
    false,
  )

  wallet_dir_host = "/u01/aipoc/wallet"

  # 公開の port（http_port / https_port）に使えない port（#1316）。platform/deploy/suite-nginx.sh の
  # SUITE_RESERVED_PORTS / SUITE_RESERVED_PORT_RANGES と同じ値にする（verify_stack_contract.py が照合する）。
  #   22: SSH / 25: SMTP / 53: DNS / 111: rpcbind / 1521・1522: Oracle の listener（ADB の接続と紛らわしい）/
  #   3306: MySQL / 5432: PostgreSQL / 6379: Redis / 9090: Prometheus など運用の道具 /
  #   3000・3001・3002: 各製品の frontend の開発・プレビュー（vite。Compute の上で調べるときにぶつからないように。#1747）/
  #   8000・8010・8020: 各製品の backend（127.0.0.1）
  reserved_ports = [22, 25, 53, 111, 1521, 1522, 3000, 3001, 3002, 3306, 5432, 6379, 9090, 8000, 8010, 8020]
  # RAG の前処理 / parser（127.0.0.1:18010〜18028。rag/scripts/rag-systemd.sh）とその予備。
  reserved_port_ranges = [[18000, 18099]]
  # 実際に公開する port（HTTPS が on なら https_port だけ、off なら http_port だけ。#1638）。
  public_ports = var.https_enabled ? [var.https_port] : [var.http_port]
  public_ports_reserved = [
    for port in local.public_ports : port
    if contains(local.reserved_ports, port) || anytrue([for range in local.reserved_port_ranges : port >= range[0] && port <= range[1]])
  ]

  # 配備する製品。すべて 1 台の Compute（compute.tf の oci_core_instance.suite）に入れる（#1316）。
  product_enabled = {
    rag    = var.deploy_rag
    nl2sql = var.deploy_nl2sql
    agent  = var.deploy_agent
  }
  # 並びは rag → nl2sql → agent（platform/deploy/suite-init.sh もこの順に配備する）。
  selected_products = [for product in ["rag", "nl2sql", "agent"] : product if local.product_enabled[product]]

  # ---------------------------------------------------------------- RAG

  # RAG の Compute に配備する前処理 / parser（サービスごとの uv の venv と systemd の unit。#286）。
  # backend と ingestion-worker は常に配備するため、この一覧には含めない。
  # CPU の parser と、OCI を呼ぶ parser（入力で選んだとき）だけを配備する。parser-docling（既定の解析エンジン）と
  # parser-structured-records-jsonl（使用エンジンに選べる構造化レコード JSONL の解析。軽い。#2457）と
  # parser-xml-tree-json（使用エンジンに選べる XML ツリー JSON の解析。軽い）と、前処理の CSV→JSONL・Excel→JSONL・Word→JSONL は
  # 常に配備する（rag/init_script.sh の REQUIRED_RAG_SERVICES。以前の stack の一覧に無くても再配備で足す。#2042・#2486）。
  # parser-structured-records-jsonl は CSV / Excel の前処理と同じく、初めて配備したときに起動する。
  # parser-xml-tree-json は既定は停止で、初めて配備しても起動しない（サービス管理画面で起動する。
  # rag/scripts/rag-systemd.sh の RAG_DEFAULT_STOPPED_SERVICES。#2453）。
  # parser-unstructured は Docling が扱えない形式を取り込むための parser で、入力で外せる（既定は配備する）。
  # 並びはサービス管理画面と同じ（rag/scripts/rag-systemd.sh の RAG_MICROSERVICES。port は変えない）。
  rag_services = concat(
    [
      "preprocess-office-to-pdf",
      "preprocess-csv-to-jsonl",
      "preprocess-excel-to-jsonl",
      "preprocess-word-to-jsonl",
      "parser-docling",
    ],
    var.rag_enable_parser_unstructured ? ["parser-unstructured"] : [],
    ["parser-structured-records-jsonl", "parser-xml-tree-json"],
  )

  # rag/backend/.env（RAG_*）。backend が python-dotenv で読み、OCI parser の unit も EnvironmentFile で読む。
  # ログインは共通認証（構成管理者 system_admin と、ユーザー管理で作る DB ユーザー。#214）。
  # PLATFORM_ENVIRONMENT は init_script.sh が書く systemd の unit の Environment が正本、service URL と
  # PLATFORM_LOCAL_STORAGE_DIR / PLATFORM_OCI_CONFIG_FILE は Settings の既定値（127.0.0.1:<port>・
  # /u01/data/production-ready-rag・実行ユーザーの ~/.oci/config）を使うため、ここには書かない。
  # サービス管理画面の起動 / 停止は、sudoers で許可した unit の systemctl だけを実行する（#286）。
  # RAG_AUDIT_CONTEXT_HASH_SALT は instance 上で生成する（state に残さない）。
  # ADB の DDL は Terraform に持たない。init_script.sh がアプリの system schema CLI で適用する。
  rag_backend_env = <<-EOT
RAG_APP_VERSION=0.1.0
RAG_LOG_LEVEL=INFO
RAG_SEARCH_TOP_K_DEFAULT=50

RAG_AUDIT_CONTEXT_HASH_SALT=

RAG_PARSER_ADAPTER_BACKEND=docling
RAG_PARSER_DOCLING_ENABLED=true
RAG_PARSER_UNSTRUCTURED_ENABLED='${var.rag_enable_parser_unstructured}'
RAG_SERVICE_CONTROL_ENABLED=true
EOT

  # ---------------------------------------------------------------- NL2SQL

  nl2sql_data_dir_host = "/u01/data/production-ready-nl2sql"
  nl2sql_oracle_connection_security = (
    var.adb_is_mtls_connection_required ? "wallet_mtls" : "walletless_tls"
  )

  # nl2sql/backend/.env（NL2SQL_*）。3製品共通の設定は platform_envs（platform/.env）に置く（#211）。
  nl2sql_backend_env = <<-EOT
NL2SQL_APP_VERSION=0.1.0
NL2SQL_LOG_LEVEL=INFO
NL2SQL_SERVICE_NAME=production-ready-nl2sql
NL2SQL_CORS_ORIGINS=["http://localhost","http://127.0.0.1"]
NL2SQL_ENABLE_METRICS=true


NL2SQL_RUNTIME_MODE=oracle
NL2SQL_PERSISTENCE_MODE=oracle
NL2SQL_SCHEMA_REFRESH_WORKER_ENABLED=true
NL2SQL_SCHEMA_REFRESH_WORKER_MODE=external
NL2SQL_QUALITY_EVALUATION_WORKER_MODE=external
NL2SQL_ONTOLOGY_WORKER_MODE=external
EOT

  # ---------------------------------------------------------------- Agent

  # Agent は python-oracledb Thin mode + Wallet(mTLS) だけで接続する。
  # 共通認証・Agent の権限・Run の保存先・定義の table は、init_script.sh が agent_system_schema --initialize で
  # 作成する（DDL は Terraform に持たない。#751 / #764）。DB は共通の PLATFORM_ORACLE_*（platform/.env）で接続する。
  # ログインは共通認証（構成管理者 system_admin と、ユーザー管理で作る DB ユーザー。#215）。
  # gunicorn は 1 worker・dispatcher は in_process に固定する（checkpoint repository は process 内に状態を持つため）。
  agent_data_dir_host = "/u01/data/production-ready-agent"

  # agent/backend/.env（AGENT_*）。3製品共通の設定は platform_envs（platform/.env）に置く（#211）。
  agent_backend_env = <<-EOT
AGENT_APP_VERSION=0.1.0
AGENT_LOG_LEVEL=INFO
AGENT_SERVICE_NAME=production-ready-agent
AGENT_CORS_ORIGINS=["http://localhost","http://127.0.0.1"]


AGENT_RUNTIME_REPOSITORY_BACKEND=${var.agent_runtime_repository_backend}
AGENT_RUNTIME_DISPATCH_MODE=in_process
AGENT_ARTIFACT_STORAGE_PATH=${local.agent_data_dir_host}/artifacts

AGENT_EXTERNAL_RAG_MCP_URL=${local.product_mcp_urls["rag"]}
AGENT_EXTERNAL_NL2SQL_MCP_URL=${local.product_mcp_urls["nl2sql"]}
AGENT_EXTERNAL_RAG_PUBLIC_URL=${local.product_browser_urls["rag"]}
EOT

  # Agent が RAG / NL2SQL の MCP（POST /api/mcp）を呼ぶ URL（#233）。配備した製品だけ。
  # 同じ Compute の backend（127.0.0.1。Nginx を通さない）を直接呼ぶ（#1316）。port は各製品の init_script.sh の
  # BACKEND_PORT と platform/deploy/suite-nginx.sh と同じ（RAG 8000・NL2SQL 8010）。
  # 認証は呼び出しごとのサービストークン（共通 .env の PLATFORM_SERVICE_TOKEN_SECRET）。
  product_mcp_urls = {
    rag    = var.deploy_rag ? "http://127.0.0.1:8000/api/mcp" : ""
    nl2sql = var.deploy_nl2sql ? "http://127.0.0.1:8010/api/mcp" : ""
  }
  # 利用者のブラウザから RAG を開く起点（#1311）。Agent の画面で RAG の図の根拠を開く短命の URL の path をこの起点に
  # 付ける。1 台の Compute（#1316）では同じ origin の /rag なので、host を含まない path にする（Terraform は
  # instance 自身の IP を user_data に書けない。ブラウザは Agent の画面と同じ origin で解決する）。
  product_browser_urls = {
    rag = var.deploy_rag ? "/rag" : ""
  }

  # ---------------------------------------------------------------- 共通 .env（platform/.env。#211）

  # 3製品共通の設定（PLATFORM_*）。Compute のリポジトリの platform/.env に置き、3 製品が同じファイルを読む
  # （既にあれば init_script.sh は上書きしない。画面で保存した値を消さないため）。
  # 値は python-dotenv が読む。single quote の中は展開されないため、入力値は single quote で囲む。
  # python-oracledb Thin mode + Wallet(mTLS) で接続する（PLATFORM_ORACLE_CLIENT_LIB_DIR は空）。
  # データの置き場所（PLATFORM_LOCAL_STORAGE_DIR）と model-settings.json は書かず、各製品の Settings の既定値を使う
  # （データは /u01/data/production-ready-<製品>、model-settings.json は platform/.env と同じ場所で 3 製品が共有）。
  # Cookie は HTTPS が on なら Secure にする（3 製品で同じ。#1316）。
  # 時間帯（PLATFORM_TIMEZONE。#3255）は suite-init.sh が Compute の OS の時間帯にも使う。
  platform_env = <<-EOT
PLATFORM_ENVIRONMENT=${var.platform_environment}
PLATFORM_AUTH_MODE=authenticated
PLATFORM_TIMEZONE=${var.timezone}

PLATFORM_ORACLE_USER='${local.effective_oracle_user}'
PLATFORM_ORACLE_PASSWORD='${local.effective_oracle_password}'
PLATFORM_ORACLE_DSN='${local.effective_oracle_dsn}'
PLATFORM_ORACLE_DRIVER_MODE=thin
PLATFORM_ORACLE_CONNECTION_SECURITY=${local.nl2sql_oracle_connection_security}
PLATFORM_ORACLE_CLIENT_LIB_DIR=
PLATFORM_ORACLE_WALLET_DIR=${local.wallet_dir_host}
PLATFORM_ORACLE_WALLET_PASSWORD='${local.effective_oracle_wallet_password}'

PLATFORM_ADMIN_LOGIN_USER_ID=${var.app_admin_login_user_id}
PLATFORM_ADMIN_LOGIN_USER_PASSWORD=${var.app_admin_login_user_password}

PLATFORM_SERVICE_TOKEN_SECRET=${random_password.service_token_secret.result}

PLATFORM_AUTH_COOKIE_SECURE=${var.https_enabled}
PLATFORM_AUTH_SESSION_COOKIE_NAME=pr_session
PLATFORM_AUTH_CSRF_COOKIE_NAME=pr_csrf
EOT

  # ---------------------------------------------------------------- アプリ用の DB ユーザー（#1668）

  # Compute の platform/deploy/suite-init.sh が、製品の配備の前に ADMIN で接続してアプリ用のユーザーを作るための入力
  # （/u01/aipoc/props/adb_admin.env。root の 0600）。ADMIN のパスワードは platform/.env・各製品の backend/.env には
  # 書かない。再配備（suite-restart.sh / suite-update.sh）で権限を付け直すため、props に残す。
  # アプリ用のユーザーのパスワードは platform.env の PLATFORM_ORACLE_PASSWORD（suite-db-user.py がそこから読む）。
  adb_admin_env = <<-EOT
SUITE_DB_ADMIN_USER=ADMIN
SUITE_DB_ADMIN_PASSWORD='${local.effective_adb_admin_password}'
SUITE_DB_APP_USER='${local.adb_app_user}'
EOT

  # ---------------------------------------------------------------- cloud-init

  # 製品ごとの backend/.env（配備する製品だけ。/u01/aipoc/props/<製品>.backend.env）。
  backend_envs = {
    rag    = local.rag_backend_env
    nl2sql = local.nl2sql_backend_env
    agent  = local.agent_backend_env
  }

  cloud_init_user_data = base64gzip(templatefile("${path.module}/cloud_init/bootstrap.template.yaml", {
    adb_admin_env       = var.adb_app_user_enabled ? base64gzip(local.adb_admin_env) : ""
    adb_name            = local.effective_adb_name
    adb_ocid            = local.effective_adb_ocid
    application_git_ref = var.application_git_ref
    application_git_url = var.application_git_url
    assign_public_ip    = tostring(!local.compute_subnet_prohibits_public_ip)
    backend_envs        = { for product in local.selected_products : product => base64gzip(local.backend_envs[product]) }
    compartment_ocid    = var.compartment_ocid
    db_dsn              = local.effective_oracle_dsn
    https_enabled       = tostring(var.https_enabled)
    http_port           = tostring(var.http_port)
    https_port          = tostring(var.https_port)
    platform_env        = base64gzip(local.platform_env)
    products            = join(" ", local.selected_products)
    rag_services        = var.deploy_rag ? join(" ", local.rag_services) : ""
    region              = var.region
    timezone            = var.timezone
    wallet_content      = data.external.wallet_files.result.wallet_content
    wallet_dir_host     = local.wallet_dir_host
  }))
}
