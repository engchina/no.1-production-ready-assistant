# Agent と RAG の連携: 出荷前の点検・障害注入・停止と復帰（#64）

Agent は実行・承認・停止を、RAG は検索・根拠・権限の判断を持つ。製品の間は HTTP MCP の契約
（`platform/contracts/mcp`）と、呼び出しごとに Run の利用者で署名する短命のサービストークン
（`PLATFORM_SERVICE_TOKEN_SECRET`）だけでつなぐ。この文書は、その境界が障害のときも閉じていることの
証拠（テスト）と、出荷の前に実際の構成で行う点検、止め方と戻し方をまとめる。

証拠は 3 種類に分ける。どれか 1 つの成功を、ほかの種類の保証として扱わない。

| 種類 | 何で確かめるか | どこで |
|---|---|---|
| 決定論の挙動（スタブ） | pytest。モデルは SDK の `ScriptedModel`、RAG / NL2SQL は契約どおりの fake の MCP（`tests/mcp_support.py`）、保存先は memory か偽の Oracle の表 | CI（`agent-backend`）と `ship_preflight --run-stub-tests` |
| 実構成の点検（読み取りだけ） | `python -m app.cli.ship_preflight`。解決した設定・health / ready・認証の拒否 | 出荷する環境（ローカルの配備でも可） |
| 人の演練 | 下の「停止と復帰」「承認の滞留」 | 隔離した環境。日時・担当・結果を出荷の記録に残す |

## 1. 出荷前の点検（`ship_preflight`）

```bash
cd agent/backend
# 稼働中の配備の設定を読む（例はローカルの配備。値は表示しない）
PLATFORM_ENV_FILE=<repo>/platform/.env AGENT_ENV_FILE=<repo>/agent/backend/.env \
  uv run python -m app.cli.ship_preflight --run-stub-tests [--as-user <user_uuid>] [--json]
```

点検は DB への書き込み・worker の起動・Run の回復をしない。RAG / NL2SQL へは `tools/list` だけを送り、
`tools/call` は送らない。各行は `status area/name: 詳細` と、pass 以外は `修復:` を出す。先頭に、点検した
コードの commit、解決した構成の識別（秘密の値を含めない hash）、時刻、読んだ `.env` を出す。

| 領域 | 項目 | blocker になる条件 |
|---|---|---|
| config | `auth_mode` / `environment` / `service_token_secret` / `{rag,nl2sql}_mcp_url` / `tool_grounding_required` / `final_validation_enabled` / `run_store` | `PLATFORM_AUTH_MODE=local`（MCP が token を検査しない）、鍵が 32 文字未満、MCP の URL が未設定、Run の保存先が memory（再起動で承認待ちと取消の記録が消える）。根拠のガードの無効化と `environment=local` は warning |
| health / ready | Agent・RAG・NL2SQL の `/api/health` と `/api/ready` | 接続できない、200 以外（ready は `checks` の ok でない項目を出す） |
| auth | 製品ごとに `no_token`（期待 401）・`wrong_audience`（401）・`unknown_subject`（Agent の鍵で署名した存在しない利用者。403） | 期待と違う応答。`unknown_subject` が 401 なら Agent と呼び先の鍵の不一致 |
| contract | `--as-user` の利用者で `tools/list`。契約のツールがそろうか | 一覧の取得の失敗（欠けたツールは warning）。`--as-user` が無ければ not_run |
| fault_injection | 下の表のスタブ検証を実行 | テストの失敗。`--run-stub-tests` が無ければ not_run |
| operations | 停止と復帰の演練・承認の滞留の担当 | 常に manual（ファイルの存在では合格にしない） |

状態は pass / blocker / warning / not_applicable / not_run / manual / error（点検自体の失敗）。判定は
blocker か error があれば `NOT_READY`（終了コード 1）、not_run があれば `INCOMPLETE`（2）、それ以外は
`READY`（0）。**証拠が無い項目は pass にしない**。`READY` でも manual の項目は人が確かめる。

確かめない範囲: 画面で保存した MCP 接続の上書き（配備の URL があれば URL は固定される）、ツール権限の保存値、
実際の検索の品質、モデルの API の応答、外部 MCP 接続。

## 2. 境界の対応表（既存の保証・今回の追加・適用しない教材の項目）

### 2.1 RAG の障害（今回の追加: `tests/test_rag_fault_injection.py`）

注入する障害: 停止（接続できない）・読み取りの timeout・500・503（再試行の上限まで）・200 で JSON でない
応答・401・403・MCP のツールのエラー（`RAG_SEARCH_ANSWER_PROFILE_FORBIDDEN`）。

