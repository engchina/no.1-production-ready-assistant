# UX 契約（3製品共通の画面の振る舞い）

RAG / NL2SQL / Agent の3製品に共通する「画面の振る舞い」の規約の正本。見た目（色・型・余白・サイズ・z-index・コンポーネント仕様）は [デザインシステム](../design-system/README.md) が正本で、本ディレクトリはデザインシステムが決めない範囲を扱う。

| 文書 | 内容 |
|---|---|
| [buttons.md](./buttons.md) | ボタンの役割・配置・命名、ページ / オブジェクト / 行 / 内容の操作の置き場所、一括選択、並べ替え列頭 |
| [messaging.md](./messaging.md) | 通知の 6 チャネル（Toast / FieldError / FormStatus / Banner / ConfirmDialog / State views）と 4 トーン、必須の欄の示し方と未入力のエラー（§3.2.1）、処理中表示、i18n 文言、API problem 契約、失敗状態の情報設計 |
| [page-archetypes.md](./page-archetypes.md) | ページの型（4 種）と共有プリミティブの使い方、詳細の出し方（一覧 + 詳細・シート・分割ペイン）、チャット（会話の画面）の骨格と共通の部品・製品の分担（§6） |
| [confirm-before-answer.md](./confirm-before-answer.md) | 回答前の確認（曖昧な質問を回答・SQL の生成の前に確かめる）: いつ聞くか・用語・確認のカードの出し方と操作・往復の上限・会話に残す表示・状態の保持と a11y・3 製品の割り当てと業務の単位の設定（#1839） |
| [run-history.md](./run-history.md) | 実行履歴（3 製品の「実行履歴」）の一覧の列・状態・絞り込み・件数と、一覧 → 全幅の詳細の構成（概要 → 処理の経過〔処理の詳細を含む〕→ 回答 → 根拠 / SQL / 検証 → フィードバック）、375px の形（#1955） |
| [workspace-state.md](./workspace-state.md) | ページ遷移・再読込のときの作業状態の保持、未保存変更の離脱ガード |
| [cross-cutting.md](./cross-cutting.md) | 横断的な保守・セキュリティ契約（更新 API の所有範囲、認可の server-side 強制、i18n と E2E locator） |

節番号は、コードやテストのコメントが `§5.1` などで参照しているため変えない。

## 優先順位

1. [デザインシステム](../design-system/README.md)（見た目・コンポーネントの振る舞い）
2. 本ディレクトリの UX 契約（画面の振る舞い）
3. 各製品の `docs/frontend-*.md` / `AGENTS.md`（製品固有の差分だけ）

製品の文書は、共通の規約を写さずに本ディレクトリへリンクし、製品固有の割り当て・例外・記録だけを書く。共通の規約を変えたいときは、製品側で上書きせずに本ディレクトリを直す（platform の Issue を立てる）。

## 実装の置き場所

- 規約に出てくる部品のうち、`@production-ready/ui`（`packages/ui`）にあるもの（`Button` / `PageHeader` / `Banner` / `FormStatus` / `FieldError` / `DataTable` / `Pagination` / `MessageText` / `useConfirm` / `toast` / `ContentActionBar` / `FormActionBar` / `BulkSelectionActions` / `ClearActionButton` / `ActionResultRegion` / `ProcessingIndicator` / `TimedLoadingState` / `RowActionMenu` / `ObjectActionBar` / `FloatingActionMenu` / `Disclosure` / `DisclosureChevron` / `ListDetailLayout` / `FixedSplitPane` / `ClarificationCard` / `ClarificationRecord` など）は、それを使う。処理中表示・分割ペインの文言は `labels`、`ObjectActionBar` / `FormActionBar` の「その他の操作」は `moreLabel`（いずれも既定は日本語）で製品の i18n から差し替える。分割ペインの比率・一覧 + 詳細の一覧の幅と開閉の保存 key は `storagePrefix` で製品ごとに分けられる。並べ替えの列頭は `DataTable` の `sort` が持つ。
