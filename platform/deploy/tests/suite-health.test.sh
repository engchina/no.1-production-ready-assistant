#!/usr/bin/env bash
# platform/deploy/suite-health.sh（backend の起動の待ち方と配備全体の確認。#1642）と、各製品の init_script.sh の
# install_uv（SELinux の context）・wait_for_backend を、systemctl / curl / journalctl を stub にして外部依存なしで検証する。
# sleep は待たずに SECONDS を進める（上限・間隔の秒数をそのまま確かめる）。
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
task_dir="$(mktemp -d)"
trap 'rm -rf -- "${task_dir}"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# 待たずに時計を進める（bash の SECONDS は代入した値から数え直す）。
fake_sleep() {
  sleep() { SECONDS=$((SECONDS + ${1%.*})); }
}

# 1. suite_wait_for_service_health: すぐ OK・再起動の繰り返しと failed で早く失敗・上限で失敗・15 秒ごとの経過。
(
  # shellcheck source=../suite-health.sh
  source "${repo}/platform/deploy/suite-health.sh"
  out="${task_dir}/wait.log"
  log() { printf '%s\n' "$*" >> "${out}"; }
  fake_sleep
  restarts_file="${task_dir}/restarts"

  # すぐ OK。
  curl() { return 0; }
  systemctl() { fail "health が OK なら状態を問い合わせない（systemctl $*）"; }
  suite_unit_restarts() { printf '0\n'; }
  : > "${out}"
  suite_wait_for_service_health demo.service http://127.0.0.1:1/api/health || fail "すぐ OK なのに失敗した"
  grep -q "demo.service: health OK" "${out}" || fail "OK の log: $(cat "${out}")"

  # 自動の再起動を繰り返す（203/EXEC など）: 再起動 3 回で、上限（300 秒）を待たずに失敗する。
  unset -f suite_unit_restarts
  # shellcheck source=../suite-health.sh
  source "${repo}/platform/deploy/suite-health.sh"
  curl() { return 7; }
  printf '5\n' > "${restarts_file}"
  systemctl() {
    case "$1" in
      is-active) printf 'activating\n'; return 3 ;;
      show)
        # 問い合わせのたびに 1 回ずつ再起動したことにする（待ち始めは 5 回。前の配備の分は数えない）。
        local count
        count="$(cat "${restarts_file}")"
        printf '%s\n' "$((count + 1))" > "${restarts_file}"
        printf '%s\n' "${count}"
        ;;
      *) fail "想定外の systemctl $*" ;;
    esac
  }
  : > "${out}"
  start="${SECONDS}"
  if suite_wait_for_service_health demo.service http://127.0.0.1:1/api/health; then fail "再起動を繰り返すのに成功した"; fi
  [ $((SECONDS - start)) -lt 30 ] || fail "再起動の繰り返しで早く失敗しない（$((SECONDS - start)) 秒）"
  grep -q "demo.service: 起動に失敗し、再起動を繰り返しています" "${out}" || fail "再起動の繰り返しの log: $(cat "${out}")"

  # failed になったら、すぐに失敗する。
  systemctl() {
    case "$1" in
      is-active) printf 'failed\n'; return 3 ;;
      show) printf '0\n' ;;
    esac
  }
  : > "${out}"
  start="${SECONDS}"
  if suite_wait_for_service_health demo.service http://127.0.0.1:1/api/health; then fail "failed なのに成功した"; fi
  # 置き換えた sleep は間隔（3 秒）以上を足すので、実時間の秒の境目のずれ（1 秒）は許す（#1677）。
  [ $((SECONDS - start)) -le 1 ] || fail "failed で待った（$((SECONDS - start)) 秒）"
  grep -q "状態 failed" "${out}" || fail "failed の log: $(cat "${out}")"

  # active のまま応答しない: 上限で失敗し、15 秒ごとに経過・状態・再起動の回数を出す。
  systemctl() {
    case "$1" in
      is-active) printf 'active\n' ;;
      show) printf '0\n' ;;
    esac
  }
  : > "${out}"
  SUITE_BACKEND_HEALTH_TIMEOUT_SECONDS=60
  start="${SECONDS}"
  if suite_wait_for_service_health demo.service http://127.0.0.1:1/api/health; then fail "応答しないのに成功した"; fi
  elapsed=$((SECONDS - start))
  [ "${elapsed}" -ge 60 ] && [ "${elapsed}" -le 63 ] || fail "上限の 60 秒で終わらない（${elapsed} 秒）"
  grep -q "health が 60 秒以内に応答しません" "${out}" || fail "上限の log: $(cat "${out}")"
  [ "$(grep -c "demo.service: 待っています（.* 秒、状態 active、再起動 0 回）。" "${out}")" -eq 3 ] \
    || fail "15 秒ごとの経過の log: $(cat "${out}")"

  # 待ち始めの後で systemctl restart により回数が 0 に戻っても、負の回数にしない。
  printf '7\n' > "${restarts_file}"
  systemctl() {
    case "$1" in
      is-active) printf 'activating\n'; return 3 ;;
      show)
        local count
        count="$(cat "${restarts_file}")"
        if [ "${count}" -eq 7 ]; then printf '0\n' > "${restarts_file}"; else printf '%s\n' "$((count + 1))" > "${restarts_file}"; fi
        printf '%s\n' "${count}"
        ;;
    esac
  }
  : > "${out}"
  if suite_wait_for_service_health demo.service http://127.0.0.1:1/api/health; then fail "回数が戻った後の再起動の繰り返しで成功した"; fi
  grep -q "起動に失敗し、再起動を繰り返しています" "${out}" || fail "回数が戻った後の log: $(cat "${out}")"
)