| 保証 | テスト |
|---|---|
| 検索が失敗したのにモデルが事実を断定しても、回答を出さずに Run を失敗にし（`runtime.grounding_tool_failed`）、「根拠が無いという意味ではありません」と示す（取得の失敗を根拠の不足にしない） | `test_rag_failure_never_becomes_a_fabricated_or_no_evidence_answer[*]` |
| 検索の失敗の後にモデルが書き込み（`nl2sql_cancel_requirement`）を要求しても、承認待ちで止まり、呼び先へ送らない | `test_write_requested_after_rag_failure_waits_for_approval_and_is_not_sent[*]` |
| 無人の実行（外部 MCP クライアント）では書き込みを送らず（`MCP_APPROVAL_SKIPPED_CODE`）、作り話の回答も出さない | `test_write_requested_after_rag_failure_is_never_sent_in_unattended_runs[*]` |
| 承認待ちの取消の後、遅れた承認・再開でも書き込みを送らない | `test_cancel_while_waiting_for_approval_after_rag_failure_never_sends_the_write` |
| 受付停止（業務 Agent の無効化）は新しい Run だけを止め、受理済みの承認待ちは残る（自動で承認・取消しない） | `test_disabling_the_agent_stops_new_runs_but_not_accepted_ones` |
| どの障害でも、呼び先へは Run の利用者（`sub`）と呼び先の `aud` の token だけを送り、別の主体で呼び直さない | 上のすべて（`_assert_no_widening`） |

`nl2sql_query` は既定で自動実行の照会（#1832。readOnlyHint は false）なので、危険な次の操作の台本には
使わない。根拠を必須にする書き込みの一律の規則は作らない。書き込みの境界は承認（と無人の実行での不実行）である。

### 2.2 既存の保証（変更していない。点検のスタブ検証で再実行する）

| 保証 | テスト |
|---|---|
| 書き込みは呼び先に届いた後の失敗で再送しない。読み取りは 502 / 504 / 503 を再試行し、timeout・500 は再送しない | `test_product_mcp.py::test_write_tool_calls_are_not_retried_after_reaching_the_server` / `test_read_only_tool_calls_retry_gateway_errors` / `test_read_only_tool_calls_are_not_retried_on_timeout_or_500` |
| 取消の後は新しいツールの step を作らない（読み取り中の取消で後続の書き込みを止める。#2609） | `test_tool_call_boundary.py::test_cancel_during_a_read_stops_the_following_write` / `test_tool_call_after_cancel_creates_no_step` |
| 承認の要否は実行の直前のツール権限で決める（構築の後に ask / deny へ変えても効く。#2610） | `test_tool_call_boundary.py::test_policy_changed_to_ask_after_build_waits_for_approval` / `test_policy_changed_to_deny_after_approval_is_not_executed` |
| 壊れた引数は handler に届かない（#2630） | `test_tool_call_boundary.py::test_invalid_arguments_never_reach_the_handler` |
| 検索の結果の「承認済み」「権限の変更」の文言で承認を飛ばさない | `test_untrusted_content_authority.py::*` |
| 期限切れの旧 owner は実行を始められない、承認の再開で旧 owner の書き込みを拒む（#2606） | `test_run_claim_fencing.py::test_stale_owner_cannot_start_after_the_lease_is_reclaimed` / `test_resume_after_approval_rejects_the_old_owner_and_its_late_writes` |
| in_process の再起動で、起動前の running / queued は失敗にし、承認待ちは残す | `test_interrupted_runs.py::test_running_and_queued_runs_before_the_start_fail_and_are_saved` |
| 送信中の書き込みは取消で切らず結果を残す（読み取りは切る） | `test_tool_call_abort.py::test_write_call_in_flight_is_not_cut_and_its_result_is_kept` / `test_cancel_cuts_the_in_flight_mcp_call` |
| 取消で承認待ちの承認を無効にし、遅れた承認を通さない。終了した Run の取消は 409 | `test_health.py::test_cancelled_run_cancels_pending_approvals_and_blocks_late_approval` / `test_cancel_finished_run_keeps_result_and_returns_conflict` |
| 失敗の経路の機微情報をモデル・Run・ログへ出さない（#2611） | `test_tool_error_masking.py::test_external_tool_error_is_masked_for_model_run_events_and_logs` |
| token の `sub` / `aud` / `run_id` / `agent_id` は Run の利用者に従う（障害の追跡は Run ID と request ID で、本文をログに出さない） | `test_product_mcp.py::test_token_subject_audience_and_claims_follow_run_user` / `test_mcp_request_spans_carry_the_mcp_method_and_tool` |

### 2.3 適用しない教材の項目

