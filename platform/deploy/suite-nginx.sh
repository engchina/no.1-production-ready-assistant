#!/usr/bin/env bash
# 1 台の Compute に置いた 3 製品の Nginx の site を生成する（#1316）。suite-init.sh と test が source する。
#
#   /rag/ /nl2sql/ /agent/   各製品の frontend（/<製品>/ を base に build した dist）
#   /<製品>/api/...           prefix を外して、各製品の backend（127.0.0.1 の別々の port）へ proxy
#   /<製品>/health            backend の /api/health
#   /platform/ca.crt          platform（共通基盤）が配る自作の CA 証明書（HTTPS が on のときだけ）
#   /platform/...             platform の共有の内容のために予約する（今は ca.crt 以外は 404）
#   /                         /agent/ へ 302（Agent を配備しないときは、最初に配備した製品へ）
#
# HTTPS が on のとき: https_port（既定 443）で TLS 1.2 / 1.3 だけを受ける。http_port（既定 80）は listen しない
# （HTTP から HTTPS への転送の server も置かない。#1638）。HSTS は付けない（IP の証明書のため）。
# HTTPS が off のとき: http_port で HTTP をそのまま配信する。port は stack の変数（#1316）。
# backend へは $request_uri から prefix だけを外した URI をそのまま渡す（%2F などのエンコードを変えない）。

# 各製品の backend の port（127.0.0.1 だけで listen する）。ローカルの開発の port と同じ（#1316 で NL2SQL を 8000 → 8010）。
# 各製品の init_script.sh の BACKEND_PORT と同じ値にする（verify_stack_contract.py が照合する）。
# 環境変数での上書きは test が隔離した port を使うためのもので、本番では変えない。
SUITE_RAG_BACKEND_PORT="${SUITE_RAG_BACKEND_PORT:-8000}"
SUITE_NL2SQL_BACKEND_PORT="${SUITE_NL2SQL_BACKEND_PORT:-8010}"
SUITE_AGENT_BACKEND_PORT="${SUITE_AGENT_BACKEND_PORT:-8020}"
# 1 台に置く製品の並び（Nginx の location の順。/ の転送先は agent を優先する）。
SUITE_PRODUCTS_ORDER=(rag nl2sql agent)

# 製品の API の本文の上限（client_max_body_size）。各製品の backend の <製品>_MAX_UPLOAD_BYTES の既定値
# （RAG・NL2SQL 200 MiB、Agent 100 MiB）+ multipart の余白 10 MiB（#306・#1597）。suite-init.sh が
# backend/.env の値から作り直して渡す。verify_stack_contract.py が backend の既定値と照合する。
#   RAG: 文書のアップロード（1 ファイル。複数のファイルは画面が 1 要求の合計を上限以内に分けて送る。#280）
#   NL2SQL: オントロジーの構築の資料（1 要求の合計）
#   Agent: 共通のアップロードの上限（Wallet の zip 20 MiB・評価セットの Excel 2 MiB と、上限の無い
#   スナップショットの import・MCP の本文の受け口）
SUITE_RAG_CLIENT_MAX_BODY_SIZE="${SUITE_RAG_CLIENT_MAX_BODY_SIZE:-210M}"
SUITE_NL2SQL_CLIENT_MAX_BODY_SIZE="${SUITE_NL2SQL_CLIENT_MAX_BODY_SIZE:-210M}"
SUITE_AGENT_CLIENT_MAX_BODY_SIZE="${SUITE_AGENT_CLIENT_MAX_BODY_SIZE:-110M}"

