# デザインシステム / UI の規則

> root の [AGENTS.md](../../AGENTS.md) から移した詳細（#2276）。root の規則と同じ強さで守る。規則を変えるときはこのファイルを直し、root には規則の要約と案内だけを書く。
> 正本: [platform/docs/design-system/](../../platform/docs/design-system/)（ARCHITECTURE.md・README.md・components-reference.md）と [UX 契約](../../platform/docs/ux-contracts/README.md)。この文書は UI の作業で守る規則の一覧で、寸法・部品の仕様はそちらにある。

- **UI に触る前に [ARCHITECTURE.md](../../platform/docs/design-system/ARCHITECTURE.md) を読む。** トークン値・部品の仕様・意図的な見た目の変更は [README.md](../../platform/docs/design-system/README.md)（以下 README）、実装の参照は [components-reference.md](../../platform/docs/design-system/components-reference.md)、画面の振る舞いは [UX 契約](../../platform/docs/ux-contracts/README.md)。
- **依存は「デザインシステムの決定 → `platform/packages/ui` → 各製品」の一方向。** 製品で部品やトークンを新規実装せず、`packages/ui` に入れる（同じ PR でよい）。
- **製品が持てるのは**: ナビ構造（nav config）と業務コピー（i18n）、データ取得・状態管理・権限、ドメイン enum → prop の対応表、画面固有の業務レイアウト、1 製品しか使わない部品（他の 2 製品が欲しがるなら `packages/ui`）。
- **色・型・余白・角丸・影・モーション・フォーカス表示・テーマは `packages/ui` が持つ。** `frontend/src/globals.css` は `@import "tailwindcss"` → `@import "@production-ready/ui/styles.css"` → `@source "../node_modules/@production-ready/ui/dist"` と、画面固有のレイアウトだけ（`main.tsx` から import すると共有ユーティリティが生成されない）。

## 禁止事項

- 生の hex・`rgba()` などの色の関数・生の px（inline style の数値を含む）。色は `--color-*` トークン（`bg-surface` / `text-fg-muted` 等）。旧名（`bg-card` / `text-muted` / `var(--primary)` 等）は未定義。
- `globals.css` に色トークンや `.dark { … }` の上書きを書く。
- 共有部品（`TextField` / `PageHeader` / `Button` / `StatusBadge` 等）の再実装。
- 操作部品の高さ・幅の手書き（「操作部品の高さと幅」）。
- `<table>` の手書き（`DataTable` を使う）。例外は元の文書の表を再現して編集するグリッド（RAG の `ReviewTextEditor.tsx`）だけで、理由をコメントに書く。
- 余白コンテナの手書き（`<div className="px-8 py-6">` 等）。`PageBody` を使う。
- `ToggleChip` をタブに使う（見方の切り替えは `Tabs`、一覧のツールバーの絞り込みは `FilterChipGroup`、それ以外の切り替えは `ToggleChip`）。
- `loading` 中のボタンのラベルの差し替え。アイコンは `icon={Upload}` で渡し、`loading` を渡す `Button` は必ず `icon` を持つ。
- `Spinner` 以外の回転するアイコン（`animate-spin` / `animate-[spin…]`・inline style の `animation: spin`・lucide の `Loader` / `Loader2` / `LoaderCircle` / `LoaderPinwheel`）。処理中は `Spinner`、ボタンは `loading`、領域は `ProcessingIndicator` / `TimedLoadingState`。`Spinner` の形と箱は変えない（README §4「`Spinner`」、e2e の `expectSpinnerStable`）。
- `focus(-visible|-within):ring-*` のフォーカス表示と `focus(-visible):outline-none`。表示はグローバルの `:focus-visible` に任せ、調整は `focus-visible:outline-*` / `-outline-offset-*`。
- 必須の印の手書き（`*`・独自のバッジ・`RequiredBadge` の直置き）と「(任意)」。必須の欄だけに部品の `required`（入力は `TextField` / `SelectField` / `SecretField`、それ以外は `FieldLabel` / `FieldLegend` / `Fieldset`。README §4「必須の表示」）。
- 製品ごとのアクセント色（製品は wordmark・ナビ・内容で区別する）。
- 絵文字と手描き SVG。アイコンは `lucide-react`（14 / 16 / 20 / 24px）。
- 共有 UI の内部パス（`dist/components/**`・`dist/tokens/*.css`）の import・テストでの読み込み。パッケージのルートと `styles.css` だけを使う。

