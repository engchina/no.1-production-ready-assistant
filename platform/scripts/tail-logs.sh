#!/usr/bin/env bash
# OCI Compute へデプロイした後のアプリログをリアルタイムに表示する読み取り専用ビューア。
# systemd unit(journald)と /var/log 配下のログを 1 コマンドで統合表示する。
# サービスの再起動など状態を変更する操作は一切行わない。
set -Eeuo pipefail

# 各製品の wrapper が製品を指定し、共有の read-only viewer を使う。
TAIL_LOGS_PRODUCT="${TAIL_LOGS_PRODUCT:-nl2sql}"
case "${TAIL_LOGS_PRODUCT}" in
  rag) WORKER_SERVICES=("production-ready-rag-ingestion-worker.service") ;;
  agent) WORKER_SERVICES=() ;;
  nl2sql) WORKER_SERVICES=(
    "production-ready-nl2sql-schema-refresh-worker.service"
    "production-ready-nl2sql-synthetic-worker.service"
    "production-ready-nl2sql-quality-evaluation-worker.service"
    "production-ready-nl2sql-ontology-worker.service"
  ) ;;
  *) echo "不明な製品です" >&2; exit 2 ;;
esac
# backend の port（127.0.0.1。1 台の Compute に 3 製品を置くため製品ごとに別。#1316）。
case "${TAIL_LOGS_PRODUCT}" in
  rag) BACKEND_PORT=8000 ;;
  nl2sql) BACKEND_PORT=8010 ;;
  agent) BACKEND_PORT=8020 ;;
esac
BACKEND_SERVICE="production-ready-${TAIL_LOGS_PRODUCT}-backend.service"
ALL_SERVICES=("${BACKEND_SERVICE}" "${WORKER_SERVICES[@]}")

NGINX_ACCESS_LOG="${NGINX_ACCESS_LOG:-/var/log/nginx/production-ready-${TAIL_LOGS_PRODUCT}-access.log}"
NGINX_ERROR_LOG="${NGINX_ERROR_LOG:-/var/log/nginx/production-ready-${TAIL_LOGS_PRODUCT}-error.log}"
INIT_LOG_PATH="${INIT_LOG_PATH:-/var/log/${TAIL_LOGS_PRODUCT}-init.log}"
UPDATE_LOG_PATH="${UPDATE_LOG_PATH:-/var/log/${TAIL_LOGS_PRODUCT}-update.log}"

# Nginx 経由の health は /<製品>/health。scheme と port は cloud-init の props（https_enabled.txt / https_port.txt /
# http_port.txt。#1316）から決める（HTTPS が on なら https_port、off なら http_port。on のとき http_port は使わない。#1638）。
SUITE_PROPS_DIR="${SUITE_PROPS_DIR:-/u01/aipoc/props}"
suite_prop() {
  tr -d '[:space:]' 2>/dev/null < "${SUITE_PROPS_DIR}/$1" || true
}
if [ "$(suite_prop https_enabled.txt)" = "false" ]; then
  PUBLIC_HEALTH_PORT="$(suite_prop http_port.txt)"
  PUBLIC_HEALTH_ORIGIN="http://127.0.0.1:${PUBLIC_HEALTH_PORT:-80}"
else
  PUBLIC_HEALTH_PORT="$(suite_prop https_port.txt)"
  PUBLIC_HEALTH_ORIGIN="https://127.0.0.1:${PUBLIC_HEALTH_PORT:-443}"
fi
BACKEND_HEALTH_URL="${BACKEND_HEALTH_URL:-http://127.0.0.1:${BACKEND_PORT}/api/health}"
PUBLIC_HEALTH_URL="${PUBLIC_HEALTH_URL:-${PUBLIC_HEALTH_ORIGIN}/${TAIL_LOGS_PRODUCT}/health}"
HEALTHCHECK_TIMEOUT_SECONDS="${HEALTHCHECK_TIMEOUT_SECONDS:-5}"

