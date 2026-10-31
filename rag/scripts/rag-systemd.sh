#!/usr/bin/env bash
# shellcheck disable=SC2034  # 定数は source する側（init_script.sh / rag-services.sh）が使う。
# RAG のマイクロサービス（前処理・parser）を systemd の unit で動かすための共通の定義と関数（#286）。
# 本番の rag/init_script.sh と、開発の rag/scripts/rag-services.sh が source する（単体では実行しない）。
#
# 各サービスは、サービスごとの uv の venv（`uv sync --locked --no-dev --python 3.12`）で動く
# ネイティブのプロセスで、127.0.0.1:<port> だけで listen する。ポートは backend のサービスカタログ
# （rag/backend/app/services/catalog.py の port）と URL 設定の既定値と同じにする（テストで照合する）。
# unit 名は production-ready-rag-<service_id>.service（backend の catalog.SYSTEMD_UNIT_PREFIX）。

# service_id|rag/ からの実装ディレクトリ|port|gunicorn の timeout（秒）
# 並びはサービス管理画面の並び（backend の catalog の順）と同じにする。ここに無い service_id は
# 提供しないサービスとして扱う（init_script.sh は rag_services.txt にあっても読み飛ばす）。
RAG_MICROSERVICES=(
  "preprocess-office-to-pdf|services/preprocess/office_to_pdf|18010|300"
  "preprocess-csv-to-jsonl|services/preprocess/csv_to_jsonl|18012|300"
  "preprocess-excel-to-jsonl|services/preprocess/excel_to_jsonl|18013|300"
  "preprocess-word-to-jsonl|services/preprocess/word_to_jsonl|18017|300"
  "parser-docling|services/parsers/docling|18020|300"
  "parser-unstructured|services/parsers/unstructured|18022|300"
  "parser-structured-records-jsonl|services/parsers/structured_records_jsonl|18023|300"
  "parser-xml-tree-json|services/parsers/xml_tree_json|18021|300"
  "parser-oci-document-understanding|services/parsers/oci_document_understanding|18028|300"
)
# 初めて配備・登録しても起動しない（既定は停止の）サービス。unit と sudoers は作るので、サービス管理画面の
# 「起動」（enable --now）で起動でき、以後は最後に操作した状態（enable / disable）を保つ。
# XML ツリー JSON は使用エンジンに選んだときだけ使う（RAG_PARSER_XML_TREE_JSON_ENABLED の既定 false と同じ。#2453）。
RAG_DEFAULT_STOPPED_SERVICES=(parser-xml-tree-json)
# 準備中（ロードマップ）のサービス（#3374）。実装は repo に残すが、配備・登録しない（init_script.sh は
# rag_services.txt にあっても読み飛ばし、以前の配備の unit は「配備しなくなった unit」として止めて消す。
# rag-services.sh も選べない）。backend の catalog の availability="roadmap" と同じ（テストで照合する）。
RAG_ROADMAP_SERVICES=(parser-oci-document-understanding)
RAG_UNIT_PREFIX="production-ready-rag-"
# 画面から操作する unit に、systemctl / journalctl だけを許可する sudoers（backend の app.services.systemd と同じ argv）。
RAG_SUDOERS_FILE_NAME="production-ready-rag-services"
RAG_SYSTEMCTL="/usr/bin/systemctl"
RAG_JOURNALCTL="/usr/bin/journalctl"
# backend の app.services.systemd.JOURNAL_FETCH_LINES と同じ（sudoers は引数まで一致させる）。
RAG_JOURNAL_FETCH_LINES=1000
RAG_PYTHON_VERSION="3.12"

rag_unit_name() {
  printf '%s%s.service\n' "${RAG_UNIT_PREFIX}" "$1"
}

# service_id の定義行（id|dir|port|timeout）を返す。未知の id は 1。
rag_service_definition() {
  local id="$1"
  local entry
  for entry in "${RAG_MICROSERVICES[@]}"; do
    if [ "${entry%%|*}" = "${id}" ]; then
      printf '%s\n' "${entry}"
      return 0
    fi
  done
  return 1
}

rag_service_field() {
  local id="$1"
  local index="$2"
  local entry
  entry="$(rag_service_definition "${id}")" || return 1
  printf '%s\n' "${entry}" | cut -d '|' -f "${index}"
}

# 初めて配備・登録したときに起動しない（既定は停止の）サービスなら 0。
rag_service_default_stopped() {
  local id="$1"
  local stopped
  for stopped in "${RAG_DEFAULT_STOPPED_SERVICES[@]}"; do
    if [ "${stopped}" = "${id}" ]; then
      return 0
    fi
  done
  return 1
}

