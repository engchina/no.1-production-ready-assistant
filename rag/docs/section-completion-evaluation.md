# 章節補完の比較と追加改善の判断

対象は #2759・#2760・#2761。最低1段の補完は仕様上の契約であり、モデルの十分との自己申告とは別に検証する。
文書側は章節・原文と active chunk_set を持ち、検索・回答プロファイルは補完の方針と予算を持つ。
KB は文書の集合だけを持つ。

## 実装の分類と採否

| 候補 | 分類とコードの根拠 | 判断 |
|---|---|---|
| 章節構造・版・頁・権限を保つ原文読取 | 実装済み。`backend/app/rag/section_map_service.py`、`section_chunk_map.py`、`answer_engine.py::_section_chunks` | 継続。取得結果の `complete` と件数をエンジンへ渡し、部分取得を全量扱いしない |
| 小さい命中から所有章節を読む | 実装済み。`generation/section_expansion.py::expand_sections` | 採用。すべての異なる所有章節に最低範囲を適用する |
| 標準 RAG と全取得経路への適用 | 実装済み。`generation/answering.py::answer_question_result` の共通補完、`_expand_context_by_sections` の回答単位の状態 | 採用。主検索・手順別・不足要求検索後に適用。検索専用 MCP は呼び出し側の読取に任せる |
| 複数の要求・文書への起点配分 | 一部実装。`seed_chunk_ids` と所有章節の重複除去。固定2起点は廃止。入力・時間の共有予算は維持 | 最低範囲の対象から後ろの起点を無言で落とさない修正を採用。要求ごとの優先順位の最適化は #2573 の実モデル評価待ち |
| 未命中の別枝の発見 | 一部実装。`retrieval/article_catalog.py` の条目録による選択。一般の不足論点への網羅的な探索は未実装 | #2623 に集約。祖先を読むことを別枝の発見の代わりにしない |
| 子の取得率による親への再帰的な自動統合 | 未実装。`expand_sections` は十分性と最低範囲で上がり、子の取得率で昇格しない | 保留。評価なしで閾値を導入しない。既存の段階的な展開とは別の比較対象 |
| 操作・表・一覧のまとまりを保つ | 一部実装。`retrieval/context_builder.py` の親子・近傍、`section_chunk_map.py` の要素境界 | 継続。意味上の完結性は構造だけで保証できない。後続操作・注記・条件を含む合成ケースを追加して比較する |
| 圧縮と配置 | 実装済みだが要評価。`generation/section_extraction.py::SectionExtractor` は原文照合を行う。入力順は命中根拠の後ろに章節原文 | 継続。抜粋を最低範囲の全量読了にしない。配置変更・恒常的な要約は効果未測定なので保留 |
| 検索から最終回答までの評価 | 一部実装。`tests/section_expansion_benchmark.py` と生成入力までの backend 統合テスト。実モデルの広い評価は #2573 | 決定論の回帰ゲートを採用。実モデルでの品質改善率とは区別する |
| 索引前の生成文脈補足 | 見出し・親の文脈は既存。生成による文脈補足の採用評価は #2593 | 保留。今回の検索後の原文補完と混同しない |

既存 Issue を再利用し、#2623・#2573・#2593 と同じ開発 Issue は作らない。
以上の追加候補は #2760 の最低範囲契約の前提にはしない。

## 同一の合成5問による比較

実行: `cd rag/packages/rag_engine && uv run python -m tests.section_expansion_benchmark`

所有章節で完結、親が必要、祖父が必要、3つの所有章節、入力予算不足の5ケースを同じ資料・同じ命中で比較する。
期待根拠は chunk の ID の集合、手順はその集合のグループとしてラベルを持つ。
十分性の判定は決定論スタブで、補完前には意図的に誤った十分判定を返す。
適応方式は期待根拠の集合を知る理想化した評価器を用い、実 LLM の能力の測定とはしない。
固定親・祖父は指定した段に達したら十分と返す比較用の評価器である。

