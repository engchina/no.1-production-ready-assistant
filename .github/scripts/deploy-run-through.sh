#!/usr/bin/env bash
# 配備の通し（Deploy Nightly。deploy-nightly.yml。#1595）。本番の Compute と同じ Oracle Linux 9 の container
# （systemd を PID 1 にした privileged の container）で、cloud-init と同じ props を置いて
# platform/deploy/suite-init.sh を実際に実行し、3 製品の配備（dnf・Node.js・uv の venv・RAG の前処理 / parser・
# frontend の build・systemd の unit・自作 CA の HTTPS・Nginx）と、Nginx を通した /rag/ /nl2sql/ /agent/ を確かめる。
#
# 本番との違い（この検査の範囲の外）:
#   - Oracle は無い（DSN は誰も listen しない 127.0.0.1 の port）。各製品の system schema の初期化は失敗し、
#     init_script.sh の degraded（web は動かし、DB を使う worker は止めたまま）で進む。DB の結合は
#     Integration Nightly（oracle-integration.yml）が確かめる。
#   - suite-init.sh の最後の配備の確認（platform/deploy/suite-health.sh の suite_check_deployment）の system schema
#     の確認（#1648）は、SUITE_CHECK_SCHEMA=skip で飛ばす（#1656。DB が無いので ready にならない）。schema が ready か
#     は Integration Nightly（実 Oracle）で確かめる。代わりに verify で、各製品の schema の確認の CLI が配備した venv
#     で動き、「DB に繋がらない（SCHEMA_STATUS_UNAVAILABLE と接続の失敗のコード）」を返す（ほかの理由で失敗しない）
#     ことを確かめる（判定は schema_summary_is_db_unreachable。#1768）。
#   - SELinux は container では無効（suite-init.sh の configure_selinux は「disabled」で飛ばす）。enforcing の
#     context・boolean は対象外で、実機（OCI）で確かめる。
#   - firewalld は container の中で動かす（privileged）。OCI の security list・公開 IP は無い（assign_public_ip=false）。
#   - repository は git clone ではなく git archive で置く（.git は無い。git-pull.sh 等の更新の経路は対象外）。
#
# 使い方: bash .github/scripts/deploy-run-through.sh <up|deploy|verify|logs|down>
#   （source したときは関数を定義するだけで何も実行しない。platform/deploy/tests/deploy-run-through.test.sh が使う）
#   up      container の image を作り、起動し、repository と props を置く
#   deploy  suite-init.sh を実行する
#   verify  Nginx を通して 3 製品の画面・API の health・CA の証明書と、schema の確認の CLI を確かめる
#   logs    診断（各 init の log・失敗した unit の journal）を出す
#   down    container を消す
# 環境変数: DOCKER（既定 docker。手元は podman）・DEPLOY_CONTAINER（既定 pr-deploy-ol9）・
#   DEPLOY_HTTPS_PORT（host に出す port。既定 8443）・DEPLOY_IMAGE_BASE（既定 oraclelinux:9）
set -euo pipefail

docker_cmd="${DOCKER:-docker}"
container="${DEPLOY_CONTAINER:-pr-deploy-ol9}"
image="pr-deploy-ol9-systemd"
base_image="${DEPLOY_IMAGE_BASE:-oraclelinux:9}"
host_port="${DEPLOY_HTTPS_PORT:-8443}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
app_root="/u01/aipoc"
repo_dir="${app_root}/no.1-production-ready-suite"
props="${app_root}/props"
products="rag nl2sql agent"

log() {
  printf '[deploy-run-through] %s\n' "$*"
}

# schema の確認の CLI が「DB に繋がらないことだけ」で失敗したか（#1768）。引数は終了コードと要約（標準出力の最後の
# 行）。要約は pr_system_settings.system_schema.deployment_check_error_summary が DB に接続できない失敗に返す
# 「状態を読めません（SCHEMA_STATUS_UNAVAILABLE: <ORA / DPY のコード>: <理由と対処>）」の形だけを認める。
# 機械の判定は印（SCHEMA_STATUS_UNAVAILABLE）と接続の失敗のコードで行い、日本語の理由・対処の文には依存しない。
# 終了 0・時間切れ（124）・要約が無い・venv / module が無い・import の失敗や設定の不備（印に続けて例外の型の名前）・
# ORA の失敗（権限の不足など。印が無い）は、DB に繋がらないこと以外の失敗なので偽を返す。
schema_summary_is_db_unreachable() {
  local status="$1" summary="$2"
  [ "${status}" -ne 0 ] && [ "${status}" -ne 124 ] || return 1
  printf '%s\n' "${summary}" | grep -qE '^状態を読めません（SCHEMA_STATUS_UNAVAILABLE: (ORA|DPY)-[0-9]{4,5}: [^）]'
}