## 画面の構成

```tsx
<AppShell sidebar={<Sidebar … footer={<SidebarAccountFooter … />} />}>
  <PageHeader title="…" actions={[{ id, kind: "primary", label, icon }]} tabs={<Tabs … />} />
  <PageBody>
    <Section title="…">…</Section>
  </PageBody>
</AppShell>
```

- この構成を外れた画面は review で差し戻す。`PageHeader` の `actions` は配列で渡す（JSX を渡さない。danger → utility → secondary → primary の順に並び、右端が primary）。画面を移るだけの操作は `ButtonLink`（`<Link className={buttonVariants(...)}>` に手書きしない）。
- **詳細・作成・編集の画面**: 「一覧へ戻る」は `PageHeader` の `back`。「作成」「保存」（primary）と「変更を破棄」（その右）はフォームの末尾の `FormActionBar` に置き、左端を入力欄の左端にそろえる（`PageHeader` の右端に置かない。画面の下端に貼り付けない。#1786 / #2715）。一覧の「新規作成」は `PageHeader` のまま。対象への操作は最初のカードの `ObjectActionBar` 1 か所、保存の失敗は確定の行の直上の `SaveErrorBanner`。例外を含む正本は README §4「詳細・作成・編集の画面の操作」。
- `PageHeader` と `PageBody` の `wide` は必ず同じ値にする（1920px で左端がずれる）。
- 単位の境界: 文字サイズとコントロール高さは px、余白とレイアウト寸法は rem（14px ルート）。本文は `"Noto Sans JP Variable", "Noto Sans JP Fallback", "Noto Sans SC Variable", "Noto Sans TC Variable", "Noto Sans HK Variable", "Roboto", system-ui, sans-serif` の 14px（4 書体とも可変書体。JP に無い字だけを SC → TC → HK で補う。`Noto Sans JP Fallback` は寸法を合わせた端末の書体。書体の並びは `tokens/fonts.css` だけに書く）。
- **利用者の内容（回答・根拠の抜粋・質問・文書のプレビューの本文）には内容の言語の `lang` を付ける**（#3335）。共有の部品（`AnswerSections` / `AnswerPlainText` / `MarkdownText` / `ExcerptContent` / `EvidenceCard` / `ChatUserMessage` / `DocumentPreview` の text）が本文から判定して付けるので、製品で `lang` を書かず、内容を文字列のまま部品に渡す（取込で言語が分かれば部品の `lang` に渡す）。製品が内容を自前の要素で出すなら `resolveContentLang` で付ける。`lang="zh-Hans"` / `zh-Hant` / `zh-HK` の要素は SC / TC / HK を先頭にした並びになる。画面の文言は `<html lang="ja">` のまま。`lang` は内容だけを包む要素に付け、部品・製品の文言（見出し・「根拠：」の行・ボタン）を包む入れ物に付けない。製品の決まった文言を内容として部品に渡すときは段落・項目の `lang: null`（#3349）。

## 操作部品の高さと幅

正本は README §4「操作部品の高さと幅」と `page-archetypes.md`「入力欄・選択欄・ボタンの高さと幅」。

