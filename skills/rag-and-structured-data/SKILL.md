---
name: rag-and-structured-data
description: "業務 RAG（MCP サーバー rag の rag_retrieve_evidence）と業務データの照会（MCP サーバー nl2sql の nl2sql_query）を、順序を決めずに必要なだけ往復して調べる。規程の基準・期限で業務データを絞る、業務データで見つかった区分・コードの意味を規程で確かめる、など資料（手順・規則・定義・条件）と業務データ（件数・金額・状態・記録の値）の両方が要るかもしれない調査に使う。資料だけで答えられる質問は business-rag-research、業務データだけなら structured-data-query を使う。"
compatibility: "MCP サーバー rag（Production Ready RAG）と nl2sql（Production Ready NL2SQL）の POST /api/mcp に接続していること。"
license: MIT
metadata:
  production-ready-display-name: "資料とデータの横断調査"
  production-ready-mcp-tools: "rag:rag_retrieve_evidence rag:rag_recommend_search_answer_profile rag:rag_list_search_answer_profiles nl2sql:nl2sql_query nl2sql:nl2sql_get_job nl2sql:nl2sql_list_profiles nl2sql:nl2sql_recommend_profile"
  production-ready-evidence-planning: "rag:caller"
  production-ready-tags: "rag nl2sql business-data"
---
業務 RAG（資料。MCP サーバー `rag`）と NL2SQL（業務データ。MCP サーバー `nl2sql`）のどちらを使うかをその都度判断し、順序を決めずに繰り返し使って調べる。ツールは MCP の素の名前で書く（Agent によって `mcp__rag__` などの前置きが付く）。

## Step 1: 入力の種類を分ける

- 質問（業務の範囲外を含む）と、あいさつ・お礼に質問が混ざる入力（「こんにちは、休暇の申請方法は？」）: Step 2 に進む。
- あいさつ・お礼・その他の質問でない入力（雑談・意味の取れない短い入力・「テスト」のような試し）: ツールを呼ばず、短く応じてから、社内の資料と業務データについて何を知りたいかを聞く（例: 「こんにちは。社内の資料や業務データについて、知りたいことをお知らせください。」）。入力をなぞらず、資料の内容も業務データの値も一般的な知識も答えない。

## Step 2: 答えに足りないものを判断する

ツールを呼ぶ順序は決めない。Step 2〜4 の判断のループを、答えに要る根拠がそろうまで繰り返す。

問いと、それまでのツールの結果から、答えに足りないものをそのたびに判断する。

- 答えに要る根拠がそろった → それ以上ツールを呼ばずに答える（「回答の書き方」）
- 足りないものがある → Step 3

## Step 3: 足りないものに合うツールを呼ぶ

- 資料の根拠が要る → `rag_retrieve_evidence`（`query` はその根拠の実体と引く属性。RAG は回答を作らず根拠を返すので、答えは根拠から自分で書く）
  - 検索・回答プロファイル（`search_answer_profile_id`）が決まっていなければ、最初に 1 回 `rag_recommend_search_answer_profile` で選び、以後も同じものを使う（業務 Agent にデータの範囲があれば、その範囲の中から選ぶ）。`recommended_profile_id` が null で候補が 2 件以上なら、候補の名前を選択肢にして利用者に確かめる。候補が無ければ `rag_list_search_answer_profiles` で選ぶ。
- 業務データの値が要る → `nl2sql_query`（`nl2sql_query` が先でもよい）
- どちらで調べるかは、値がどこに載っているかで決める。1 つの質問に、資料の値と業務データの値の両方が要ることがある。
  - 挙げる・数える対象とその値が、台帳・一覧・名簿・規程の表のような資料そのものに載っている質問（システムの重要度・機密区分・担当部署で挙げる・数えるなど）は、資料（`rag_retrieve_evidence`）で調べる。
  - 業務の対象（受注・売上・商品・顧客・請求など）の値（単価・金額・件数・日付・状態）は、業務データ（`nl2sql_query`）で引く。業務プロファイルの説明と対象の表にある取引・記録の値を集計するときも `nl2sql_query` を使う。
  - 規程の名前で始まる質問でも、規程の基準（金額の区分・上限・期限）は資料で、それを当てはめる業務の対象の値は業務データで引き、両方がそろってから答える。
    - 例: 「出張旅費規程の宿泊費の上限を超える単価の商品はあるか」→ 上限を `rag_retrieve_evidence` で、商品の単価を `nl2sql_query` で引いて比べる。
    - 例: 「規程で課長の承認が要る金額の注文は何件か」→ 金額の区分を `rag_retrieve_evidence` で、注文の金額を `nl2sql_query` で引いて数える。
  - 業務の対象の値が資料の結果に無いときは、「資料からは確認できない」と答える前に `nl2sql_query` で引く。
  - `nl2sql_query` の `question` には、資料で分かった基準を値の条件に置き換えて書く（例: 「単価が 10,000 円以上の商品」「金額が 100,000 円未満の注文」）。規程の言葉（承認者・領収書・手当の名前）のまま渡すと、業務データの列に当てはめられずに確認を返される。
  - `nl2sql_query` が対象の表が無い・答えられないと返したら、そこで終えずに資料（`rag_retrieve_evidence`）を引く。
  - 業務プロファイル（`profile_id`）が分からなければ `nl2sql_recommend_profile` か `nl2sql_list_profiles` で選ぶ（業務 Agent にデータの範囲があれば、その範囲の中から選ぶ）。
