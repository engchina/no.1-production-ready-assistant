#!/usr/bin/env bash
# 配備した Compute の運用のスクリプト（#1566）を外部依存なしで検証する:
# suite-pull.sh（main の最新の取得）・suite-restart.sh（全サービスの再起動）・suite-update.sh（取得と再配備）。
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
task_dir="$(mktemp -d)"
trap 'rm -rf -- "${task_dir}"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.com GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.com
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1

# 1. suite-pull.sh: 追跡ファイルの変更があれば中止し、無ければ main を origin の最新にする。未追跡のファイルは保つ。
(
  origin="${task_dir}/origin.git"
  work="${task_dir}/work"
  git init -q --bare -b main "${origin}"
  git clone -q "${origin}" "${work}" 2>/dev/null
  printf 'v1\n' > "${work}/app.txt"
  git -C "${work}" add app.txt
  git -C "${work}" commit -q -m v1
  git -C "${work}" push -q origin main
  # 配備と同じく depth 1 の clone（file:// でないと depth が効かない）。
  export SUITE_REPO_DIR="${task_dir}/deployed"
  git clone -q --branch main --depth 1 --single-branch "file://${origin}" "${SUITE_REPO_DIR}"
  printf 'v2\n' > "${work}/app.txt"
  git -C "${work}" commit -q -am v2
  git -C "${work}" push -q origin main
  latest="$(git -C "${work}" rev-parse HEAD)"
  # shellcheck source=../suite-pull.sh
  source "${repo}/platform/deploy/suite-pull.sh"

  printf 'local edit\n' > "${SUITE_REPO_DIR}/app.txt"
  if suite_pull >/dev/null 2>&1; then fail "追跡ファイルの変更があるのに取得した"; fi
  [ "$(cat "${SUITE_REPO_DIR}/app.txt")" = "local edit" ] || fail "変更を消した"
  git -C "${SUITE_REPO_DIR}" checkout -q -- app.txt

  printf 'keep\n' > "${SUITE_REPO_DIR}/untracked.txt"
  suite_pull >/dev/null || fail "取得に失敗した"
  [ "$(git -C "${SUITE_REPO_DIR}" rev-parse HEAD)" = "${latest}" ] || fail "origin の main の最新になっていない"
  [ "$(git -C "${SUITE_REPO_DIR}" symbolic-ref --short HEAD)" = "main" ] || fail "main の branch にいない"
  [ "$(cat "${SUITE_REPO_DIR}/app.txt")" = "v2" ] || fail "取得したファイルになっていない"
  [ -f "${SUITE_REPO_DIR}/untracked.txt" ] || fail "未追跡のファイルを消した"
  suite_pull | grep -q "すでに最新です" || fail "最新のときの表示"
  [ "$(git -C "${SUITE_REPO_DIR}" rev-parse --is-shallow-repository)" = "true" ] || fail "depth 1 の clone の深さを変えた"

  # 履歴をすべて持つ clone（開発機を配備にも使う場合）は shallow にしない（worktree の merge-base が誤るため）。
  export SUITE_REPO_DIR="${task_dir}/full"
  git clone -q --branch main "file://${origin}" "${SUITE_REPO_DIR}"
  printf 'v3\n' > "${work}/app.txt"
  git -C "${work}" commit -q -am v3
  git -C "${work}" push -q origin main
  suite_pull >/dev/null || fail "履歴をすべて持つ clone で取得に失敗した"
  [ "$(git -C "${SUITE_REPO_DIR}" rev-parse HEAD)" = "$(git -C "${work}" rev-parse HEAD)" ] || fail "履歴をすべて持つ clone が最新になっていない"
  [ "$(git -C "${SUITE_REPO_DIR}" rev-parse --is-shallow-repository)" = "false" ] || fail "履歴をすべて持つ clone を shallow にした"
)

