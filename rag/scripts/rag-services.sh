#!/usr/bin/env bash
# 開発環境で RAG の前処理 / parser マイクロサービスを、本番と同じ経路（サービスごとの uv の venv +
# systemd の unit + sudoers）で動かす（#286）。unit の定義は本番の init_script.sh と共通
# （scripts/rag-systemd.sh）。backend は scripts/start-backend.sh（uv run）で手元に起動し、
# 「サービス管理」画面から unit を起動 / 停止する。
#
# 使い方:
#   scripts/rag-services.sh sync [対象]       # 各サービスの venv を作る（uv sync。sudo 不要）
#   scripts/rag-services.sh install [対象]    # venv を作り、systemd の unit と sudoers を登録して起動する（sudo を使う）
#   scripts/rag-services.sh render <dir> [対象]  # unit と sudoers を <dir> に書くだけ（確認用。sudo 不要）
#   scripts/rag-services.sh status [対象]     # unit の状態
#   scripts/rag-services.sh uninstall         # このスクリプトが登録した unit と sudoers を消す（sudo を使う）
#   scripts/rag-services.sh run <service>     # systemd を使わず前面で起動する（systemd の無い環境向け。Ctrl+C で停止）
#
# 対象（省略時は --cpu）:
#   --cpu   前処理 4 つと既定の CPU parser（parser-docling。PDF と画像と、前処理の構造化レコードの JSONL）
#   --unstructured  parser-unstructured（Docling が扱えないテキスト・Office・メール などを取り込む場合）
#   --all   すべて
#   <service_id>  個別に指定（例: parser-docling）
#
# torch を使うサービス（parser-docling・parser-unstructured）の lock は、PyTorch の CPU 版の index の torch を
# 持ち、CUDA の wheel（triton・nvidia-* / cuda-*）を入れない（各 pyproject.toml の [[tool.uv.index]]。#1650）。
#
# 環境変数:
#   RAG_SERVICES_MODELS=skip  sync で、サービスが実行時に使うモデル（docling-tools models download）を取得しない
#                           （モデルの要らない pytest だけを動かす CI 用。#1512）。未設定なら取得する。
#
# unit は現在のユーザー（sudo で実行したときは SUDO_USER）で動き、backend と同じユーザーの
# ~/.cache にモデルを置く。起動 / 停止の状態は systemd の enable / disable で保ち、install を
# 再実行しても最後に操作した状態に戻す（初めて登録する unit は起動する）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAG_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SUITE_DIR="$(cd "${RAG_DIR}/.." && pwd)"
# shellcheck source-path=SCRIPTDIR source=rag-systemd.sh
source "${SCRIPT_DIR}/rag-systemd.sh"

RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_GROUP="$(id -gn "${RUN_USER}")"
RUN_HOME="$(getent passwd "${RUN_USER}" | cut -d: -f6)"
RUN_HOME="${RUN_HOME:-${HOME}}"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
SUDOERS_DIR="${SUDOERS_DIR:-/etc/sudoers.d}"
# backend の既定値（RAG_SERVICE_RUNTIME_ENV_FILE / PLATFORM_ENV_FILE / backend/.env）と同じ場所。
RUNTIME_ENV_FILE="${RAG_DIR}/backend/service-runtime.env"
PLATFORM_ENV_FILE="${SUITE_DIR}/platform/.env"
BACKEND_ENV_FILE="${RAG_DIR}/backend/.env"
MODELS_VARIANT="${RAG_SERVICES_MODELS:-}"

CPU_SERVICES=(
  preprocess-office-to-pdf
  preprocess-csv-to-jsonl
  preprocess-excel-to-jsonl
  preprocess-word-to-jsonl
  parser-docling
)
UNSTRUCTURED_SERVICES=(parser-unstructured)
SELECTED=()

log() {
  printf '[rag-services] %s\n' "$*"
}

usage() {
  sed -n '2,/^set -euo pipefail$/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'
}

add_selected() {
  local service
  for service in "$@"; do
    if ! rag_service_definition "${service}" >/dev/null; then
      log "未知のサービスです: ${service}"
      return 1
    fi
    if [[ " ${SELECTED[*]} " != *" ${service} "* ]]; then
      SELECTED+=("${service}")
    fi
  done
}