in_container() {
  "${docker_cmd}" exec -i "${container}" bash -euo pipefail -c "$1"
}

# cloud-init（terraform/stack/cloud_init/bootstrap.template.yaml）が入れる package と、OCI の image にある
# opc のユーザー・sudo・systemd を入れた image。
build_image() {
  "${docker_cmd}" build -t "${image}" -f - . <<EOF
FROM ${base_image}
RUN dnf -y install systemd procps-ng sudo hostname iproute ca-certificates dos2unix firewalld git openssl \\
      policycoreutils-python-utils tar tmux unzip \\
    && dnf clean all \\
    && useradd --create-home opc \\
    && echo 'opc ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/90-opc
STOPSIGNAL SIGRTMIN+3
CMD ["/usr/sbin/init"]
EOF
}

# terraform/stack/locals.tf の platform_env・<製品>_backend_env と同じ鍵（値は CI 用）。
write_props() {
  local admin_password secret
  admin_password="Ci$(openssl rand -hex 8)_Aa1"
  secret="$(openssl rand -hex 32)"
  echo "::add-mask::${admin_password}"
  echo "::add-mask::${secret}"
  in_container "install -d -m 0755 ${props} /u01/aipoc/wallet"
  "${docker_cmd}" exec -i "${container}" bash -c "umask 077; cat > ${props}/platform.env" <<EOF
PLATFORM_OCI_REGION=us-chicago-1
PLATFORM_OCI_COMPARTMENT_ID=

PLATFORM_ORACLE_USER='ADMIN'
PLATFORM_ORACLE_PASSWORD='${admin_password}'
PLATFORM_ORACLE_DSN='127.0.0.1:1599/NOT_DEPLOYED'
PLATFORM_ORACLE_DRIVER_MODE=thin
PLATFORM_ORACLE_CONNECTION_SECURITY=walletless_tls
PLATFORM_ORACLE_CLIENT_LIB_DIR=
PLATFORM_ORACLE_WALLET_DIR=/u01/aipoc/wallet
PLATFORM_ORACLE_WALLET_PASSWORD=''
PLATFORM_ORACLE_ADB_OCID=
PLATFORM_ORACLE_ADB_REGION=us-chicago-1

PLATFORM_UPLOAD_STORAGE_BACKEND=local
PLATFORM_OBJECT_STORAGE_REGION=us-chicago-1
PLATFORM_OBJECT_STORAGE_BUCKET=production-ready
PLATFORM_OBJECT_STORAGE_NAMESPACE=

PLATFORM_ADMIN_LOGIN_USER_ID=system_admin
PLATFORM_ADMIN_LOGIN_USER_PASSWORD=${admin_password}

PLATFORM_SERVICE_TOKEN_SECRET=${secret}

PLATFORM_AUTH_COOKIE_SECURE=true
PLATFORM_ENVIRONMENT=staging
PLATFORM_AUTH_MODE=authenticated
PLATFORM_AUTH_SESSION_COOKIE_NAME=pr_session
PLATFORM_AUTH_CSRF_COOKIE_NAME=pr_csrf
EOF
  "${docker_cmd}" exec -i "${container}" bash -c "umask 077; cat > ${props}/rag.backend.env" <<'EOF'
RAG_APP_VERSION=0.1.0
RAG_LOG_LEVEL=INFO

RAG_AUDIT_CONTEXT_HASH_SALT=

RAG_PARSER_ADAPTER_BACKEND=docling
RAG_PARSER_DOCLING_ENABLED=true
RAG_PARSER_UNSTRUCTURED_ENABLED='false'
RAG_SERVICE_CONTROL_ENABLED=true
EOF
  "${docker_cmd}" exec -i "${container}" bash -c "umask 077; cat > ${props}/nl2sql.backend.env" <<'EOF'
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
NL2SQL_SELECT_AI_CREDENTIAL_NAME=NL2SQL_OCI_CRED
NL2SQL_SELECT_AI_REGION=us-chicago-1
EOF
  "${docker_cmd}" exec -i "${container}" bash -c "umask 077; cat > ${props}/agent.backend.env" <<'EOF'
AGENT_APP_VERSION=0.1.0
AGENT_LOG_LEVEL=INFO
AGENT_SERVICE_NAME=production-ready-agent
AGENT_CORS_ORIGINS=["http://localhost","http://127.0.0.1"]


AGENT_RUNTIME_REPOSITORY_BACKEND=oracle_checkpoint
AGENT_RUNTIME_DISPATCH_MODE=in_process
AGENT_ARTIFACT_STORAGE_PATH=/u01/data/production-ready-agent/artifacts

AGENT_EXTERNAL_RAG_MCP_URL=http://127.0.0.1:8000/api/mcp
AGENT_EXTERNAL_NL2SQL_MCP_URL=http://127.0.0.1:8010/api/mcp
AGENT_EXTERNAL_RAG_PUBLIC_URL=/rag
EOF
  # terraform/stack/locals.tf の rag_services（既定の入力。parser-unstructured・OCI の parser は入れない）。
  in_container "
    printf '%s\n' '${products}' > ${props}/products.txt
    printf '%s\n' true > ${props}/https_enabled.txt
    printf '%s\n' 80 > ${props}/http_port.txt
    printf '%s\n' 443 > ${props}/https_port.txt
    printf '%s\n' false > ${props}/assign_public_ip.txt
    printf '%s\n' 'preprocess-office-to-pdf preprocess-csv-to-jsonl preprocess-excel-to-jsonl preprocess-word-to-jsonl parser-docling parser-structured-records-jsonl parser-xml-tree-json' > ${props}/rag_services.txt
  "
  # walletless_tls でも RAG の init_script.sh は wallet.zip を展開する（中身は使わない。空の zip は unzip が失敗する）。
  # zip は host の python3 で作る（container の image に python3 が無いことがある）。
  python3 -c 'import io, sys, zipfile
buffer = io.BytesIO()
with zipfile.ZipFile(buffer, "w") as archive:
    archive.writestr("README.txt", "CI deploy run-through (no Oracle)\n")
sys.stdout.buffer.write(buffer.getvalue())' \
    | "${docker_cmd}" exec -i "${container}" bash -c "umask 077; cat > ${props}/wallet.zip"
}

