#!/usr/bin/env bash
# rag/init_script.sh（OCI Resource Manager stack の Compute 初期化）の振る舞いを外部依存なしで検証する。
# RAG は Docker を使わず、uv の venv + systemd で動かす（#286）。systemctl / runuser は
# fake にし、手元の systemd は触らない。visudo があれば、生成した sudoers の構文も確かめる。
set -euo pipefail

TEST_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${TEST_SCRIPT_DIR}/../.." && pwd)"
TEST_TMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "${TEST_TMP_DIR:?}"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

prepare_case() {
  local case_dir="$1"
  mkdir -p \
    "${case_dir}/app/props" \
    "${case_dir}/app/no.1-production-ready-assistant/rag/backend" \
    "${case_dir}/app/no.1-production-ready-assistant/platform" \
    "${case_dir}/units" \
    "${case_dir}/sudoers.d" \
    "${case_dir}/bin" \
    "${case_dir}/service-home"
  export APP_ROOT="${case_dir}/app"
  export RAG_INIT_TEST_MODE=true
  export SYSTEMD_UNIT_DIR="${case_dir}/units"
  export SUDOERS_DIR="${case_dir}/sudoers.d"
  export DATA_DIR="${case_dir}/data"
  export SERVICE_HOME="${case_dir}/service-home"
  export LEGACY_COMPOSE_WRAPPER="${case_dir}/bin/rag-compose"
  export DOCKER_VOLUMES_DIR="${case_dir}/docker-volumes"
  export APP_USER SERVICE_USER APP_GROUP SERVICE_GROUP
  APP_USER="$(id -un)"
  SERVICE_USER="$(id -un)"
  APP_GROUP="$(id -gn)"
  SERVICE_GROUP="$(id -gn)"
}

# systemctl の fake。enable / disable は ${FAKE_CASE_DIR}/enabled/<unit> で持ち、呼び出しを記録する。
install_fake_systemctl() {
  mkdir -p "$1/enabled"
  FAKE_CASE_DIR="$1"
  systemctl() {
    printf '%s\n' "$*" >> "${FAKE_CASE_DIR}/systemctl.log"
    local unit
    case "$1" in
      enable)
        shift
        [ "${1:-}" = "--now" ] && shift
        for unit in "$@"; do touch "${FAKE_CASE_DIR}/enabled/${unit}"; done
        ;;
      disable)
        shift
        [ "${1:-}" = "--now" ] && shift
        for unit in "$@"; do rm -f -- "${FAKE_CASE_DIR:?}/enabled/${unit:?}"; done
        ;;
      is-enabled)
        shift
        [ "${1:-}" = "--quiet" ] && shift
        [ -e "${FAKE_CASE_DIR}/enabled/$1" ]
        ;;
    esac
  }
}

run_services_case() (
  local scenario="$1"
  local services="$2"
  local file_name="${3:-rag_services.txt}"
  local allowed="${4:-}"
  local case_dir="${TEST_TMP_DIR}/${scenario}"
  prepare_case "${case_dir}"
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"
  trap - ERR
  # stack が配備してよいサービスを絞った場合（提供するが許可しないサービスの拒否を確かめる）。
  if [ -n "${allowed}" ]; then
    # shellcheck disable=SC2034 # source した init_script.sh の load_rag_services が読む。
    read -r -a ALLOWED_RAG_SERVICES <<< "${allowed}"
  fi

  printf '%s\n' "${services}" > "${APP_ROOT}/props/${file_name}"
  if load_rag_services > "${case_dir}/load.log" 2>&1; then
    printf '%s\n' "${RAG_SERVICES[*]}" > "${case_dir}/services"
    echo ok > "${case_dir}/result"
  else
    echo rejected > "${case_dir}/result"
  fi
)

run_initialization_case() (
  local scenario="$1"
  local fail_pattern="$2"
  local case_dir="${TEST_TMP_DIR}/${scenario}"
  prepare_case "${case_dir}"
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"

  retry_command() {
    local attempts="$1"
    shift
    printf '%s | attempts=%s\n' "$*" "${attempts}" >> "${case_dir}/commands.log"
    if [ -n "${fail_pattern}" ] && printf '%s\n' "$*" | grep -q "${fail_pattern}"; then
      return 1
    fi
    return 0
  }

  initialize_database_schema
  printf '%s\n' "${DATABASE_INITIALIZATION_READY}" > "${case_dir}/ready"
)

run_venv_case() (
  local case_dir="${TEST_TMP_DIR}/venvs"
  prepare_case "${case_dir}"
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"
  RAG_SERVICES=(preprocess-office-to-pdf parser-docling parser-unstructured)

  retry_command() {
    local attempts="$1"
    shift
    printf '%s | attempts=%s\n' "$*" "${attempts}" >> "${case_dir}/commands.log"
    # docling のモデル取得は失敗させ、配備が止まらないことを確かめる。
    if printf '%s\n' "$*" | grep -q 'docling-tools models download'; then
      return 1
    fi
    return 0
  }

  install_service_venvs > "${case_dir}/venvs.log" 2>&1
)

run_runtime_env_case() (
  local case_dir="${TEST_TMP_DIR}/runtime-env"
  prepare_case "${case_dir}"
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"

  cat > "${APP_ROOT}/props/backend.env" <<'EOF'
RAG_PARSER_ADAPTER_BACKEND='unst$ructured'
RAG_AUDIT_CONTEXT_HASH_SALT=
EOF
  cat > "${APP_ROOT}/props/platform.env" <<'EOF'
PLATFORM_ENVIRONMENT=staging
PLATFORM_AUTH_MODE=authenticated
PLATFORM_ORACLE_USER='rag_app'
PLATFORM_ORACLE_PASSWORD='Db$Pass#123'
EOF
  install_runtime_env
  cp "${BACKEND_DIR}/.env" "${case_dir}/first.env"
  cp "${PLATFORM_ENV_FILE}" "${case_dir}/first-platform.env"
  # 画面から保存した値（API key 等）は再実行で消えない。
  printf 'PLATFORM_ENTERPRISE_AI_API_KEY=saved-on-screen\n' >> "${PLATFORM_ENV_FILE}"
  install_runtime_env
  cp "${BACKEND_DIR}/.env" "${case_dir}/second.env"
  cp "${PLATFORM_ENV_FILE}" "${case_dir}/second-platform.env"
)