| 教材の項目 | 理由 |
|---|---|
| RuntimeBinding・外部 Runtime Adapter・固定の Binding token・plugin marketplace | 内蔵 Runtime と呼び出しごとの短命のサービストークンを維持する |
| lease ≥ 2×p95 と heartbeat の無条件の移植 | 現行は running を再 claim せず、世代 token（claim fencing）で旧 owner を拒む |
| 期限超過の承認の自動承認・自動取消 | 承認は人の判断。滞留は下の手順で人が扱う |
| request の自己申告の approvals / granted / rag_token | 承認は Run・呼び出し ID・ツール・引数に結び付いた記録だけ。権限は token の `sub` から呼び先が組み立てる |
| Docker 配備・Nginx | ネイティブ配備（systemd）。reverse proxy は配備しない |

## 3. 停止と復帰

止める範囲の小さい順に使う。どれも送信済みの書き込みを取り消さない（実行済み・結果不明・未実行は Run の
step と NL2SQL の実行履歴で確かめる）。

| 操作 | 方法 | 効く範囲 | 効かない範囲 |
|---|---|---|---|
| 受付停止（業務 Agent 単位） | 業務 Agent の一覧の「無効にする」（`PATCH /api/agents/{id}` `{"enabled": false}`） | 新しい Run（チャット・MCP）を `agent_disabled`（409）で拒む | 受理済みの queued / running / 承認待ち |
| 能力の禁止（ツール単位） | 運用設定 > ツール権限で deny（`PATCH /api/settings/tool-policy`） | 実行の直前に判定するので、構築済みの Run・承認の再開でも新しい呼び出しを止める | 送信中の呼び出し |
| 実行の取消（Run 単位） | 画面の取消（`POST /api/runs/{id}/cancel`） | 以後の外部操作、承認待ちの承認、送信中の読み取り | 送信中の書き込み（結果を残す）、終了した Run（409） |
| 全体停止 | 配備: `sudo systemctl stop production-ready-agent-backend`（RAG / NL2SQL は止めない）。ローカル: `agent/scripts/start-all.sh` の Ctrl+C | Agent のすべての入口 | 呼び先で実行中の処理（NL2SQL のジョブは NL2SQL の画面で確かめる） |

復帰:

1. 原因を直し、`ship_preflight` を再実行して blocker が無いことを確かめる（RAG の障害なら health / ready・
   auth の行）。
2. 全体停止からは `sudo platform/deploy/suite-restart.sh --no-build`（ローカルは `start-all.sh`）。in_process
   の再起動では、起動前の running / queued は「中断」で失敗になり、承認待ちは残る。失敗した Run は利用者が
   再実行する（自動で再実行しない）。
3. 能力の禁止は、原因を直した後に deny を外す。受付停止は「有効」に戻す。戻す人は停止した人と別の運用者が
   確認する。

戻し（rollback）: 業務 Agent の変更は前の公開版へ戻す（`POST /api/agents/{id}/versions/{version}/restore`）。
Skill の更新の一括反映は `POST /api/agent-skill-updates/rollback` で戻す。コードは前の commit に戻して `suite-restart.sh` で再配備する。ただし
DB の migration は戻さないので、migration を含む版は戻さず修正版を出す。

## 4. 承認の滞留

- 承認待ちは期限で自動承認も自動取消もしない。滞留した Run は、依頼した利用者か運用者が承認・却下・取消を決める。
- 確認する役割: 業務 Agent の管理者（承認の判断）、運用者（取消・停止）。連絡と引継ぎの経路は出荷の記録に
  書く（実在の個人情報はこの repository に書かない）。
- 受付停止の後も承認待ちは残る。停止の理由が書き込みの安全性なら、承認する前に能力の禁止か取消を使う。

## 5. lesson12 での演習の例

1. スタブ: `uv run pytest tests/test_rag_fault_injection.py -v`。障害ごとに「作り話をしない」「書き込みを送らない」が並ぶ。
2. 実構成: 上のコマンドで `ship_preflight` を実行し、領域ごとの行と判定を読む。
3. 鍵の不一致（稼働中のサービスを変えない）: 点検の環境変数だけで鍵を差し替える
   （`PLATFORM_SERVICE_TOKEN_SECRET=<別の 32 文字以上> ... ship_preflight`。環境変数は `.env` より優先する）。
   `auth/*.unknown_subject` が鍵の不一致の blocker になり、修復の手順が出る。
4. 障害注入（隔離した環境だけ）: RAG の backend を止めて再点検すると、`health/rag`・`ready/rag` が blocker、
   `auth/rag.*` が error（確認不能。pass にしない）になる。