up() {
  build_image
  "${docker_cmd}" rm -f "${container}" >/dev/null 2>&1 || true
  local systemd_args=(--cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock)
  if [ "$(basename "${docker_cmd}")" = "podman" ]; then
    # podman は systemd の container の mount を自分で用意する（手元の確認用）。
    systemd_args=(--systemd=always)
  fi
  "${docker_cmd}" run -d --name "${container}" --privileged "${systemd_args[@]}" \
    -p "127.0.0.1:${host_port}:443" "${image}" >/dev/null
  # systemd が起動し終わるのを待つ（degraded は、container で動かない unit があるため許す）。
  local state=""
  for _ in $(seq 1 60); do
    state="$("${docker_cmd}" exec "${container}" systemctl is-system-running 2>/dev/null || true)"
    case "${state}" in
      running | degraded) break ;;
    esac
    sleep 2
  done
  log "systemd: ${state:-unknown}"
  case "${state}" in
    running | degraded) ;;
    *) log "systemd が起動しません。"; exit 1 ;;
  esac
  in_container "install -d -m 0755 ${repo_dir}"
  # DEPLOY_SOURCE_TAR（git archive の tar）があればそれを、無ければ HEAD を置く。
  if [ -n "${DEPLOY_SOURCE_TAR:-}" ]; then
    "${docker_cmd}" exec -i "${container}" tar -x -C "${repo_dir}" < "${DEPLOY_SOURCE_TAR}"
  else
    git -C "${repo_root}" archive --format=tar HEAD | "${docker_cmd}" exec -i "${container}" tar -x -C "${repo_dir}"
  fi
  write_props
  log "repository と props を置きました（products: ${products}）。"
}

deploy() {
  local start="${SECONDS}" status=0
  "${docker_cmd}" exec "${container}" bash -c \
    "APP_ROOT=${app_root} SUITE_CHECK_SCHEMA=skip bash ${repo_dir}/platform/deploy/suite-init.sh" || status=$?
  log "suite-init.sh: 終了 ${status}（$((SECONDS - start)) 秒）。"
  return "${status}"
}