# configure_systemd を 2 回（初回の配備・再配備）実行する。初回の前に、以前の配備が置いた
# 提供しなくなったサービス（parser-asr・parser-oci-genai-vision。#2042。parser-structured-records-jsonl・
# parser-xml-tree-json。#21）の unit を置く。再配備の前に、利用者が画面で parser-unstructured を停止した
# （disable --now）状態にし、stack から preprocess-csv-to-jsonl を外す。
run_systemd_case() (
  local case_dir="${TEST_TMP_DIR}/systemd"
  prepare_case "${case_dir}"
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"
  install_fake_systemctl "${case_dir}"
  if ! command -v visudo >/dev/null 2>&1; then
    visudo() {
      echo "SKIP: visudo が無いため sudoers の構文検査を省略した。" >&2
    }
  fi

  local removed
  for removed in parser-asr parser-oci-genai-vision parser-structured-records-jsonl parser-xml-tree-json; do
    printf '[Unit]\nDescription=removed %s\n' "${removed}" > "${SYSTEMD_UNIT_DIR}/production-ready-rag-${removed}.service"
    touch "${case_dir}/enabled/production-ready-rag-${removed}.service"
  done
  RAG_SERVICES=(preprocess-office-to-pdf preprocess-csv-to-jsonl parser-docling parser-unstructured)
  configure_systemd > "${case_dir}/first.log" 2>&1
  cp "${case_dir}/systemctl.log" "${case_dir}/first-systemctl.log"
  cp "${SUDOERS_DIR}/production-ready-rag-services" "${case_dir}/first-sudoers"

  # 画面で parser-unstructured を停止した（app.services.systemd の disable --now）。
  systemctl disable --now production-ready-rag-parser-unstructured.service
  : > "${case_dir}/systemctl.log"
  RAG_SERVICES=(preprocess-office-to-pdf parser-docling parser-unstructured)
  configure_systemd > "${case_dir}/second.log" 2>&1
  cp "${case_dir}/systemctl.log" "${case_dir}/second-systemctl.log"
)

# 既存の Compute の再配備（suite-update.sh → suite-restart.sh → suite-init.sh → init_script.sh の main。#2042）。
# 以前の stack が書いた rag_services.txt（提供しなくなった preprocess-url-to-markdown・preprocess-image-enhance・parser-oci-genai-vision・
# parser-asr・preprocess-pdf-to-page-images（#3347）・preprocess-pii-redact（#3358）・parser-structured-records-jsonl・parser-xml-tree-json（#21）がある）と、以前の配備の unit がある状態から、main と同じ順に load_rag_services → configure_systemd を実行する。
run_redeploy_case() (
  local case_dir="${TEST_TMP_DIR}/redeploy"
  local service
  prepare_case "${case_dir}"
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"
  install_fake_systemctl "${case_dir}"
  if ! command -v visudo >/dev/null 2>&1; then
    visudo() { :; }
  fi

  printf '%s\n' 'preprocess-office-to-pdf preprocess-pdf-to-page-images preprocess-pii-redact preprocess-url-to-markdown preprocess-image-enhance parser-docling parser-oci-genai-vision parser-asr parser-oci-document-understanding parser-mineru parser-structured-records-jsonl parser-xml-tree-json' \
    > "${APP_ROOT}/props/rag_services.txt"
  chmod 0640 "${APP_ROOT}/props/rag_services.txt"
  for service in preprocess-office-to-pdf preprocess-pdf-to-page-images preprocess-pii-redact preprocess-url-to-markdown preprocess-image-enhance parser-docling parser-oci-genai-vision parser-asr parser-oci-document-understanding parser-mineru parser-structured-records-jsonl parser-xml-tree-json; do
    printf '[Unit]\nDescription=previous %s\n' "${service}" > "${SYSTEMD_UNIT_DIR}/production-ready-rag-${service}.service"
    touch "${case_dir}/enabled/production-ready-rag-${service}.service"
  done
  load_rag_services > "${case_dir}/load.log" 2>&1
  configure_systemd > "${case_dir}/configure.log" 2>&1
)

# 改名したサービス（#2457 の CSV→JSONL・Excel→JSONL。#2484）の再配備。以前の stack が書いた rag_services.txt（旧名）と
# 旧名の unit（CSV は起動中、Excel は利用者が停止した）がある状態から、main と同じ順に load_rag_services → configure_systemd を
# 2 回（更新・同じ配備の繰り返し）実行する。あわせて、最新の stack（新名の一覧・旧名の unit なし）の新規配備も実行する。
run_renamed_redeploy_case() (
  local case_dir="${TEST_TMP_DIR}/renamed"
  local service
  prepare_case "${case_dir}"
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"
  install_fake_systemctl "${case_dir}"
  if ! command -v visudo >/dev/null 2>&1; then
    visudo() { :; }
  fi

  printf '%s\n' 'preprocess-csv-to-json preprocess-excel-to-json preprocess-office-to-pdf parser-docling' \
    > "${APP_ROOT}/props/rag_services.txt"
  for service in preprocess-csv-to-json preprocess-excel-to-json preprocess-office-to-pdf parser-docling; do
    printf '[Unit]\nDescription=previous %s\n' "${service}" > "${SYSTEMD_UNIT_DIR}/production-ready-rag-${service}.service"
    touch "${case_dir}/enabled/production-ready-rag-${service}.service"
  done
  systemctl disable --now production-ready-rag-preprocess-excel-to-json.service
  : > "${case_dir}/systemctl.log"
  load_rag_services > "${case_dir}/first-load.log" 2>&1
  configure_systemd > "${case_dir}/first-configure.log" 2>&1
  cp "${case_dir}/systemctl.log" "${case_dir}/first-systemctl.log"
  cp "${APP_ROOT}/props/rag_services.txt" "${case_dir}/first-rag_services.txt"
  cp "${SUDOERS_DIR}/production-ready-rag-services" "${case_dir}/first-sudoers"

  : > "${case_dir}/systemctl.log"
  load_rag_services > "${case_dir}/second-load.log" 2>&1
  configure_systemd > "${case_dir}/second-configure.log" 2>&1
  cp "${case_dir}/systemctl.log" "${case_dir}/second-systemctl.log"
)

run_renamed_fresh_case() (
  local case_dir="${TEST_TMP_DIR}/renamed-fresh"
  prepare_case "${case_dir}"
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"
  install_fake_systemctl "${case_dir}"
  if ! command -v visudo >/dev/null 2>&1; then
    visudo() { :; }
  fi

  printf '%s\n' 'preprocess-csv-to-jsonl preprocess-excel-to-jsonl parser-docling' > "${APP_ROOT}/props/rag_services.txt"
  load_rag_services > "${case_dir}/load.log" 2>&1
  configure_systemd > "${case_dir}/configure.log" 2>&1
)

run_migration_case() (
  local case_dir="${TEST_TMP_DIR}/migration"
  prepare_case "${case_dir}"
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"
  install_fake_systemctl "${case_dir}"

  printf '[Unit]\nDescription=legacy compose\n' > "${SYSTEMD_UNIT_DIR}/production-ready-rag.service"
  cat > "${LEGACY_COMPOSE_WRAPPER}" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "${case_dir}/compose.log"
EOF
  chmod +x "${LEGACY_COMPOSE_WRAPPER}"
  mkdir -p "${DOCKER_VOLUMES_DIR}/production-ready-rag_backend-local-storage/_data/uploads" \
    "${DOCKER_VOLUMES_DIR}/production-ready-rag_oci-config/_data"
  printf 'original\n' > "${DOCKER_VOLUMES_DIR}/production-ready-rag_backend-local-storage/_data/uploads/a.pdf"
  printf '[DEFAULT]\n' > "${DOCKER_VOLUMES_DIR}/production-ready-rag_oci-config/_data/config"

  migrate_from_docker_compose > "${case_dir}/migration.log" 2>&1
  # 2 回目は何もしない（既にデータがある場所は上書きしない）。
  printf 'changed\n' > "${DOCKER_VOLUMES_DIR}/production-ready-rag_backend-local-storage/_data/uploads/a.pdf"
  migrate_from_docker_compose >> "${case_dir}/migration.log" 2>&1
)

