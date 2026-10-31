#!/usr/bin/env bash
set -euo pipefail

TEST_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${TEST_SCRIPT_DIR}/../.." && pwd)"
TEST_TMP_DIR="$(mktemp -d)"
LAST_SCENARIO_DIR=""
MOCK_BIN_DIR=""
MOCK_OFFICIAL_SHASUMS=""
MOCK_OFFICIAL_TARBALL=""
trap 'rm -rf -- "${TEST_TMP_DIR}"' EXIT

link_system_command() {
  local command_name="$1"
  local target
  target="$(command -v "${command_name}")"
  ln -s "${target}" "${MOCK_BIN_DIR}/${command_name}"
}

write_mock_commands() {
  local command_name

  for command_name in awk bash cat chmod cp dirname grep gzip install ln mktemp rm seq sha256sum tar; do
    link_system_command "${command_name}"
  done

  # dnf の retry の待ち（retry_command の sleep）を飛ばす。
  cat > "${MOCK_BIN_DIR}/sleep" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK

  cat > "${MOCK_BIN_DIR}/curl" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
output=""
url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o)
      output="$2"
      shift 2
      ;;
    -*)
      shift
      ;;
    *)
      url="$1"
      shift
      ;;
  esac
done
printf 'curl %s\n' "${url}" >> "${MOCK_CALL_LOG}"

write_response() {
  local source_path="$1"
  if [ -n "${output}" ]; then
    cp "${source_path}" "${output}"
  else
    cat "${source_path}"
  fi
}

case "${url}" in
  */SHASUMS256.txt)
    write_response "${MOCK_OFFICIAL_SHASUMS}"
    ;;
  */node-v24.*.tar.gz)
    write_response "${MOCK_OFFICIAL_TARBALL}"
    ;;
  *)
    echo "unexpected curl URL: ${url}" >&2
    exit 1
    ;;
esac
MOCK

  cat > "${MOCK_BIN_DIR}/uname" <<'MOCK'
#!/usr/bin/env bash
if [ "${1:-}" = "-m" ]; then
  printf '%s\n' "${MOCK_UNAME_MACHINE:-x86_64}"
  exit 0
fi
exit 1
MOCK

  # Oracle Linux の dnf（AppStream の module nodejs:24。#1611）。MOCK_DNF_FAIL の部分の引数で失敗する。
  cat > "${MOCK_BIN_DIR}/dnf" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
printf 'dnf %s\n' "$*" >> "${MOCK_CALL_LOG}"
if [ -n "${MOCK_DNF_FAIL:-}" ] && [[ " $* " == *" ${MOCK_DNF_FAIL} "* ]]; then
  exit 1
fi
if [ "$*" = "-y install nodejs npm" ]; then
  printf '%s\n' "${MOCK_INSTALLED_NODE_VERSION:-v24.13.0}" > "${MOCK_STATE_DIR}/node-version"
  if [ "${MOCK_DNF_INSTALL_CREATES_NPM:-1}" = "1" ]; then
    cat > "${MOCK_BIN_DIR}/npm" <<'NPM'
#!/usr/bin/env bash
printf '%s\n' "${MOCK_NPM_VERSION:-11.6.2}"
NPM
    chmod +x "${MOCK_BIN_DIR}/npm"
  fi
fi
MOCK

  cat > "${MOCK_BIN_DIR}/node" <<'MOCK'
#!/usr/bin/env bash
cat "${MOCK_STATE_DIR}/node-version"
MOCK

  chmod +x \
    "${MOCK_BIN_DIR}/curl" \
    "${MOCK_BIN_DIR}/dnf" \
    "${MOCK_BIN_DIR}/node" \
    "${MOCK_BIN_DIR}/sleep" \
    "${MOCK_BIN_DIR}/uname"
}

