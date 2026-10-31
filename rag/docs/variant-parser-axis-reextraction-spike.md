# 設計 spike: parser/前処理 軸の再抽出(1 文書 N 抽出)

> ステータス: **設計確定・未実装**(2026-06-21 主要判断確定)。variant 後続 #6。P1 から実装着手可。
> 関連: [multi-recipe-variants-decision メモリ], `app/rag/variant_keys.py`, `app/rag/variant_planner.py`,
> `app/api/routes/documents.py`(materialization loop), `app/clients/oracle.py`(extraction/chunk_set 永続層)。

## 1. 問題(現状のギャップ)

現状の variant materialization は **chunking 軸しか実際には効かない**。

- `chunk_set_id = hash(source_sha256, **preprocess, parser**, chunking…)`(`_CHUNK_SET_FIELDS`)で、
  **preprocess / parser を既にキーに含む**。
- ところが**抽出(StructuredExtraction)は 1 文書につき 1 つ**しか持たない
  (`rag_documents.extraction` 単一 JSON 列、`save_extraction` で UPDATE)。
- materialization loop は各 chunk_set で `detail.extraction`(**同じ 1 つの抽出**)を再分割するだけ。
- ⟹ **parser/preprocess が違う chunk_set も、owning(最初/既定)の抽出を共有**し、
  parser 差分は**チャンクに反映されず静かに無視**される。chunk_set_id は別だが中身の親抽出は同じ。

> 例: KB-A=`parser=docling` と KB-B=`parser=unstructured` に同一文書が所属 → chunk_set は 2 つできるが、
> 両方とも **先に確定した parser の抽出**を分割する。後者の parser は効いていない。

当初ユーザ要望「1 文書 × N(**前処理 × Parser** × Chunking × Index)」の **前処理/Parser 軸が未成立**。

## 2. ゴール

1 文書が **preprocess×parser の組合せごとに別の抽出**を保持できるようにし、
chunk_set がその正しい抽出を分割する。extract(高コスト)は **parser グループごとに 1 回**、
chunking 変種はその抽出を再利用する。

## 3. キーイング