# --- 配備する前処理 / parser（CPU / OCI だけ。提供しないサービスは読み飛ばし、必ず配備する parser は足す） ---
# stack が配備してよい一覧は、提供するサービス（rag-systemd.sh の RAG_MICROSERVICES）と同じ並び。
allowed_order="$(RAG_INIT_TEST_MODE=true bash -c 'source "$1/init_script.sh"; printf "%s\n" "${ALLOWED_RAG_SERVICES[*]}"' _ "${REPO_DIR}")"
provided_order="$(bash -c 'source "$1/scripts/rag-systemd.sh"; for e in "${RAG_MICROSERVICES[@]}"; do printf "%s " "${e%%|*}"; done' _ "${REPO_DIR}")"
test "${allowed_order} " = "${provided_order}" \
  || fail "init_script.sh の ALLOWED_RAG_SERVICES が rag-systemd.sh の RAG_MICROSERVICES と一致しない: ${allowed_order}"
run_services_case services-default "preprocess-office-to-pdf parser-unstructured parser-docling"
test "$(cat "${TEST_TMP_DIR}/services-default/result")" = "ok" || fail "既定の service が拒否された"
test "$(cat "${TEST_TMP_DIR}/services-default/services")" = \
  "preprocess-office-to-pdf parser-unstructured parser-docling preprocess-csv-to-jsonl preprocess-excel-to-jsonl preprocess-word-to-jsonl" \
  || fail "service の一覧が rag_services.txt と一致しない"
run_services_case services-legacy-file "backend ingestion-worker parser-unstructured parser-docling" compose_services.txt
test "$(cat "${TEST_TMP_DIR}/services-legacy-file/result")" = "ok" \
  || fail "以前の stack の compose_services.txt を読めない"
test "$(cat "${TEST_TMP_DIR}/services-legacy-file/services")" = \
  "parser-unstructured parser-docling preprocess-csv-to-jsonl preprocess-excel-to-jsonl preprocess-word-to-jsonl" \
  || fail "以前の stack の backend / ingestion-worker を前処理 / parser として扱った"
# 以前の stack が書いた一覧に、提供しなくなったサービス（#270 の parser-marker・#2042 の parser-asr /
# parser-oci-genai-vision・preprocess-url-to-markdown・preprocess-image-enhance・#3347 の preprocess-pdf-to-page-images・#3358 の preprocess-pii-redact・
# #21 の parser-structured-records-jsonl / parser-xml-tree-json）や未知の名前があっても、再配備を止めずに読み飛ばす。
run_services_case services-removed "parser-docling parser-asr parser-oci-genai-vision preprocess-url-to-markdown preprocess-image-enhance preprocess-pdf-to-page-images preprocess-pii-redact parser-marker parser-structured-records-jsonl parser-xml-tree-json frontend"
test "$(cat "${TEST_TMP_DIR}/services-removed/result")" = "ok" \
  || fail "提供しなくなったサービスが rag_services.txt にあると再配備が失敗する"
test "$(cat "${TEST_TMP_DIR}/services-removed/services")" = "parser-docling preprocess-csv-to-jsonl preprocess-excel-to-jsonl preprocess-word-to-jsonl" \
  || fail "提供しなくなったサービスを配備の対象にした"
for removed in parser-asr parser-structured-records-jsonl parser-xml-tree-json; do
  grep -Fqx "[rag-init] Skipping RAG service that is no longer provided: ${removed}" "${TEST_TMP_DIR}/services-removed/load.log" \
    || fail "提供しなくなった ${removed} を読み飛ばしたことを log に出していない"
done
run_services_case services-glob "parser-docling *"
test "$(cat "${TEST_TMP_DIR}/services-glob/result")" = "ok" || fail "glob の名前で再配備が失敗した"
test "$(cat "${TEST_TMP_DIR}/services-glob/services")" = "parser-docling preprocess-csv-to-jsonl preprocess-excel-to-jsonl preprocess-word-to-jsonl" \
  || fail "glob が展開された、または読み飛ばされていない"
# 提供するが stack が許可しないサービスは拒否する。
run_services_case services-not-allowed "parser-docling parser-unstructured" rag_services.txt \
  "parser-docling"
test "$(cat "${TEST_TMP_DIR}/services-not-allowed/result")" = "rejected" \
  || fail "stack が許可していない service を拒否していない"
# 必ず配備する parser（parser-docling）と前処理 preprocess-word-to-jsonl が
# 以前の stack の一覧に無くても、足して配備する。
run_services_case services-missing-required "parser-unstructured preprocess-office-to-pdf"
test "$(cat "${TEST_TMP_DIR}/services-missing-required/result")" = "ok" \
  || fail "必ず配備する parser が無い一覧で再配備が失敗した"
test "$(cat "${TEST_TMP_DIR}/services-missing-required/services")" = \
  "parser-unstructured preprocess-office-to-pdf parser-docling preprocess-csv-to-jsonl preprocess-excel-to-jsonl preprocess-word-to-jsonl" \
  || fail "必ず配備する parser（parser-docling）を足していない"
grep -Fq 'Adding required RAG service missing from' "${TEST_TMP_DIR}/services-missing-required/load.log" \
  || fail "必ず配備する parser を足したことを log に出していない"

# --- DB 初期化（アプリの system schema CLI を SERVICE_USER で実行する） ---
run_initialization_case success ""
test "$(cat "${TEST_TMP_DIR}/success/ready")" = "true" || fail "schema 初期化成功時に ready にならない"
grep -Fq "run_as_service_user_in_dir ${TEST_TMP_DIR}/success/app/no.1-production-ready-assistant/rag/backend .venv/bin/python -m app.rag.system_schema_cli initialize | attempts=5" \
  "${TEST_TMP_DIR}/success/commands.log" || fail "system schema CLI が SERVICE_USER・retry 付きで実行されていない"
if grep -q 'docker\|compose' "${TEST_TMP_DIR}/success/commands.log"; then
  fail "system schema CLI を docker で実行している"
fi
run_initialization_case degraded 'system_schema_cli initialize'
test "$(cat "${TEST_TMP_DIR}/degraded/ready")" = "false" || fail "schema 初期化失敗時に ready=false にならない"