# 2. suite-restart.sh --no-build: 有効な production-ready-* の service（証明書の更新の oneshot を除く）と Nginx を
#    再起動し、配備全体（service・backend と Nginx を通した health・Nginx）を確かめる。
(
  export PROPS_DIR="${task_dir}/restart-props"
  mkdir -p "${PROPS_DIR}"
  printf 'rag agent\n' > "${PROPS_DIR}/products.txt"
  # shellcheck source=../suite-restart.sh
  source "${repo}/platform/deploy/suite-restart.sh"
  calls="${task_dir}/restart-calls.log"
  systemctl() {
    if [ "$1" = "list-unit-files" ]; then
      printf '%s\n' \
        "production-ready-rag-backend.service enabled enabled" \
        "production-ready-rag-parser-docling.service enabled enabled" \
        "production-ready-suite-tls-renew.service enabled enabled" \
        "production-ready-agent-backend.service enabled enabled"
      return 0
    fi
    printf 'systemctl %s\n' "$*" >> "${calls}"
  }
  nginx() { printf 'nginx %s\n' "$*" >> "${calls}"; }
  suite_restart_services >/dev/null
  grep -qxF "systemctl restart production-ready-rag-backend.service production-ready-rag-parser-docling.service production-ready-agent-backend.service" "${calls}" \
    || fail "再起動の対象: $(cat "${calls}")"
  if grep -q "tls-renew" "${calls}"; then fail "証明書の更新の oneshot を再起動した"; fi
  grep -qxF "nginx -t" "${calls}" || fail "Nginx の設定を確かめない"
  grep -qxF "systemctl restart nginx" "${calls}" || fail "Nginx を再起動しない"

  [ "$(suite_deployed_products | tr '\n' ' ')" = "rag agent " ] || fail "配備した製品: $(suite_deployed_products | tr '\n' ' ')"
  # 再起動の後の確認は suite-init.sh の最後と同じ suite_check_deployment（#1642。suite-health.test.sh が詳しく確かめる）。
  # 配備した製品と、props の HTTPS の設定から Nginx を通す URL を決める。
  SUITE_HEALTH_TIMEOUT_SECONDS=0
  SUITE_CHECK_SETTLE_SECONDS=0
  systemctl() {
    case "$1" in
      list-unit-files) printf '%s\n' "production-ready-rag-backend.service enabled enabled" "production-ready-agent-backend.service enabled enabled" ;;
      is-active) printf 'active\n' ;;
      show) printf '0\n' ;;
    esac
  }
  journalctl() { :; }
  # system schema の確認（#1648）は DB に繋がないよう差し替える（suite-health.test.sh が詳しく確かめる）。
  suite_run_schema_check() { printf 'status=ready\n'; }
  curl() { case "$*" in *":8000/api/health"* | *"127.0.0.1:443/rag/api/health"*) return 0 ;; *) return 7 ;; esac; }
  if suite_check_deployed >/dev/null; then fail "Agent が応答しないのに成功した"; fi
  curl() { printf '%s\n' "$*" >> "${calls}"; }
  : > "${calls}"
  check_out="$(suite_check_deployed)" || fail "すべて応答するのに失敗した"
  printf '%s\n' "${check_out}" | grep -qxF '[suite-restart] check: agent backend OK / services 1/1 active / nginx OK' \
    || fail "確認の要約: ${check_out}"
  grep -qF "https://127.0.0.1:443/agent/api/health" "${calls}" || fail "Nginx を通した health を確かめない: $(cat "${calls}")"
  printf 'false\n' > "${PROPS_DIR}/https_enabled.txt"
  : > "${calls}"
  suite_check_deployed >/dev/null || fail "HTTPS が off の確認に失敗した"
  grep -qF "http://127.0.0.1:80/rag/api/health" "${calls}" || fail "HTTPS が off のときに http で確かめない: $(cat "${calls}")"
)