# 2. suite_check_deployment: 製品ごとの 1 行の要約・証明書の更新の oneshot を除く・NG の journal・非 0。
(
  # shellcheck source=../suite-nginx.sh
  source "${repo}/platform/deploy/suite-nginx.sh"
  # shellcheck source=../suite-health.sh
  source "${repo}/platform/deploy/suite-health.sh"
  out="${task_dir}/check.log"
  journal="${task_dir}/journal.log"
  log() { printf '[suite-init] %s\n' "$*" >> "${out}"; }
  journalctl() { printf 'journalctl %s\n' "$*" >> "${journal}"; }
  # system schema の確認（#1648）は DB に繋がないよう差し替える（3. で詳しく確かめる）。
  suite_run_schema_check() { printf 'status=ready\n'; }
  fake_sleep
  SUITE_CHECK_TIMEOUT_SECONDS=30
  states_file="${task_dir}/states"
  restarts_dir="${task_dir}/check-restarts"
  mkdir -p "${restarts_dir}"
  systemctl() {
    case "$1" in
      list-unit-files)
        printf '%s\n' \
          "production-ready-rag-backend.service enabled enabled" \
          "production-ready-rag-ingestion-worker.service enabled enabled" \
          "production-ready-nl2sql-backend.service enabled enabled" \
          "production-ready-nl2sql-ontology-worker.service enabled enabled" \
          "production-ready-agent-backend.service enabled enabled" \
          "production-ready-suite-tls-renew.service enabled enabled"
        ;;
      is-active)
        local state
        state="$(sed -n "s/^$2 //p" "${states_file}")"
        printf '%s\n' "${state:-active}"
        [ "${state:-active}" = "active" ]
        ;;
      show)
        # 再起動を繰り返す unit（restarts_dir に印）は、問い合わせのたびに回数が増える。
        if [ -f "${restarts_dir}/$5" ]; then
          local count
          count="$(cat "${restarts_dir}/$5")"
          printf '%s\n' "$((count + 1))" > "${restarts_dir}/$5"
          printf '%s\n' "${count}"
        else
          printf '0\n'
        fi
        ;;
      *) fail "想定外の systemctl $*" ;;
    esac
  }

  # すべて OK（service の再起動が SUITE_CHECK_SETTLE_SECONDS の間ないことも確かめる）。
  curl() { return 0; }
  : > "${states_file}"
  : > "${out}"
  start="${SECONDS}"
  suite_check_deployment https://127.0.0.1:443 rag nl2sql agent || fail "すべて OK なのに失敗した: $(cat "${out}")"
  [ $((SECONDS - start)) -ge 10 ] || fail "service が続くことを確かめずに終わった"
  for expected in \
    '[suite-init] check: rag backend OK / services 2/2 active / nginx OK' \
    '[suite-init] check: nl2sql backend OK / services 2/2 active / nginx OK' \
    '[suite-init] check: agent backend OK / services 1/1 active / nginx OK' \
    '[suite-init] check: nginx.service active（https://127.0.0.1:443）' \
    '[suite-init] check: rag schema OK（status=ready）' \
    '[suite-init] check: nl2sql schema OK（status=ready）' \
    '[suite-init] check: agent schema OK（status=ready）'; do
    grep -qxF "${expected}" "${out}" || fail "OK の要約（${expected}）: $(cat "${out}")"
  done
  if grep -q "tls-renew" "${out}"; then fail "証明書の更新の oneshot を確かめた"; fi
  [ ! -e "${journal}" ] || fail "OK なのに journal を出した"

  # NL2SQL の backend が起動しない（203/EXEC の再起動の繰り返し）・Agent が active に見えても再起動を繰り返す。
  curl() {
    case "$*" in
      *":8010/api/health"* | *"/nl2sql/api/health"*) return 22 ;;
      *) return 0 ;;
    esac
  }
  printf 'production-ready-nl2sql-backend.service activating\n' > "${states_file}"
  printf '0\n' > "${restarts_dir}/production-ready-agent-backend.service"
  : > "${out}"
  start="${SECONDS}"
  if suite_check_deployment https://127.0.0.1:443 rag nl2sql agent; then fail "NG があるのに成功した"; fi
  [ $((SECONDS - start)) -ge 30 ] || fail "上限まで待たずに NG にした"
  grep -qxF '[suite-init] check: rag backend OK / services 2/2 active / nginx OK' "${out}" || fail "RAG の要約: $(cat "${out}")"
  grep -qF '[suite-init] check: nl2sql backend NG / services 1/2 active（NG: production-ready-nl2sql-backend.service activating（再起動 0 回）） / nginx NG' "${out}" \
    || fail "NL2SQL の要約: $(cat "${out}")"
  grep -qF '[suite-init] check: agent backend OK / services 0/1 active（NG: production-ready-agent-backend.service active（再起動' "${out}" \
    || fail "再起動を繰り返す Agent を NG にしない: $(cat "${out}")"
  grep -qxF "journalctl -u production-ready-nl2sql-backend.service -n 80 --no-pager" "${journal}" || fail "NG の journal: $(cat "${journal}")"
  grep -qxF "journalctl -u production-ready-agent-backend.service -n 80 --no-pager" "${journal}" || fail "Agent の journal: $(cat "${journal}")"
  if grep -q "rag-backend" "${journal}"; then fail "OK の service の journal を出した"; fi
  tail -n 1 "${out}" | grep -q "check: NG" || fail "最後の行が NG ではない: $(tail -n 1 "${out}")"
  rm -f "${restarts_dir}/production-ready-agent-backend.service"

  # Nginx が止まっている・Nginx を通した health だけが NG。--status と同じ 1 回だけの確認（待たない）。
  curl() { case "$*" in *"127.0.0.1:443/"*) return 7 ;; *) return 0 ;; esac; }
  printf 'nginx.service inactive\n' > "${states_file}"
  : > "${out}"
  : > "${journal}"
  start="${SECONDS}"
  if SUITE_CHECK_TIMEOUT_SECONDS=0 SUITE_CHECK_SETTLE_SECONDS=0 suite_check_deployment https://127.0.0.1:443 rag; then
    fail "Nginx が止まっているのに成功した"
  fi
  [ $((SECONDS - start)) -le 1 ] || fail "1 回だけの確認で待った（$((SECONDS - start)) 秒）"
  grep -qxF '[suite-init] check: rag backend OK / services 2/2 active / nginx NG' "${out}" || fail "Nginx の NG: $(cat "${out}")"
  grep -qxF '[suite-init] check: nginx.service NG（inactive）' "${out}" || fail "Nginx の状態: $(cat "${out}")"
  grep -qxF "journalctl -u nginx.service -n 80 --no-pager" "${journal}" || fail "Nginx の journal: $(cat "${journal}")"
)