# --- サービスごとの uv の venv（Python 3.12・lock どおり） ---
run_venv_case
venv_commands="${TEST_TMP_DIR}/venvs/commands.log"
rag_dir="${TEST_TMP_DIR}/venvs/app/no.1-production-ready-assistant/rag"
grep -Fq "run_as_app_user_in_dir ${rag_dir}/backend uv sync --locked --no-dev --python 3.12 | attempts=3" "${venv_commands}" \
  || fail "backend の venv を uv sync --locked --no-dev --python 3.12 で作っていない"
grep -Fq "run_as_app_user_in_dir ${rag_dir}/services/parsers/docling uv sync --locked --no-dev --python 3.12 | attempts=3" "${venv_commands}" \
  || fail "parser-docling の venv をサービスのディレクトリで作っていない"
grep -Fq "run_as_app_user_in_dir ${rag_dir}/services/parsers/unstructured uv sync --locked --no-dev --python 3.12 | attempts=3" "${venv_commands}" \
  || fail "parser-unstructured の venv をサービスのディレクトリで作っていない"
grep -Fq "run_as_app_user_in_dir ${rag_dir}/services/preprocess/office_to_pdf uv sync --locked --no-dev --python 3.12 | attempts=3" "${venv_commands}" \
  || fail "preprocess-office-to-pdf の venv をサービスのディレクトリで作っていない"
# 取込時の PIIマスクと spaCy の日本語モデルは削除した（#3358）。venv は lock どおりで、追加の取得をしない。
grep -Eq -- "--inexact|spacy download" "${venv_commands}" && fail "削除した PIIマスクの venv の例外（--inexact・spaCy のモデル）が残っている"
grep -Fq "run_as_service_user_in_dir ${rag_dir}/services/parsers/docling .venv/bin/docling-tools models download" "${venv_commands}" \
  || fail "docling のモデルを SERVICE_USER の ~/.cache に取得していない"
grep -Fq "WARNING: parser-docling could not download its models now" "${TEST_TMP_DIR}/venvs/venvs.log" \
  || fail "docling のモデル取得の失敗で配備が止まった、または警告を出していない"

# --- backend/.env（生成する secret は1回だけ作り、再実行でも同じ値を使う） ---
run_runtime_env_case
first="${TEST_TMP_DIR}/runtime-env/first.env"
second="${TEST_TMP_DIR}/runtime-env/second.env"
grep -Eq '^RAG_AUDIT_CONTEXT_HASH_SALT=[0-9a-f]{64}$' "${first}" || fail "RAG_AUDIT_CONTEXT_HASH_SALT が生成されていない"
cmp -s "${first}" "${second}" || fail "再実行で生成済みの secret が変わった"
grep -Fqx "RAG_PARSER_ADAPTER_BACKEND='unst\$ructured'" "${first}" || fail "入力の値が変更された"
test "$(stat -c '%a' "${TEST_TMP_DIR}/runtime-env/app/no.1-production-ready-assistant/rag/backend/.env")" = "600" \
  || fail "backend/.env の permission が 0600 ではない"
test "$(stat -c '%a' "${TEST_TMP_DIR}/runtime-env/app/props/audit_context_hash_salt")" = "600" \
  || fail "生成した secret の permission が 0600 ではない"

# --- platform/.env（3製品共通の設定。PLATFORM_*） ---
grep -Fqx "PLATFORM_ORACLE_PASSWORD='Db\$Pass#123'" "${TEST_TMP_DIR}/runtime-env/first-platform.env" \
  || fail "共通 .env に入力の DB password が入っていない"
if grep -q '^PLATFORM_' "${first}"; then
  fail "backend/.env に共通の設定（PLATFORM_*）が入っている"
fi
test "$(stat -c '%a' "${TEST_TMP_DIR}/runtime-env/app/no.1-production-ready-assistant/platform/.env")" = "600" \
  || fail "platform/.env の permission が 0600 ではない"
grep -Fqx 'PLATFORM_ENTERPRISE_AI_API_KEY=saved-on-screen' "${TEST_TMP_DIR}/runtime-env/second-platform.env" \
  || fail "再実行で画面から保存した共通 .env の値が消えた"

# --- systemd の unit・状態の保持・sudoers ---
run_systemd_case
units="${TEST_TMP_DIR}/systemd/units"
rag_dir="${TEST_TMP_DIR}/systemd/app/no.1-production-ready-assistant/rag"
service_user="$(id -un)"
backend_unit="${units}/production-ready-rag-backend.service"
worker_unit="${units}/production-ready-rag-ingestion-worker.service"
docling_unit="${units}/production-ready-rag-parser-docling.service"
csv_unit="${units}/production-ready-rag-preprocess-csv-to-jsonl.service"
first_log="${TEST_TMP_DIR}/systemd/first-systemctl.log"
second_log="${TEST_TMP_DIR}/systemd/second-systemctl.log"
grep -Fqx "ExecStart=${rag_dir}/backend/.venv/bin/gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker --bind 127.0.0.1:8000 --workers 2 --timeout 60 --graceful-timeout 30 --keep-alive 5 --error-logfile - --no-control-socket" "${backend_unit}" \
  || fail "backend が venv の gunicorn で 127.0.0.1:8000 だけに bind していない"
grep -Fqx "User=${service_user}" "${backend_unit}" || fail "backend が SERVICE_USER で動いていない"
! grep -q 'Environment=PLATFORM_ENVIRONMENT=' "${backend_unit}" || fail "共通の環境設定を unit で上書きしています"
grep -Fqx 'Environment=RAG_INGESTION_QUEUE_INPROCESS_WORKER_ENABLED=false' "${backend_unit}" \
  || fail "backend の in-process worker を無効にしていない（取込ジョブを二重に実行しうる）"
grep -Fqx "ExecStart=${rag_dir}/backend/.venv/bin/python -m app.rag.ingestion_worker" "${worker_unit}" \
  || fail "ingestion-worker の unit が app.rag.ingestion_worker を起動していない"
grep -Fqx 'KillMode=mixed' "${worker_unit}" || fail "ingestion-worker の KillMode が mixed ではない（#305）"
grep -Fqx 'TimeoutStopSec=90' "${worker_unit}" || fail "ingestion-worker の TimeoutStopSec が 90 秒ではない"
grep -Fqx 'Environment=RAG_INGESTION_QUEUE_SHUTDOWN_GRACE_SECONDS=60' "${worker_unit}" \
  || fail "ingestion-worker の停止の grace が unit で 60 秒になっていない（#357）"
# 停止の grace + 子の停止待ち（SIGTERM から SIGKILL まで 10 秒）より TimeoutStopSec を長くする（#357）。
worker_grace="$(sed -n 's/^Environment=RAG_INGESTION_QUEUE_SHUTDOWN_GRACE_SECONDS=//p' "${worker_unit}")"
worker_stop_timeout="$(sed -n 's/^TimeoutStopSec=//p' "${worker_unit}")"
[ "${worker_stop_timeout}" -gt $((worker_grace + 10)) ] \
  || fail "ingestion-worker の TimeoutStopSec が停止の grace + 10 秒より短い（job を QUEUED に戻す前に SIGKILL される）"
