# フロントエンド ボタン設計（NL2SQL の差分）

> ボタンの役割・配置・命名の共通規約は platform の [UX 契約 buttons.md](../../platform/docs/ux-contracts/buttons.md)、見た目は [デザインシステム README §4 Button](../../platform/docs/design-system/README.md) が正本。
> 本書には NL2SQL 固有の割り当て・例外・記録だけを書く。節番号は共通規約にそろえる。

---

## 3. NL2SQLでの役割の割り当て

- コメント/アノテーションの対象情報取得、SQL生成、保存、実行は工程を進める主操作（`primary/lg`）。
- ページ全体の表示更新・DB構造再取得は `PageHeader` の `utility`。通常表示は `secondary/md`。
- コピー、ダウンロード、状態の再確認、追加読込は局所ツール（`secondary/sm`）。追加読込は `ListPlus`、更新は `RefreshCw`。
- 入力欄に並ぶ取得/接続テストは入力欄と同じ高さの段（既定 md 36px、タッチ端末は 44px）にし、`icon` を渡す。`touchTarget` は使わない（design-system README §4「操作部品の高さと幅」、#613）。
- 同じ操作行は主操作・補助操作とも同じsize。非同期操作は必ず `icon` propを使い、loading時もラベルと幅を保つ。
- `danger` は実際の破壊的確定に使う。選択・未選択の変化でvariantやアイコンを切り替えず、`disabled`だけを変更する。

---

## 6.2 Ontology グラフの操作部

Issue #435 のユーザー指定により、`OntologyGraphCanvas` の操作部は #307 (`9dcaf63`) の親 commit の外観を維持する。オントロジー構築と SQL 生成結果で共通適用する。他画面のボタン標準には波及させない。

- **モード選択**: `aria-pressed` 付き専用 `<button>` を使用する。外枠は border / card 背景 / 軽い shadow、desktop 40px・mobile 44px。項目は desktop 32px・mobile 36px、`text-xs`、13px icon、選択中は `bg-primary text-primary-foreground` とする。
- **凡例フィルタ**: `aria-pressed` 付き専用 `<button>` で 10px の小字と色見本を表示する。表示中は透明背景、非表示は opacity 40% + 打消し線。一般 Button の選択枠や高さを適用しない。
- **拡大・縮小・フィット・配置リセット・検索前後移動**: 共通 `<Button variant="ghost" size="sm" data-button-layout="graph-tool">` を使用する。旧版 `sm` の高さ 2rem・左右 padding 0.75rem・15px icon を専用 named layout で維持する。`aria-label`、native disabled、focus-visible は維持する。
- **検索の前後移動バー**: 検索欄・モード外枠と同じ lg（40px、タッチ端末は 44px。画面幅ではなく入力方式で決める。#613）に揃える。
- SQL 結果の入れ子など、グラフ自体が狭い場合はモードと検索ツールを折り返す。モード外枠は 40/44px を最小高さとして内容に合わせて伸ばし、親の `overflow-hidden` によるボタンの裁切を防ぐ。
- これらは旧版のコンパクトな外観を復元する明示的な寸法例外である。キーボード操作、選択状態の読み上げ、375px の折り返しを Playwright で確認する。取得・生成・実行などの一般アクションには適用しない。

---

## 7. 命名（NL2SQL 固有）

- **SQL 生成の主操作**: 自然言語から SQL を生成し、安全確認後に実行まで進むボタンは `SQL を生成して実行` とする。ボタンを参照する案内文も同じ名称に揃え、SQL 入力を直接実行する `SQL 実行` と区別する。
- **新しい作業**: SQL 生成は `新しい質問を開始` とする。破棄確認は質問・結果の消去と生成条件・実行オプションの初期化を説明し、確定ボタンも起点と同じ文言にする。