- 高さは `size`（`sm` 32 / **`md` 36（既定）** / `lg` 40px。タッチ端末は 44px）。**1 つの行の中の入力欄・選択欄・ボタンには同じ `size` を渡す。**
- 幅は入る値の種類で `width`（`xs` 数値・短いコード・表示件数 / `sm` 日付・期間・短い選択 / `md` 名前・ID・一般の選択 / `lg` 長い名前・対象の選択・一覧の検索欄 / `full` URL・OCID・文章・SQL・`TextareaField`）。**grid のセルの欄にも、grid の外の単独の欄にも渡す**（段の幅が上限。#1891）。フォームの本文は `FormGrid`（最大 60rem）に置く。例外: RAG の検索・3 製品のチャットの対象の選択は、問い合わせの入力と端をそろえて `full`（#635）。入力欄とその操作の行は `FieldActionRow`、複数行の高さは `rows`。
- **placeholder は欄の幅に収める**（収まらなければ短い例にし、説明は `helper` へ）。e2e は共通の `findClippedPlaceholders`（`platform/packages/ui/e2e/layout-checks.ts`）で確かめる。
- **製品で書かない**: `touchTarget`、操作部品への `h-*` / `min-h-*`、欄への `w-*` / `max-w-*`（lint が検出する）。
- **フォームの入力は共有の部品**（`SelectField` / `SearchableSelectField` / `TextField` / `SecretField` / `SearchField` / `TextareaField`）。ネイティブで残すのは checkbox / radio / file / range / hidden / color と部品で表せない所だけで、理由を添えて lint を局所的に除外し `fieldControlClassName({ size, width })` を使う。e2e は combobox を押して選び（`chooseSelectFieldOption`）、高さは入力方式で期待する（`expectedControlHeight`）。

## 読み込み中・一覧・ページング

3 製品で NL2SQL の基準にそろえ、新しい一覧は NL2SQL の同種の画面を見本にする。部品と定数は components-reference.md「読み込み中と一覧の表示密度」、一覧の型と例外は `page-archetypes.md`「一覧の型と、基準から外す例外」。

- **読み込み中**: 領域の先頭に `TimedLoadingState`（または `ProcessingIndicator`）で「〜を読み込んでいます」と経過時間を 1 か所だけ出し、領域は内容の形の `Skeleton` で覆う。文字だけの「読み込み中…」・空白・画面全体のスピナーは使わない。再読み込みは前の内容を出したまま、ボタンの `loading` で示す（`messaging.md` §3.6）。
- **一覧**: `DataTable` の `stickyHeader` と `visibleRows`（共通の定数）で表の中を縦スクロールにし、製品で行数・高さを書かない。
- **件数**: 一覧の件数は `ListToolbar` の `count`（`ListCount`。「全 N 件」/「N 件（全 M 件）」）1 か所。1 ページでも出し、見出しのバッジと二重にしない。一覧の検索欄は `ListToolbar` が `lg` で固定する（#1891）。
- **ページング**: 共通の `Pagination` / `usePagination`（既定 25 件。#1952）を表の直下に置き、1 ページならページ送りを出さない。形は advanced（「1–25 / 123 件」・表示件数・最初へ / 前へ / ページ番号の入力 / 次へ / 最後へ。#1891）。サーバー側は `OffsetPagination`、カーソル型は `useCursorPages` + `CursorPagination`（前へ・N / M ページ・次への縮約版）、表示件数の選択は `onPageSizeChange`（10 / 25 / 50 / 100。既定 25）、文言は `paginationLabels()`。製品でページングのつなぎを書かない。ページ番号と表示件数は作業状態に残す。backend は `pr_backend_core.api` の helper（backend-standard.md「一覧のページング」）。
- **一覧の絞り込み**は `SearchField`（検索ボタンを置かない）。重い検索（LLM・ベクトル検索・SQL の生成）は `type="search"` にせず、ボタンと Enter（`isSubmitEnter`）で実行する（`page-archetypes.md`「一覧の絞り込みの検索」）。
- **一覧のツールバーの絞り込み**は、部品を同じ高さ（md）で縦の中心にそろえ、欄の上にラベルを置かない: 選択は `FilterSelect`（「種類: すべて」）、「すべて」を含め 5 つまでの排他の状態は `FilterChipGroup`（件数つき可）、条件があるときだけ `ListToolbar` の `onClearFilters`（「条件をクリア」）。375px は検索欄を先頭に全幅、絞り込みが 3 つ以上なら `filtersLayout="sheet"`（「絞り込み（N）」→ シート）。期間はプリセットの `FilterSelect`、状態は作業状態に残す。ツールバーの中の欄の幅の段は画面ではなくツールバーの幅（container）で切り替わる（`ListToolbar` が包む）。e2e は `findToolbarMisalignment` / `findToolbarFieldWidthMismatch`（`page-archetypes.md`「一覧の絞り込みの部品」。#1933）。
- 基準から外す一覧は、`page-archetypes.md` の例外の表に理由を書く。
- Playwright で読み込み中・行が多いとき・2 ページ以上（ページ番号を入れて移る `jumpToPage`）を desktop と 375px で確かめる。