grep -Fqx 'Restart=on-failure' "${worker_unit}" || fail "ingestion-worker の Restart が on-failure ではない"
if grep -Fq 'INPROCESS_WORKER' "${worker_unit}"; then
  fail "ingestion-worker の unit が in-process worker の設定を持っている"
fi
grep -Fqx "ExecStart=${rag_dir}/services/parsers/docling/.venv/bin/gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker --bind 127.0.0.1:18020 --workers 1 --timeout 300 --graceful-timeout 30 --error-logfile -" "${docling_unit}" \
  || fail "parser-docling が自分の venv で 127.0.0.1:18020 に bind していない"
grep -Fqx "WorkingDirectory=${rag_dir}/services/parsers/docling" "${docling_unit}" || fail "parser-docling の作業ディレクトリが違う"
grep -Fqx "EnvironmentFile=-${rag_dir}/backend/service-runtime.env" "${docling_unit}" \
  || fail "parser-docling がサービス実行用の env（HF / OCI Enterprise AI）を読まない"
grep -Fqx "Environment=HOME=${TEST_TMP_DIR}/systemd/service-home" "${docling_unit}" \
  || fail "parser-docling の HOME（モデルのキャッシュ）が SERVICE_HOME ではない"
grep -Fqx "User=${service_user}" "${docling_unit}" || fail "parser-docling が SERVICE_USER で動いていない"
if grep -Fq "platform/.env" "${docling_unit}"; then
  fail "OCI 以外の parser に共通 .env（secret）を渡している"
fi
if grep -Eiq 'docker|compose' "${backend_unit}" "${worker_unit}" "${docling_unit}"; then
  fail "systemd の unit が docker を使っている"
fi
# 初回の配備: 既定のサービスを起動し、backend / worker を enable + restart する。
grep -qx 'enable production-ready-rag-backend.service production-ready-rag-ingestion-worker.service' "${first_log}" \
  || fail "backend / ingestion-worker を enable していない"
grep -qx 'restart production-ready-rag-backend.service production-ready-rag-ingestion-worker.service' "${first_log}" \
  || fail "backend / ingestion-worker を restart していない"
for unit in preprocess-office-to-pdf preprocess-csv-to-jsonl parser-docling parser-unstructured; do
  grep -qx "enable --now production-ready-rag-${unit}.service" "${first_log}" \
    || fail "初回の配備で ${unit} を起動していない"
done
# 以前の配備が置いた、提供しなくなったサービスの unit は止めて消す（#2042・#21）。
for removed in parser-asr parser-oci-genai-vision parser-structured-records-jsonl parser-xml-tree-json; do
  grep -qx "disable --now production-ready-rag-${removed}.service" "${first_log}" \
    || fail "提供しなくなった ${removed} を止めていない"
  test ! -e "${units}/production-ready-rag-${removed}.service" || fail "提供しなくなった ${removed} の unit が残っている"
  if grep -Fq "production-ready-rag-${removed}.service" "${TEST_TMP_DIR}/systemd/first-sudoers"; then
    fail "sudoers が提供しなくなった ${removed} を許可している"
  fi
done
# 再配備: 最後に「起動」した unit は restart、最後に「停止」した unit は停止のまま。
grep -qx 'restart production-ready-rag-parser-docling.service' "${second_log}" \
  || fail "再配備で起動中の parser-docling を restart していない"
if grep -Eq '^(enable --now|restart) production-ready-rag-parser-unstructured.service$' "${second_log}"; then
  fail "利用者が停止した parser-unstructured を再配備で起動した"
fi
grep -qx 'stop production-ready-rag-parser-unstructured.service' "${second_log}" \
  || fail "利用者が停止した parser-unstructured を停止のままにしていない"
test ! -e "${TEST_TMP_DIR}/systemd/enabled/production-ready-rag-parser-unstructured.service" \
  || fail "再配備で parser-unstructured が enable に戻った"
# stack から外したサービスの unit は止めて消す。
grep -qx 'disable --now production-ready-rag-preprocess-csv-to-jsonl.service' "${second_log}" \
  || fail "stack から外した preprocess-csv-to-jsonl を止めていない"
test ! -e "${csv_unit}" || fail "stack から外した preprocess-csv-to-jsonl の unit が残っている"
test -e "${backend_unit}" || fail "backend の unit を消した"
# sudoers: 画面から操作する unit の systemctl / journalctl だけ（引数まで固定）。backend / worker は含めない。
sudoers="${TEST_TMP_DIR}/systemd/sudoers.d/production-ready-rag-services"
test "$(stat -c '%a' "${sudoers}")" = "440" || fail "sudoers の permission が 0440 ではない"
grep -Fq '/usr/bin/systemctl enable --now production-ready-rag-parser-docling.service' "${sudoers}" \
  || fail "sudoers が parser-docling の起動（enable --now）を許可していない"
grep -Fq '/usr/bin/systemctl disable --now production-ready-rag-parser-docling.service' "${sudoers}" \
  || fail "sudoers が parser-docling の停止（disable --now）を許可していない"
grep -Fq '/usr/bin/systemctl restart production-ready-rag-parser-docling.service' "${sudoers}" \
  || fail "sudoers が parser-docling の再起動を許可していない"
grep -Fq '/usr/bin/journalctl -u production-ready-rag-parser-docling.service -n 1000 --no-pager -o short-iso' "${sudoers}" \
  || fail "sudoers が parser-docling のログ（backend と同じ argv）を許可していない"
grep -Fqx "${service_user} ALL=(root) NOPASSWD: RAG_SERVICE_CONTROL" "${sudoers}" \
  || fail "sudoers が SERVICE_USER に限定されていない"
if grep -Eq 'backend\.service|ingestion-worker\.service|NOPASSWD: ALL|\*' "${sudoers}"; then
  fail "sudoers が画面の対象外の unit・任意のコマンドを許可している"
fi
grep -Fq 'production-ready-rag-preprocess-csv-to-jsonl' "${TEST_TMP_DIR}/systemd/first-sudoers" \
  || fail "初回の sudoers に配備した preprocess-csv-to-jsonl が入っていない"
if command -v visudo >/dev/null 2>&1; then
  visudo -cf "${sudoers}" >/dev/null || fail "生成した sudoers を visudo が受け付けない"
fi

# --- 既存の Compute の再配備（suite-update.sh）: 必ず配備する parser を足し、提供しなくなったサービスを消す（#2042） ---
# suite-update.sh → suite-restart.sh → suite-init.sh → rag/init_script.sh の main の順に呼ぶこと。
suite_deploy_dir="${REPO_DIR}/../platform/deploy"
grep -Fq 'exec bash "${SUITE_RESTART_SCRIPT}"' "${suite_deploy_dir}/suite-update.sh" \
  || fail "suite-update.sh が suite-restart.sh を実行しない"
grep -Fq 'SUITE_INIT_SCRIPT="${SUITE_INIT_SCRIPT:-${SUITE_DEPLOY_DIR}/suite-init.sh}"' "${suite_deploy_dir}/suite-restart.sh" \
  || fail "suite-restart.sh が suite-init.sh を実行しない"