# 2b. system schema の確認（#1648）: init_script.sh は表の初期化に失敗しても backend を起動する（health は OK）。
#     schema の CLI の check が ready でなければ check を NG にし、要約・初期化の log の最後の ORA・権限の手がかりを出して
#     非 0 で終わる。schema の確認は待たずに 1 回だけ。CLI は製品のユーザーで backend の .venv の python で実行する。
(
  # shellcheck source=../suite-nginx.sh
  source "${repo}/platform/deploy/suite-nginx.sh"
  # shellcheck source=../suite-health.sh
  source "${repo}/platform/deploy/suite-health.sh"
  out="${task_dir}/schema.log"
  calls="${task_dir}/schema-calls.log"
  log() { printf '[suite-init] %s\n' "$*" >> "${out}"; }
  journalctl() { :; }
  fake_sleep
  systemctl() {
    case "$1" in
      list-unit-files)
        printf '%s\n' "production-ready-rag-backend.service enabled enabled" \
          "production-ready-nl2sql-backend.service enabled enabled"
        ;;
      is-active) printf 'active\n' ;;
      show) printf '0\n' ;;
    esac
  }
  curl() { return 0; }
  SUITE_PRODUCT_LOG_DIR="${task_dir}/var-log"
  mkdir -p "${SUITE_PRODUCT_LOG_DIR}"
  printf '%s\n' '[nl2sql-init] Initializing NL2SQL system schema with an idempotent migration.' \
    '{"error":{"code":"ORA-01031","message":"システムテーブル操作に失敗しました（DatabaseError）。"},"ok":false}' \
    > "${SUITE_PRODUCT_LOG_DIR}/nl2sql-init.log"
  suite_run_schema_check() {
    printf 'schema-check %s\n' "$1" >> "${calls}"
    case "$1" in
      nl2sql)
        printf 'log line\nstatus=missing・操作 failed・前回の操作の失敗 ORA-01031\n'
        return 1
        ;;
      *) printf 'status=ready\n' ;;
    esac
  }
  : > "${out}"
  : > "${calls}"
  start="${SECONDS}"
  if SUITE_CHECK_SETTLE_SECONDS=0 suite_check_deployment https://127.0.0.1:443 rag nl2sql; then
    fail "schema が NG なのに成功した: $(cat "${out}")"
  fi
  [ $((SECONDS - start)) -le 1 ] || fail "service が OK なのに schema の NG で待った（$((SECONDS - start)) 秒）"
  [ "$(cat "${calls}")" = "$(printf 'schema-check rag\nschema-check nl2sql')" ] || fail "schema の確認は製品ごとに 1 回: $(cat "${calls}")"
  for expected in \
    '[suite-init] check: rag backend OK / services 1/1 active / nginx OK' \
    '[suite-init] check: nl2sql backend OK / services 1/1 active / nginx OK' \
    '[suite-init] check: rag schema OK（status=ready）' \
    '[suite-init] check: nl2sql schema NG（status=missing・操作 failed・前回の操作の失敗 ORA-01031）' \
    "[suite-init] check: nl2sql の初期化の log（${SUITE_PRODUCT_LOG_DIR}/nl2sql-init.log）の最後のエラー: ORA-01031"; do
    grep -qxF "${expected}" "${out}" || fail "schema の NG の行（${expected}）: $(cat "${out}")"
  done
  grep -qF 'check: nl2sql の DB ユーザー（PLATFORM_ORACLE_USER）の権限が足りない可能性があります。terraform/README.md' "${out}" \
    || fail "権限の不足の手がかり: $(cat "${out}")"
  if grep -qF 'check: rag の DB ユーザー' "${out}"; then fail "OK の製品に手がかりを出した"; fi
  tail -n 1 "${out}" | grep -q "check: NG" || fail "最後の行が NG ではない: $(tail -n 1 "${out}")"

  # 時間切れ（timeout の 124）と、要約を出さない失敗。
  suite_run_schema_check() { return 124; }
  : > "${out}"
  if SUITE_CHECK_SETTLE_SECONDS=0 suite_check_deployment https://127.0.0.1:443 rag; then fail "時間切れで成功した"; fi
  grep -qxF '[suite-init] check: rag schema NG（確認が 120 秒以内に終わりません）' "${out}" || fail "時間切れの行: $(cat "${out}")"
  suite_run_schema_check() { return 1; }
  : > "${out}"
  if SUITE_CHECK_SETTLE_SECONDS=0 suite_check_deployment https://127.0.0.1:443 rag; then fail "要約の無い失敗で成功した"; fi
  grep -qF '[suite-init] check: rag schema NG（確認の CLI が要約を出さずに終わりました（終了コード 1。' "${out}" \
    || fail "要約の無い失敗の行: $(cat "${out}")"

  # SUITE_CHECK_SCHEMA=skip（CI の DB の無い配備の通し。#1656）: CLI を呼ばず、製品ごとに skipped の行を出して成功する。
  suite_run_schema_check() { printf 'schema-check %s\n' "$1" >> "${calls}"; return 1; }
  : > "${out}"
  : > "${calls}"
  SUITE_CHECK_SCHEMA=skip SUITE_CHECK_SETTLE_SECONDS=0 suite_check_deployment https://127.0.0.1:443 rag nl2sql \
    || fail "schema の確認を飛ばしたのに失敗した: $(cat "${out}")"
  [ ! -s "${calls}" ] || fail "skip なのに schema の CLI を呼んだ: $(cat "${calls}")"
  for expected in \
    '[suite-init] check: rag schema skipped（SUITE_CHECK_SCHEMA=skip）' \
    '[suite-init] check: nl2sql schema skipped（SUITE_CHECK_SCHEMA=skip）'; do
    grep -qxF "${expected}" "${out}" || fail "skipped の行（${expected}）: $(cat "${out}")"
  done
  tail -n 1 "${out}" | grep -qF 'check: OK（' || fail "最後の行が OK ではない: $(tail -n 1 "${out}")"
  tail -n 1 "${out}" | grep -qF 'system schema の確認は飛ばしました（SUITE_CHECK_SCHEMA=skip）' \
    || fail "OK の行に skip を書かない: $(tail -n 1 "${out}")"
  # skip 以外の値と既定（未設定）は確かめる（本番の既定は変えない）。
  : > "${out}"
  : > "${calls}"
  if SUITE_CHECK_SCHEMA=check SUITE_CHECK_SETTLE_SECONDS=0 suite_check_deployment https://127.0.0.1:443 rag; then
    fail "skip 以外の値で schema の NG を飛ばした"
  fi
  [ "$(cat "${calls}")" = "schema-check rag" ] || fail "skip 以外の値で schema の CLI を呼ばない: $(cat "${calls}")"
  if grep -qF 'schema skipped' "${out}"; then fail "skip 以外の値で skipped を出した: $(cat "${out}")"; fi

  # 本物の suite_run_schema_check: 製品のユーザーで backend の .venv の python の check を実行する。venv が無ければ NG。
  unset -f suite_run_schema_check
  # shellcheck source=../suite-health.sh
  source "${repo}/platform/deploy/suite-health.sh"
  SUITE_REPO_DIR="${task_dir}/suite"
  APP_USER=opc
  for product in rag nl2sql agent; do
    mkdir -p "${SUITE_REPO_DIR}/${product}/backend/.venv/bin"
    : > "${SUITE_REPO_DIR}/${product}/backend/.venv/bin/python"
    /bin/chmod 0755 "${SUITE_REPO_DIR}/${product}/backend/.venv/bin/python"
  done
  runuser() { printf '%s\n' "$*" >> "${calls}"; printf 'status=ready\n'; }
  : > "${calls}"
  [ "$(suite_run_schema_check rag)" = "status=ready" ] || fail "runuser の出力を返さない"
  suite_run_schema_check nl2sql >/dev/null
  suite_run_schema_check agent >/dev/null
  grep -qF -- "-u opc -- env HOME=/home/opc" "${calls}" || fail "製品のユーザーで実行しない: $(cat "${calls}")"
  for expected in \
    "cd '${SUITE_REPO_DIR}/rag/backend' && exec timeout 120 .venv/bin/python -m app.rag.system_schema_cli check" \
    "cd '${SUITE_REPO_DIR}/nl2sql/backend' && exec timeout 120 .venv/bin/python -m app.cli.nl2sql_system_schema --check" \
    "cd '${SUITE_REPO_DIR}/agent/backend' && exec timeout 120 .venv/bin/python -m app.cli.agent_system_schema --check"; do
    grep -qF -- "${expected}" "${calls}" || fail "schema の確認の command（${expected}）: $(cat "${calls}")"
  done
  rm -rf "${SUITE_REPO_DIR}/agent/backend/.venv"
  if output="$(suite_run_schema_check agent)"; then fail "venv が無いのに成功した"; fi
  [ "${output}" = "backend の venv がありません（${SUITE_REPO_DIR}/agent/backend/.venv）" ] || fail "venv が無いときの要約: ${output}"
)

