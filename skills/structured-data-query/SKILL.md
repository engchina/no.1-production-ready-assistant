---
name: structured-data-query
description: "業務データの照会（MCP サーバー nl2sql。Production Ready NL2SQL）で、自然言語の質問から SQL を作って実行し、表形式の結果（件数・金額・一覧・推移・集計）を返す。売上・受注・顧客・在庫など業務データの数値や一覧を尋ねられたら、SQL という言葉が無くてもこのスキルを使う。足りない条件（期間・対象など）は NL2SQL が利用者への問いとして返す。規程・手順など資料の内容は business-rag-research、資料と業務データの両方が要る調査は rag-and-structured-data を使う。"
compatibility: "MCP サーバー nl2sql（Production Ready NL2SQL の POST /api/mcp）に接続していること。"
license: MIT
metadata:
  production-ready-display-name: "構造化データ照会"
  production-ready-mcp-tools: "nl2sql:nl2sql_query nl2sql:nl2sql_get_job nl2sql:nl2sql_list_profiles nl2sql:nl2sql_recommend_profile"
  production-ready-tags: "nl2sql structured-data audit-sql"
---
業務データの質問を NL2SQL（MCP サーバー `nl2sql`）で SQL にして実行し、結果の行で答える。ツールは MCP の素の名前で書く（Agent によって `mcp__nl2sql__` などの前置きが付く）。

## Step 1: 入力の種類を分ける

- 質問（業務の範囲外を含む）と、あいさつ・お礼に質問が混ざる入力（「こんにちは、今月の受注の件数は？」）: Step 2 に進む。
- あいさつ・お礼・その他の質問でない入力（雑談・意味の取れない短い入力・「テスト」のような試し）: ツールを呼ばず、短く応じてから、業務データ（件数・金額・一覧など）について何を知りたいかを聞く（例: 「こんにちは。業務データについて、知りたいことをお知らせください。」）。入力をなぞらず、業務データの値も一般的な知識も答えない。

## Step 2: 業務データを照会する

1. 業務プロファイルを決める。分からなければ `nl2sql_recommend_profile` で選び、決まらなければ `nl2sql_list_profiles` から選ぶ（業務 Agent にデータの範囲があれば、その範囲の中から）。
2. 利用者の質問のまま `nl2sql_query` を呼ぶ。表・列は NL2SQL が業務プロファイルの schema で決め、決まらなければ問いを返すので、利用者に表名・列名を聞かない。
3. 結果の `status` ごとに「結果ごとの対応」に従う。

## Step 3: 結果ごとの対応

- `done`: 結果の行で答える。
- `pending` / `running`: `job_id` を `nl2sql_get_job` に渡して続きを取る。結果が出ないまま答えるときは、実行中であることと `job_id` を伝え、結果を推測で補わない。
- `needs_clarification`: SQL はまだ作っていない。
  1. 利用者がこの質問の中で既に答えている内容（前の確認への答え・質問の言葉）で `requirement.question` に答えられるなら、聞き直さずに 3. の形で呼び直す。
  2. 答えられなければ、`requirement.question` の問いを 1 つだけ、候補があれば候補の名前を添えて利用者にそのまま聞き、その回答では問いだけを返す（推測で選ばない・SQL や結果を作らない）。
  3. 呼び直すときは、同じ `question` と `profile_id` に、`requirement.session_id` を `requirement_session_id`、利用者の答えを `answer` に入れる。利用者が確認せずに進めると選んだら、`answer` の代わりに `skip_clarification: true` を渡す。
- `unanswerable` / `error`: `error_message`（答えられない理由・エラーの内容）を伝える。

## Step 4: してはいけないこと

- `needs_clarification` の問いを、別の業務プロファイルや言い換えた `question` で `nl2sql_query` を呼び直して避けない。
- 業務データのツールを呼ばずに、一般的な知識や推測で件数・金額・状態などの値を答えない。範囲外に見える質問も、範囲外かを調べる前に決めず、先に業務データのツールで調べる。調べても業務データで確かめられないときは、確かめられないため答えられないと答える。
- 結果の SQL は監査・説明用で、自分では実行しない。
