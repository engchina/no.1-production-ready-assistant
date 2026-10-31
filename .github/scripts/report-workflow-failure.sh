#!/usr/bin/env bash
# main の CI（push）と nightly（schedule / 手動実行）の失敗を Issue にする（#339）。
#
# 失敗した job を製品（job 名の " / " より前。例: "RAG / Backend" → rag）ごとにまとめ、
# 「workflow 名 + 製品」ごとに 1 つの Issue で追跡する。
#   - 同じ key の open な Issue（label: ci-failure、本文に key の marker）があれば comment で更新する。
#   - 無ければ Issue を作る（label: ci-failure と製品の label）。
# 復旧後の close は人が行う（原因と修正 PR を Issue に書いてから閉じる）。
#
# 使い方: RUN_ID=<workflow run の ID> GITHUB_REPOSITORY=<owner/repo> GH_TOKEN=<token> report-workflow-failure.sh
#   DRY_RUN=1 のときは Issue を作らず、作る内容を標準出力に出す（手元の確認用）。
set -euo pipefail

: "${RUN_ID:?RUN_ID を指定してください}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY を指定してください}"
DRY_RUN="${DRY_RUN:-0}"
LABEL="ci-failure"
repo="${GITHUB_REPOSITORY}"

run_json="$(gh api "repos/${repo}/actions/runs/${RUN_ID}")"
workflow="$(jq -r '.name' <<<"${run_json}")"
run_url="$(jq -r '.html_url' <<<"${run_json}")"
event="$(jq -r '.event' <<<"${run_json}")"
branch="$(jq -r '.head_branch' <<<"${run_json}")"
sha="$(jq -r '.head_sha' <<<"${run_json}")"
commit_title="$(jq -r '(.head_commit.message // "") | split("\n")[0]' <<<"${run_json}")"
run_started="$(jq -r '.run_started_at // .created_at' <<<"${run_json}")"

# 失敗した job と、その中で失敗した step。集約用の `CI OK` は他の job の失敗を映すだけなので除く。
# job の時間切れ（timeout-minutes）は job も run も cancelled になるため、cancelled の job は annotation で
# 時間切れかを確かめて失敗に含める（#1586）。手で取り消した job は含めない。
all_jobs="$(gh api --paginate "repos/${repo}/actions/runs/${RUN_ID}/jobs?per_page=100" -q '.jobs[]' | jq -s '.')"
timed_out_ids="$(
  jq -r '.[] | select(.conclusion == "cancelled") | .id' <<<"${all_jobs}" | while IFS= read -r job_id; do
    [ -n "${job_id}" ] || continue
    if gh api "repos/${repo}/check-runs/${job_id}/annotations" -q '.[].message' 2>/dev/null \
      | grep -q 'exceeded the maximum execution time'; then
      printf '%s\n' "${job_id}"
    fi
  done | jq -R 'tonumber' | jq -s '.'
)"
failed_jobs="$(
  jq --argjson timed_out "${timed_out_ids}" '
    [ .[]
      | select(.conclusion == "failure" or .conclusion == "timed_out"
               or (.conclusion == "cancelled" and (.id as $id | $timed_out | index($id))))
      | select(.name != "CI OK")
      | {name, url: .html_url,
         steps: (if .conclusion == "cancelled" then "時間切れ（timeout-minutes）"
                 else ([.steps[]? | select(.conclusion == "failure") | .name] | join(", ")) end)} ]' \
    <<<"${all_jobs}"
)"

product_of() {
  local name="$1"
  if [[ "${name}" == *" / "* ]]; then
    printf '%s' "${name%% / *}" | tr '[:upper:]' '[:lower:]'
  else
    printf 'general'
  fi
}

label_of() {
  case "$1" in
    rag) echo "product:rag" ;;
    nl2sql) echo "product:nl2sql" ;;
    agent) echo "product:agent" ;;
    *) echo "platform" ;;
  esac
}

run_conclusion="$(jq -r '.conclusion // ""' <<<"${run_json}")"
# 取り消された run で時間切れの job が無ければ、手で取り消したもの。通知しない。
if [ "${run_conclusion}" = "cancelled" ] && [ "$(jq 'length' <<<"${failed_jobs}")" -eq 0 ]; then
  echo "run ${RUN_ID} は取り消され、時間切れの job はありません（通知しない）。"
  exit 0
fi

# job が取れない失敗（workflow の構文エラー等）も通知する。
if [ "$(jq 'length' <<<"${failed_jobs}")" -eq 0 ]; then
  failed_jobs='[{"name":"(job なし)","url":"","steps":"workflow の起動・構文を確認してください"}]'
fi

products="$(jq -r '.[].name' <<<"${failed_jobs}" | while IFS= read -r name; do product_of "${name}"; echo; done | sort -u)"

if [ "${DRY_RUN}" != "1" ]; then
  gh label create "${LABEL}" --repo "${repo}" --color B60205 \
    --description "main の CI / nightly の失敗（自動起票。#339）" >/dev/null 2>&1 || true
fi

open_issues="$(gh issue list --repo "${repo}" --label "${LABEL}" --state open --limit 200 --json number,body)"

while IFS= read -r product; do
  [ -n "${product}" ] || continue
  key="${workflow}:${product}"
  marker="<!-- ci-failure-key: ${key} -->"
  job_lines="$(
    jq -r --arg p "${product}" '
      .[] | select(
        (if (.name | contains(" / ")) then (.name | split(" / ")[0] | ascii_downcase) else "general" end) == $p
      )
      | "  - " + (if .url != "" then "[" + .name + "](" + .url + ")" else .name end)
        + (if .steps != "" then "（失敗した step: " + .steps + "）" else "" end)' <<<"${failed_jobs}"
  )"
  details="$(cat <<EOF
- workflow: ${workflow}（event: \`${event}\`、branch: \`${branch}\`）
- run: ${run_url}
- 開始: ${run_started}
- commit: \`${sha:0:12}\` ${commit_title}
- 失敗した job:
${job_lines}
EOF
)"
  number="$(jq -r --arg m "${marker}" '[.[] | select(.body | contains($m)) | .number][0] // empty' <<<"${open_issues}")"

  if [ -n "${number}" ]; then
    body="$(printf '再び失敗しました。\n\n%s\n' "${details}")"
    if [ "${DRY_RUN}" = "1" ]; then
      printf '[dry-run] comment on #%s\n%s\n\n' "${number}" "${body}"
    else
      gh issue comment "${number}" --repo "${repo}" --body "${body}"
    fi
    continue
  fi

  title="ci: main の「${workflow}」が失敗している（${product}）"
  body="$(cat <<EOF
${marker}
> main の CI / nightly が失敗したため自動で起票しました（\`.github/workflows/ci-failure-issue.yml\`、#339）。
> 同じ workflow・製品の失敗は、この Issue が open の間はコメントで追記します。

## 問題

main の「${workflow}」が失敗しています。main が赤のままだと、以降の PR の検証結果を信頼できません。最優先で直してください。

## 症状

${details}

## 原因

調査中（run のログと、失敗した job の artifact を確認して追記してください）。

## 修正方針

調査中。原因を直す PR をこの Issue に紐づけ、main の同じ workflow が成功したことを確かめてから close します。
EOF
)"
  label="$(label_of "${product}")"
  if [ "${DRY_RUN}" = "1" ]; then
    printf '[dry-run] create issue: %s (labels: %s,%s)\n%s\n\n' "${title}" "${LABEL}" "${label}" "${body}"
  else
    gh issue create --repo "${repo}" --title "${title}" --label "${LABEL}" --label "${label}" --body "${body}"
  fi
done <<<"${products}"