grep -Fq 'local init_script="${SUITE_REPO_DIR}/${product}/init_script.sh"' "${suite_deploy_dir}/suite-init.sh" \
  || fail "suite-init.sh が製品の init_script.sh を実行しない"
main_order="$(awk '/^main\(\) \{/ {found = 1} found && /^  (load_rag_services|configure_systemd)$/ {printf "%s ", $1} found && /^\}/ {exit}' "${REPO_DIR}/init_script.sh")"
test "${main_order}" = "load_rag_services configure_systemd " \
  || fail "init_script.sh の main が load_rag_services → configure_systemd の順に呼ばない: ${main_order}"
run_redeploy_case
redeploy_dir="${TEST_TMP_DIR}/redeploy"
redeploy_log="${redeploy_dir}/systemctl.log"
# 削除した parser-oci-document-understanding・parser-mineru（#22）・parser-structured-records-jsonl・
# parser-xml-tree-json（#21）は以前の一覧にあっても書き戻さない。
test "$(cat "${redeploy_dir}/app/props/rag_services.txt")" = \
  "preprocess-office-to-pdf parser-docling preprocess-csv-to-jsonl preprocess-excel-to-jsonl preprocess-word-to-jsonl" \
  || fail "再配備で rag_services.txt を今の一覧に書き戻していない: $(cat "${redeploy_dir}/app/props/rag_services.txt")"
test "$(stat -c '%a' "${redeploy_dir}/app/props/rag_services.txt")" = "640" \
  || fail "rag_services.txt の書き戻しで mode が変わった"
find "${redeploy_dir}/app/props" -name 'rag_services.txt.*' | grep -q . && fail "rag_services.txt の一時ファイルが残っている"
grep -Fq 'Adding required RAG service missing from' "${redeploy_dir}/load.log" \
  || fail "必ず配備する前処理（CSV→JSONL など）を足したことを log に出していない"
grep -qx 'restart production-ready-rag-parser-docling.service' "${redeploy_log}" \
  || fail "再配備で起動中の parser-docling を restart していない"
# #2484 の前の再配備で CSV / Excel の前処理が一覧から消えた環境も、再配備で unit・sudoers を入れて起動する（#2486）。
for service in preprocess-csv-to-jsonl preprocess-excel-to-jsonl; do
  grep -qx "enable --now production-ready-rag-${service}.service" "${redeploy_log}" \
    || fail "一覧に無い ${service} を再配備で起動していない"
  grep -Fq "/usr/bin/systemctl enable --now production-ready-rag-${service}.service" \
    "${redeploy_dir}/sudoers.d/production-ready-rag-services" \
    || fail "再配備の sudoers が ${service} の操作を許可していない"
done
for removed in preprocess-pdf-to-page-images preprocess-pii-redact preprocess-url-to-markdown preprocess-image-enhance parser-oci-genai-vision parser-asr parser-oci-document-understanding parser-mineru parser-structured-records-jsonl parser-xml-tree-json; do
  grep -Fqx "[rag-init] Skipping RAG service that is no longer provided: ${removed}" "${redeploy_dir}/load.log" \
    || fail "提供しなくなった ${removed} を読み飛ばしたことを log に出していない"
  grep -qx "disable --now production-ready-rag-${removed}.service" "${redeploy_log}" \
    || fail "再配備で提供しなくなった ${removed} を止めていない"
  grep -Fq "Removing systemd unit that is no longer deployed: production-ready-rag-${removed}.service" "${redeploy_dir}/configure.log" \
    || fail "提供しなくなった ${removed} の unit を消したことを log に出していない"
  test ! -e "${redeploy_dir}/units/production-ready-rag-${removed}.service" \
    || fail "再配備で提供しなくなった ${removed} の unit が残っている"
  if grep -Fq "production-ready-rag-${removed}.service" "${redeploy_dir}/sudoers.d/production-ready-rag-services"; then
    fail "再配備の sudoers が提供しなくなった ${removed} を許可している"
  fi
done
# rag_services.txt を書けなくても配備は続ける（読み込みで毎回直す）。
(
  case_dir="${TEST_TMP_DIR}/redeploy-readonly"
  prepare_case "${case_dir}"
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"
  printf '%s\n' 'parser-docling parser-asr' > "${APP_ROOT}/props/rag_services.txt"
  mktemp() { return 1; }
  load_rag_services > "${case_dir}/load.log" 2>&1 || fail "rag_services.txt を書けないと配備が止まる"
  test "${RAG_SERVICES[*]}" = "parser-docling preprocess-csv-to-jsonl preprocess-excel-to-jsonl preprocess-word-to-jsonl" \
    || fail "書けないときの一覧が違う: ${RAG_SERVICES[*]}"
  grep -Fq 'WARNING: Could not update' "${case_dir}/load.log" || fail "書けなかったことを log に出していない"
  test "$(cat "${APP_ROOT}/props/rag_services.txt")" = "parser-docling parser-asr" || fail "書けないはずの rag_services.txt が変わった"
)
# 書き戻した後の再配備は一覧を変えない（書き戻さない）。
(
  prepare_case "${redeploy_dir}"
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"
  load_rag_services > "${redeploy_dir}/load-again.log" 2>&1
)
if grep -Fq 'Updated ' "${redeploy_dir}/load-again.log" || grep -Fq 'Adding required' "${redeploy_dir}/load-again.log"; then
  fail "書き戻した rag_services.txt を再配備でまた直した"
fi

# --- 改名したサービス（CSV→JSONL・Excel→JSONL。#2457・#2484）: 旧名を新名で配備し、旧名の unit の状態を引き継ぐ ---
run_services_case services-renamed "preprocess-csv-to-json preprocess-excel-to-json preprocess-office-to-pdf parser-docling"
test "$(cat "${TEST_TMP_DIR}/services-renamed/result")" = "ok" || fail "改名したサービスの旧名で再配備が失敗した"
test "$(cat "${TEST_TMP_DIR}/services-renamed/services")" = \
  "preprocess-csv-to-jsonl preprocess-excel-to-jsonl preprocess-office-to-pdf parser-docling preprocess-word-to-jsonl" \
  || fail "改名したサービスの旧名を新名として配備の対象にしていない: $(cat "${TEST_TMP_DIR}/services-renamed/services")"
grep -Fqx '[rag-init] Reading renamed RAG service as its new name: preprocess-csv-to-json -> preprocess-csv-to-jsonl' \
  "${TEST_TMP_DIR}/services-renamed/load.log" || fail "旧名を新名として読んだことを log に出していない"
if grep -Fq 'no longer provided: preprocess-' "${TEST_TMP_DIR}/services-renamed/load.log"; then
  fail "改名したサービスを提供しなくなったサービスとして読み飛ばした"
