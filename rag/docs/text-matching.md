# 非モデル照合の基礎機能

## 責務と導入の判断

`rag_engine.matching` は、解析・条件・順位・理由の四層を持つプロセス内の純粋な部品です。
LLM・embedding・reranker・DB・外部 API を呼びません。呼出側が先に権限と対象範囲を確かめた候補だけを渡します。
一致は語の検出であり、規則の適用、条件の確定、同意、承認、事実の裏付けではありません。

初期利用者は回答ルールです。検索・回答プロファイルの知識の層に属し、KB の membership と文書レシピには影響しません。
Agent / NL2SQL に同じ入力・出力・最終判断の契約が必要とは確認できなかったため、platform へ抽出しません。
動的なルールの適用・競合の解決は #3664、確認への自由文の否定・曖昧性の判断は #3657 の責務です。

## 四層の契約

| 層 | 型と入口 | 保証 |
| --- | --- | --- |
| 解析 | `AnalysisPolicy` / `AnalyzedText` / `analyze` | 原文と解析文字の対応、解析版、方式を保持 |
| 条件 | `Clause` / `Condition` / `evaluate` | exact・term・phrase・明示した substring、must・should・exclude・最低一致数 |
| 順位 | `CandidateMatch` / `rank_candidates` | 同義語グループの最大寄与、有限の非負の重み、入力順・キーによる安定同点順、上限 |
| 理由 | `ClauseMatch` / `ConditionMatch` / `RankedMatch.explanation` | 原文範囲、フィールド、条件名、寄与、未充足条件、候補上限による除外 |

`score_kind=ranking_only` は信頼度でも指示の優先度でもありません。除外条件は寄与を加算しません。
同じグループの別名・同じ語の反復・複数フィールドへの重複登録は最大の一票です。
最低一致数も異なるグループを数えます。独立した要件は別グループとして明示します。
フィールドの重要度や phrase の重みは `Clause.weight` で用途が決めます。識別力による重みや BM25 は導入していません。
小規模の規則へ全文検索の計算方式を自動適用しません。

肯定条件なし、条件名の重複、空の語、最低一致数の範囲外、非有限・負の重みは設定エラーです。
`must` に対象を置き、同じ `Clause.alternatives` に操作の別名を設定した `should` を置けば、対象 AND 操作のいずれかを表現できます。
自然文の否定を `exclude` が理解するという意味ではありません。自由文の意味上の確定結果は型にありません。

## 言語・位置・識別子

- 既定は NFKC、casefold、空白統合です。位置は入力した原文の Python codepoint による半開区間です。UTF-8 byte・JavaScript UTF-16 の位置ではありません。
- 全角・半角の展開、濁点・結合文字・Hangul の合成、casefold の展開、空白の統合後も原文位置を返します。変換が一対一でないときは元の文字群の範囲を返します。
- 解析版は Unicode データの版と、正規化・casefold・空白・分詞方式を含みます。同じ入力・方式・版で決定論的に処理します。
- `unicode_words` は Unicode の文字列のまとまり、`cjk_characters` は日中の文字単位です。形態素・意味の解析ではありません。無空白の日中文中の語を探す用途は、明示した phrase を使います。任意の部分文字列は substring として区別します。
- phrase は英数字の途中を切り出しません。term は選んだ解析方式の完全な token、exact は解析後の値全体です。
- コードや ID は `normalization=literal, casefold=False, whitespace=preserve` と exact を呼出側が指定できます。Oracle の引用付き識別子などへ NFKC を一律に適用しません。
- 一つの条件が返す範囲は既定 8 件までです。原文自体を説明 payload や一般ログへ複製しません。保存と表示は既存の権限・保存期間に従います。

## 回答ルールの移行契約

`knowledge.rule_matching.rank_rules` が用途別方針を持ちます。初期移行では既存の保存形式と意味を変更しません。