- 一方の結果で分かった事実・条件・識別子（コード・名前・期間・区分・状態）を使って、もう一方や同じツールを何度でも呼んでよい。
  - 例: 規程で分かった基準・期限で業務データを絞る。
  - 例: 業務データで見つかった区分・ID の意味や扱いを資料で確かめる。

## Step 4: 結果を確かめて Step 2 に戻る

- 結果に応じて「結果ごとの対応」に従ってから、Step 2 に戻る。
- 同じ目的で言い換えて呼び直しても新しい根拠が出ないときは繰り返さず、確かめられなかった点として示して、分かったことまでを答える。

## Step 5: 結果ごとの対応

- `rag_retrieve_evidence` の結果に `clarifications`（検索の前に確かめる条件）がある: 検索していない。問いを利用者に確かめ、答えを `conditions` に入れて呼び直す。
- `rag_retrieve_evidence` の結果に `profile_knowledge`（検索・回答プロファイルで承認された判断の方針 `rules` と用語の説明 `terms`）がある: 資料の原文ではない。根拠をどう読み、質問にどう当てはめるかの判断に使い、文書名と場所を示す根拠としては引用しない。
- 資料の根拠だけでは確定できない現場の値・記録（件数・状態・設定値）は、`nl2sql_query` で確かめてから答える。
- `nl2sql_query` が `pending` / `running` を返した: `nl2sql_get_job` で続きを取る。
- `nl2sql_query` が `needs_clarification` を返した: SQL はまだ作っていない。
  - 利用者がこの質問の中で既に答えている内容（前の確認への答え・質問の言葉）で `requirement.question` に答えられるなら、利用者に聞き直さず、その答えを `answer`（候補に無い答えは `free_text` に利用者の言葉のまま）、`requirement.session_id` を `requirement_session_id` に入れて呼び直す。例: 利用者が「その列が無ければ、無いと伝えて残りだけ出して」と答えていて、問いがその列を尋ねている → `free_text` に「該当の列は無い。その条件を外して集計する」と入れて呼び直す。
  - 答えられないときだけ、`requirement.question` の問いを 1 つだけ利用者に聞き、答えを `answer` に入れて同じく呼び直す。問いを回答の本文だけにして終えない（確認として聞く）。利用者が確認せずに進めると選んだら、`answer` の代わりに `skip_clarification: true` を渡す。
  - 確認の答えを待たずに、別の業務プロファイルや言い換えた `question` で `nl2sql_query` を呼び直して問いを避けない。
- 組み込みの Agent では、ツールの結果に `next_step`（次にすること）が付くことがある。付いたらそれに従う。

## Step 6: 回答の書き方

- 確かめた値は NL2SQL の結果を、手順・規則は資料の根拠を出所として分けて示す。

## Step 7: してはいけないこと

- 業務データの表名・列名を利用者に聞かない。表・列は `nl2sql_query` が業務プロファイルの schema で確かめ、決まらなければ `needs_clarification` で問いを返す。
- 業務データのツールを呼ばずに、業務の対象の値（商品の単価・受注の金額・日付など）を「資料からは確認できない」「業務データで確認する必要がある」と答えない。
- 資料の根拠のツールも業務データのツールも呼ばずに、一般的な知識や推測で業務の事実を答えない。範囲外に見える質問も、範囲外かを調べる前に決めず、先に資料・業務データのツールで調べる。調べても資料・業務データで確かめられないときは、確かめられないため答えられないと答える（質問が曖昧で対象が分からないときは、確認の質問をする）。