| 方針 | 必要根拠 Recall | 手順充足 | 誤十分判定率 | 不要 chunk 平均 | 本文 token 見積平均 | 原文読取平均 | 判定平均 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 補完なし | 0.3333 | 0.30 | 1.00 | 0.4 | 416.0 | 0.0 | 0.0 |
| 所有章節のみ・評価器なし | 0.6333 | 0.70 | 0.00* | 0.8 | 832.0 | 1.4 | 0.0 |
| 親まで | 0.7333 | 0.80 | 0.20 | 3.6 | 1782.8 | 3.0 | 1.8 |
| 祖父まで | 0.8667 | 0.90 | 0.00 | 5.6 | 2498.0 | 3.6 | 2.4 |
| 適応的な段階展開 | 0.8667 | 0.90 | 0.00 | 2.4 | 1545.6 | 2.6 | 1.6 |

\* 所有章節のみは内容が十分とは判定しないため、誤十分判定は0となる。手順充足0.70の通り、最低範囲の補完だけで
回答の完結を保証しない。予算不足ケースでは必要根拠が残り、未読を示して止まる。
実行結果の各行に `elapsed_ms`（実測）、入力 token の見積もり、引用の原文 ID、未読数、読了状態も出す。
ローカルの実測時間は I/O・LLM をスタブにした値であり、OCI や Oracle の時間・費用ではない。
この表から実サービスの高速化率や品質向上率を主張しない。

同じ5問で、固定祖父より少ない不要文脈・入力で同じ取得 Recall になるため、最低範囲と適応的な展開を維持する。
この結果は子の取得率で統合する方式を測っていないため、その方式の有効化の根拠にはしない。

## 追加候補を比較する計画

#2573 の評価では、上記5種類に適用外分岐、否定・例外、表の注記、章節跨ぎ、版切替、権限外を加え、
補完なし・所有章節・親・祖父・子の取得率による統合を同じ命中候補で比較する。
検索直後、補完後、最終生成入力、最終回答の4点にラベルを付け、根拠 Recall、要求/手順充足、誤十分判定率、
不要文脈、適用外分岐の混入、引用の原文/頁、版と権限、入力/出力 token と料金、待ち時間を分けて保存する。
原文の抜粋には前提・否定・例外・後続操作が残ったかのラベルを付ける。

自動統合の閾値は校正用と検証用のケースを分けて設定し、欠落・権限漏れ・引用の誤りを増やさず、
同等の Recall と充足で入力または待ち時間を減らせた場合にのみ採用する。効果がない、または未測定なら保留する。
#2623 の未命中の別枝は祖先展開とは別の軸、#2593 の索引前補足は索引の版を揃えた別実験にする。

## 参考となる公式の方式

- [LangChain ParentDocumentRetriever](https://reference.langchain.com/python/langchain-classic/retrievers/parent_document_retriever/ParentDocumentRetriever): 小さい検索単位から大きい原文へ戻す参考。
- [LlamaIndex Auto Merging](https://developers.llamaindex.ai/python/framework/integrations/retrievers/auto_merging_retriever/): 子から親への統合の比較対象。
- [LlamaIndex Sentence Window](https://developers.llamaindex.ai/python/examples/node_postprocessor/metadatareplacementdemo/): 生成前に周辺文脈を復元する参考。
- [LangChain Contextual Compression](https://www.langchain.com/blog/improving-document-retrieval-with-contextual-compression): 関連部分の抽出と選別の参考。
- [Anthropic Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval): 索引前補足は今回とは別の評価軸。
- [Lost in the Middle](https://arxiv.org/abs/2307.03172): 長い入力の配置も評価する理由。

参考実装の provider やライブラリを導入せず、Oracle AI Vector Search、OCI Generative AI の rerank、
OCI Enterprise AI の既存構成で評価する。普遍的な固定の読取段数という業界標準は前提にしない。