- 有効状態、title と triggers の対象、各ラベル 3 点、既にラベルが命中した場合だけ本文の共通語に 1 点を保ちます。本文の語だけでは候補にしません。
- 英数字・アンダースコアだけのラベルは既存の ASCII 語境界、それ以外は既存の部分一致を明示して使います。新しい基礎機能の term / phrase を既存の日本語ルールへ暗黙に適用しません。
- 既存の文字列が異なれば意味上の同義語とは推測しません。既存の正規化で同一のラベルだけ重複除去し、意味上のグループを新しく保存値へ付与しません。
- 上限（通常 6 件）、0 以下の上限を 1 件とする既存の挙動、同点での入力順を保ちます。
- 呼出側が選んだ `pinned` は既存の別経路です。語が命中したと偽らず `caller_selected` を記録します。タグは認可・同意の根拠ではありません。
- 既存の `expansion_decisions` に理由を加え、命中したフィールド・範囲・寄与・rank・上限除外を保存します。範囲の元は `offset_source=question_input` です。表示用に正規化した質問の位置と取り違えないでください。
- OR 条件で不命中のラベルの詳細はルールごとの診断へ複製せず、未充足条件を保持します。基礎層の `evaluate` は全条件の結果を返せます。

## 全製品の棚卸しと段階的な移行

調査基点は `7f8649a2b5`。Python の `NFKC` / `casefold` / `match` / `similarity` と既存テストを確認し、検索の候補取得と、照合結果を使う最終判断を分けました。
UI の検索欄や認可の filter、SQL の文法解析を同じ照合器で置換する調査ではありません。

| 製品・経路 | 入出力・言語・規模 / 上限 | 既存の正規化と最終責務 | 状態・理由 / 主な回帰 |
| --- | --- | --- | --- |
| RAG `runtime_knowledge._matched_rules` | 質問と最大 1000 ルール、各 32 trigger、通常 6 候補。日中英の設定語 | NFKC・casefold・空白、ASCII 語境界と部分一致。適用・権限は呼出側 | **接続済み**。`test_rule_matching` / `test_runtime_knowledge*` |
| RAG `_matched_terms` | 最大 1000 用語、各 32 別名、通常 8 候補 | 正式名と別名で重みが異なる。検索語の展開が目的 | **正規化キーだけ共通化**。順位の移行は別評価が必要。`test_runtime_knowledge*` |
| RAG `domain_keywords._keyword_matches` | 管理語最大 1000、抽出上限 16 | 語境界・長い語優先、包含語の抑制 | **保留**。抽出契約をルールの候補順位へ変えない。`test_domain_keywords` |
| RAG `approved_faq._best_question_match` / `_similarity_score` | 質問と FAQ 質問群、候補数と閾値は既存設定 | lower・空白除去・末尾句読点・包含比・n-gram。任意の embedding と scope の寄与は別層 | **保留**。直接回答の閾値と意味照合を維持。`test_approved_faq*` |
| RAG `support_guide_runtime.score_guide` / `support_guide_query` | 許可された公開ガイドの語・別名・質問例と質問。例の類似度・任意の embedding | 空白除去の NFKC・casefold。公開版・条件・分岐・既知条件で最終決定 | **保留**。空白除去と例の類似度を同じ語照合に置換しない。`test_support_guide_matching` |
| RAG `search_answer_profile_knowledge._already_answered` / `rule_selection` | 現在の選択肢 ID と構造化した回答、必要時は自由文 | 候補の語照合と分離。構造化回答・単独の表示名、または一括認識後に原文照合した選択だけを確定 | **専用の final decision**。否定・未定・比較を語の包含だけで肯定しない。`test_rule_selection` / `test_search_answer_profile_clarification` |
| RAG `text_search_tokenizer` / `OracleClient` | 検索文・言語資源から Oracle Text の問い合わせと順位付き chunk | 既存 Sudachi / stemming / 検索文 escaping、ACL・KB・active chunk_set の絞り込み | **変更なし**。DB 索引と検索文は既存基盤。`test_text_search_tokenizer` / Oracle の統合テスト |
| Agent `evidence_match.matched_features` / `order_weights` | 回答の ID・コード・引用・章節と、今回許可された根拠 | NFKC・空白除去、型別に特徴を集めて出典の順を決める | **対象外**。ルール候補と異なる証拠契約。`test_answer_evidence` / `test_validation_evidence_order` |
| Agent `guide_steps` / `support_task` | RAG MCP のガイド・手順・確認結果と現在の Run | 条件・版・構造化した選択肢の対応。自由文の取消も専用処理 | **対象外**。MCP の境界・既知条件・承認を維持。`test_guide_steps` / `test_clarification_cancel` |
| Agent `output_fields.output_value_matches` | 型付きの期待値と実際の出力 | 日付・数値・文字列の型別の正規化 | **対象外**。出力評価を単語の命中に変えない。`test_agent_output_fields` |
| NL2SQL `object_identity.object_match_key` / `catalog_match_key` | 引用付き owner / object / column の名前とカタログ | 引用符・大文字小文字の区別。権限とは別 | **対象外**。破壊的な正規化は不可。`test_nl2sql_object_identity` |
| NL2SQL `service._profile_recommendation_tokens` / 類似履歴 | 利用可能なプロファイルと質問、所有者で絞った履歴 | NFKC・n-gram・template 除去と相対スコア。候補提示だけ | **保留**。既存の精度評価が先。`test_recommend_confidence` / `test_nl2sql_similar_history_pool` |
| 各製品 security router / 一覧 filter | 利用可能な対象の ID・状態・検索欄 | 名前の部分一致は UI の絞込。ACL・所有者・状態の判定は backend | **統合対象外**。候補照合を認可へ昇格させない。各製品の security テスト |