# 準備中（ロードマップ）で配備しないサービスなら 0。
rag_service_roadmap() {
  local id="$1"
  local roadmap
  for roadmap in "${RAG_ROADMAP_SERVICES[@]}"; do
    if [ "${roadmap}" = "${id}" ]; then
      return 0
    fi
  done
  return 1
}

rag_service_dir() { rag_service_field "$1" 2; }
rag_service_port() { rag_service_field "$1" 3; }
rag_service_timeout() { rag_service_field "$1" 4; }

# サービスごとの venv を作る（uv の lock どおり。開発用の依存は入れない）。
rag_uv_sync_args() {
  printf '%s\n' sync --locked --no-dev --python "${RAG_PYTHON_VERSION}"
}

# サービスが実行時に使うモデルを、サービスの実行ユーザーの ~/.cache に取得するコマンド
# （サービスの実行ユーザーが、サービスのディレクトリで実行する）。実行時の DL を避けるため。
# 失敗しても unit は起動できる（docling は初回の解析でモデルを取得する）。
rag_service_model_command() {
  case "$1" in
    parser-docling)
      printf '%s\n' '.venv/bin/docling-tools models download'
      ;;
  esac
}

# マイクロサービスの unit を書く。
#   $1 unit のパス / $2 service_id / $3 rag/ の絶対パス / $4 実行ユーザー / $5 group / $6 HOME
#   $7 サービス実行用の env ファイル（backend が書く。HF_* と実効 OCI Enterprise AI 設定）
#   $8 共通 .env（platform/.env）/ $9 backend/.env（OCI parser だけが読む）
rag_write_microservice_unit() {
  local unit_path="$1"
  local id="$2"
  local rag_dir="$3"
  local user="$4"
  local group="$5"
  local home="$6"
  local runtime_env="$7"
  local platform_env="$8"
  local backend_env="$9"
  local dir port timeout workdir env_files extra_env

  dir="$(rag_service_dir "${id}")" || return 1
  port="$(rag_service_port "${id}")"
  timeout="$(rag_service_timeout "${id}")"
  workdir="${rag_dir}/${dir}"
  env_files=""
  extra_env=""
  case "${id}" in
    parser-oci-*)
      # OCI parser は OCI 認証・Object Storage の設定を backend と同じ .env から読む。
      env_files="EnvironmentFile=-${platform_env}
EnvironmentFile=-${backend_env}
"
      ;;
    parser-docling)
      extra_env="Environment=RAG_ENGINE_OUTPUT_DIR=/tmp/production-ready-rag-engine-runs
"
      ;;
  esac

  cat > "${unit_path}" <<EOF
# rag/scripts/rag-systemd.sh が生成する（手で編集しない）。#286
[Unit]
Description=Production Ready RAG ${id}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${user}
Group=${group}
WorkingDirectory=${workdir}
Environment=HOME=${home}
Environment=PYTHONUNBUFFERED=1
Environment=PORT=${port}
${extra_env}${env_files}EnvironmentFile=-${runtime_env}
ExecStart=${workdir}/.venv/bin/gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker --bind 127.0.0.1:${port} --workers 1 --timeout ${timeout} --graceful-timeout 30 --error-logfile -
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

# 画面から操作する unit の systemctl / journalctl だけを許可する sudoers を書く。
#   $1 出力先 / $2 backend の実行ユーザー / $3.. unit 名
rag_write_sudoers() {
  local path="$1"
  local user="$2"
  shift 2
  local unit
  local commands=()
  for unit in "$@"; do
    commands+=(
      "${RAG_SYSTEMCTL} enable --now ${unit}"
      "${RAG_SYSTEMCTL} disable --now ${unit}"
      "${RAG_SYSTEMCTL} restart ${unit}"
      "${RAG_JOURNALCTL} -u ${unit} -n ${RAG_JOURNAL_FETCH_LINES} --no-pager -o short-iso"
    )
  done
  {
    printf '# rag/scripts/rag-systemd.sh が生成する（手で編集しない）。#286\n'
    printf '# RAG の backend（%s）に、サービス管理画面から操作する unit の systemctl と journalctl だけを許可する。\n' "${user}"
    printf '# 引数まで固定する（backend の app.services.systemd が同じ argv を sudo -n で実行する）。\n'
    printf 'Cmnd_Alias RAG_SERVICE_CONTROL = '
    local index=0
    local command
    for command in "${commands[@]}"; do
      if [ "${index}" -gt 0 ]; then
        printf ', \\\n    '
      fi
      printf '%s' "${command}"
      index=$((index + 1))
    done
    printf '\n%s ALL=(root) NOPASSWD: RAG_SERVICE_CONTROL\n' "${user}"
  } > "${path}"
}