ACTION="tail"
LINES="${TAIL_LOGS_LINES:-200}"
SINCE=""
GREP_PATTERN=""
MIN_LEVEL=""
ID_FILTER=""
JSON_EXPORT=false
FOLLOW=true
RAW=false
COLOR_ENABLED=false
SHOW_UNIT=false
UNIT_SELECTION_MADE=false
FILE_SELECTION_MADE=false
NO_COLOR_REQUESTED=false
PYTHON_BIN=""
SELECTED_UNITS=()
SELECTED_FILES=()   # "label=path" 形式
JOURNAL_ARGS=()
JOURNAL_PRIVILEGED=()
PIDS=()
_cleaning=0

log() {
  printf '[${TAIL_LOGS_PRODUCT}-logs] %s\n' "$*"
}

warn() {
  printf '[${TAIL_LOGS_PRODUCT}-logs] WARNING: %s\n' "$*" >&2
}

die() {
  printf '[${TAIL_LOGS_PRODUCT}-logs] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: ./scripts/tail-logs.sh [options]

OCI Compute 上のアプリログをリアルタイム表示します(読み取り専用)。
引数なしで backend と 製品の workerの systemd unit を統合追尾します。

対象の選択:
  --backend         backend unit のみを追尾します。
  --workers         製品の worker のみを追尾します。
  --unit <name>     unit を個別指定します(複数回指定可)。
                    backend / schema-refresh / quality-evaluation / ontology の
                    短縮名と完全な unit 名の双方を受理します。
  --nginx           Nginx の access / error ログを対象にします。
  --init            /var/log/${TAIL_LOGS_PRODUCT}-init.log を対象にします。
  --update          /var/log/${TAIL_LOGS_PRODUCT}-update.log を対象にします。
  --all             製品の unit + Nginx + init + update をすべて対象にします。

  --nginx / --init / --update を単独指定した場合は、そのファイルだけを表示します。
  unit も同時に見るときは --all または --backend などと併用してください。

表示の調整:
  -n, --lines <N>   追尾開始前に遡って表示する行数(既定: 200)。
  --since <expr>    journalctl --since へ委譲します(例: "-15 min", today)。
  --grep <pattern>  拡張正規表現で行を絞り込みます。
  --level <LEVEL>   DEBUG / INFO / WARNING / ERROR / CRITICAL の下限で絞り込みます。
                    JSON として解析できた行にのみ適用し、traceback など解析できない
                    行は取りこぼしを防ぐため常に表示します。
  --no-follow       追尾せず、一括出力して終了します(報告用スナップショット)。
  --json            JST の安全な JSON Lines を export します。
  --request-id / --run-id / --job-id <ID>  相関 ID で絞り込みます。
  --raw             整形せず journalctl の生出力(-o short-iso)を表示します。
  --no-color        色付けを無効にします(環境変数 NO_COLOR でも同じ)。

その他:
  --status          追尾せず、unit の稼働状態とヘルスチェック結果を表示して終了します。
  -h, --help        このヘルプを表示します。

journald は権限不足のとき自動的に sudo へフォールバックします。
JSON ログの整形には python3 を使用します(未導入の場合は生出力へ縮退)。

Environment overrides:
  NGINX_ACCESS_LOG / NGINX_ERROR_LOG   Nginx ログのパス
  INIT_LOG_PATH / UPDATE_LOG_PATH      init / update ログのパス
  BACKEND_HEALTH_URL / PUBLIC_HEALTH_URL --status のヘルスチェック URL
EOF
}

usage_error() {
  printf '[${TAIL_LOGS_PRODUCT}-logs] ERROR: %s\n' "$*" >&2
  usage >&2
  exit 2
}

# 短縮名と完全な unit 名の双方を受理して完全な unit 名へ解決する。
resolve_unit() {
  case "$1" in
    backend) printf '%s\n' "${BACKEND_SERVICE}" ;;
    schema | schema-refresh) printf '%s\n' "production-ready-nl2sql-schema-refresh-worker.service" ;;
    synthetic) printf '%s\n' "production-ready-nl2sql-synthetic-worker.service" ;;
    quality | quality-evaluation) printf '%s\n' "production-ready-nl2sql-quality-evaluation-worker.service" ;;
    ontology) printf '%s\n' "production-ready-nl2sql-ontology-worker.service" ;;
    ingestion | ingestion-worker) printf '%s\n' "production-ready-rag-ingestion-worker.service" ;;
    production-ready-"${TAIL_LOGS_PRODUCT}"-*.service) printf '%s\n' "$1" ;;
    production-ready-"${TAIL_LOGS_PRODUCT}"-*) printf '%s\n' "$1.service" ;;
    *) return 1 ;;
  esac
}