parse_targets() {
  SELECTED=()
  if [ "$#" -eq 0 ]; then
    add_selected "${CPU_SERVICES[@]}"
    return
  fi
  local arg
  for arg in "$@"; do
    case "${arg}" in
      --cpu) add_selected "${CPU_SERVICES[@]}" ;;
      --unstructured) add_selected "${UNSTRUCTURED_SERVICES[@]}" ;;
      --all) add_selected "${CPU_SERVICES[@]}" "${UNSTRUCTURED_SERVICES[@]}" ;;
      *) add_selected "${arg}" ;;
    esac
  done
}

require_uv() {
  if ! command -v uv >/dev/null 2>&1; then
    log "uv が見つかりません。https://docs.astral.sh/uv/ を参照してインストールしてください。"
    exit 1
  fi
}

# サービスが使う OS のコマンドが無ければ知らせる（Oracle Linux 9 は dnf で入れる。rag/docs/deployment.md）。
warn_missing_os_tools() {
  local service
  for service in "${SELECTED[@]}"; do
    case "${service}" in
      preprocess-office-to-pdf | preprocess-word-to-jsonl)
        command -v soffice >/dev/null 2>&1 || log "注意: ${service} は LibreOffice（soffice）を使います。"
        ;;
      parser-unstructured)
        command -v pdftoppm >/dev/null 2>&1 || log "注意: ${service} は poppler-utils（pdftoppm）を使います。"
        command -v tesseract >/dev/null 2>&1 || log "注意: ${service} は tesseract（tesseract-langpack-jpn）を使います。"
        ;;
    esac
  done
}

sync_services() {
  local service dir model_command
  local sync_args=()
  require_uv
  case "${MODELS_VARIANT}" in
    "" | skip) ;;
    *)
      log "RAG_SERVICES_MODELS は skip だけを指定できます（未設定ならモデルを取得する）: ${MODELS_VARIANT}"
      exit 2
      ;;
  esac
  warn_missing_os_tools
  for service in "${SELECTED[@]}"; do
    dir="${RAG_DIR}/$(rag_service_dir "${service}")"
    mapfile -t sync_args < <(rag_uv_sync_args "${service}")
    log "${service}: uv ${sync_args[*]}"
    (cd "${dir}" && uv "${sync_args[@]}")
    model_command="$(rag_service_model_command "${service}")"
    if [ -n "${model_command}" ] && [ "${MODELS_VARIANT}" = "skip" ]; then
      log "${service}: RAG_SERVICES_MODELS=skip のため、モデルを取得しません。"
    elif [ -n "${model_command}" ]; then
      (cd "${dir}" && bash -c "${model_command}") \
        || log "注意: ${service} のモデルを取得できませんでした（初回の解析で取得します）。"
    fi
  done
}

render_units() {
  local out_dir="$1"
  local service
  local units=()
  mkdir -p "${out_dir}"
  for service in "${SELECTED[@]}"; do
    rag_write_microservice_unit "${out_dir}/$(rag_unit_name "${service}")" "${service}" "${RAG_DIR}" \
      "${RUN_USER}" "${RUN_GROUP}" "${RUN_HOME}" "${RUNTIME_ENV_FILE}" "${PLATFORM_ENV_FILE}" "${BACKEND_ENV_FILE}"
    units+=("$(rag_unit_name "${service}")")
  done
  rag_write_sudoers "${out_dir}/${RAG_SUDOERS_FILE_NAME}" "${RUN_USER}" "${units[@]}"
}

require_systemd() {
  if [ ! -d /run/systemd/system ]; then
    log "systemd が PID 1 ではありません。scripts/rag-services.sh run <service> で前面に起動してください。"
    exit 1
  fi
}

installed_units() {
  local entry unit
  for entry in "${RAG_MICROSERVICES[@]}"; do
    unit="$(rag_unit_name "${entry%%|*}")"
    if [ -f "${SYSTEMD_UNIT_DIR}/${unit}" ]; then
      printf '%s\n' "${unit}"
    fi
  done
}