fi
# 旧名と新名が両方あっても、新名を 1 つだけ配備する。
run_services_case services-renamed-duplicate "preprocess-csv-to-jsonl preprocess-csv-to-json parser-docling"
test "$(cat "${TEST_TMP_DIR}/services-renamed-duplicate/services")" = \
  "preprocess-csv-to-jsonl parser-docling preprocess-excel-to-jsonl preprocess-word-to-jsonl" \
  || fail "旧名と新名の両方があると新名を重ねて配備する"
run_renamed_redeploy_case
renamed_dir="${TEST_TMP_DIR}/renamed"
renamed_first_log="${renamed_dir}/first-systemctl.log"
renamed_second_log="${renamed_dir}/second-systemctl.log"
test "$(cat "${renamed_dir}/first-rag_services.txt")" = \
  "preprocess-csv-to-jsonl preprocess-excel-to-jsonl preprocess-office-to-pdf parser-docling preprocess-word-to-jsonl" \
  || fail "旧名の rag_services.txt を新名で書き戻していない: $(cat "${renamed_dir}/first-rag_services.txt")"
for service in preprocess-csv-to-jsonl preprocess-excel-to-jsonl; do
  test -f "${renamed_dir}/units/production-ready-rag-${service}.service" || fail "旧名からの更新で ${service} の unit を書いていない"
  grep -Fq "/usr/bin/systemctl enable --now production-ready-rag-${service}.service" "${renamed_dir}/first-sudoers" \
    || fail "旧名からの更新の sudoers が ${service} の操作を許可していない"
done
for service in preprocess-csv-to-json preprocess-excel-to-json; do
  grep -qx "disable --now production-ready-rag-${service}.service" "${renamed_first_log}" || fail "旧名の ${service} の unit を止めていない"
  test ! -e "${renamed_dir}/units/production-ready-rag-${service}.service" || fail "旧名の ${service} の unit が残っている"
  if grep -Fq "production-ready-rag-${service}.service" "${renamed_dir}/first-sudoers"; then
    fail "sudoers が旧名の ${service} を許可している"
  fi
done
# 起動していた旧名の CSV は新名で起動し、利用者が停止した旧名の Excel は新名でも停止のまま。
grep -qx 'enable --now production-ready-rag-preprocess-csv-to-jsonl.service' "${renamed_first_log}" \
  || fail "起動していた旧名の CSV の前処理を新名で起動していない"
if grep -Eq '^(enable|start|restart)( --now)? production-ready-rag-preprocess-excel-to-jsonl.service$' "${renamed_first_log}"; then
  fail "利用者が停止した旧名の Excel の前処理を新名で起動した"
fi
test ! -e "${renamed_dir}/enabled/production-ready-rag-preprocess-excel-to-jsonl.service" \
  || fail "利用者が停止した旧名の Excel の前処理が新名で enable になった"
grep -Fq 'Leaving production-ready-rag-preprocess-excel-to-jsonl.service stopped (renamed; last operation on the old unit: stop).' \
  "${renamed_dir}/first-configure.log" || fail "旧名の停止を引き継いだことを log に出していない"
# 同じ配備の繰り返し: 一覧を変えず、新名の CSV は restart、Excel は停止のまま。
if grep -Fq 'Updated ' "${renamed_dir}/second-load.log" || grep -Fq 'Reading renamed' "${renamed_dir}/second-load.log"; then
  fail "書き戻した rag_services.txt を繰り返しの再配備でまた直した"
fi
grep -qx 'restart production-ready-rag-preprocess-csv-to-jsonl.service' "${renamed_second_log}" \
  || fail "繰り返しの再配備で新名の CSV の前処理を restart していない"
grep -qx 'stop production-ready-rag-preprocess-excel-to-jsonl.service' "${renamed_second_log}" \
  || fail "繰り返しの再配備で停止の Excel の前処理を停止のままにしていない"
if grep -Eq '^disable --now production-ready-rag-preprocess-(csv|excel)-to-jsonl.service$' "${renamed_second_log}"; then
  fail "繰り返しの再配備で新名の前処理の unit を消した"
fi
# 最新の stack（新名の一覧・旧名の unit なし）の新規配備: 既定どおり起動する。
run_renamed_fresh_case
for service in preprocess-csv-to-jsonl preprocess-excel-to-jsonl; do
  grep -qx "enable --now production-ready-rag-${service}.service" "${TEST_TMP_DIR}/renamed-fresh/systemctl.log" \
    || fail "新規配備で ${service} を起動していない"
done

# --- 以前の Docker Compose の配備からの移行 ---
run_migration_case
migration_dir="${TEST_TMP_DIR}/migration"
grep -qx 'disable --now production-ready-rag.service' "${migration_dir}/systemctl.log" \
  || fail "以前の compose の unit を止めていない"
test ! -e "${migration_dir}/units/production-ready-rag.service" || fail "以前の compose の unit が残っている"
grep -qx 'down --remove-orphans' "${migration_dir}/compose.log" || fail "以前のコンテナを消していない"
test -x "${migration_dir}/bin/rag-compose.legacy-docker" || fail "以前の rag-compose を退避していない"
test "$(cat "${migration_dir}/data/uploads/a.pdf")" = "original" \
  || fail "アップロード原本を docker volume から移していない、または 2 回目で上書きした"
test -f "${migration_dir}/service-home/.oci/config" || fail "OCI の設定を docker volume から移していない"

# --- reverse proxy: RAG は単独では配備せず（#1316）、suite も reverse proxy（Nginx）を配備しない（#19） ---
for unexpected in configure_nginx nginx_client_max_body_size sites-available proxy_pass APPLICATION_PORT; do
  if grep -Fq "${unexpected}" "${REPO_DIR}/init_script.sh"; then
    fail "単独の配備の Nginx の設定（${unexpected}）が残っている"
  fi
done
grep -Fq 'FRONTEND_BASE_PATH="${FRONTEND_BASE_PATH:-/rag/}"' "${REPO_DIR}/init_script.sh" \
  || fail "frontend を /rag/ を base に build しない"
# backend の回答生成の上限（RAG_ANSWER_TIMEOUT_SECONDS の設定の上限）が画面（630 秒）より短いこと。
answer_timeout_max="$(sed -n 's/^ENTERPRISE_AI_TIMEOUT_MAX_SECONDS = \([0-9]*\)\.0$/\1/p' "${REPO_DIR}/backend/app/config.py")"
test "${answer_timeout_max}" = "600" || fail "LLM 1 回の timeout の上限（回答生成の上限）が 600 秒ではない"
awk '/rag_answer_timeout_seconds: float = Field\(/ {found = 1} found {print} found && /^    \)$/ {exit}' \
  "${REPO_DIR}/backend/app/config.py" | grep -Fq 'le=ENTERPRISE_AI_TIMEOUT_MAX_SECONDS,' \
  || fail "回答生成の上限（RAG_ANSWER_TIMEOUT_SECONDS）が LLM 1 回の timeout の上限で抑えられていない"
# reverse proxy の設定の雛形を製品に置かない（#306・#19）。
test ! -e "${REPO_DIR}/frontend/nginx.conf.template" \
  || fail "使われない frontend/nginx.conf.template が固定の upload の上限を持ったまま残っている"