add_unit() {
  local unit="$1" existing
  for existing in ${SELECTED_UNITS[@]+"${SELECTED_UNITS[@]}"}; do
    [ "${existing}" = "${unit}" ] && return 0
  done
  SELECTED_UNITS+=("${unit}")
}

add_file() {
  local label="$1" path="$2" existing
  for existing in ${SELECTED_FILES[@]+"${SELECTED_FILES[@]}"}; do
    [ "${existing#*=}" = "${path}" ] && return 0
  done
  SELECTED_FILES+=("${label}=${path}")
}

select_backend() {
  UNIT_SELECTION_MADE=true
  add_unit "${BACKEND_SERVICE}"
}

select_workers() {
  local unit
  UNIT_SELECTION_MADE=true
  for unit in "${WORKER_SERVICES[@]}"; do
    add_unit "${unit}"
  done
}

select_all_units() {
  local unit
  UNIT_SELECTION_MADE=true
  for unit in "${ALL_SERVICES[@]}"; do
    add_unit "${unit}"
  done
}

select_nginx_files() {
  FILE_SELECTION_MADE=true
  add_file "nginx-access" "${NGINX_ACCESS_LOG}"
  add_file "nginx-error" "${NGINX_ERROR_LOG}"
}

require_value() {
  [ "$#" -ge 2 ] || usage_error "$1 には値が必要です。"
}

parse_args() {
  local unit
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --backend)
        select_backend
        ;;
      --workers)
        select_workers
        ;;
      --unit)
        require_value "$@"
        unit="$(resolve_unit "$2")" || usage_error "不明な unit です: $2"
        UNIT_SELECTION_MADE=true
        add_unit "${unit}"
        shift
        ;;
      --nginx)
        select_nginx_files
        ;;
      --init)
        FILE_SELECTION_MADE=true
        add_file "init" "${INIT_LOG_PATH}"
        ;;
      --update)
        FILE_SELECTION_MADE=true
        add_file "update" "${UPDATE_LOG_PATH}"
        ;;
      --all)
        select_all_units
        select_nginx_files
        add_file "init" "${INIT_LOG_PATH}"
        add_file "update" "${UPDATE_LOG_PATH}"
        ;;
      -n | --lines)
        require_value "$@"
        case "$2" in
          '' | *[!0-9]*) usage_error "--lines には 0 以上の整数を指定してください: $2" ;;
        esac
        LINES="$2"
        shift
        ;;
      --since)
        require_value "$@"
        SINCE="$2"
        shift
        ;;
      --grep)
        require_value "$@"
        GREP_PATTERN="$2"
        shift
        ;;
      --level)
        require_value "$@"
        case "$(printf '%s' "$2" | tr '[:lower:]' '[:upper:]')" in
          DEBUG | INFO | WARNING | ERROR | CRITICAL) MIN_LEVEL="$(printf '%s' "$2" | tr '[:lower:]' '[:upper:]')" ;;
          *) usage_error "--level には DEBUG / INFO / WARNING / ERROR / CRITICAL を指定してください: $2" ;;
        esac
        shift
        ;;
      --request-id | --run-id | --job-id)
        require_value "$@"
        [[ "$2" =~ ^[A-Za-z0-9._:-]{1,128}$ ]] || usage_error "相関 ID の形式が不正です。"
        ID_FILTER="${1#--}:$2"
        shift
        ;;
      --json) JSON_EXPORT=true ;;
      --no-follow)
        FOLLOW=false
        ;;
      --raw)
        RAW=true
        ;;
      --no-color)
        NO_COLOR_REQUESTED=true
        ;;
      --status)
        ACTION="status"
        ;;
      -h | --help)
        ACTION="help"
        return 0
        ;;
      *)
        usage_error "不明な引数です: $1"
        ;;
    esac
    shift
  done
}