# backend へ転送する API の待ち時間（秒。proxy_read_timeout / proxy_send_timeout）。値の根拠（#1597）:
# - 既定（全 API）600 秒: SSE は 10〜15 秒ごとに heartbeat を送る（共通の進み具合の SSE
#   pr_backend_core.chat_progress・RAG のチャットの SSE・Agent の Run の SSE）ので、間が空かない。
# - RAG の LLM を呼ぶ同期の API 660 秒: 回答の通しの上限 rag_answer_timeout_seconds（既定・上限 600 秒）と
#   画面の待ち時間 630 秒より長く。RAG 検索のストリーム（search/stream）は heartbeat が無く、LLM 1 回の間
#   （最大 600 秒）何も届かない。
# - NL2SQL の LLM を呼ぶ同期の API 2460 秒: LLM 1 回の timeout 600 秒 × 試行 4 回
#   （oci_enterprise_ai_max_retries 3。timeout も再試行する）+ 余白 60 秒。SQL から質問へ（reverse/deep）の
#   通しの期限 1800 秒もこの中。画面は 65 分まで待つ（API_TIMEOUT_MS.longRunningJob）。
# - Agent のツールの直接の呼び出し（tools/invoke）960 秒: 外部 MCP の timeout の上限 900 秒
#   （_MCP_TIMEOUT_MAX_SECONDS。RAG の既定は 630 秒）+ 余白 60 秒。
SUITE_PROXY_TIMEOUT_SECONDS=600
# shellcheck disable=SC2034 # suite_long_api_regex / suite_long_api_timeout が製品の名前から参照する
SUITE_RAG_LONG_API_TIMEOUT_SECONDS=660
# shellcheck disable=SC2034 # suite_long_api_regex / suite_long_api_timeout が製品の名前から参照する
SUITE_NL2SQL_LONG_API_TIMEOUT_SECONDS=2460
# shellcheck disable=SC2034 # suite_long_api_regex / suite_long_api_timeout が製品の名前から参照する
SUITE_AGENT_LONG_API_TIMEOUT_SECONDS=960
# 長い待ち時間にする API（/<製品>/api/ の後ろの path の正規表現。完全一致）。
# RAG: チャットと RAG 検索の回答生成（#375）・作成中の回答の再購読（#1175）・
# 品質評価の同期の API（#383）・業務ガイドの下書きを試す（#1288）と、同じ回答生成を呼ぶ MCP（rag_search）。
# shellcheck disable=SC2034 # suite_long_api_regex / suite_long_api_timeout が製品の名前から参照する
SUITE_RAG_LONG_API_PATHS=(
  'search'
  'search/stream'
  'search-answer-profiles/[^/]+/support-guides/[^/]+/try'
  'evaluation/run'
  'chat/conversations/[^/]+/messages/stream'
  'chat/conversations/[^/]+/messages/[^/]+/stream'
  'mcp'
)
# NL2SQL: LLM を同期で呼ぶ API（SQL の分析・SQL から質問へ・コメント / 注釈 / ドメインの SQL の生成・
# DB 管理の失敗の分析と結合条件の抽出・DB 管理チャットの SQL の生成と失敗の分析（#2264））。
# SQL の生成と実行は job（POST /nl2sql/jobs と進み具合の SSE）。
# shellcheck disable=SC2034 # suite_long_api_regex / suite_long_api_timeout が製品の名前から参照する
SUITE_NL2SQL_LONG_API_PATHS=(
  'nl2sql/analyze'
  'nl2sql/reverse/deep'
  'nl2sql/reverse/sql'
  'nl2sql/reverse/question-sql'
  'nl2sql/comments/suggest'
  'nl2sql/comments/generate-sql'
  'nl2sql/annotations/generate-sql'
  'nl2sql/domains/generate-sql'
  'nl2sql/db-admin/analyze-error'
  'nl2sql/db-admin/extract-join-where'
  'nl2sql/db-admin-chat/generate'
  'nl2sql/db-admin-chat/conversations/[^/]+/turns/[^/]+/analyze-error'
)
# Agent: ツールを同期で呼ぶ API（RAG の rag_search などの外部 MCP を待つ）。Run は非同期で、進み具合は SSE。
# shellcheck disable=SC2034 # suite_long_api_regex / suite_long_api_timeout が製品の名前から参照する
SUITE_AGENT_LONG_API_PATHS=(
  'tools/invoke'
)