# --- 静的な不変条件 ---
init_script="${REPO_DIR}/init_script.sh"
grep -Fq 'WALLET_DIR="${APP_ROOT}/wallet"' "${init_script}" || fail "Wallet の配置先が変わった"
grep -Fq 'find "${WALLET_DIR}" -type f -exec chmod 0600 {} \;' "${init_script}" || fail "Wallet の file が 0600 ではない"
if grep -Eiq '^\s*[^#]*\b(CREATE|ALTER|DROP)\s+(TABLE|INDEX|USER)\b' "${init_script}"; then
  fail "init_script.sh に DDL を埋め込んでいる"
fi
if grep -Eiq -- '--profile[ =]gpu|nvidia|cuda' "${init_script}"; then
  fail "init_script.sh が GPU を扱っている"
fi
# Docker を入れない・使わない（以前の compose の配備を止める rag-compose の呼び出しだけは残す）。
if grep -Eq '^\s*(docker|docker-compose)\b|docker-ce|docker-compose-plugin|download\.docker\.com|docker run|docker compose' "${init_script}"; then
  fail "init_script.sh が Docker を入れる、または使っている"
fi
grep -Fq 'uv sync --locked --no-dev --python ${RAG_PYTHON_VERSION}' "${init_script}" \
  || fail "backend の venv を Python 3.12 の lock どおりに作っていない"

# --- 開発環境（scripts/rag-services.sh）も本番と同じ unit と sudoers を作る ---
dev_dir="${TEST_TMP_DIR}/dev-render"
bash "${REPO_DIR}/scripts/rag-services.sh" render "${dev_dir}" > "${TEST_TMP_DIR}/dev-render.log" 2>&1 \
  || fail "scripts/rag-services.sh render が失敗した"
test -f "${dev_dir}/production-ready-rag-parser-docling.service" || fail "開発環境の parser-docling の unit を作っていない"
for removed in parser-structured-records-jsonl parser-xml-tree-json; do
  test ! -e "${dev_dir}/production-ready-rag-${removed}.service" \
    || fail "開発環境の既定（--cpu）に削除した ${removed} を含めた（#21）"
done
test ! -e "${dev_dir}/production-ready-rag-parser-unstructured.service" \
  || fail "開発環境の既定（--cpu）に Unstructured を含めた（Docling が既定。--unstructured で足す）"
grep -Fqx "ExecStart=${REPO_DIR}/services/parsers/docling/.venv/bin/gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker --bind 127.0.0.1:18020 --workers 1 --timeout 300 --graceful-timeout 30 --error-logfile -" \
  "${dev_dir}/production-ready-rag-parser-docling.service" || fail "開発環境の unit が本番と同じ起動方法ではない"
grep -Fqx "User=$(id -un)" "${dev_dir}/production-ready-rag-parser-docling.service" \
  || fail "開発環境の unit が現在のユーザーで動かない"
grep -Fqx "$(id -un) ALL=(root) NOPASSWD: RAG_SERVICE_CONTROL" "${dev_dir}/production-ready-rag-services" \
  || fail "開発環境の sudoers が現在のユーザーに限定されていない"
if command -v visudo >/dev/null 2>&1; then
  visudo -cf "${dev_dir}/production-ready-rag-services" >/dev/null || fail "開発環境の sudoers を visudo が受け付けない"
fi
if bash "${REPO_DIR}/scripts/rag-services.sh" render "${TEST_TMP_DIR}/dev-unknown" 'parser-docling;id' >/dev/null 2>&1; then
  fail "scripts/rag-services.sh が未知のサービス名を受け付けた"
fi
test ! -e "${REPO_DIR}/scripts/build-services.sh" || fail "Docker のイメージを build するスクリプトが残っている"

# --- torch の CPU 版（#1650）: 配備・CI に GPU は無いので、RAG の lock に CUDA の wheel を入れない ---
# torch は PyTorch の CPU 版の index から入れ（各 pyproject.toml の [[tool.uv.index]] と [tool.uv.sources]）、
# triton・nvidia-* / cuda-* を lock に持たない。PyPI の Linux の torch は CUDA 版で、数 GB の wheel を依存に持つ。
torch_locks=0
while IFS= read -r lock; do
  if grep -Eq '^name = "(triton|nvidia-[^"]*|cuda-[^"]*)"$' "${lock}"; then
    fail "${lock#"${REPO_DIR}/"} に CUDA の wheel（triton・nvidia-* / cuda-*）がある（torch を CPU 版の index から入れる）"
  fi
  if grep -Fqx 'name = "torch"' "${lock}"; then
    torch_locks=$((torch_locks + 1))
    if awk '$0 == "name = \"torch\"" { getline; getline; print }' "${lock}" | grep -Fvqx 'source = { registry = "https://download.pytorch.org/whl/cpu" }'; then
      fail "${lock#"${REPO_DIR}/"} の torch が PyTorch の CPU 版の index から入らない"
    fi
  fi
done < <(find "${REPO_DIR}/backend" "${REPO_DIR}/packages" "${REPO_DIR}/services" -name uv.lock -not -path '*/.venv/*' | sort)
test "${torch_locks}" -ge 2 || fail "torch を持つ lock が見つからない（parser-docling・parser-unstructured）"

# --- OS の package（Oracle Linux 9 の dnf。#1611）: 前処理 / parser の分だけ足す ---
os_packages="$(
  RAG_INIT_TEST_MODE=true bash -c 'source "$1/init_script.sh"; RAG_SERVICES=(parser-docling); system_packages; echo ---; RAG_SERVICES=(parser-docling preprocess-office-to-pdf parser-unstructured preprocess-word-to-jsonl); system_packages' _ "${REPO_DIR}"
)"
base_packages="${os_packages%%---*}"
all_packages="${os_packages#*---}"
for package in gcc make git sudo unzip; do
  printf '%s\n' "${base_packages}" | grep -qx "${package}" || fail "基本の package に ${package} が無い"
done
for package in libreoffice-core google-noto-sans-cjk-ttc-fonts poppler-utils tesseract tesseract-langpack-jpn; do
  printf '%s\n' "${base_packages}" | grep -qx "${package}" && fail "parser-docling だけなのに ${package} を入れる"
  printf '%s\n' "${all_packages}" | grep -qx "${package}" || fail "前処理 / parser の package に ${package} が無い"
done
test "$(printf '%s\n' "${all_packages}" | grep -cx 'libreoffice-core')" -eq 1 || fail "同じ package を重ねて入れる"
for package in nginx netfilter-persistent build-essential fonts-noto-cjk tesseract-ocr; do
  printf '%s\n' "${all_packages}" | grep -qx "${package}" && fail "${package} を入れる（Oracle Linux に無い・配備しない）"
done
grep -Fq 'dnf_cmd install --setopt=install_weak_deps=False "${packages[@]}"' "${REPO_DIR}/init_script.sh" \
  || fail "推奨の package を入れずに dnf で入れない"

echo "rag init_script deployment behavior verified."
