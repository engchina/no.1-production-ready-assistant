# 合成データの事前確認と適用

## 動作

Issue #510。新しい合成データ生成は、業務の対象表へ直接書き込まず、生成番号ごとに独立した Oracle 表へ生成する。生成終了後に対象表ごとのデータを表示し、確認語と確認ダイアログで適用する。適用では Select AI を再呼び出さず、表示対象となった同じ生成データをコピーする。画面の表示件数上限を超える生成行も適用対象である。

- `POST /api/nl2sql/synthetic-data/runs` は常に事前確認を必要とする。request に直接適用を選択する field はない。旧 `/generate` も同じ受付処理を使う。
- `GET /runs/{run_id}/results` は新しい記録では今回生成した行のみを返す。`sample_rows` に指定した参考用の既存行は含めない。応答の `preview_digest` はその表の生成行全体の SHA-256。
- `POST /runs/{run_id}/apply` は `confirmation`（単一表は owner-qualified 表名、複数表は `ADMIN_EXECUTE`）と全対象表の `previews: {表名: preview_digest}` が必要。
- `POST /runs/{run_id}/discard` は生成終了後の未適用データを破棄する。適用済みの対象表は変更しない。
- `preview` と `review_status`（`pending / ready / applied / discarded`）、`applied_at` を既存 run payload に追加。生成の status と適用状態を分離する。migration は不要。旧記録は `preview=false` として従来の現在表内容を表示し、適用操作は提供しない。

## データ・認可・トランザクション

一時表は接続 schema の `NL2SQL_SP_<run UUID>_<対象番号>`。一般の業務 object 一覧には表示しない。作成予定の名前を DDL より先に run に保存し、作成した object ID、列定義、制約、参考行の ROWID、生成行の checksum と件数を保持する。表・列コメント、主キー・一意・CHECK・外部キーを複製する。複数対象間の外部キーは一時表へ写像し、対象外の参照先は元の表を参照する。参考行は最大 `sample_rows` 件であり、既存参考行だけに未検証の親参照を許す `ENABLE NOVALIDATE` を使用する。

取得・適用・破棄では user / DB context / Profile 権限を確認する。適用は run 行を `FOR UPDATE` でロックし、対象表と一時表をロックした後に object ID、列・制約定義、全生成行の checksum と件数を再検証する。親から子の順に明示列付き INSERT を行い、全表の INSERT と適用済み receipt を同一 transaction で commit する。制約違反などの失敗は全表 rollback。応答消失後の再送は適用済み receipt を返し、二重挿入しない。

一時表を Oracle が更新している間は適用・破棄できない。worker が失われた場合も、Oracle operation の終了と元 session の消滅を確認するまで結果を確定しない。自動で生成や適用を再送しない。

## 保持とクリーンアップ

確認用データは生成終了から24時間保持する。破棄、生成失敗・0件、期限切れの一時表は worker が削除する。生成記録（履歴）は期間で削除せず、期限を過ぎた確認の記録は閲覧だけにする（表示・適用・破棄はできない。#3235）。適用した業務データは削除しない。cleanup が失敗した場合は一時表の管理情報を消さず再試行する。予約名と作成時 object ID が一致しない表は削除しない。

DDL 完了直後かつ object ID 保存前に worker が消失した場合、または Oracle operation/session の状態を確認できない場合は、安全のため記録と一時表を保持する。管理者は run の生成番号、記録された一時表名、Oracle operation/session と object ID を確認して復旧する。識別情報を無視した削除・自動再生成は行わない。

## 対応範囲

- 仮想列・IDENTITY 列、有効なトリガー、対象間の循環・自己参照 FK は生成前に理由付きで拒否する。確認値と適用値の同一性、挿入順序を保証するため。
- 従来から非対応の LOB / RAW / VECTOR 等は引き続き拒否する。
- 全対象の生成件数が確認できた `completed` のみ適用可能。一部生成は閲覧と破棄を提供する。
- 既存の主キーとの競合や、生成後の対象表変更は適用時に拒否する。既存行の上書きやキーの自動置換はしない。
- 接続 schema に一時表を作成できる権限・容量と、外部 FK の参照権限が必要。未充足の場合は対象表へ書き込まず生成失敗として表示する。

## 検証

`backend/tests/test_nl2sql_synthetic_preview.py` は受付、生成先隔離、状態・確認語・checksum・所有権の拒否、再送、失敗保持、破棄と cleanup retry、API 境界を検証する。

`NL2SQL_SYNTHETIC_LIVE_TEST=1 .venv/bin/pytest tests/test_nl2sql_synthetic_preview_oracle_live.py -k atomic_apply` はランダムな隔離テスト表だけを作成し、実 Oracle の主外キー、参考行除外、全体 rollback、原子的 receipt、再送、改変拒否、cleanup を検証する。

`NL2SQL_SYNTHETIC_LLM_LIVE_TEST=1 .venv/bin/pytest tests/test_nl2sql_synthetic_preview_oracle_live.py -k select_ai` は既存 OCI Select AI Profile を使い、一時表への実 API 生成を1行だけ検証する。テスト後に一時表を削除する。CI では接続設定がないため両実 DB テストを skip する。

Playwright の `synthetic preview` シナリオは desktop / mobile-375 で全表確認、確認取消、reload 時の確認解除、適用、破棄、一部生成、適用エラーを検証する。