create_official_node_fixture() {
  local mode="$1"
  local node_dir="node-v24.19.0-linux-x64"
  local official_dir="${LAST_SCENARIO_DIR}/official-release"
  local tarball_path="${official_dir}/${node_dir}.tar.gz"

  mkdir -p "${official_dir}/${node_dir}/bin"
  cat > "${official_dir}/${node_dir}/bin/node" <<'NODE'
#!/usr/bin/env bash
printf 'v24.19.0\n'
NODE
  if [ "${mode}" != "missing_npm" ]; then
    cat > "${official_dir}/${node_dir}/bin/npm" <<'NPM'
#!/usr/bin/env bash
printf '11.6.2\n'
NPM
  fi
  cat > "${official_dir}/${node_dir}/bin/npx" <<'NPX'
#!/usr/bin/env bash
printf '11.6.2\n'
NPX
  cat > "${official_dir}/${node_dir}/bin/corepack" <<'COREPACK'
#!/usr/bin/env bash
printf '0.34.0\n'
COREPACK
  chmod +x "${official_dir}/${node_dir}/bin/"*
  tar -czf "${tarball_path}" -C "${official_dir}" "${node_dir}"

  MOCK_OFFICIAL_SHASUMS="${official_dir}/SHASUMS256.txt"
  MOCK_OFFICIAL_TARBALL="${tarball_path}"
  case "${mode}" in
    valid | missing_npm)
      (cd "${official_dir}" && sha256sum "${node_dir}.tar.gz" > SHASUMS256.txt)
      ;;
    checksum_mismatch)
      printf '0000000000000000000000000000000000000000000000000000000000000000  %s.tar.gz\n' "${node_dir}" > "${MOCK_OFFICIAL_SHASUMS}"
      ;;
    missing_match)
      (cd "${official_dir}" && sha256sum "${node_dir}.tar.gz" | sed 's/linux-x64/linux-s390x/' > SHASUMS256.txt)
      ;;
    *)
      echo "unknown official fixture mode: ${mode}" >&2
      exit 1
      ;;
  esac
}

run_nodejs_case() {
  local scenario="$1"
  local initial_node_version="$2"
  local installed_node_version="$3"
  local install_creates_npm="$4"
  local entrypoint="$5"
  local dnf_fail="$6"
  local official_mode="$7"
  local machine="$8"

  LAST_SCENARIO_DIR="${TEST_TMP_DIR}/${scenario}"
  MOCK_BIN_DIR="${LAST_SCENARIO_DIR}/bin"
  mkdir -p "${MOCK_BIN_DIR}" "${LAST_SCENARIO_DIR}/state"
  MOCK_STATE_DIR="${LAST_SCENARIO_DIR}/state"
  MOCK_CALL_LOG="${LAST_SCENARIO_DIR}/calls.log"
  MOCK_OFFICIAL_SHASUMS="${LAST_SCENARIO_DIR}/missing-SHASUMS256.txt"
  MOCK_OFFICIAL_TARBALL="${LAST_SCENARIO_DIR}/missing-node.tar.gz"
  : > "${MOCK_CALL_LOG}"
  printf '%s\n' "${initial_node_version}" > "${MOCK_STATE_DIR}/node-version"
  if [ "${official_mode}" != "none" ]; then
    create_official_node_fixture "${official_mode}"
  fi
  write_mock_commands

  (
    export APPLICATION_PORT=80
    export APP_ROOT="${LAST_SCENARIO_DIR}/app-root"
    export NL2SQL_INIT_TEST_MODE=true
    export NODEJS_OFFICIAL_RELEASE_BASE_URL="https://nodejs.test/download/release/latest-v24.x"
    export NODEJS_OFFICIAL_INSTALL_DIR="${LAST_SCENARIO_DIR}/official-install"
    export NODEJS_OFFICIAL_BIN_DIR="${MOCK_BIN_DIR}"
    export MOCK_DNF_FAIL="${dnf_fail}"
    export MOCK_DNF_INSTALL_CREATES_NPM="${install_creates_npm}"
    export MOCK_BIN_DIR
    export MOCK_CALL_LOG
    export MOCK_INSTALLED_NODE_VERSION="${installed_node_version}"
    export MOCK_NPM_VERSION="11.6.2"
    export MOCK_OFFICIAL_SHASUMS
    export MOCK_OFFICIAL_TARBALL
    export MOCK_STATE_DIR
    export MOCK_UNAME_MACHINE="${machine}"

    # init_script prepends system paths for production; reset PATH afterward so
    # tests never see this host's real node/npm/dnf.
    # shellcheck source=/dev/null
    source "${REPO_DIR}/init_script.sh"
    PATH="${MOCK_BIN_DIR}"
    "${entrypoint}"
  )
}