# 3. suite-restart.sh（既定）: 今のコードから build する。再配備の前に今の backend/.env を props へ写し
#    （元の props は退避）、suite-init.sh を実行する（#1577。suite-pull.sh の後の再起動で最新のコードを使う）。
(
  export APP_ROOT="${task_dir}/rebuild"
  export SUITE_REPO_DIR="${APP_ROOT}/no.1-production-ready-suite"
  export PROPS_DIR="${APP_ROOT}/props"
  mkdir -p "${SUITE_REPO_DIR}/rag/backend" "${SUITE_REPO_DIR}/agent/backend" "${PROPS_DIR}"
  printf 'rag agent\n' > "${PROPS_DIR}/products.txt"
  printf 'RAG_FROM_TERRAFORM=1\n' > "${PROPS_DIR}/rag.backend.env"
  printf 'RAG_FROM_TERRAFORM=1\nRAG_SAVED_ON_SCREEN=1\n' > "${SUITE_REPO_DIR}/rag/backend/.env"
  # Agent の backend/.env はまだ無い（props をそのまま使う）。
  printf 'AGENT_FROM_TERRAFORM=1\n' > "${PROPS_DIR}/agent.backend.env"
  init_marker="${task_dir}/init-called"
  export SUITE_INIT_SCRIPT="${task_dir}/fake-suite-init.sh"
  printf '#!/usr/bin/env bash\ncp "%s" "%s"\n' "${PROPS_DIR}/rag.backend.env" "${init_marker}" > "${SUITE_INIT_SCRIPT}"
  # shellcheck source=../suite-restart.sh
  source "${repo}/platform/deploy/suite-restart.sh"
  suite_rebuild_and_restart >/dev/null || fail "再配備に失敗した"
  grep -qxF "RAG_SAVED_ON_SCREEN=1" "${init_marker}" || fail "suite-init.sh の前に backend/.env を props へ写していない"
  [ "$(cat "${PROPS_DIR}/agent.backend.env")" = "AGENT_FROM_TERRAFORM=1" ] || fail "backend/.env が無い製品の props を変えた"
  backup="$(find "${APP_ROOT}/recovery" -name rag.backend.env)"
  [ -n "${backup}" ] && [ "$(cat "${backup}")" = "RAG_FROM_TERRAFORM=1" ] || fail "元の props を退避していない"
  [ "$(stat -c '%a' "${PROPS_DIR}/rag.backend.env")" = "600" ] || fail "props の権限"
  printf '#!/usr/bin/env bash\nexit 1\n' > "${SUITE_INIT_SCRIPT}"
  if suite_rebuild_and_restart >/dev/null 2>&1; then fail "再配備の失敗を成功と報告した"; fi
)

# 4. suite-update.sh: 取得の後に、取得した新しい suite-restart.sh（既定 = build して再起動）を実行する。
#    ADB の NG の案内に出すコマンドは suite-update.sh（#1745。画面から起動すれば再実行は要らない旨を出す。#1762）。
grep -qxF '  suite_pull' "${repo}/platform/deploy/suite-update.sh" || fail "suite-update.sh が取得しない"
grep -qxF '  exec bash "${SUITE_RESTART_SCRIPT}"' "${repo}/platform/deploy/suite-update.sh" \
  || fail "suite-update.sh が取得の後に suite-restart.sh（build して再起動）を実行しない"
grep -qxF '  export SUITE_OPS_RERUN_COMMAND="${SUITE_DEPLOY_DIR}/suite-update.sh"' "${repo}/platform/deploy/suite-update.sh" \
  || fail "suite-update.sh が実行し直すコマンドを渡さない"