# 公開の port（http_port / https_port）に使えない port（#1316）。Terraform の locals.tf の reserved_ports と
# reserved_port_ranges と同じ値にする（verify_stack_contract.py が照合する。手で実行したときも先に失敗させる）。
#   22: SSH / 25: SMTP / 53: DNS / 111: rpcbind / 1521・1522: Oracle の listener（ADB の接続と紛らわしい）/
#   3306: MySQL / 5432: PostgreSQL / 6379: Redis / 9090: Prometheus など運用の道具 /
#   3000・3001・3002: 各製品の frontend の開発・プレビュー（vite。Compute の上で調べるときにぶつからないように。#1747）/
#   8000・8010・8020: 各製品の backend（127.0.0.1）
SUITE_RESERVED_PORTS=(22 25 53 111 1521 1522 3000 3001 3002 3306 5432 6379 9090 8000 8010 8020)
# RAG の前処理 / parser（127.0.0.1:18010〜18028。rag/scripts/rag-systemd.sh）とその予備。
SUITE_RESERVED_PORT_RANGES=("18000-18099")

# 公開の port として使えるか（1〜65535 の整数で、予約していない）。使えなければ理由を stderr に書いて 1。
suite_validate_public_port() {
  local name="$1"
  local port="$2"
  local reserved range low high
  if ! [[ "${port}" =~ ^[0-9]{1,5}$ ]] || [ "${port}" -lt 1 ] || [ "${port}" -gt 65535 ]; then
    echo "${name} must be an integer between 1 and 65535: ${port}" >&2
    return 1
  fi
  for reserved in "${SUITE_RESERVED_PORTS[@]}"; do
    if [ "${port}" -eq "${reserved}" ]; then
      echo "${name} ${port} is reserved (SSH, well-known services, or a port the suite uses on the Compute)." >&2
      return 1
    fi
  done
  for range in "${SUITE_RESERVED_PORT_RANGES[@]}"; do
    low="${range%-*}"
    high="${range#*-}"
    if [ "${port}" -ge "${low}" ] && [ "${port}" -le "${high}" ]; then
      echo "${name} ${port} is reserved for the RAG preprocessing / parser services (${range})." >&2
      return 1
    fi
  done
}

# 製品の API の client_max_body_size。
suite_client_max_body_size() {
  case "$1" in
    rag) printf '%s\n' "${SUITE_RAG_CLIENT_MAX_BODY_SIZE}" ;;
    nl2sql) printf '%s\n' "${SUITE_NL2SQL_CLIENT_MAX_BODY_SIZE}" ;;
    agent) printf '%s\n' "${SUITE_AGENT_CLIENT_MAX_BODY_SIZE}" ;;
    *) return 1 ;;
  esac
}

# 製品の長い待ち時間の API の location の正規表現と待ち時間（秒）。
suite_long_api_regex() {
  local -n paths="SUITE_${1^^}_LONG_API_PATHS"
  local IFS='|'
  printf '^/%s/api/(%s)$\n' "$1" "${paths[*]}"
}

suite_long_api_timeout() {
  local -n seconds="SUITE_${1^^}_LONG_API_TIMEOUT_SECONDS"
  printf '%s\n' "${seconds}"
}

suite_backend_port() {
  case "$1" in
    rag) printf '%s\n' "${SUITE_RAG_BACKEND_PORT}" ;;
    nl2sql) printf '%s\n' "${SUITE_NL2SQL_BACKEND_PORT}" ;;
    agent) printf '%s\n' "${SUITE_AGENT_BACKEND_PORT}" ;;
    *) return 1 ;;
  esac
}