install_services() {
  local tmp_dir service unit
  local new_units=()
  local all_units=()
  require_systemd
  sync_services
  tmp_dir="$(mktemp -d)"
  render_units "${tmp_dir}"
  for service in "${SELECTED[@]}"; do
    unit="$(rag_unit_name "${service}")"
    if [ ! -f "${SYSTEMD_UNIT_DIR}/${unit}" ]; then
      new_units+=("${unit}")
    fi
    sudo install -m 0644 "${tmp_dir}/${unit}" "${SYSTEMD_UNIT_DIR}/${unit}"
  done
  # sudoers は登録済みのすべての unit（前回の install を含む）を対象に作り直す。
  mapfile -t all_units < <(installed_units)
  rag_write_sudoers "${tmp_dir}/${RAG_SUDOERS_FILE_NAME}" "${RUN_USER}" "${all_units[@]}"
  sudo visudo -cf "${tmp_dir}/${RAG_SUDOERS_FILE_NAME}" >/dev/null
  sudo install -m 0440 -o root -g root "${tmp_dir}/${RAG_SUDOERS_FILE_NAME}" "${SUDOERS_DIR}/${RAG_SUDOERS_FILE_NAME}"
  rm -rf -- "${tmp_dir:?}"
  sudo systemctl daemon-reload
  for service in "${SELECTED[@]}"; do
    unit="$(rag_unit_name "${service}")"
    if [[ " ${new_units[*]} " == *" ${unit} "* ]]; then
      log "${unit}: 初めて登録したので起動します。"
      sudo systemctl enable --now "${unit}"
    elif systemctl is-enabled --quiet "${unit}"; then
      log "${unit}: 最後の操作が「起動」なので再起動します。"
      sudo systemctl restart "${unit}"
    else
      log "${unit}: 最後の操作が「停止」なので停止のままにします。"
    fi
  done
  log "backend は PLATFORM_ENVIRONMENT=local で起動すると、サービス管理画面から操作できます。"
}

uninstall_services() {
  local unit
  local units=()
  require_systemd
  mapfile -t units < <(installed_units)
  for unit in "${units[@]}"; do
    log "${unit} を止めて消します。"
    sudo systemctl disable --now "${unit}" || true
    sudo rm -f -- "${SYSTEMD_UNIT_DIR:?}/${unit:?}"
  done
  sudo rm -f -- "${SUDOERS_DIR:?}/${RAG_SUDOERS_FILE_NAME:?}"
  sudo systemctl daemon-reload
}

status_services() {
  local service
  for service in "${SELECTED[@]}"; do
    systemctl --no-pager status "$(rag_unit_name "${service}")" --lines 0 || true
  done
}

run_service() {
  local service="$1"
  local dir port timeout
  require_uv
  dir="${RAG_DIR}/$(rag_service_dir "${service}")" || {
    log "未知のサービスです: ${service}"
    exit 1
  }
  port="$(rag_service_port "${service}")"
  timeout="$(rag_service_timeout "${service}")"
  SELECTED=("${service}")
  sync_services
  cd "${dir}"
  log "${service} を 127.0.0.1:${port} で起動します（Ctrl+C で停止）。"
  exec .venv/bin/gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker \
    --bind "127.0.0.1:${port}" --workers 1 --timeout "${timeout}" --graceful-timeout 30 \
    --access-logfile - --error-logfile -
}

main() {
  local command="${1:-}"
  [ "$#" -gt 0 ] && shift
  case "${command}" in
    sync)
      parse_targets "$@"
      sync_services
      ;;
    install)
      parse_targets "$@"
      install_services
      ;;
    render)
      if [ "$#" -lt 1 ]; then
        usage
        exit 2
      fi
      local out_dir="$1"
      shift
      parse_targets "$@"
      render_units "${out_dir}"
      ;;
    status)
      parse_targets "$@"
      status_services
      ;;
    uninstall)
      uninstall_services
      ;;
    run)
      if [ "$#" -ne 1 ]; then
        usage
        exit 2
      fi
      run_service "$1"
      ;;
    -h | --help | help)
      usage
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