# Nginx を通して確かめる（container の中から 127.0.0.1:443 と、host に出した port の両方）。
verify() {
  local failures=0 product url code
  check() {
    local name="$1" url="$2" expect="$3" where="$4"
    if [ "${where}" = "container" ]; then
      code="$("${docker_cmd}" exec "${container}" curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "${url}" || true)"
    else
      code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "${url}" || true)"
    fi
    if [ "${code}" = "${expect}" ]; then
      log "OK ${name}: ${url} → ${code}"
    else
      log "NG ${name}: ${url} → ${code}（期待 ${expect}）"
      failures=$((failures + 1))
    fi
  }
  for product in ${products}; do
    check "${product} の画面" "https://127.0.0.1/${product}/" 200 container
    check "${product} の API の health" "https://127.0.0.1/${product}/api/health" 200 container
    check "${product} の画面（host から）" "https://127.0.0.1:${host_port}/${product}/" 200 host
  done
  # system schema の確認の CLI（suite-init.sh では SUITE_CHECK_SCHEMA=skip で飛ばした。#1656）: 本番と同じ
  # suite_run_schema_check（製品のユーザー・配備した venv）で実行し、DB に繋がらないこと（SCHEMA_STATUS_UNAVAILABLE と
  # 接続の失敗のコード）だけで失敗することを確かめる（module が無い・import の失敗・要約が無いなどは NG。
  # schema_summary_is_db_unreachable。#1768）。
  local output status summary
  for product in ${products}; do
    status=0
    output="$("${docker_cmd}" exec "${container}" bash -c \
      "SUITE_REPO_DIR=${repo_dir} && source ${repo_dir}/platform/deploy/suite-health.sh && suite_run_schema_check ${product}")" \
      || status=$?
    summary="$(printf '%s\n' "${output}" | sed '/^[[:space:]]*$/d' | tail -n 1)"
    if schema_summary_is_db_unreachable "${status}" "${summary}"; then
      log "OK ${product} の schema の確認の CLI: DB に繋がらない（${summary}）"
    else
      log "NG ${product} の schema の確認の CLI: 終了 ${status}・${summary:-要約なし}（期待: 非 0 で「状態を読めません（SCHEMA_STATUS_UNAVAILABLE: <ORA / DPY のコード>: …）」）"
      failures=$((failures + 1))
    fi
  done
  # 入口（/）は転送する。CA の証明書は配る。HTTPS が on のときは port 80 で listen しない（#1638）。
  check "入口の転送" "https://127.0.0.1/" 302 container
  check "CA の証明書" "https://127.0.0.1/platform/ca.crt" 200 container
  code="$("${docker_cmd}" exec "${container}" curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1/ || true)"
  if [ "${code}" = "000" ]; then
    log "OK port 80 は listen しない"
  else
    log "NG port 80 が応答しました（${code}）"
    failures=$((failures + 1))
  fi
  # 自作の CA で、Compute の private IP（サーバー証明書の SAN）のサーバー証明書を検証できる（-k なし）。
  local private_ip
  private_ip="$("${docker_cmd}" exec "${container}" bash -c \
    "ip -4 route get 1.1.1.1 | awk '{for (i = 1; i <= NF; i++) if (\$i == \"src\") {print \$(i + 1); exit}}'" || true)"
  if [ -n "${private_ip}" ] && "${docker_cmd}" exec "${container}" curl -sS -o /dev/null --max-time 10 \
    --cacert /u01/aipoc/ssl/ca.crt "https://${private_ip}/agent/"; then
    log "OK 自作の CA でサーバー証明書を検証できる"
  else
    log "NG 自作の CA でサーバー証明書を検証できない"
    failures=$((failures + 1))
  fi
  [ "${failures}" -eq 0 ] || { log "${failures} 件の確認が失敗しました。"; return 1; }
  log "すべての確認が成功しました。"
}

logs() {
  # shellcheck disable=SC2016 # container の中の bash で展開する
  "${docker_cmd}" exec "${container}" bash -c '
    for f in /var/log/suite-init.log /var/log/rag-init.log /var/log/nl2sql-init.log /var/log/agent-init.log; do
      [ -f "$f" ] || continue
      echo "::group::$f（末尾 200 行）"; tail -n 200 "$f"; echo "::endgroup::"
    done
    echo "::group::失敗した unit"; systemctl --failed --no-pager || true; echo "::endgroup::"
    for unit in $(systemctl list-units --failed --plain --no-legend "production-ready-*" nginx.service 2>/dev/null | awk "{print \$1}"); do
      echo "::group::journal: $unit"; journalctl -u "$unit" -n 80 --no-pager || true; echo "::endgroup::"
    done
    echo "::group::disk"; df -h / || true; echo "::endgroup::"
  ' || true
}

down() {
  "${docker_cmd}" rm -f "${container}" >/dev/null 2>&1 || true
}

# source したとき（テスト）は関数の定義だけ。
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  return 0
fi

case "${1:-}" in
  up) up ;;
  deploy) deploy ;;
  verify) verify ;;
  logs) logs ;;
  down) down ;;
  *) echo "使い方: $0 <up|deploy|verify|logs|down>" >&2; exit 2 ;;
esac