ガイド・FAQ の任意のモデルによる意味照合は残します。この移行で無断停止・非モデルへの fallback の成功扱いをしません。
今後同じ契約を必要とする製品が増えた時点で、必要な公開型だけ platform に抽出します。製品同士を import しません。

## 基線・評価・性能

再実行: `cd rag/packages/rag_engine && uv run --locked python -m benchmarks.text_matching`。
基線は benchmark 内の移行前の選択関数です。実行時は import しません。13 組の合成入力で選択と順位を同じ条件で比較します。
文字幅、別名、語境界、日中英、重複、無効、同点、top-k、多数候補に加え、否定・比較・話題切替は「非モデルの語検出の限界」を期待値にします。
これらの場面で語の存在は検出しても、意図や対象が確定したとは主張しません。

2026-10-27 の同一ローカルプロセス、warmup 後 9 回、時間計測とは別の 1 回を tracemalloc で測定した結果:

| 合成入力 | 基線 p95 / peak | 理由付き p95 / peak | 選択・順位 |
| --- | --- | --- | --- |
| 1000 条・各 1 trigger・同点 | 84.1 ms / 299.7 KiB | 51.5 ms / 2764.4 KiB | 同一 |
| 1000 条・各 32 trigger | 1883.5 ms / 332.9 KiB | 466.8 ms / 2772.8 KiB | 同一 |

13 組すべて順位が同一、合成の期待値に対する誤命中・漏れは 0 件でした。これは意味上の適用精度の向上を示す測定ではありません。
初版は後者で p95 2254 ms / 22210 KiB だったため、そのまま採用せず、反復する正規表現のコンパイルと不命中条件の診断の保持を削減して再測定しました。
理由付きのメモリ増加は残ります。候補数は既存の上限で抑え、原文を一般ログへ出さず、モデルの呼出も増やさない初期移行として採用します。
速度は実行環境に依存するため CI の絶対時間の合否条件にはしません。多言語の単語区切りの一般的な精度や、実業務の正解率は未評価です。

設計参考: [bool query](https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-bool-query) の組合せと最低一致数、
[dis_max](https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-dis-max-query) の同じ意味の最大寄与。
検索サービスや新しい DB は導入していません。
