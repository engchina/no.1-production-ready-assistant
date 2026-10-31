---
name: article-catalog-lookup
description: "業務 RAG（MCP サーバー rag）の、条で構成された文書（法令・規程などを XML ツリー JSON で取り込んだ文書）から、質問の根拠になる条を、文書の条の目録で選んで本文を読み、確かめてから答える。根拠の取得（rag_retrieve_evidence）の根拠に答えを直接決める条が無いとき、事例の言葉と条文の言葉が違って検索で条が当たらないときに使う。資料の検索そのものは business-rag-research を使う。"
compatibility: "MCP サーバー rag（Production Ready RAG。rag_outline の format=compact がある版）に接続していること。"
license: MIT
metadata:
  production-ready-display-name: "条の目録から根拠を探す"
  production-ready-mcp-tools: "rag:rag_retrieve_evidence rag:rag_outline rag:rag_read_section"
  production-ready-evidence-planning: "rag:caller"
  production-ready-tags: "rag research articles"
---
検索で当たらない根拠の条を、文書の条の目録から選んで読む（MCP サーバー `rag`）。ツールは MCP の素の名前で書く（Agent によって `mcp__rag__` などの前置きが付く）。

## Step 1: 条の目録から根拠の条を選んで読む

1. いつもどおり `rag_retrieve_evidence` で調べる。根拠に答えを直接決める条があれば、ここで終える。
   - 根拠の `role` が `catalog_article` のものは、RAG が条の目録から質問の根拠として選んだ条（検索の語と条文の語が違っても選ぶ）。まずこれらを読み、質問に当てはまる条を根拠にする。足りないときだけ 2 以降で目録を読む。
2. 根拠に出た文書のうち、章節の名前が「第◯条」の形の文書を、根拠の順に 3 つまで選ぶ。
3. 文書ごとに `rag_outline` を `format: "compact"` で呼び、`lines` から根拠になりそうな条を 8 つまで選ぶ。
   - 事例の言葉ではなく、条の見出しの概念（資格・届出・期限・免除・時効・訂正・範囲・特例など）に置き換えて選ぶ。
   - 答えを直接決める条を先に、定義・手続の条を後にする。本則に無ければ附則（特例・経過措置）も見る。
4. 選んだ条を `rag_read_section` に `document_id` と `citation`（目録の行のとおり。例 `第11条の2`、`附則（制定時）第3条`）で読み、質問に当てはまる条だけを残す。
5. 残した条を根拠に答える（文書名と条を示す）。

## Step 2: 結果ごとの対応

- `rag_outline` の `sections_omitted` が 0 より大きい: 目録が途中で切れている。見たい章（`lines` の章の行）の `section_id` を、通常の `rag_outline`（`format` を付けない）で確かめ、`section_id` を付けて `format: "compact"` で読み直す。
- `rag_read_section` が見つからないと返した: `citation` を目録の行の書き方（附則は「附則（〈附則の名前〉）第N条」）に直して読み直す。
- 条で構成された文書が根拠に無い: このスキルは使わず、検索の根拠で答える。

## Step 3: してはいけないこと

- 読んでいない条を根拠にしない。条の番号を記憶や推測で書かない（目録にある条だけ）。
- 目録から選ぶのは 1 文書 8 条まで、文書は 3 つまで。