# 5. ADB の起動の確認（#1745）: build・--no-build は DB を使う処理の前に suite-adb.py ensure を実行する。NG でも
#    warning を出して build・再起動を続ける（#1756）。--status は status だけ（起動しない）で、結果を確認の終了コードに
#    含めない。
#    suite-adb.py の状態ごとの振る舞いは suite-adb.test.sh が確かめる。
(
  export APP_ROOT="${task_dir}/adb"
  export SUITE_REPO_DIR="${APP_ROOT}/no.1-production-ready-suite"
  export PROPS_DIR="${APP_ROOT}/props"
  export SUITE_OPS_INHERITED=true
  mkdir -p "${PROPS_DIR}"
  printf 'nl2sql agent\n' > "${PROPS_DIR}/products.txt"
  # shellcheck source=../suite-restart.sh
  source "${repo}/platform/deploy/suite-restart.sh"
  calls="${task_dir}/adb-calls.log"
  adb_status=0
  id() { printf '0\n'; }
  suite_run_adb() {
    printf 'adb %s\n' "$*" >> "${calls}"
    return "${adb_status}"
  }
  suite_rebuild_and_restart() { printf 'rebuild\n' >> "${calls}"; }
  suite_restart_services() { printf 'restart-services\n' >> "${calls}"; }
  suite_check_deployed() { printf 'check\n' >> "${calls}"; }

  : > "${calls}"
  suite_restart_main >/dev/null || fail "ADB が使えるのに build が失敗した"
  [ "$(cut -d' ' -f1-2 "${calls}" | tr '\n' ' ')" = "adb ensure rebuild check " ] || fail "build の順: $(cat "${calls}")"
  grep -qF -- "--timeout-seconds 600 --interval-seconds 15 --script-name ${repo}/platform/deploy/suite-restart.sh" "${calls}" \
    || fail "ensure の引数: $(cat "${calls}")"
  : > "${calls}"
  SUITE_OPS_RERUN_COMMAND=/opt/suite-update.sh suite_restart_main >/dev/null || fail "suite-update.sh からの build"
  grep -qF -- "--script-name /opt/suite-update.sh" "${calls}" || fail "suite-update.sh を実行し直すコマンドにしない: $(cat "${calls}")"
  : > "${calls}"
  suite_restart_main --no-build >/dev/null || fail "--no-build が失敗した"
  [ "$(cut -d' ' -f1-2 "${calls}" | tr '\n' ' ')" = "adb ensure restart-services check " ] || fail "--no-build の順: $(cat "${calls}")"
  grep -qF -- "--script-name ${repo}/platform/deploy/suite-restart.sh --no-build" "${calls}" || fail "--no-build の実行し直すコマンド"

  # ADB の確認・起動が NG（OCI の認証が無い・権限が無い・時間切れ・venv が無い）でも、warning を出して build・再起動を
  # 続け、ADB の NG だけでは終了コードを失敗にしない（#1756。DB に接続できなくてもアプリケーションは起動する）。
  for adb_status in 1 124; do
    for args in "" "--no-build"; do
      : > "${calls}"
      out="$(suite_restart_main ${args:+"${args}"} 2>&1)" || fail "ADB の NG（${adb_status}）で失敗にした（${args:-build}）: ${out}"
      expected="rebuild"
      [ -z "${args}" ] || expected="restart-services"
      [ "$(cut -d' ' -f1-2 "${calls}" | tr '\n' ' ')" = "adb ensure ${expected} check " ] \
        || fail "ADB の NG で build・再起動を続けない（${args:-build}）: $(cat "${calls}")"
      printf '%s\n' "${out}" | grep -qF "[suite-restart] WARNING: ADB を使える状態にできませんでした（上の [suite-adb] NG の行に理由と手で起動する方法があります）。build・再起動は続けます。" \
        || fail "warning（${args:-build}）: ${out}"
      printf '%s\n' "${out}" | grep -qF "そのまま DB を使えます（sudo ${repo}/platform/deploy/suite-restart.sh${args:+ ${args}} の再実行は要りません。" \
        || fail "実行し直すコマンド（${args:-build}）: ${out}"
      if [ "${adb_status}" -eq 124 ]; then
        printf '%s\n' "${out}" | grep -qF "ADB の確認が時間内に終わりませんでした" || fail "timeout の理由: ${out}"
      fi
    done
  done
  # build・確認の失敗は今どおり失敗にする（ADB の NG とは別）。
  suite_check_deployed() { printf 'check\n' >> "${calls}"; return 1; }
  if suite_restart_main >/dev/null 2>&1; then fail "確認の失敗を成功にした"; fi
  suite_check_deployed() { printf 'check\n' >> "${calls}"; }
  : > "${calls}"
  suite_restart_main --status >/dev/null || fail "--status が ADB の状態で失敗した"
  [ "$(cut -d' ' -f1-2 "${calls}" | tr '\n' ' ')" = "adb status check " ] || fail "--status が起動を試みた: $(cat "${calls}")"

  # skip: 確かめない。check / skip 以外の値は warning を出して check として扱う（止めない）。
  adb_status=0
  : > "${calls}"
  SUITE_ADB_AUTOSTART=skip suite_restart_main >/dev/null || fail "skip で失敗した"
  [ "$(tr '\n' ' ' < "${calls}")" = "rebuild check " ] || fail "skip で確かめた: $(cat "${calls}")"
  : > "${calls}"
  out="$(SUITE_ADB_AUTOSTART=yes suite_restart_main 2>&1)" || fail "SUITE_ADB_AUTOSTART の誤りで止めた: ${out}"
  [ "$(cut -d' ' -f1-2 "${calls}" | tr '\n' ' ')" = "adb ensure rebuild check " ] || fail "SUITE_ADB_AUTOSTART の誤りで check しない: $(cat "${calls}")"
  printf '%s\n' "${out}" | grep -qF "WARNING: SUITE_ADB_AUTOSTART は check か skip にしてください（yes）。check として扱います。" \
    || fail "SUITE_ADB_AUTOSTART の誤りの warning: ${out}"
)

