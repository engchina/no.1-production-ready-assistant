# 検索・回答フロー高度戦略の実装計画(段階導入)

> 本ドキュメントは、回答フロー(`app/rag/answer_engine.py`・`packages/rag_engine`)に追加しうる
> **実行配線が重い高度な検索・回答方式** の設計メモをまとめる。いずれも確定スタック
> (モデルの API〔OpenAI 互換の API。LLM・埋め込み・再ランク〕/ Oracle AI Database)を不変とし、外部ベクトル DB・別
> LLM provider は導入しない。GPU / 版管理 schema DDL / 実 Oracle を要するものは **本リポジトリの
> CI(GPU・実 DB なし)では検証不能** のため、決定論部分の実装 → 実環境配線 → 検証の順で導入する。
>
> #595 で旧 standard の回答エンジンを削除し、この計画の土台だった検索モードの選択
> (`rag_retrieval_strategy`・`app/rag/retrieval_strategy.py`・`retrieval_adapter.py`)、PageIndex-lite の
> ツリー検索(`app/rag/reasoning_tree.py`)、検索・根拠確認・回答生成の stage サービス
> (`services/pipeline/{retrieval,grounding,agentic,generation}`)も削除した。以下の戦略を取り入れる場合は、
> 回答フローの検索(`AnswerEngine._search`。原質問主軸の重み付き RRF)に経路を足す形で設計し直す。

最終更新: 2026-09-30

## 現在の状態

| 戦略 | 種別 | 現状 |
|---|---|---|
| `reasoning_tree_search`(PageIndex 型) | 検索方式 | PageIndex-lite の実装(navigation 要約から LLM が section を選ぶ)と検索モードの選択は #595 で削除した。回答フローでは、画面目録で操作画面を探す(`RAG_SCREEN_LINKING_ENABLED`。番号付きの見出しの目録から LLM が画面を選ぶ)が近い役割を持つ |
| `colpali_visual_retrieval`(ColPali 型) | 検索方式 | 戦略の登録ごと #595 で削除した。未着手 |
| `self_reflective`(Self-RAG) | 回答生成 | 未着手。回答フローの回答文の生成後の根拠確認(監査)が近い役割を持つ |
| Temporal GraphRAG | 文書構造索引の構築/検索 | 未着手(未実装だった設定 `RAG_GRAPH_TEMPORAL_ENABLED` は #301 で削除) |
| RAPTOR 検索時昇格 | 検索 | 取込側で summary node を索引済。通常の検索が summary node にヒットする |

---

## 1. reasoning_tree_search(PageIndex 型 vectorless 推論検索)

### 目的
cosine 類似度ではなく **LLM が章節 tree を navigation** して関連 section を選ぶ。専門文書(金融・
法律・技術マニュアル)で、検索経路が監査可能(どの section を展開/スキップ/命中したか)になる。

### 設計メモ
- **tree 構築(取込時 or 検索時キャッシュ)**: 既存の `DocumentElement.section_path` /
  `parent_id` 階層から、文書ごとに `section tree`(node = {title, summary, page_range,
  child_ids})を構築。要約は LLM(RAPTOR と共用可)。
  既存 `navigation` JSON(`app/rag/navigation.py`)を再利用して node を永続化する。
- **検索時 navigation**: LLM に「query + 現在 node の title/summary 群」を渡し、各 node
  で yes/no(展開/スキップ)を JSON で判断 → 命中 leaf の chunk を Oracle から取得。踏破 node 列は
  回答フローの実行記録(`diagnostics.answer.execution_steps`)に残して監査可能にする。
- **融合**: 回答フローの検索の RRF に 1 チャネルとして加える(画面目録の候補と同じく、足すだけで減らさない)。
- **opt-in / コスト**: query ごとに複数 LLM 呼び出し。深さ・幅の上限を設け、失敗/未設定時は通常の検索だけで回答する。

---

## 2. colpali_visual_retrieval(VLM late-interaction 視覚検索)

### 目的
ページ画像から **OCR を介さず直接検索**。複雑レイアウト(表・図・多欄)・スキャン PDF の検索精度を
上げる。視覚特徴(multi-vector / late interaction)で query とページをマッチング。

### 設計メモ(要 GPU + 版管理 schema DDL)
- **取込時**: PDF の各ページを画像に描画する(前処理 `pdf_to_page_images` は #3347 で削除した)→ **GPU サービス**(ColQwen/ColPali を
  transformers でロード)で **multi-vector embedding** を生成。画像対応モデル（VLM）の経路でも近似可能だが
  multi-vector が要点。
- **索引(schema 変更)**: Oracle AI Database に視覚 embedding 列/表(`rag_page_visual_vectors`、
  `VECTOR` 複数 or per-patch 行)を追加。**版管理された schema DDL artifact の変更が必要**
  (`requires_reprovision`、自動変更しない)。
- **検索時**: query を同モデルで embedding 化し、**late interaction(MaxSim)** スコアで page を
  ランク。回答フローの検索の RRF にチャネルとして加える。
- **opt-in / コスト**: GPU 常時必要。未配線/未設定なら通常の検索だけで回答する。

### 注意
- `ExtractionMetadataValue`(scalar)/ 確定スタックは不変。multi-vector は別表で持つ。
- CI は GPU 非搭載のため、remap/契約/縮退を fixture で検証し、実 GPU は手動/staging 検証。

---

## 3. 残りの高度戦略(設計メモ)

- **self_reflective(Self-RAG)**: LLM が
  `{"answer":..., "confidence":0-1, "grounded_in_context":bool}` を出力し、`confidence<閾値` or
  `grounded_in_context==false` で **1 回だけ再検索**する。回答フローの CRAG(根拠の評価と補正検索)と
  回答文の生成後の根拠確認が同じ役割を持つため、足すならその中で扱う。
- **Temporal GraphRAG**: 取込時に entity / relationship へ timestamp(`valid_from/valid_to`)を付与し、
  検索時に query の時間文脈(「最新の」「2024 年時点」)を抽出して Oracle の条件でフィルタする。
  回答の検索で graph を使う経路は #595 で削除したため、検索時の利用から設計し直す。設定は実装と同時に
  追加する(未実装の設定は置かない。#301)。今の文書構造索引の構築(`entities`)は文書と章・節の見出しの
  つながりだけを作る(読む経路の無かった claims / community summary は #621 で削除した)。
- **RAPTOR 検索時昇格**: 既に summary node を索引済。leaf hit 時に対応する summary node を回答の文脈へ
  昇格する経路を、回答フローの small-to-big(親本文・前後の child)と合わせて追加する(opt-in)。

---

## 検証方針(共通)

- 決定論部分(tree navigation 判定の集約、late-interaction スコアの数値、reflection パース)は
  injectable + unit test で CI 緑にする。
- LLM/VLM/GPU/Oracle を要する実行は **staging(実モデルの API/Oracle/GPU host)** で結合検証し、
  file-processing / retrieval staging gate(`docs/evaluation`)に指標(retrieval recall / table QA /
  page hit / tree path coverage)を合流させる。
- すべて opt-in・未配線時は既存の回答フローの挙動のままにし、既定の挙動・レイテンシを変えない。