run_nodejs_case "dnf-module-recovers-node18-without-npm" "v18.19.1" "v24.13.0" "1" "install_nodejs" "" "none" "x86_64"
grep -qx 'dnf -y module reset nodejs' "${LAST_SCENARIO_DIR}/calls.log"
grep -qx 'dnf -y module enable nodejs:24' "${LAST_SCENARIO_DIR}/calls.log"
grep -qx 'dnf -y install nodejs npm' "${LAST_SCENARIO_DIR}/calls.log"
test "$(cat "${LAST_SCENARIO_DIR}/state/node-version")" = "v24.13.0"
test -x "${LAST_SCENARIO_DIR}/bin/npm"

if run_nodejs_case "dnf-module-only-rejects-missing-npm" "v18.19.1" "v24.13.0" "0" "install_nodejs_from_dnf_module" "" "none" "x86_64"; then
  echo "AppStream の npm 欠落状態を成功扱いしてしまいました。" >&2
  exit 1
fi
grep -qx 'dnf -y install nodejs npm' "${LAST_SCENARIO_DIR}/calls.log"
test ! -e "${LAST_SCENARIO_DIR}/bin/npm"

run_nodejs_case "fallback-module-enable-fails" "v18.19.1" "v24.13.0" "1" "install_nodejs" "enable" "valid" "x86_64"
test "$(grep -cx 'dnf -y module enable nodejs:24' "${LAST_SCENARIO_DIR}/calls.log")" -eq 5
if grep -q 'dnf -y install nodejs' "${LAST_SCENARIO_DIR}/calls.log"; then
  echo "module enable の失敗後に dnf install が実行されました。" >&2
  exit 1
fi
test "$("${LAST_SCENARIO_DIR}/bin/node" --version)" = "v24.19.0"
test "$("${LAST_SCENARIO_DIR}/bin/npm" --version)" = "11.6.2"

run_nodejs_case "fallback-non-node24-from-dnf" "v18.19.1" "v20.19.5" "1" "install_nodejs" "" "valid" "x86_64"
grep -qx 'dnf -y install nodejs npm' "${LAST_SCENARIO_DIR}/calls.log"
test "$("${LAST_SCENARIO_DIR}/bin/node" --version)" = "v24.19.0"
test "$("${LAST_SCENARIO_DIR}/bin/npm" --version)" = "11.6.2"

run_nodejs_case "fallback-after-dnf-validation-fails" "v18.19.1" "v24.13.0" "0" "install_nodejs" "" "valid" "x86_64"
grep -qx 'dnf -y install nodejs npm' "${LAST_SCENARIO_DIR}/calls.log"
test "$("${LAST_SCENARIO_DIR}/bin/node" --version)" = "v24.19.0"
test "$("${LAST_SCENARIO_DIR}/bin/npm" --version)" = "11.6.2"

if run_nodejs_case "rejects-unsupported-official-arch" "v18.19.1" "v24.13.0" "1" "install_nodejs" "enable" "valid" "riscv64"; then
  echo "unsupported architecture を成功扱いしてしまいました。" >&2
  exit 1
fi