# / の転送先。Agent を配備していれば /agent/、無ければ並びの最初の製品。
suite_root_redirect_target() {
  local product
  for product in "$@"; do
    if [ "${product}" = "agent" ]; then
      printf '/agent/\n'
      return 0
    fi
  done
  for product in "${SUITE_PRODUCTS_ORDER[@]}"; do
    if suite_contains "${product}" "$@"; then
      printf '/%s/\n' "${product}"
      return 0
    fi
  done
  return 1
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

# backend へ渡す proxy の共通の設定。3 製品とも HTTP だけ（push は SSE、操作は REST。WebSocket は持たない。#2260）。
#   $1: X-Forwarded-Prefix（/<製品>）
_suite_proxy_headers() {
  cat <<EOF
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Request-ID \$pr_request_id;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Prefix ${1};
        proxy_set_header Connection "";
EOF
  # SSE（text/event-stream）と応答の流れを溜めない: buffering・cache・gzip を切る（gzip は出力を溜めて
  # 圧縮するため、SSE の event と heartbeat が届かなくなる。backend も X-Accel-Buffering: no を付ける）。
  cat <<'EOF'
        proxy_buffering off;
        proxy_cache off;
        gzip off;
EOF
}

# 製品の API の location の先頭（ログの設定まで）。
#   $1: 製品  $2: location の指定（"= /rag/api/auth/login" など）
_suite_api_location_head() {
  local product="$1"
  cat <<EOF
    location $2 {
        set \$pr_service_name "production-ready-${product}";
        access_log ${SUITE_NGINX_LOG_DIR:-/var/log/nginx}/production-ready-${product}-access.log production_ready_json if=\$pr_loggable;
        error_log ${SUITE_NGINX_LOG_DIR:-/var/log/nginx}/production-ready-${product}-error.log warn;
EOF
}

# 製品ごとの location。今までの各製品の init_script.sh の location（SSE・MCP・login の上限・health）を
# /<製品>/ の下へ移したもの。
#   $1: 製品  $2: frontend の dist
_suite_product_locations() {
  local product="$1"
  local dist_dir="$2"
  local port body_size timeout
  port="$(suite_backend_port "${product}")"
  body_size="$(suite_client_max_body_size "${product}")"
  timeout="$(suite_long_api_timeout "${product}")"

  cat <<EOF

    # ---------------------------------------------------------------- ${product}（/${product}/ → 127.0.0.1:${port}）

    location = /${product} {
        return 301 /${product}/;
    }

    location = /${product}/api {
        return 308 /${product}/api/;
    }

    # ログインの API だけ、送信元 IP ごとに緩く上限を掛ける（#1173。zone は
    # platform/templates/nginx/login-rate-limit.conf）。回数の制限の正本は backend（PLATFORM_AUTH_LOGIN_*。
    # 既定は 15 分にログイン名ごと 5 回・送信元 IP ごと 20 回）で、ここはそれより緩い（1 秒 1 回・burst 30）。
EOF
  _suite_api_location_head "${product}" "= /${product}/api/auth/login"
  cat <<EOF
        limit_req zone=pr_login burst=30 nodelay;
        limit_req_status 429;
        error_page 429 = @pr_login_rate_limited;
        proxy_pass http://127.0.0.1:${port}\$pr_suite_upstream_uri;
EOF
  _suite_proxy_headers "/${product}"
  echo "    }"

  cat <<'EOF'

    # LLM を呼ぶ同期の API だけ待ち時間を延ばす（値の根拠は suite-nginx.sh の SUITE_*_LONG_API_*。#1597）。
EOF
  _suite_api_location_head "${product}" "~ $(suite_long_api_regex "${product}")"
  cat <<EOF
        client_max_body_size ${body_size};
        proxy_pass http://127.0.0.1:${port}\$pr_suite_upstream_uri;
EOF
  _suite_proxy_headers "/${product}"
  cat <<EOF
        proxy_send_timeout ${timeout}s;
        proxy_read_timeout ${timeout}s;
    }

EOF
  _suite_api_location_head "${product}" "/${product}/api/"
  cat <<EOF
        client_max_body_size ${body_size};
        proxy_pass http://127.0.0.1:${port}\$pr_suite_upstream_uri;
EOF
  _suite_proxy_headers "/${product}"
  cat <<EOF
    }

    location = /${product}/health {
        proxy_pass http://127.0.0.1:${port}/api/health;
        proxy_set_header Host \$host;
        access_log off;
    }

    # frontend（/${product}/ を base に build した dist）。画面の URL は index.html に返す（SPA）。
    # index.html（と名前に hash の無いファイル）は毎回 server に確かめさせる（no-cache。etag で変わらなければ 304）。
    # 付けないとブラウザが古い index.html を経験則で使い続け、配備の後も古い JS を読む。
    location /${product}/ {
        set \$pr_service_name "production-ready-${product}";
        access_log ${SUITE_NGINX_LOG_DIR:-/var/log/nginx}/production-ready-${product}-access.log production_ready_json if=\$pr_loggable;
        error_log ${SUITE_NGINX_LOG_DIR:-/var/log/nginx}/production-ready-${product}-error.log warn;
        alias ${dist_dir}/;
        index index.html;
        try_files \$uri \$uri/ /${product}/index.html;
        add_header Cache-Control "no-cache" always;
    }

    # build の出力（名前に内容の hash が付く JS・CSS・フォントなど）は変わらないので長く cache させる。
    location /${product}/assets/ {
        set \$pr_service_name "production-ready-${product}";
        access_log ${SUITE_NGINX_LOG_DIR:-/var/log/nginx}/production-ready-${product}-access.log production_ready_json if=\$pr_loggable;
        error_log ${SUITE_NGINX_LOG_DIR:-/var/log/nginx}/production-ready-${product}-error.log warn;
        alias ${dist_dir}/assets/;
        add_header Cache-Control "public, max-age=31536000, immutable" always;
    }

    # build が出すサードパーティのライセンス（licenses/javascript.md・licenses/fonts/*.txt。#3392）。
    # mime.types に .md が無く既定の application/octet-stream になり、ブラウザがダウンロードするので、
    # この下だけ .md / .txt を text/plain; charset=utf-8 にしてブラウザで読めるようにする（#3420）。
    # text/markdown は inline で表示しないブラウザがあるため使わない。無いファイルは SPA の index.html にせず 404。
    location /${product}/licenses/ {
        set \$pr_service_name "production-ready-${product}";
        access_log ${SUITE_NGINX_LOG_DIR:-/var/log/nginx}/production-ready-${product}-access.log production_ready_json if=\$pr_loggable;
        error_log ${SUITE_NGINX_LOG_DIR:-/var/log/nginx}/production-ready-${product}-error.log warn;
        alias ${dist_dir}/licenses/;
        types {
            text/plain md txt;
        }
        default_type text/plain;
        charset utf-8;
        add_header Cache-Control "no-cache" always;
        add_header X-Content-Type-Options "nosniff" always;
    }
EOF
}

