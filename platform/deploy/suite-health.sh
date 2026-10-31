#!/usr/bin/env bash
# 配備した service の起動の確認（#1642）。source して使う（単独では実行しない）。
#
#   suite_wait_for_service_health <unit> <url>
#     各製品の init_script.sh の wait_for_backend が使う。一定の間隔で health を確かめ、service が failed になるか、
#     待ち始めから再起動を繰り返したら上限を待たずに失敗を返す（呼び出し元が status と journal を出す）。
#     以前は失敗のたびに間隔を延ばして 30 回（約 39 分）待ち、崩れ続ける service でも最後まで journal を出さなかった。
#   suite_check_deployment <nginx_base_url> <product>...
#     suite-init.sh の最後と suite-restart.sh（--status を含む）が使う配備全体の確認。有効な production-ready-* の
#     service（証明書の更新の oneshot を除く）がすべて active か、各製品の backend の health、Nginx が active か、
#     Nginx を通した各製品の health を確かめ、製品ごとに 1 行の要約を出す。NG なら NG の service の journal の
#     末尾を出して失敗を返す。backend の port は suite-nginx.sh の suite_backend_port（呼び出し元が source する）。
#     各製品の system schema が ready かも、製品の schema の CLI の check（DDL を実行しない）で 1 回だけ確かめる
#     （#1648）。init_script.sh は表の初期化に失敗しても backend を起動する（画面から直せる）ため、health だけでは
#     OK に見える。NG なら CLI の要約（状態・不足・前回の失敗の ORA コード）と、権限の不足の手がかりを出す。
#     テストは suite_run_schema_check を差し替える（DB に繋がらない）。
#     SUITE_CHECK_SCHEMA=skip のときだけ schema の確認を飛ばし、製品ごとに skipped の行を出す（#1656）。DB の無い
#     配備の通し（CI の Deploy Nightly。.github/scripts/deploy-run-through.sh）用で、運用者が使う設定ではない。
#     既定（未設定）は確かめる。
#
# log は呼び出し元の関数（既定 log。SUITE_HEALTH_LOG で関数名を変えられる）で出す。
# 製品の init_script.sh は、配備した repository の platform/deploy/ からこのファイルを source する（製品は単独では
# 配備しない。#1316）。

# 待ち方（環境変数で変えられる）。
SUITE_BACKEND_HEALTH_TIMEOUT_SECONDS="${SUITE_BACKEND_HEALTH_TIMEOUT_SECONDS:-300}"
SUITE_BACKEND_HEALTH_INTERVAL_SECONDS="${SUITE_BACKEND_HEALTH_INTERVAL_SECONDS:-3}"
SUITE_BACKEND_HEALTH_REPORT_SECONDS="${SUITE_BACKEND_HEALTH_REPORT_SECONDS:-15}"
# 待ち始めからの再起動がこの回数に達したら、起動に失敗して再起動を繰り返しているとみなす。
SUITE_BACKEND_HEALTH_MAX_RESTARTS="${SUITE_BACKEND_HEALTH_MAX_RESTARTS:-3}"
# 配備全体の確認: すべて OK になるまで待つ上限と、service が再起動せずに続くことを確かめる秒数。
SUITE_CHECK_TIMEOUT_SECONDS="${SUITE_CHECK_TIMEOUT_SECONDS:-180}"
SUITE_CHECK_SETTLE_SECONDS="${SUITE_CHECK_SETTLE_SECONDS:-10}"
SUITE_CHECK_INTERVAL_SECONDS="${SUITE_CHECK_INTERVAL_SECONDS:-3}"
SUITE_CHECK_JOURNAL_LINES="${SUITE_CHECK_JOURNAL_LINES:-80}"
# system schema の確認（#1648）: 1 製品の CLI の上限の秒数と、製品の init の log の場所。
SUITE_SCHEMA_CHECK_TIMEOUT_SECONDS="${SUITE_SCHEMA_CHECK_TIMEOUT_SECONDS:-120}"
# schema の確認を飛ばす（skip だけを認める。CI の DB の無い配備の通し用。#1656）。既定は確かめる。
SUITE_CHECK_SCHEMA="${SUITE_CHECK_SCHEMA:-check}"
SUITE_PRODUCT_LOG_DIR="${SUITE_PRODUCT_LOG_DIR:-/var/log}"
SUITE_UNIT_PATTERN="production-ready-*.service"
SUITE_TLS_RENEW_SERVICE="production-ready-suite-tls-renew.service"

_suite_health_log() {
  "${SUITE_HEALTH_LOG:-log}" "$@"
}