# 3. suite_local_nginx_base_url: props の HTTPS と port（無ければ HTTPS on・443）。
(
  # shellcheck source=../suite-health.sh
  source "${repo}/platform/deploy/suite-health.sh"
  props="${task_dir}/props"
  mkdir -p "${props}"
  [ "$(suite_local_nginx_base_url "${props}")" = "https://127.0.0.1:443" ] || fail "既定の URL: $(suite_local_nginx_base_url "${props}")"
  printf '8443\n' > "${props}/https_port.txt"
  [ "$(suite_local_nginx_base_url "${props}")" = "https://127.0.0.1:8443" ] || fail "https_port の URL"
  printf 'false\n' > "${props}/https_enabled.txt"
  printf '8080\n' > "${props}/http_port.txt"
  [ "$(suite_local_nginx_base_url "${props}")" = "http://127.0.0.1:8080" ] || fail "HTTPS が off の URL"
)

# 4. 各製品の init_script.sh: install_uv は新規・既存のどちらでも、SELinux が有効なら uv / uvx の context を戻す。
#    wait_for_backend は共通の helper で待ち、再起動の繰り返しで早く失敗して status と journal を出す。
for product in rag nl2sql agent; do
  (
    prefix="${product^^}"
    export "${prefix}_INIT_TEST_MODE=true"
    # shellcheck source=/dev/null
    source "${repo}/${product}/init_script.sh"
    trap - ERR
    calls="${task_dir}/${product}-calls.log"
    : > "${calls}"
    UV_BIN_DIR="${task_dir}/${product}-bin"
    mkdir -p "${UV_BIN_DIR}"
    : > "${UV_BIN_DIR}/uv"
    log() { :; }
    retry_command() { :; }
    install() { :; }
    chmod() { :; }
    uv() { printf 'uv 0.0.0\n'; }
    restorecon() { printf 'restorecon %s\n' "$*" >> "${calls}"; }
    selinuxenabled() { return 0; }
    install_uv
    grep -qxF "restorecon -v ${UV_BIN_DIR}/uv" "${calls}" || fail "${product}: 既にある uv の context を戻さない: $(cat "${calls}")"
    : > "${UV_BIN_DIR}/uvx"
    : > "${calls}"
    restore_uv_selinux_context
    grep -qxF "restorecon -v ${UV_BIN_DIR}/uv ${UV_BIN_DIR}/uvx" "${calls}" || fail "${product}: uvx の context を戻さない: $(cat "${calls}")"
    selinuxenabled() { return 1; }
    : > "${calls}"
    install_uv
    [ ! -s "${calls}" ] || fail "${product}: SELinux が無効なのに restorecon した: $(cat "${calls}")"
    # 新しく入れたときも、入れた後に戻す（installer の代わりに、curl の stub が fake の uv を置く）。
    selinuxenabled() { return 0; }
    unset -f uv
    fake_bin="${task_dir}/${product}-path"
    mkdir -p "${fake_bin}"
    ln -sf "$(command -v env)" "${fake_bin}/env"
    ln -sf "$(command -v sh)" "${fake_bin}/sh"
    curl() {
      printf 'curl %s\n' "$*" >> "${calls}"
      printf '#!/bin/sh\necho uv 0.0.0\n' > "${fake_bin}/uv"
      /bin/chmod 0755 "${fake_bin}/uv"
      printf 'exit 0\n'
    }
    : > "${calls}"
    PATH="${fake_bin}" install_uv >/dev/null
    grep -q "^curl -LsSf https://astral.sh/uv/install.sh" "${calls}" || fail "${product}: uv を入れない: $(cat "${calls}")"
    tail -n 1 "${calls}" | grep -qxF "restorecon -v ${UV_BIN_DIR}/uv ${UV_BIN_DIR}/uvx" \
      || fail "${product}: 入れた後に context を戻さない: $(cat "${calls}")"

    grep -qF 'suite_wait_for_service_health' "${repo}/${product}/init_script.sh" || fail "${product}: 共通の helper で待たない"
    if grep -qE 'retry_command [0-9]+ curl .*/api/health' "${repo}/${product}/init_script.sh"; then
      fail "${product}: 以前の待ち方（retry_command と curl）が残っている"
    fi
    sleep() { SECONDS=$((SECONDS + ${1%.*})); }
    # shellcheck disable=SC2034 # wait_for_backend（source した init_script.sh）が読む
    DATABASE_INITIALIZATION_READY=true
    restarts_file="${task_dir}/${product}-restarts"
    printf '0\n' > "${restarts_file}"
    systemctl() {
      case "$1" in
        is-active) printf 'activating\n'; return 3 ;;
        show)
          local count
          count="$(cat "${restarts_file}")"
          printf '%s\n' "$((count + 1))" > "${restarts_file}"
          printf '%s\n' "${count}"
          ;;
      esac
    }
    # すぐ OK。
    curl() { return 0; }
    dump_service_diagnostics() { printf 'diagnostics %s\n' "$*" >> "${calls}"; }
    : > "${calls}"
    wait_for_backend || fail "${product}: health が OK なのに失敗した"
    [ ! -s "${calls}" ] || fail "${product}: OK なのに diagnostics を出した"
    # 再起動の繰り返し: 早く失敗し、backend の unit の diagnostics を出す。
    curl() { return 7; }
    start="${SECONDS}"
    if wait_for_backend; then fail "${product}: 再起動を繰り返すのに成功した"; fi
    [ $((SECONDS - start)) -lt 30 ] || fail "${product}: 早く失敗しない（$((SECONDS - start)) 秒）"
    grep -qxF "diagnostics production-ready-${product}-backend.service" "${calls}" || fail "${product}: diagnostics: $(cat "${calls}")"
  )
