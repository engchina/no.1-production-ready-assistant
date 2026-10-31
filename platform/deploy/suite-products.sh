#!/usr/bin/env bash
# 1 台に置く 3 製品の並びと backend の port（#1316・#19）。suite-init.sh・suite-restart.sh と test が source する。
# 以前は Nginx の site の生成（suite-nginx.sh）と同じファイルにあった。Nginx の配備は #19 で削除した
# （前段に reverse proxy を置くかは利用者が決める）。

# 各製品の backend の port（127.0.0.1 だけで listen する）。ローカルの開発の port と同じ（#1316 で NL2SQL を 8000 → 8010）。
# 各製品の init_script.sh の BACKEND_PORT と同じ値にする（verify_stack_contract.py が照合する）。
# 環境変数での上書きは test が隔離した port を使うためのもので、本番では変えない。
SUITE_RAG_BACKEND_PORT="${SUITE_RAG_BACKEND_PORT:-8000}"
SUITE_NL2SQL_BACKEND_PORT="${SUITE_NL2SQL_BACKEND_PORT:-8010}"
SUITE_AGENT_BACKEND_PORT="${SUITE_AGENT_BACKEND_PORT:-8020}"
# 1 台に置く製品の並び（配備と確認の順）。
# shellcheck disable=SC2034 # source する suite-init.sh・suite-restart.sh が使う
SUITE_PRODUCTS_ORDER=(rag nl2sql agent)

suite_backend_port() {
  case "$1" in
    rag) printf '%s\n' "${SUITE_RAG_BACKEND_PORT}" ;;
    nl2sql) printf '%s\n' "${SUITE_NL2SQL_BACKEND_PORT}" ;;
    agent) printf '%s\n' "${SUITE_AGENT_BACKEND_PORT}" ;;
    *) return 1 ;;
  esac
}

suite_contains() {
  local needle="$1"
  shift
  local value
  for value in "$@"; do
    [ "${value}" = "${needle}" ] && return 0
  done
  return 1
}