# unit の今の状態（active / activating / failed / inactive など）。
suite_unit_state() {
  local state
  state="$(systemctl is-active "$1" 2>/dev/null || true)"
  printf '%s\n' "${state:-unknown}"
}

# unit の自動の再起動の回数（systemd の NRestarts。読めなければ 0）。
suite_unit_restarts() {
  local count
  count="$(systemctl show -p NRestarts --value "$1" 2>/dev/null | tr -dc '0-9' || true)"
  printf '%s\n' "${count:-0}"
}

suite_health_ok() {
  curl -fsS -k --max-time 5 -o /dev/null "$1" 2>/dev/null
}

suite_wait_for_service_health() {
  local unit="$1"
  local url="$2"
  local timeout="${SUITE_BACKEND_HEALTH_TIMEOUT_SECONDS}"
  local interval="${SUITE_BACKEND_HEALTH_INTERVAL_SECONDS}"
  local report="${SUITE_BACKEND_HEALTH_REPORT_SECONDS}"
  local max_restarts="${SUITE_BACKEND_HEALTH_MAX_RESTARTS}"
  local start="${SECONDS}" next_report="${report}"
  local initial_restarts restarts elapsed state
  initial_restarts="$(suite_unit_restarts "${unit}")"
  _suite_health_log "${unit}: health を待ちます（${url}。上限 ${timeout} 秒、間隔 ${interval} 秒）。"
  while true; do
    if suite_health_ok "${url}"; then
      _suite_health_log "${unit}: health OK（$((SECONDS - start)) 秒）。"
      return 0
    fi
    elapsed=$((SECONDS - start))
    state="$(suite_unit_state "${unit}")"
    restarts=$(($(suite_unit_restarts "${unit}") - initial_restarts))
    # systemctl restart などで回数が 0 に戻ったときは、今の回数を待ち始めからの回数とみなす。
    if [ "${restarts}" -lt 0 ]; then
      restarts="$(suite_unit_restarts "${unit}")"
    fi
    if [ "${state}" = "failed" ] || [ "${restarts}" -ge "${max_restarts}" ]; then
      _suite_health_log "${unit}: 起動に失敗し、再起動を繰り返しています（${elapsed} 秒、状態 ${state}、待ち始めからの再起動 ${restarts} 回）。"
      return 1
    fi
    if [ "${elapsed}" -ge "${timeout}" ]; then
      _suite_health_log "${unit}: health が ${timeout} 秒以内に応答しません（状態 ${state}、再起動 ${restarts} 回、${url}）。"
      return 1
    fi
    if [ "${elapsed}" -ge "${next_report}" ]; then
      _suite_health_log "${unit}: 待っています（${elapsed} 秒、状態 ${state}、再起動 ${restarts} 回）。"
      next_report=$((next_report + report))
    fi
    sleep "${interval}"
  done
}

# 有効（enabled）な suite の service。証明書の更新の oneshot は除く。
suite_enabled_units() {
  systemctl list-unit-files --type=service --state=enabled --no-legend --plain "${SUITE_UNIT_PATTERN}" 2>/dev/null \
    | awk '{print $1}' | grep -vxF "${SUITE_TLS_RENEW_SERVICE}" || true
}

# Compute の中から Nginx を通す URL の起点（cloud-init の props の https_enabled.txt・http_port.txt・https_port.txt。
# 無ければ HTTPS on・80 / 443。suite-init.sh の https_enabled / http_port / https_port と同じ既定値）。
suite_local_nginx_base_url() {
  local props_dir="$1"
  local https port
  https="$(tr -d '[:space:]' 2>/dev/null < "${props_dir}/https_enabled.txt" || true)"
  if [ "${https:-true}" != "false" ]; then
    port="$(tr -d '[:space:]' 2>/dev/null < "${props_dir}/https_port.txt" || true)"
    printf 'https://127.0.0.1:%s\n' "${port:-443}"
  else
    port="$(tr -d '[:space:]' 2>/dev/null < "${props_dir}/http_port.txt" || true)"
    printf 'http://127.0.0.1:%s\n' "${port:-80}"
  fi
}