# 要求の header の buffer（HTTP / HTTPS の server で共通）。既定（4 × 8k）は 1 行（Cookie・URL）が 8k まで。
# 共有の Cookie（#1565。pr_session / pr_csrf は各 43 文字）は小さいが、Cookie は port を問わず host
# （IP）ごとに送られ、同じ host の別のアプリの Cookie も載る。HTTP/2 の header もこの値で上限を決める。
_suite_client_header_buffers() {
  cat <<'EOF'
    large_client_header_buffers 4 16k;
EOF
}

# server の中身（HTTP / HTTPS で共通）。
_suite_server_body() {
  local https_enabled="$1"
  local repo_dir="$2"
  local ssl_dir="$3"
  shift 3
  local products=("$@")
  local product root_target

  root_target="$(suite_root_redirect_target "${products[@]}")"
  cat <<EOF
    server_name _;

    # 転送先を相対の Location にする（443 以外の port・IP のまま、同じ origin に戻す）。
    absolute_redirect off;

    set \$pr_service_name "production-ready-suite";
    access_log ${SUITE_NGINX_LOG_DIR:-/var/log/nginx}/production-ready-suite-access.log production_ready_json if=\$pr_loggable;
    error_log ${SUITE_NGINX_LOG_DIR:-/var/log/nginx}/production-ready-suite-error.log warn;

    # 製品の外（frontend・CA 証明書・ログイン）の本文の上限。API は製品ごとの location で backend の上限 + 余白。
    client_max_body_size 10m;
EOF
  _suite_client_header_buffers
  cat <<EOF
    proxy_connect_timeout 60s;
    proxy_send_timeout ${SUITE_PROXY_TIMEOUT_SECONDS}s;
    proxy_read_timeout ${SUITE_PROXY_TIMEOUT_SECONDS}s;
    # 応答の header を読む buffer（proxy_buffering off でも使う）。既定（4k）は、RAG の文書の
    # Content-Disposition（ファイル名 512 バイトまで → filename* の percent-encode で 1.5 KB まで）・
    # Content-Security-Policy・Set-Cookie（pr_session / pr_csrf）・X-Request-ID が重なると足りなくなり 502 になる。
    proxy_buffer_size 16k;
    proxy_buffers 8 16k;

    location @pr_login_rate_limited {
        default_type application/json;
        add_header Retry-After 60 always;
        return 429 '{"success":false,"data":null,"error_code":"SECURITY_RATE_LIMITED","error_messages":["ログインの試行が多すぎます。しばらく待ってから、もう一度お試しください。"]}';
    }

    location = / {
        return 302 ${root_target};
    }

    # /platform/ は platform（共通基盤）が配る内容のために予約する。今は CA 証明書だけを配り、ほかは 404。
    location /platform/ {
        return 404;
    }
EOF
  if [ "${https_enabled}" = "true" ]; then
    cat <<EOF

    # 自作の Root CA の証明書（利用者の端末に取り込んで、ブラウザの警告を消す）。秘密鍵は配らない。
    location = /platform/ca.crt {
        alias ${ssl_dir}/ca.crt;
        types { }
        default_type application/x-x509-ca-cert;
        add_header Content-Disposition 'attachment; filename="production-ready-root-ca.crt"' always;
        add_header Cache-Control "no-cache" always;
        add_header X-Content-Type-Options "nosniff" always;
    }
EOF
  fi

  for product in "${SUITE_PRODUCTS_ORDER[@]}"; do
    suite_contains "${product}" "${products[@]}" || continue
    _suite_product_locations "${product}" "${repo_dir}/${product}/frontend/dist"
  done

  cat <<'EOF'

    # 製品・platform の外の path は返さない（Nginx の既定のページを出さない）。
    location / {
        return 404;
    }
EOF
}

