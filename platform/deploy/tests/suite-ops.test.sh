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

# 2. suite-restart.sh --no-build: 有効な production-ready-* の service を再起動し、配備全体（service・backend の
#    health）を確かめる。reverse proxy（Nginx）は配備しないので再起動しない（#19）。
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
        "production-ready-agent-backend.service enabled enabled"
      return 0
    fi
    printf 'systemctl %s\n' "$*" >> "${calls}"
  }
  nginx() { printf 'nginx %s\n' "$*" >> "${calls}"; }
  suite_restart_services >/dev/null
  grep -qxF "systemctl restart production-ready-rag-backend.service production-ready-rag-parser-docling.service production-ready-agent-backend.service" "${calls}" \
    || fail "再起動の対象: $(cat "${calls}")"
  if grep -q "nginx" "${calls}"; then fail "Nginx を扱った（#19）: $(cat "${calls}")"; fi

  [ "$(suite_deployed_products | tr '\n' ' ')" = "rag agent " ] || fail "配備した製品: $(suite_deployed_products | tr '\n' ' ')"
  # 再起動の後の確認は suite-init.sh の最後と同じ suite_check_deployment（#1642。suite-health.test.sh が詳しく確かめる）。
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
  curl() { case "$*" in *":8000/api/health"*) return 0 ;; *) return 7 ;; esac; }
  if suite_check_deployed >/dev/null; then fail "Agent が応答しないのに成功した"; fi
  curl() { printf '%s\n' "$*" >> "${calls}"; }
  : > "${calls}"
  check_out="$(suite_check_deployed)" || fail "すべて応答するのに失敗した"
  printf '%s\n' "${check_out}" | grep -qxF '[suite-restart] check: agent backend OK / services 1/1 active' \
    || fail "確認の要約: ${check_out}"
  grep -qF "http://127.0.0.1:8020/api/health" "${calls}" || fail "backend の health を確かめない: $(cat "${calls}")"
  if grep -qE "127\.0\.0\.1:(80|443)/" "${calls}"; then fail "reverse proxy を通した health を確かめた: $(cat "${calls}")"; fi
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
grep -qxF '  suite_pull' "${repo}/platform/deploy/suite-update.sh" || fail "suite-update.sh が取得しない"
grep -qxF '  exec bash "${SUITE_RESTART_SCRIPT}"' "${repo}/platform/deploy/suite-update.sh" \
  || fail "suite-update.sh が取得の後に suite-restart.sh（build して再起動）を実行しない"

# 5. DB の起動・停止は扱わない（#10。DB は ADB Free コンテナで、起動・停止は docker 側の責務）。build・--no-build・
#    --status は ADB の状態を確かめずに、それぞれの処理と確認だけを行う。
(
  export APP_ROOT="${task_dir}/no-adb"
  export SUITE_REPO_DIR="${APP_ROOT}/no.1-production-ready-suite"
  export PROPS_DIR="${APP_ROOT}/props"
  export SUITE_OPS_INHERITED=true
  mkdir -p "${PROPS_DIR}"
  printf 'nl2sql agent\n' > "${PROPS_DIR}/products.txt"
  # shellcheck source=../suite-restart.sh
  source "${repo}/platform/deploy/suite-restart.sh"
  calls="${task_dir}/no-adb-calls.log"
  id() { printf '0\n'; }
  suite_rebuild_and_restart() { printf 'rebuild\n' >> "${calls}"; }
  suite_restart_services() { printf 'restart-services\n' >> "${calls}"; }
  suite_check_deployed() { printf 'check\n' >> "${calls}"; }

  : > "${calls}"
  suite_restart_main >/dev/null || fail "build が失敗した"
  [ "$(tr '\n' ' ' < "${calls}")" = "rebuild check " ] || fail "build の順: $(cat "${calls}")"
  : > "${calls}"
  suite_restart_main --no-build >/dev/null || fail "--no-build が失敗した"
  [ "$(tr '\n' ' ' < "${calls}")" = "restart-services check " ] || fail "--no-build の順: $(cat "${calls}")"
  : > "${calls}"
  suite_restart_main --status >/dev/null || fail "--status が失敗した"
  [ "$(tr '\n' ' ' < "${calls}")" = "check " ] || fail "--status の順: $(cat "${calls}")"
  # build・確認の失敗は失敗にする。
  suite_check_deployed() { printf 'check\n' >> "${calls}"; return 1; }
  if suite_restart_main >/dev/null 2>&1; then fail "確認の失敗を成功にした"; fi
  ! grep -q "suite-adb\|SUITE_ADB" "${repo}/platform/deploy/suite-restart.sh" "${repo}/platform/deploy/suite-update.sh" \
    || fail "ADB の起動・停止の処理が残っている"
)

for script in suite-pull.sh suite-restart.sh suite-update.sh; do
  [ -x "${repo}/platform/deploy/${script}" ] || fail "${script} に実行権限が無い"
  bash "${repo}/platform/deploy/${script}" --help | grep -q "Usage:" || fail "${script} --help"
done

echo "suite-ops: pass"
