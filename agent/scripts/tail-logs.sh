#!/usr/bin/env bash
# 共通の読み取り専用ログ viewer。時刻は JST、service の状態は変更しない。
TAIL_LOGS_PRODUCT=agent
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../platform/scripts" && pwd)/tail-logs.sh"