# site を標準出力に書く。
#   $1: HTTPS（true / false）  $2: suite の repository  $3: 証明書の directory
#   $4: http_port（HTTPS が off のときだけ使う）  $5: https_port（HTTPS が on のときだけ使う）
#   $6...: 配備する製品（rag / nl2sql / agent）
# 製品の API の本文の上限は SUITE_<製品>_CLIENT_MAX_BODY_SIZE（suite-init.sh が backend/.env から作る）。
# 環境変数 SUITE_LISTEN_ADDRESS は test が 127.0.0.1 だけで listen するためのもの（本番は全 address）。
suite_nginx_site() {
  local https_enabled="$1"
  local repo_dir="$2"
  local ssl_dir="$3"
  local http_port="$4"
  local https_port="$5"
  shift 5
  local products=("$@")
  local address="${SUITE_LISTEN_ADDRESS:-}"
  local http_listen="${address:+${address}:}${http_port}"
  local https_listen="${address:+${address}:}${https_port}"
  local product

  if [ "${#products[@]}" -eq 0 ]; then
    echo "suite_nginx_site: no product is selected" >&2
    return 1
  fi
  for product in "${products[@]}"; do
    if ! suite_contains "${product}" "${SUITE_PRODUCTS_ORDER[@]}"; then
      echo "suite_nginx_site: unknown product: ${product}" >&2
      return 1
    fi
  done
  # 使う側の port だけを確かめる（HTTPS が on なら http_port は使わない。#1638）。
  if [ "${https_enabled}" = "true" ]; then
    suite_validate_public_port https_port "${https_port}" || return 1
  else
    suite_validate_public_port http_port "${http_port}" || return 1
  fi

  cat <<'EOF'
# platform/deploy/suite-init.sh が生成する（手で編集しない）。#1316
# backend へ渡す URI: /<製品>/api/... の $request_uri から製品の prefix だけを外す（query とエンコードはそのまま）。
map $request_uri $pr_suite_upstream_uri {
    default "/";
    "~^/(?:rag|nl2sql|agent)(?<pr_suite_rest>/api(?:[/?].*)?)$" $pr_suite_rest;
}
EOF

  if [ "${https_enabled}" = "true" ]; then
    # http_port は listen しない（HTTP から HTTPS への転送もしない。HSTS は付けない。#1638）。
    cat <<EOF

server {
    listen ${https_listen} ssl http2 default_server;

    ssl_certificate ${ssl_dir}/server.crt;
    ssl_certificate_key ${ssl_dir}/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:pr_suite_ssl:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

EOF
  else
    cat <<EOF

server {
    listen ${http_listen} default_server;

EOF
  fi
  _suite_server_body "${https_enabled}" "${repo_dir}" "${ssl_dir}" "${products[@]}"
  echo "}"
}