# 対象未指定なら 4 unit を既定にする。ファイルだけを指定した場合は unit を追尾しない。
resolve_sources() {
  if [ "${RAW}" = true ] && { [ "${JSON_EXPORT}" = true ] || [ -n "${ID_FILTER}" ]; }; then
    usage_error "--raw は --json / 相関 ID filter と併用できません。"
  fi
  if [ "${UNIT_SELECTION_MADE}" = false ] && [ "${FILE_SELECTION_MADE}" = false ]; then
    select_all_units
  fi
  if [ "${#SELECTED_UNITS[@]}" -gt 1 ]; then
    SHOW_UNIT=true
  fi
}

resolve_color() {
  COLOR_ENABLED=false
  [ "${NO_COLOR_REQUESTED}" = true ] && return 0
  [ -n "${NO_COLOR:-}" ] && return 0
  [ -t 1 ] || return 0
  COLOR_ENABLED=true
}

resolve_python() {
  PYTHON_BIN=""
  [ "${RAW}" = true ] && return 0
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
    return 0
  fi
  warn "python3 が見つからないため、JSON ログを整形せずそのまま表示します。"
}

# journald を読めるか確認し、必要なら sudo へフォールバックする。
resolve_journal_access() {
  JOURNAL_PRIVILEGED=()
  command -v journalctl >/dev/null 2>&1 || die "journalctl が見つかりません。systemd のあるホストで実行してください。"
  if journalctl -n 0 --no-pager >/dev/null 2>&1; then
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo journalctl -n 0 --no-pager >/dev/null 2>&1; then
    JOURNAL_PRIVILEGED=(sudo)
    return 0
  fi
  die "journald を読み取れません。sudo ./scripts/tail-logs.sh で実行してください。"
}

build_journal_args() {
  local unit
  JOURNAL_ARGS=(--no-pager -n "${LINES}")
  for unit in ${SELECTED_UNITS[@]+"${SELECTED_UNITS[@]}"}; do
    JOURNAL_ARGS+=(-u "${unit}")
  done
  [ -n "${SINCE}" ] && JOURNAL_ARGS+=(--since "${SINCE}")
  [ -n "${GREP_PATTERN}" ] && JOURNAL_ARGS+=(-g "${GREP_PATTERN}")
  if [ "${RAW}" = true ] || [ -z "${PYTHON_BIN}" ]; then
    JOURNAL_ARGS+=(-o short-iso)
  else
    JOURNAL_ARGS+=(-o json)
  fi
  [ "${FOLLOW}" = true ] && JOURNAL_ARGS+=(-f)
  return 0
}

# journald の JSON 1 行を人間が読める 1 行へ整形するストリームフィルタ。
FORMATTER_PY="$(cat "$(dirname "${BASH_SOURCE[0]}")/log_viewer.py")"

format_stream() {
  if [ -z "${PYTHON_BIN}" ]; then
    cat
    return
  fi
  TAIL_LOGS_MIN_LEVEL="${MIN_LEVEL}" \
    TAIL_LOGS_SHOW_UNIT="${SHOW_UNIT}" \
    TAIL_LOGS_COLOR="${COLOR_ENABLED}" \
    TAIL_LOGS_ID_FILTER="${ID_FILTER}" \
    TAIL_LOGS_JSON="${JSON_EXPORT}" \
    TAIL_LOGS_PRODUCT="${TAIL_LOGS_PRODUCT}" \
    "${PYTHON_BIN}" -c "${FORMATTER_PY}"
}

start_journal_stream() {
  [ "${#SELECTED_UNITS[@]}" -gt 0 ] || return 0
  build_journal_args
  (
    "${JOURNAL_PRIVILEGED[@]}" journalctl "${JOURNAL_ARGS[@]}" | format_stream
  ) &
  PIDS+=("$!")
}