## 操作の結果・状態のメッセージ

正本は [`messaging.md` §10](../../platform/docs/ux-contracts/messaging.md#10-操作の結果状態の出し方位置幅形出す情報705)。新しい画面は最初から、既存の画面は触るときに従う。結果は起点の操作の**直下**に**全幅**で出し、テスト・接続確認は結果パネル、保存は Toast / `FormStatus`、処理の失敗は danger の `Banner`、対象の状態は見出しの `StatusBadge`。技術的な詳細は「詳細」に畳み、自動で消さない。

## lint

- 遵守ルールの正本は `platform/docs/design-system/adherence.oxlintrc.json` と `design-system-plugin.mjs`。各製品は **コピーせず相対パスで参照する**（書き方は [platform/AGENTS.md](../../platform/AGENTS.md)「lint」）。
- 製品固有のルールは adherence と同じルール名を上書きせず（セレクタが消える）、別名にする。
- 誤検知・正当な例外は `// oxlint-disable-next-line <rule>`（ESLint は `eslint-disable-next-line`）と理由のコメントで局所的に除外する。ルールを緩めるなら adherence を変える Issue を立てる。

## 既存ルールとの優先順位

- 部品の見た目と振る舞い（サイズ・variant・アイコン・loading・ヘッダーの並び・フォーカス・テーマ）は、`ui-ux-pro-max` skill の一般論や製品の `docs/` より `platform/docs/design-system/` を優先する。
- 画面の振る舞いは `platform/docs/ux-contracts/` が 3 製品共通の正本。製品の `docs/frontend-*.md` には、それらが決めない製品固有の差分（割り当て・例外）だけを書く。

## UI 変更の検証

- **UI/UX の作業（設計・実装・レビュー・改善）は必ず `ui-ux-pro-max` skill を使う。**
- 変更ごとに Playwright で実画面を確かめる: desktop と 375px、必要に応じて空 / 読込 / エラー / ブロック、1280px / 1920px（タイトルと本文の左端が揃う）、キーボード（最初の Tab で「本文へスキップ」、フォーカスリング、`Tabs` の ← → / Home / End）。状態は色だけで表さない。意図的な見た目の変更は README §7 と照合して PR に書く。
- ローカルの e2e は関係する spec だけを、1 回おおむね 1 分以内で（`-g` や spec のパス）。
- **画面幅で結果が変わる test には `{ tag: "@responsive" }` を付ける**（レイアウトの位置・幅・並び、ナビのドロワー、横スクロール・折り返し、タッチ端末の高さ・タップ、`isMobile` / project 名で確かめる内容が変わるもの。mobile だけで実行する test は必ず。spec 全体なら先頭の describe に）。中で `setViewportSize` で幅を決める test と、末尾の汎用の横あふれの確認だけの test には付けない。迷ったら付ける。規則の正本は `platform/scripts/e2e-impact/mobile-scope.ts`。PR の mobile と同じ範囲は `E2E_MOBILE_SCOPE=responsive npx playwright test …` で確かめる。
- **画面のコードを変えたら、その画面を検証する既存の spec も同じ PR で直す（#885）。** 影響を受ける spec は `python3 platform/scripts/e2e_impact.py select --product <製品> --base origin/main --download`（CI の `e2e-impact` と同じ選択）で選び、PR の前に実行する。spec は `test` を製品の fixture（RAG・Agent `e2e/fixtures/test.ts`、NL2SQL `tests/e2e/_helpers/test.ts`）から import する（CI の `check-imports`）。