if run_nodejs_case "rejects-checksum-mismatch" "v18.19.1" "v24.13.0" "1" "install_nodejs" "enable" "checksum_mismatch" "x86_64"; then
  echo "checksum mismatch を成功扱いしてしまいました。" >&2
  exit 1
fi

if run_nodejs_case "rejects-missing-matching-tarball" "v18.19.1" "v24.13.0" "1" "install_nodejs" "enable" "missing_match" "x86_64"; then
  echo "matching tarball 欠落を成功扱いしてしまいました。" >&2
  exit 1
fi

if run_nodejs_case "rejects-official-missing-npm" "v18.19.1" "v24.13.0" "1" "install_nodejs" "enable" "missing_npm" "x86_64"; then
  echo "official tarball npm 欠落状態を成功扱いしてしまいました。" >&2
  exit 1
fi

run_data_dir_migration_case() {
  local scenario="$1"
  local case_dir="${TEST_TMP_DIR}/${scenario}"
  local source_script="${REPO_DIR}/init_script.sh"

  mkdir -p "${case_dir}"
  (
    export APP_USER
    export APPLICATION_PORT=80
    export NL2SQL_INIT_TEST_MODE=true
    APP_USER="$(id -un)"
    # shellcheck source=/dev/null
    source "${source_script}"
    DATA_DIR="${case_dir}/data/production-ready-nl2sql"
    LEGACY_DATA_DIR="${case_dir}/production-ready-nl2sql"
    mkdir -p "$(dirname "${DATA_DIR}")"

    case "${scenario}" in
      data-dir-legacy-missing)
        install -d -m 0755 "${DATA_DIR}"
        migrate_legacy_data_dir
        test -d "${DATA_DIR}"
        test -L "${LEGACY_DATA_DIR}"
        test "$(readlink "${LEGACY_DATA_DIR}")" = "${DATA_DIR}"
        ;;
      data-dir-legacy-copied)
        install -d -m 0755 "${DATA_DIR}" "${LEGACY_DATA_DIR}/nested"
        printf 'legacy-data\n' > "${LEGACY_DATA_DIR}/nested/source.txt"
        migrate_legacy_data_dir
        test "$(cat "${DATA_DIR}/nested/source.txt")" = "legacy-data"
        test -L "${LEGACY_DATA_DIR}"
        test "$(readlink "${LEGACY_DATA_DIR}")" = "${DATA_DIR}"
        local backup_dir
        backup_dir="$(
          find "$(dirname "${LEGACY_DATA_DIR}")" \
            -maxdepth 1 \
            -type d \
            -name "$(basename "${LEGACY_DATA_DIR}").legacy-*" \
            -print \
            -quit
        )"
        test -n "${backup_dir}"
        test "$(cat "${backup_dir}/nested/source.txt")" = "legacy-data"
        ;;
      data-dir-both-populated)
        install -d -m 0755 "${DATA_DIR}" "${LEGACY_DATA_DIR}"
        printf 'new-data\n' > "${DATA_DIR}/current.txt"
        printf 'legacy-data\n' > "${LEGACY_DATA_DIR}/source.txt"
        local output
        output="$(migrate_legacy_data_dir)"
        printf '%s\n' "${output}" | grep -q 'skipping legacy migration to avoid overwrite'
        test ! -L "${LEGACY_DATA_DIR}"
        test "$(cat "${DATA_DIR}/current.txt")" = "new-data"
        test "$(cat "${LEGACY_DATA_DIR}/source.txt")" = "legacy-data"
        ;;
      *)
        echo "unknown data directory migration scenario: ${scenario}" >&2
        exit 1
        ;;
    esac
  )
}

run_data_dir_migration_case "data-dir-legacy-missing"
run_data_dir_migration_case "data-dir-legacy-copied"
run_data_dir_migration_case "data-dir-both-populated"

echo "init_script Node.js 24 install and data directory migration test: ok"