start_file_stream() {
  local label="$1" path="$2"
  local -a reader=()
  if [ ! -e "${path}" ]; then
    warn "ログファイルが存在しないため対象から外します: ${path}"
    return 0
  fi
  if [ ! -r "${path}" ]; then
    command -v sudo >/dev/null 2>&1 || {
      warn "ログファイルを読み取れないため対象から外します: ${path}"
      return 0
    }
    reader=(sudo)
  fi
  if [ "${FOLLOW}" = true ]; then
    reader+=(tail -F -n "${LINES}" "${path}")
  else
    reader+=(tail -n "${LINES}" "${path}")
  fi
  (
    if [ -n "${GREP_PATTERN}" ]; then
      "${reader[@]}" 2>/dev/null | grep --line-buffered -E -- "${GREP_PATTERN}" |
        format_stream
    else
      "${reader[@]}" 2>/dev/null | format_stream
    fi
  ) &
  PIDS+=("$!")
}

cleanup() {
  # 再入防止: 最初の Ctrl+C で全トラップを解除し、二度目以降の INT は無視する。
  if [ "${_cleaning}" -eq 1 ]; then
    return
  fi
  _cleaning=1
  trap - INT TERM EXIT
  # ジョブ制御の "Terminated" 通知を出さずに停止する。
  set +m

  local pid waited alive
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do
    kill -- "-${pid}" 2>/dev/null || kill "${pid}" 2>/dev/null || true
  done
  waited=0
  while [ "${waited}" -lt 15 ]; do
    alive=0
    for pid in ${PIDS[@]+"${PIDS[@]}"}; do
      if kill -0 "${pid}" 2>/dev/null; then
        alive=1
      fi
    done
    if [ "${alive}" -eq 0 ]; then
      break
    fi
    sleep 0.2
    waited=$((waited + 1))
  done
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do
    if kill -0 "${pid}" 2>/dev/null; then
      kill -9 -- "-${pid}" 2>/dev/null || kill -9 "${pid}" 2>/dev/null || true
    fi
  done
}

print_health() {
  local url="$1" code
  local insecure=()
  # 同じ host の自己確認だけ、証明書の名前（公開 IP・private IP）と 127.0.0.1 の違いを無視する。
  case "${url}" in
    https://127.0.0.1/* | https://127.0.0.1:*) insecure=(--insecure) ;;
  esac
  code="$(curl -s ${insecure[@]+"${insecure[@]}"} -o /dev/null -m "${HEALTHCHECK_TIMEOUT_SECONDS}" -w '%{http_code}' "${url}" 2>/dev/null || true)"
  if [ -z "${code}" ] || [ "${code}" = "000" ]; then
    code="unreachable"
  fi
  printf '  %-56s %s\n' "${url}" "${code}"
}

run_status() {
  local service state
  log "systemd unit:"
  for service in "${ALL_SERVICES[@]}"; do
    state="$(systemctl is-active "${service}" 2>/dev/null || true)"
    [ -n "${state}" ] || state="unknown"
    printf '  %-56s %s\n' "${service}" "${state}"
  done
  log "ヘルスチェック:"
  print_health "${BACKEND_HEALTH_URL}"
  print_health "${PUBLIC_HEALTH_URL}"
  log "ログを追尾するには ./scripts/tail-logs.sh を引数なしで実行してください。"
}

run_tail() {
  local entry
  resolve_journal_access
  trap cleanup INT TERM
  trap cleanup EXIT
  # 各バックグラウンドジョブを独立したプロセスグループにし、Ctrl+C でまとめて停止できるようにする。
  set -m

  if [ "${#SELECTED_UNITS[@]}" -gt 0 ]; then
    log "追尾する unit: ${SELECTED_UNITS[*]}"
  fi
  for entry in ${SELECTED_FILES[@]+"${SELECTED_FILES[@]}"}; do
    start_file_stream "${entry%%=*}" "${entry#*=}"
  done
  start_journal_stream

  if [ "${#PIDS[@]}" -eq 0 ]; then
    die "追尾できるログがありません。"
  fi
  if [ "${FOLLOW}" = true ]; then
    log "Ctrl+C で終了します。"
  fi
  wait ${PIDS[@]+"${PIDS[@]}"} 2>/dev/null || true
}

main() {
  parse_args "$@"
  if [ "${ACTION}" = "help" ]; then
    usage
    return 0
  fi
  if [ "${ACTION}" = "status" ]; then
    run_status
    return 0
  fi
  resolve_sources
  resolve_color
  resolve_python
  run_tail
}

if [ "${TAIL_LOGS_TEST_MODE:-false}" != "true" ]; then
  main "$@"
fi