`compute_extraction_id` を追加(preprocess+parser のみ）:

```
_EXTRACTION_FIELDS = ("rag_preprocess_profile", "rag_parser_adapter_backend")
extraction_id = hash(source_sha256, _EXTRACTION_FIELDS)            # 例: "ex_<sha1先頭>"
chunk_set_id  = hash(source_sha256, preprocess, parser, chunking…) # 既存のまま
```

- 不変条件: **chunk_set の preprocess+parser から extraction_id が一意に決まる**
  (chunk_set_id ⊃ extraction_id の入力)。chunk_set は「自分の親抽出」を計算可能。
- 派生層(metadata/graph/nav)の keying は変更不要(chunk_set_id に重ねる現行のまま)。
  ただし将来 metadata(field 抽出)が抽出由来なら extraction 層へ寄せる余地あり(別件)。

## 4. ストレージ

### 新表 `rag_document_extractions`
| 列 | 用途 |
|---|---|
| `extraction_id` VARCHAR2(64) PK | preprocess+parser のキー |
| `document_id` VARCHAR2(64) FK→rag_documents ON DELETE CASCADE | 親文書 |
| `tenant_id_hash` | テナント |
| `recipe_subset` JSON | `{preprocess, parser}`(可視化・drift 用) |
| `extraction_json` JSON / CLOB | StructuredExtraction 本体 |
| `status` VARCHAR2(32) | `EXTRACTING` / `EXTRACTED` / `ERROR` |
| `quality_json` JSON | IngestionQualityReport(parser_profile 等) |
| `created_at` / `updated_at` TIMESTAMP TZ | |

- index: `(document_id, status)`。
- `rag_chunk_sets` に **`extraction_id` 列追加**(FK→rag_document_extractions)。各 chunk_set が親抽出を指す。

### 抽出数上限(2026-06-21 決定: 設ける)
- **`MAX_EXTRACTIONS_PER_DOCUMENT`(既定 8 程度)** を planner / materialization に設ける。
  parser×preprocess の組合せ暴発を防ぐ。上限超過は plan 構築時に **owning を優先して打ち切り + warning**
  (取込は止めない、縮退)。chunk_set 側の既存上限とは別軸。

### 既存 `rag_documents.extraction` の扱い(2026-06-21 決定: 即時に新表正本へ)
- **データが無いため段階移行(ミラー期間)は不要**。最初から **`rag_document_extractions` を正本**にする。
- migration は「新表 + `rag_chunk_sets.extraction_id` 列作成 + 既存 `rag_documents.extraction` があれば
  owning extraction_id で 1 行 backfill(データ無しなら no-op)」。`rag_documents.extraction` 列は
  **下位互換の縮退読み用に当面残す**が正本ではない(後日 drop は別 migration)。
- 版管理 migration artifact、**実 Oracle AI Database で検証必須**(未検証 DDL は積まない方針)。

## 5. Materialization(loop の再構成)

現状: extract 1 回 → chunk_set ごとに index(同一抽出を再利用)。

新:
```
plan.extractions = { extraction_id: { recipe(preprocess,parser), chunk_set_ids: [...] } }   # plan を 2 段に
for extraction_id, group in plan.extractions:               # parser グループ単位
    if not exists(extraction_id):
        extraction = run_extract(group.recipe)              # ← parser/preprocess 別に 1 回だけ(高コスト)
        upsert_extraction(extraction_id, document_id, extraction, status=EXTRACTED)
    else:
        extraction = load_extraction(extraction_id)         # 既存を再利用(再 parse しない)
    for chunk_set_id in group.chunk_set_ids:                # chunking 変種
        index(extraction, chunk_set_id, extraction_id=extraction_id)   # 抽出再利用 + chunk_set タグ
reconcile(plan)                                              # extraction/chunk_set/binding/GC
```

- `IngestionPipeline` を **extract と index に分離**(現状 `ingest`=extract+index、`index_reviewed`=index のみ)。
  新たに **「extraction_id を指定して extract する」/「extraction を渡して index する」**を明示化。
- 成功 metric/audit の集約(#4 `record_outcome`)は **最後の (extraction×chunk_set) のみ True** に拡張。

## 6. plan_planner の変更

`variant_planner` に extraction 層を足す:
- 入力: source_sha256, global_settings, kb_configs。
- `compute_extraction_id` で各 KB recipe を **extraction にグルーピング** → `plan.extractions`。
- `plan.chunk_sets` は維持(各 chunk_set は extraction_id を持つ)。
- `diff_plan` も extraction 追加/削除を扱う。

## 7. refcount / GC

- **chunk_set GC は現行どおり**(binding refcount 0 で削除)。
- **extraction の refcount = それを参照する chunk_set 数**。0 になったら extraction も GC。
  `delete_document_extractions_except(keep_extraction_ids)` を reconcile 末尾に追加。

## 8. レビューゲート(2 段階処理)との相互作用 — **決定: 案 A(2026-06-21 確定)**

現状は **1 抽出をプレビュー確認 → index**。N 抽出では:

- **採用: 案 A — 既定(owning)抽出のみ人がレビュー、他 parser 変種は派生として自動生成**。
  人は「正の抽出」を 1 つ確認(REVIEW)→ approve 時に **owning extraction を index しつつ、他 parser の
  extraction は再レビューなしで extract→index** する。UX が軽い。代替 parser の品質はレビューを経ない点は許容。
- gate-off 経路は全 extraction を自動(現行の単一経路の自然な拡張)。
- (不採用: 案 B = extraction ごとにレビュー。N 倍の確認コストで重い。将来オプション化の余地のみ残す。)

## 9. コスト / 性能

- N parser = **N 回の parse/VLM 呼び出し**(高コスト)。これは「N parser を要求した」ことの当然の対価で、
  ユーザが KB 設定で明示的に分岐したときのみ発生(既定 single parser なら従来どおり 1 回)。
- segment cache は content+parser キーで、**parser が違えば別キャッシュ**(共有しない)。意図どおり。

## 10. UI への波及(別 PR)

- KB 詳細の variant 表示(実装済)を **extraction → chunk_set の 2 階層**に拡張可能
  (「Docling 抽出 ▸ chunk 1000 / 2000」「Unstructured 抽出 ▸ chunk 1000」)。
- 「文書にレシピを能動追加」UI(前処理/Parser/Chunking を選んで variant を作る)も将来ここに乗る。

## 11. 段階実装(リスク分割)

| Phase | 内容 | 後方互換 |
|---|---|---|
| **P1 keying+表+正本化** | `compute_extraction_id`(+`MAX_EXTRACTIONS_PER_DOCUMENT`)、`rag_document_extractions` + `rag_chunk_sets.extraction_id`、**新表を即正本化**(既存単一抽出を owning extraction_id で backfill、データ無し=no-op)。読み書きを新表へ。挙動不変 | ○(単一 extraction では従来同一) |
| **P2 plan 2 段化** | `plan.extractions`、planner グルーピング(上限適用)、`diff_plan` 拡張(決定論ユニットテスト) | ○(単一 extraction では P1 と同一) |
| **P3 materialization** | extract/index 分離、parser グループごとに extract→各 chunking で index、extraction refcount/GC。**実 Oracle で parser 2 種→2 抽出を検証** | ○(単一は縮退) |
| **P4 review-gate(案 A)** | approve で owning をレビュー→他 parser を派生として自動 extract→index | ○ |
| **P5 UI** | variant 表示を extraction▸chunk_set の 2 階層化、能動レシピ追加 | — |

## 12. 決定事項(2026-06-21)/ 残る注意

- ✅ **レビューゲート = 案 A**(既定 owning のみレビュー、他 parser は派生として自動)。
- ✅ **抽出数上限あり** = `MAX_EXTRACTIONS_PER_DOCUMENT`(既定 8 程度)、超過は owning 優先で打ち切り + warning。
- ✅ **旧 `rag_documents.extraction` は即時に新表正本へ**(データ無しのため段階移行不要。列は当面縮退読み用に残す)。
- ⚠ **DDL 検証**: 新表 + `rag_chunk_sets.extraction_id` + migration を **実 Oracle AI Database で検証**してから main へ
  (未検証 DDL を積まない方針。P1/P3 で実 Oracle テスト必須)。

## 13. 非ゴール(この #6 では扱わない)

- routed 配信(別件・要 Router 新設)。
- graph/nav dedup(moot)。
- 派生層 keying の抽出層への寄せ替え(metadata field 抽出が抽出由来なら別件で検討)。