# 6. suite_run_adb: 配備した製品のうち、最初に venv がある製品の backend の venv の Python で suite-adb.py を、
#    repository の platform/.env を渡して実行する（root なら APP_USER で。ここは root でないのでそのまま）。
(
  export APP_ROOT="${task_dir}/adb-run"
  export SUITE_REPO_DIR="${APP_ROOT}/no.1-production-ready-suite"
  export PROPS_DIR="${APP_ROOT}/props"
  mkdir -p "${PROPS_DIR}" "${SUITE_REPO_DIR}/agent/backend/.venv/bin"
  printf 'rag agent\n' > "${PROPS_DIR}/products.txt"
  args_file="${task_dir}/adb-run-args"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$@" > "%s"\n' "${args_file}" > "${SUITE_REPO_DIR}/agent/backend/.venv/bin/python"
  chmod +x "${SUITE_REPO_DIR}/agent/backend/.venv/bin/python"
  # shellcheck source=../suite-restart.sh
  source "${repo}/platform/deploy/suite-restart.sh"
  [ "$(suite_adb_python)" = "${SUITE_REPO_DIR}/agent/backend/.venv/bin/python" ] || fail "venv の Python: $(suite_adb_python)"
  suite_run_adb status --script-name x >/dev/null || fail "suite_run_adb が失敗した"
  [ "$(tr '\n' ' ' < "${args_file}")" = "${repo}/platform/deploy/suite-adb.py status --script-name x --env-file ${SUITE_REPO_DIR}/platform/.env " ] \
    || fail "suite-adb.py の引数: $(tr '\n' ' ' < "${args_file}")"
  rm -rf "${SUITE_REPO_DIR}/agent/backend/.venv"
  out="$(suite_run_adb status 2>&1)" && fail "venv が無いのに成功した"
  printf '%s\n' "${out}" | grep -qF "製品の backend の venv" || fail "venv が無いときの理由: ${out}"
)

for script in suite-pull.sh suite-restart.sh suite-update.sh; do
  [ -x "${repo}/platform/deploy/${script}" ] || fail "${script} に実行権限が無い"
  bash "${repo}/platform/deploy/${script}" --help | grep -q "Usage:" || fail "${script} --help"
done

echo "suite-ops: pass"