done

# 5. 各製品の init_script.sh の retry_command（system schema の初期化など）: 失敗の間だけ 5・10・…秒待ち、最後の
#    失敗の後は待たない（DB の停止中に配備を無駄に延ばさない。#1779）。成功したら試し直さない。
for product in rag nl2sql agent; do
  (
    prefix="${product^^}"
    export "${prefix}_INIT_TEST_MODE=true"
    # shellcheck source=/dev/null
    source "${repo}/${product}/init_script.sh"
    trap - ERR
    # shellcheck disable=SC2329 # retry_command（source した init_script.sh）が呼ぶ
    log() { :; }
    sleeps="${task_dir}/${product}-retry-sleeps.log"
    : > "${sleeps}"
    # shellcheck disable=SC2329 # retry_command（source した init_script.sh）が呼ぶ
    sleep() { printf '%s\n' "$1" >> "${sleeps}"; }
    attempts_file="${task_dir}/${product}-retry-attempts"
    printf '0\n' > "${attempts_file}"
    # shellcheck disable=SC2329 # retry_command（source した init_script.sh）が呼ぶ
    always_fail() { printf '%s\n' "$(($(cat "${attempts_file}") + 1))" > "${attempts_file}"; return 7; }
    status=0
    retry_command 5 always_fail || status=$?
    [ "${status}" -eq 7 ] || fail "${product}: retry_command が最後の終了コードを返さない（${status}）"
    [ "$(cat "${attempts_file}")" = "5" ] || fail "${product}: 5 回試さない（$(cat "${attempts_file}")）"
    [ "$(tr '\n' ' ' < "${sleeps}")" = "5 10 15 20 " ] || fail "${product}: 待ちの間隔: $(tr '\n' ' ' < "${sleeps}")"
    : > "${sleeps}"
    printf '0\n' > "${attempts_file}"
    # shellcheck disable=SC2329 # retry_command（source した init_script.sh）が呼ぶ
    second_ok() {
      local count=$(($(cat "${attempts_file}") + 1))
      printf '%s\n' "${count}" > "${attempts_file}"
      [ "${count}" -ge 2 ]
    }
    retry_command 5 second_ok || fail "${product}: 2 回目の成功で 0 にならない"
    [ "$(cat "${attempts_file}")" = "2" ] && [ "$(tr '\n' ' ' < "${sleeps}")" = "5 " ] \
      || fail "${product}: 成功の後に試し直した・待った（$(cat "${attempts_file}") 回、待ち $(tr '\n' ' ' < "${sleeps}")）"
  ) || exit 1
done

echo "suite-health: pass"