# 1 回の確認。結果は _SUITE_CHECK_* に置く。service は active で、再起動の回数が SUITE_CHECK_SETTLE_SECONDS の間
# 変わっていないものを OK とする（自動の再起動を繰り返す service は、その瞬間だけ active に見えることがある）。
_suite_check_poll() {
  local base="$1"
  shift
  local product unit state restarts port total active backend nginx_health detail
  local units=()
  mapfile -t units < <(suite_enabled_units)
  _SUITE_CHECK_OK=true
  _SUITE_CHECK_LINES=()
  _SUITE_CHECK_BAD_UNITS=()
  for product in "$@"; do
    total=0
    active=0
    detail=""
    for unit in "${units[@]}"; do
      case "${unit}" in
        "production-ready-${product}-"*) ;;
        *) continue ;;
      esac
      total=$((total + 1))
      state="$(suite_unit_state "${unit}")"
      restarts="$(suite_unit_restarts "${unit}")"
      if [ "${_SUITE_CHECK_RESTARTS[${unit}]:-}" != "${restarts}" ]; then
        _SUITE_CHECK_RESTARTS[${unit}]="${restarts}"
        _SUITE_CHECK_CHANGED_AT[${unit}]="${SECONDS}"
      fi
      if [ "${state}" = "active" ] \
        && [ $((SECONDS - ${_SUITE_CHECK_CHANGED_AT[${unit}]})) -ge "${_SUITE_CHECK_SETTLE}" ]; then
        active=$((active + 1))
      else
        _SUITE_CHECK_OK=false
        _SUITE_CHECK_BAD_UNITS+=("${unit}")
        detail="${detail}${detail:+, }${unit} ${state}（再起動 ${restarts} 回）"
      fi
    done
    port="$(suite_backend_port "${product}")"
    backend=OK
    if ! suite_health_ok "http://127.0.0.1:${port}/api/health"; then
      backend=NG
      _SUITE_CHECK_OK=false
    fi
    nginx_health=OK
    if ! suite_health_ok "${base}/${product}/api/health"; then
      nginx_health=NG
      _SUITE_CHECK_OK=false
    fi
    if [ "${total}" -eq 0 ]; then
      _SUITE_CHECK_OK=false
      detail="有効な production-ready-${product}-* の service がありません"
    fi
    _SUITE_CHECK_LINES+=("check: ${product} backend ${backend} / services ${active}/${total} active${detail:+（NG: ${detail}）} / nginx ${nginx_health}")
  done
  state="$(suite_unit_state nginx.service)"
  if [ "${state}" = "active" ]; then
    _SUITE_CHECK_LINES+=("check: nginx.service active（${base}）")
  else
    _SUITE_CHECK_OK=false
    _SUITE_CHECK_BAD_UNITS+=(nginx.service)
    _SUITE_CHECK_LINES+=("check: nginx.service NG（${state}）")
  fi
}

# 各製品の system schema の確認の CLI の引数（backend の .venv の python に渡す。DDL を実行しない。#1648）。
suite_schema_check_args() {
  case "$1" in
    rag) printf '%s\n' "-m app.rag.system_schema_cli check" ;;
    nl2sql) printf '%s\n' "-m app.cli.nl2sql_system_schema --check" ;;
    agent) printf '%s\n' "-m app.cli.agent_system_schema --check" ;;
    *) return 1 ;;
  esac
}

# 製品の schema の確認を、製品のプロセスと同じユーザー（APP_USER）で実行する。標準出力の最後の行が要約、
# 終了コード 0 が ready（124 は時間切れ）。CLI の log（標準エラー）は出さない。テストはこの関数を差し替える。
suite_run_schema_check() {
  local product="$1"
  local user="${APP_USER:-opc}"
  local home="${APP_HOME:-/home/${user}}"
  local backend_dir="${SUITE_REPO_DIR:-/u01/aipoc/no.1-production-ready-suite}/${product}/backend"
  local args
  if ! args="$(suite_schema_check_args "${product}")"; then
    printf '確認の方法がありません（%s）\n' "${product}"
    return 1
  fi
  if [ ! -x "${backend_dir}/.venv/bin/python" ]; then
    printf 'backend の venv がありません（%s/.venv）\n' "${backend_dir}"
    return 1
  fi
  runuser -u "${user}" -- env HOME="${home}" PATH="/usr/local/bin:/usr/bin:/bin" \
    bash -c "cd '${backend_dir}' && exec timeout ${SUITE_SCHEMA_CHECK_TIMEOUT_SECONDS} .venv/bin/python ${args}" \
    2>/dev/null
}