| 対象 | ラベル | 押下時の範囲 / 保持されるもの |
|---|---|---|
| 直接 SQL / 管理 SQL / 共通 SQL 実行カード | `SQL 入力・結果をリセット` | SQL 本文・読込ファイル表示・実行結果・エラー・実行状態・確認語を消去。取得件数上限がある画面では 100 に戻す。DB の変更を取り消す操作ではない。 |
| テーブル作成のファイル取込 / データ CSV 取込 | `取込設定・結果をリセット` | ファイル・確認語・取込結果等を消去。テーブル作成は表名・シート名も消去し、CSV 取込はモードを追加に戻す（取込先テーブルは保持）。取込済み DB データは保持。 |
| 生成データ表示 | `表示件数・結果をリセット` | 取得件数上限を既定値に戻し、表示結果と関連エラーを消去。対象テーブル・DB データは保持。データの管理のデータの表示は、行を選ぶとすぐに表示し取得のたびに結果を置き換えるので、このボタンを持たない（#2330）。 |
| Profile 保存 | `実行設定・同期表示をリセット` | 確認語・再構築オプション・同期ジョブの表示対象・投入エラーを解除。Profile の編集内容は保持。投入済みジョブの中止ではない。 |
| Ontology 接地確認 | `質問・確認結果をクリア` | 質問・確認結果・ノード/エッジ選択・検索表示を消去し、グラフ表示を全体に戻す。グラフ自体は保持。 |
| 実行履歴の検索・フィルタ | `絞り込みを解除` | 検索語を空にし、評価・安全性フィルタをすべてに戻す。履歴・並べ替えは保持。 |
| 構築資料 / QA / 取込ファイル欄 | `ファイル選択を解除` | ローカルのファイル選択を解除。取込画面は関連する確認語・結果表示等も解除し、他の設定は保持。取込ファイル欄の aria-label は `取込ファイル選択を解除`。 |
| 学習データ取込後のファイル欄 | `ファイル名表示を消去` | ファイル名表示だけを消去。選択時に実施した取込や学習データを取り消さない。 |

---

## 11. 過去の移行記録

- ✅ `<Button>` に React 19 流の `ref` 対応を追加(ConfirmDialog のフォーカス制御で利用)。
- ✅ 手書き secondary ボタン(`StateViews` 再試行 / `DashboardHeader` 更新)を `<Button variant="secondary">` へ統一。
- ✅ 主アクションバーの高さ表現を `min-h-10` override → `size="lg"` に統一(設定各画面)。
- ✅ 共通 `<ToggleChip>` を抽出し、FileList フィルタ・検索モード切替を移行(`role="group"` 付与)。
- ✅ ページ上部操作を `<PageHeader>` / `<PageActionBar>` へ統一し、レスポンシブな `その他の操作` メニューへ対応。

## 12. 適用範囲・検証

適用: PageHeader / ObjectActions / FormActionBar / ContentActionBar / BulkSelectionActions、確認ダイアログ、入力横、ログイン、各設定、質問・生成 SQL・結果、データ/テーブル/ビュー、Profile、学習・履歴・評価、権限、グラフ操作、ページング、エラー再試行、通知内操作。共有パッケージの状態 hook/store はそのまま使い、アクションを含む Pagination / ErrorState / Toaster の表示はアプリ共通 Button を使う。

`tests/e2e/button-standards.spec.ts` は実 React 部品を使って light/dark × desktop/mobile-375 の寸法・状態・操作・focus・danger 確認・overflow・ページング・再試行を検証する。各機能の既存 Playwright spec はユーザーフローの回帰を担当する。

### Markdown オントロジーの構築と公開（Issue #491）

- 下書き／公開版の表示切替は `ManagementTabs`。独立した構築結果・公開能力の操作は表示しない。
- Markdown の保存・コピー・移行確認は `secondary/sm`、公開準備と確認済み内容の公開は `ContentActionBar` 内の `primary/lg`（同じ行の移行確認もlg）。確認ダイアログで Profile と対象版を示す。
- 「公開結果を確認」は保存した冪等キーによる読み取りだけを行う。Function / Action Type のグラフ詳細に実行操作を設けない。
- 概念種類は主要6／補助7にまとめる。欠けた種類をダミーノードや操作ボタンで補わない。モバイルの入力・操作領域は 44px を確保する。