# 1 製品の system schema の確認。結果を _SUITE_CHECK_LINES に足し、NG なら _SUITE_CHECK_OK=false にする。
# NG のときは、製品の初期化の log の最後の ORA / PLS のコードと、権限の不足の手がかりも出す。
_suite_check_schema() {
  local product="$1"
  local output summary status=0 log_file last_error
  output="$(suite_run_schema_check "${product}")" || status=$?
  summary="$(printf '%s\n' "${output}" | sed '/^[[:space:]]*$/d' | tail -n 1)"
  if [ "${status}" -eq 0 ]; then
    _SUITE_CHECK_LINES+=("check: ${product} schema OK（${summary:-ready}）")
    return 0
  fi
  _SUITE_CHECK_OK=false
  if [ "${status}" -eq 124 ]; then
    summary="確認が ${SUITE_SCHEMA_CHECK_TIMEOUT_SECONDS} 秒以内に終わりません"
  fi
  if [ -z "${summary}" ]; then
    summary="確認の CLI が要約を出さずに終わりました（終了コード ${status}。backend で .venv/bin/python $(suite_schema_check_args "${product}" || true) を実行して確かめてください）"
  fi
  _SUITE_CHECK_LINES+=("check: ${product} schema NG（${summary}）")
  log_file="${SUITE_PRODUCT_LOG_DIR}/${product}-init.log"
  last_error="$(grep -oE '(ORA|PLS)-[0-9]{5}' "${log_file}" 2>/dev/null | tail -n 1 || true)"
  if [ -n "${last_error}" ]; then
    _SUITE_CHECK_LINES+=("check: ${product} の初期化の log（${log_file}）の最後のエラー: ${last_error}")
  fi
  # 権限の不足（insufficient privileges・表領域の quota・Oracle Text の CTX_DDL・参照できない view など）。
  if printf '%s %s\n' "${summary}" "${last_error}" \
    | grep -qE 'ORA-01031|ORA-01950|ORA-01536|ORA-00942|ORA-04043|PLS-00201'; then
    _SUITE_CHECK_LINES+=("check: ${product} の DB ユーザー（PLATFORM_ORACLE_USER）の権限が足りない可能性があります。terraform/README.md「アプリ用の DB ユーザー」の権限を付けてから（配備の props に adb_admin.env があれば suite-restart.sh で付け直せる）、初期化をやり直してください。")
  fi
  _SUITE_CHECK_LINES+=("check: ${product} の初期化のやり直しは ${log_file} の Recovery の行（画面の システムテーブル でもできる）。")
}

suite_check_deployment() {
  local base="$1"
  shift
  local start="${SECONDS}" line unit product schema_note="system schema が ready です"
  declare -gA _SUITE_CHECK_RESTARTS=() _SUITE_CHECK_CHANGED_AT=()
  _SUITE_CHECK_SETTLE="${SUITE_CHECK_SETTLE_SECONDS}"
  _suite_health_log "check: 配備を確かめます（製品 $*、上限 ${SUITE_CHECK_TIMEOUT_SECONDS} 秒）。"
  while true; do
    _suite_check_poll "${base}" "$@"
    if [ "${_SUITE_CHECK_OK}" = "true" ] || [ $((SECONDS - start)) -ge "${SUITE_CHECK_TIMEOUT_SECONDS}" ]; then
      break
    fi
    sleep "${SUITE_CHECK_INTERVAL_SECONDS}"
  done
  # system schema は待たずに 1 回だけ確かめる（backend は起動時に DDL を実行しないので、待っても変わらない）。
  # SUITE_CHECK_SCHEMA=skip（CI の DB の無い配備の通し。#1656）は確かめずに skipped の行だけを出す。
  if [ "${SUITE_CHECK_SCHEMA}" = "skip" ]; then
    schema_note="system schema の確認は飛ばしました（SUITE_CHECK_SCHEMA=skip）"
    for product in "$@"; do
      _SUITE_CHECK_LINES+=("check: ${product} schema skipped（SUITE_CHECK_SCHEMA=skip）")
    done
  else
    for product in "$@"; do
      _suite_check_schema "${product}"
    done
  fi
  for line in "${_SUITE_CHECK_LINES[@]}"; do
    _suite_health_log "${line}"
  done
  if [ "${_SUITE_CHECK_OK}" = "true" ]; then
    _suite_health_log "check: OK（すべての service が active で、各製品の health が backend と Nginx の両方で応答し、${schema_note}）。"
    return 0
  fi
  for unit in "${_SUITE_CHECK_BAD_UNITS[@]}"; do
    _suite_health_log "check: ${unit} の journal の末尾（${SUITE_CHECK_JOURNAL_LINES} 行）:"
    journalctl -u "${unit}" -n "${SUITE_CHECK_JOURNAL_LINES}" --no-pager 2>&1 || true
  done
  _suite_health_log "check: NG（上の check の行と journal を確認してください。backend が NG なら sudo journalctl -u production-ready-<製品>-backend -n 200 --no-pager、schema が NG なら /var/log/<製品>-init.log）。"
  return 1
}
