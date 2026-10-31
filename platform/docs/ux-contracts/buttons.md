# ボタンの役割・配置・命名（Button）

> 3製品共通の正本。ボタンを新しく作る・直すときは本規約に従う。類似機能のボタンは、サイズ・variant・配置・命名をそろえる。
> 見た目（色・高さ・アイコン枠・loading・disabled・フォーカス）は [デザインシステム README §4 Button](../design-system/README.md) が正本。
> 操作結果の通知は [messaging.md](./messaging.md)（Toast / FormStatus / ConfirmDialog）を使う。
> 製品ごとの役割の割り当て・文言・例外は、各製品の `docs/frontend-button-spec.md` に書く。

---

## 0. 原則

1. **アクションボタンは必ず共通 `Button`（`@production-ready/ui`）を使う。** 同じ見た目を生 `<button>` で作り直さない。
2. **同時に操作する領域の主ボタン（primary）は原則 1 つ**（`primary-action`）。他は secondary / ghost に従わせる。
3. **類似機能は同じ size・variant・配置・文言 key の規則**にする。
4. **文言は i18n 経由**、アイコンは Lucide（emoji 禁止）。
5. **破壊的操作は danger + 確認ダイアログ**（`useConfirm`）。主アクションから視覚的に分離する。

---

## 1. ボタンの種別（4 分類）

| 種別 | 実体 | 用途 |
|---|---|---|
| **Action Button** | `<Button>` | 保存・実行・キャンセル・再試行など主たる操作 |
| **Icon-only Button** | `<Button iconOnly>` | 閉じる ×・削除・表示切替など。**aria-label 必須** |
| **Toggle / Segmented chip** | `aria-pressed` 付きの選択 | フィルタ・モード・対象の切替（§6） |
| **Nav item** | リンク / ボタン（Sidebar） | ナビゲーション。本規約の対象外 |

---

## 2. 見た目とサイズの正本

色・高さ・アイコン枠・loading・disabled・フォーカスは [デザインシステム README §4 Button](../design-system/README.md) と共通 `Button` が管理する。役割 × 配置の選択表も同節を正とする。製品では寸法・色・角丸を上書きせず、共有ボタンを作り直さない。

## 3. 役割の割り当て（共通の考え方）

- 工程を進める操作（対象の取得、生成、保存、実行など、入力を確定して次へ進む操作）は主操作（`primary/lg`）。
- ページ全体の表示更新・外部データの再取得は `PageHeader` の `utility`。通常表示は `secondary/md`。
- コピー、ダウンロード、状態の再確認、追加読込は局所ツール（`secondary/sm`）。追加読込は `ListPlus`、更新は `RefreshCw`。
- 入力欄に並ぶ取得 / 接続テストは入力欄と同じ高さの段（既定 `md`。lg の入力欄の横は `lg`）にし、`icon` を渡す。`touchTarget` は使わない（タッチ端末では入力欄もボタンも 44px になる。design-system README §4「操作部品の高さと幅」、#613）。
- 同じ操作行は主操作・補助操作とも同じ size。非同期操作は必ず `icon` prop を使い、loading 中もラベルと幅を保つ。
- `danger`（赤塗り）は実際の破壊的確定に使う。選択・未選択の変化で variant やアイコンを切り替えず、`disabled` だけを変える。
- 確定の前の起点（確認ダイアログを開くボタン）と、取り消せる停止・拒否（処理中のジョブのキャンセル、承認の拒否、Run のキャンセル）は赤塗りにしない。`secondary` / `ghost` + `tone="danger"`（赤文字）にするか、「その他の操作」メニューに入れる。確定は `ConfirmDialog` の `danger` ボタンで行う（#355）。
- `variant="danger"` と `tone="danger"` は同時に指定できない（型で禁止。赤地に赤文字になる）。

各製品の具体的な割り当て（どの画面のどの操作が主操作か）は、製品の `docs/frontend-button-spec.md` に書く。

## 3.1 その場の実行と停止（#413）

止められる操作は、結果をどこで待つかで 2 つに分ける。

| 種類 | 例 | ボタン |
|---|---|---|
| **その場で結果を待つ操作**（応答をその画面で待ち、画面を離れたら止まる。ブラウザの request を `AbortController` で止める） | RAG の検索・チャットの送信・ナレッジベースの検索テスト、Agent のチャットの送信（下の注） | **1 つのボタン**を、実行中は同じ位置で「停止」に切り替える |
| **バックグラウンドの job**（サーバーで進み、再読み込み・画面の移動の後も続く。中止はサーバーへの要求） | NL2SQL の SQL 生成・実行・オントロジー構築・SQL 生成評価、RAG の取込・品質評価、Agent の Run（実行履歴の Run の詳細） | 今のまま、開始のボタンとは**別に**、進捗の表示のそばに「中止」を置く（[デザインシステム README §4「カード内の操作行」](../design-system/README.md)） |

**注（Agent のチャット、#805）**: Agent のチャットの 1 往復はサーバーの Run（job）だが、利用者はチャットの入力欄の下で回答を待つので「その場で結果を待つ操作」として扱い、送信のボタンを Run が `queued` / `running` / `waiting_approval` の間「停止」にする。停止は Run の中止（`POST /api/runs/{run_id}/cancel`）を送り、止めた Run は会話の中に止めた状態を出す（途中までの内容は残す）。送信の要求中（Run の id がまだ無い間）に押したら、Run ができた直後に中止する。実行履歴の Run の詳細の「キャンセル」は job の中止のまま。

対象の状態を切り替える操作（RAG のサービスの起動 / 停止、Agent の Runtime の停止など）は、止める「処理」が無いため本節の対象外（各製品の画面の型に従う）。

**理由**: その場の操作は、実行中に押せる操作が停止だけになる。押せない「実行」（`loading`）の横に「停止」を並べると、押せないボタンがノイズになり、停止が押した位置と違う所に出る。1 つのボタンを切り替えれば、押した位置にそのまま停止が出てレイアウトもずれない（ChatGPT・Claude・Gemini・Perplexity と同じ定番の型）。job は実行中も開始の条件を見直したり別の操作をしたりするため、開始と中止を分け、中止を進捗のそばに置く。

**その場で結果を待つ操作の 1 つのボタン**

- 実行中は、同じ `<button>` のまま「停止」＋停止のアイコン（`Square`）・`secondary`（既定の tone）に切り替える。取り消せる停止なので赤塗りにしない（§3）。止めても途中までの内容は残り、もう一度実行できるので、注意の色（赤文字）も付けない。
- 実行中に押せる操作は停止だけ。ボタンは `loading` を使わず、処理中は結果の領域の `ProcessingIndicator` が動くスピナー・経過時間・`role="status"` の読み上げを出す（[messaging.md §3.7](./messaging.md)。動くスピナーは同じ処理に 1 つ）。
- 同じ要素のまま切り替えるので、フォーカスは実行 → 停止 → 待機の間ずっとボタンに残る。条件で別々の `<Button>` を出し分けない（要素が替わるとフォーカスが `body` へ外れる）。
- 位置と幅を変えない。ラベルの長さが違うとき（「検索テスト」→「停止」）は、長い方の幅を予約する。
- 実行できない間（入力が空など）は `aria-disabled`（フォーカスを受ける）。停止・完了の後に実行できない状態へ戻っても、フォーカスが外れない（§8 の `loading` と同じ理由。#355）。
- ダブルクリックの 2 回目（`detail >= 2`）と、押し続けた Enter / Space の繰り返し（`repeat`）は無視する。1 回目で実行が始まって「停止」に変わった直後に、続けて停止しない。
- **入力欄の Enter は実行だけにつなぐ。実行中の Enter では停止しない**（何もしない）。停止はボタン（クリック・フォーカスして Enter / Space）だけで行う。入力欄は実行中も使える（チャットは次の質問を書ける）。
- 読み上げは、ボタンの名前の変化ではなく、既存の `aria-live` の状態（「回答を生成しています」など）で伝える。
- 配置はフォームの操作行の規則に従う（§4、§5.2.1）。条件の入力（「詳細条件」など）があるフォームでは、条件の後（フォームの最後）の区切り線の下に左寄せで置き、375px では全幅にする。1 行の入力だけの場合（検索テスト・チャットの入力欄）は入力欄の右（375px では下）に置く。
- 実装は `packages/ui` の `RunStopButton`（判定は `runStopClickAction` / `isRepeatedActivationKey`）。RAG（検索・チャット・検索テスト）と Agent（チャット）が使う（#413 で RAG に作り、#805 で `packages/ui` へ移した）。製品で同じボタンを作らない。

## 4. 配置

工程 / フォームの主操作は入力内容の末尾に置く。補助操作を同じ高さで並べ、破壊的操作は離す。
ページ操作は共通 `PageHeader`、局所ツールは `ContentActionBar`、対象オブジェクトの操作は `ObjectActionBar`、設定のカードのフォームの確定・取消は `FormActionBar` に置く。

**詳細・作成・編集の画面（1 ページ = 1 つの対象。#618 / #1786）**: 「一覧へ戻る」は `PageHeader` の `back`（左上・タイトルの上。右の操作の列に入れない）。確定（作成・保存）は**フォームの末尾の `FormActionBar` の primary**（左端を入力欄の左端にそろえる）、「変更を破棄」はその右の secondary。`PageHeader` の右端には置かない。確定の行は画面の下端に貼り付けず、内容と一緒にスクロールする（#2715）。対象への操作（アーカイブ・削除・復元・パスワードのリセット・無効化など）は最初のカードの見出しの右の `ObjectActionBar` 1 か所。保存の失敗は確定の行の直上の `SaveErrorBanner`（messaging.md §3.3.1）。作成の画面の確定は「作成」（一覧の「新規作成」は作成の画面を開く操作で、`PageHeader` の右端のまま）。文言は「一覧へ戻る」「保存」「作成」「変更を破棄」（「一覧に戻る」「保存する」「作成する」「変更を元に戻す」は使わない）。例外は確認語が要る保存（確認語欄の操作行）。詳細は [design-system README §4「詳細・作成・編集の画面の操作」](../design-system/README.md)。

---

## 4.1 ボタン押下後のスクロール / フォーカス

- **ナビゲーション以外のボタン押下では、ページ全体を自動で先頭へ戻さない。** ローカルな実行・保存・更新・コピーは、今のスクロール位置と作業文脈を保つ。
- 実行結果・エラー・処理中表示は、影響を受ける最小の領域に置く。結果 / エラーが表示範囲の外に出る場合だけ `scrollIntoView({ block: "nearest", inline: "nearest" })` で最小限誘導する。
- 処理中に利用者が手動でスクロールした場合、その処理の完了時に自動で誘導しない。利用者の移動を優先する。
- 既存の結果を再実行で一時的に消す画面は、`ActionResultRegion` で直前の結果領域の高さを保ち、ページ高さの急な縮小による `scrollTop` のクランプを防ぐ。
- `prefers-reduced-motion: reduce` ではスクロールを `auto` にする。通常時の誘導は必要最小限の smooth scroll を許す。
- URL hash、ブラウザの戻る / 進む、サイドナビなど明示的なナビゲーションは、各製品の App のスクロール復元 / hash target の規則を正とし、本節の対象外とする。

---

## 5. ページヘッダー操作

ページ単位の操作は `PageHeader` の `actions`（`PageAction` descriptor）で宣言し、表示順・variant・モバイルでの縮約は共有部品に任せる。

```tsx
<PageHeader
  title={t("nav.tables")}
  actions={[
    { id: "create", kind: "primary", label: t("table.create"), icon: Code2, onClick: startCreate },
    { id: "import", kind: "secondary", label: t("table.import"), icon: Upload, onClick: startImport },
    { id: "refresh", kind: "utility", label: t("common.action.refresh"), icon: RefreshCw, onClick: load, loading },
  ]}
/>
```

- 固定の並びは **danger → utility（今の表示の再取得 → 外部データの同期）→ secondary → primary**（右端が主操作）。配列順ではなく `kind` で並べ、同じ `kind` の中は宣言順を保つ。
- `utility`（表示更新など）は `secondary` と同じ枠付きの見た目で表示される。compact メニューの中は `ghost`。
- `primary` はページ全体で最大 1 件。`primary/secondary` は作業開始のグループ、`utility` はページツールのグループ、`danger` は危険操作のグループとし、グループの境界に軽い区切りと余白を置く（`PageHeader` が縦の区切り線を描く。「その他の操作」メニューでは危険操作の前に区切り線。#355）。
- 今の表示を取り直す操作の文言は `最新情報を取得`（共通の key は `common.action.refresh`。同じ画面のパネルの中の取り直しは `〈対象〉の最新情報を取得`、失敗の通知は `再試行`。§7「表示を取り直す操作」）。外部データや構造の同期を始める操作は、対象を名前に含めた別の文言にする。
- 非同期操作は `loading` を渡し、処理中の再送信を防ぐ。成功の Toast と回復可能なエラーの表示は [messaging.md](./messaging.md) に従い handler 側で行う。
- `lg` 未満で操作が 3 件以上なら、最も優先度の高い操作だけを表示し、残りを `その他の操作` メニューへ入れる（2 件のときはメニューが 1 項目だけになるので畳まず両方を出す。#582）。メニューを開くボタンと `role="menu"` の読み上げ名は「その他の操作（<操作のグループの名前>）」にし、カードの「その他の操作」と区別する。メニューは `aria-expanded` / `aria-controls` / `role="menu"`、Esc、矢印 / Home / End、フォーカスの復帰を満たす。メニューは §5.1 の「メニューの表示方向」に従い viewport 内に表示する（操作が折り返して「その他の操作」が左端に来ても左外に切れない。#363）。
- ヘッダーには今の判断を変える状態・リスク・バックグラウンドの進捗だけを置く。件数は一覧 / タブ / 操作パネルへ、同期の最終時刻は同期操作の近くへ置く。

---

## 5.1 オブジェクト操作（一覧行 / 詳細）

一覧 + 詳細の管理画面では、同じ対象オブジェクトの操作を `EntityAction` descriptor で 1 回だけ定義し、表示場所ごとに `RowActionMenu` / `ObjectActionBar` へ渡す。左の一覧と右の詳細で別々にボタン列を組まない。

- **ページ操作**：新規作成・表示更新など、対象を選ばない操作は `PageHeader` に置く。
- **一覧行**：行内は常に表示する `RowActionMenu` 1 個だけにする。複数の文字ボタンを横に並べない。
- **行選択**：一覧 / 詳細の単一選択の画面では、行の操作以外の領域のクリックで対象を選び、右側の詳細または編集対象を更新する。`詳細` / `編集` のような選択だけの導線は `RowActionMenu` に入れない。
- **詳細**：`ObjectActionBar` は最大 2 個の非破壊・高頻度の操作を `secondary` で表示し、残りを `その他の操作` メニューへ入れる。
- **危険操作**：無効化・アーカイブ・削除などは、一覧行 / 詳細ではメニュー項目として扱い、確定は `ConfirmDialog` または専用の確認ダイアログの `danger` ボタンで行う。
- **危険操作の確認面**：確認ダイアログ / 確認語の入力領域は中立の背景にする。danger は左のアクセント・文言の色・状態 badge・確定ボタンに限り、ヘッダー / 本文 / 対象欄を広い danger の背景で塗らない（[messaging.md §3.5](./messaging.md#35-confirmdialog)）。
- **一括操作**：複数選択を入れる場合は一括操作のバーを使い、一括モードの間は行内の `RowActionMenu` を disabled または非表示にする。
  - バーは一覧の直上に**常に表示**し、行の選択で出し入れしない。選択が 0 件のときは一括操作と選択解除を disabled にし、左側に「行を選ぶと、まとめて〜できます」のように何ができるかを示す。選択の有無で表の位置を動かさず（レイアウトの移動を出さない）、一括操作があることを選ぶ前から見せるため（#699）。
  - 見た目は中性の面（`border-border bg-surface-sunken`）で、選択の件数・進み具合を左、操作を右に置く。件数は操作のラベルに `（n）` で添える。
- **アクセシビリティ**：メニューの trigger は `aria-haspopup="menu"` / `aria-expanded` / `aria-controls`、メニューは `role="menu"`、項目は `role="menuitem"`。Esc で閉じ、ArrowUp/Down/Home/End で移動し、Esc の後は trigger にフォーカスを戻す。
- **配置**：行内の trigger は右寄せの icon-only ghost、詳細の操作バーはヘッダーの右側で `secondary` + overflow。danger の項目はメニュー内で区切り線を置く。メニューの面は scroll container の中に absolute で置かず、viewport 基準の fixed / portal で表示する。
- **メニューの表示方向**：方向は trigger に最も近い実スクロール祖先を優先して判定し、なければ viewport を使う。下の空きが足りなければ上へ反転する。上下どちらも足りなければ、空きが大きい側を選び `max-height` + 内部スクロールにする。件数が少なく container が実際にはスクロールしていない場合は、メニューを container に閉じ込めず viewport 内に表示する。左右は trigger の端にそろえる（既定は右端）。そろえた端で viewport（左右 8px の余白の内側）に入らず、反対の端なら入るときは反対の端にそろえる（375px で左端に来た trigger は左端揃え）。どちらの端でも入らなければ viewport の内側にずらす（#363）。

---

## 5.2 コンテンツ内の局所ツール操作

コードブロック、プレビュー、結果、局所のテーブルなど、特定のコンテンツだけに作用する操作は `ContentActionBar` にまとめる。左側はタイトル・説明・状態などの情報、右側は操作ボタンにする。

- **配置**：操作はコンテンツ面の右上に置く。左側は本文の読み始め、タイトル、説明、状態表示に残す。
- **対象**：コピー、ダウンロード、出力、局所プレビューの実行など、直下または同じカード内のコンテンツだけに作用する操作。
- **見た目**：`<Button variant="secondary" size="sm">` を既定にする。低頻度の補助でも ghost にせず、同じ局所ツール群では variant と size をそろえる。
- **工程を進める操作**：対象情報の取得、生成、AI 抽出など、入力を確定して次の工程へ進む操作は局所ツールではなく主アクションとして `size="lg"` にする。`ContentActionBar` で説明と操作を並べる場合も同じ。同じバーの再試行ボタンも `lg` にそろえる。
- **構造**：`ContentActionBar` は `aria-label` 必須。複雑な左側の状態は `leading`、単純な情報は `title` / `description` / `meta` を使う。
- **レスポンシブ**：375px では折り返してよい。ただし操作はコンテンツの上に残し、本文やコードの開始位置を押し下げすぎない。ページの横スクロールを出さない。
- **境界**：ページ全体・選択オブジェクト全体に作用する操作は `PageHeader` / `ObjectActionBar` に置き、`ContentActionBar` に混ぜない。

## 5.2.1 フォームの操作行

フォームの末尾の操作は `FormActionBar`（`packages/ui`。#226）にまとめる。手書きのボタン列を組まない。設定の画面のカード（1 ページに保存の対象が複数ある画面）はカードの末尾に置く。詳細・作成・編集の画面（1 ページ = 1 つの対象）の確定もフォームの末尾の `FormActionBar` に置き、画面の下端に貼り付けない（§4、#1786 / #2715。#618 では `PageHeader` の右端、#1786 では長いフォームの途中で下端に貼り付けていた）。

- **並び**：`primaryActions`（保存・作成など）→ `secondaryActions`（キャンセルなど）の順に直置きし、`status`（`FormStatus` など）はその右に置く。
- **破壊的操作**：`dangerActions`（削除など）は赤いボタンとして直置きせず、右端の「その他の操作」メニューへまとめる（`moreLabel` で製品の i18n から差し替える。既定は日本語）。メニューは WAI-ARIA の Menu Button（`aria-haspopup="menu"`、`Escape` / 矢印 / `Home` / `End`、閉じたら起点へフォーカスを戻す）。
- **一覧・詳細との共有**：同じ対象の操作は `EntityAction` で 1 回だけ定義し、フォームへは `entityActionToFormAction` で渡す（[§5.1](#51-オブジェクト操作一覧行--詳細)）。
- **構造**：`aria-label` 必須（例: `ロール編集操作`）。上に区切り線を引き、375px では縦に積む。
- **送信**：`<form>` の中の保存は `type: "submit"`（`FormActionDescriptor`。既定は `"button"`）にして、Enter による暗黙の送信と `onSubmit` の検証をそのまま使う。保存中もラベルは変えず、`loading` で先頭アイコンをスピナーにする（#296）。

## 5.3 一括選択バー

複数選択のリストで全選択を出す場合は、必ず全解除も同じスコープに並べ、`BulkSelectionActions` を使う。

- **配置**：リストの直上またはグループ見出しの先頭側（日本語 UI では左側）に置く。グループ見出しではタイトル・件数の下に左揃えで置き、右寄せにしない。対象のスコープ（全件 / 表示中 / グループ）が変わる位置に混ぜない。
- **業務操作との分離**：全選択・選択解除は左側の選択範囲コントロールとしてまとめ、選択項目に適用する追加・保存・削除・実行などの一括操作は右側へ分ける。
- **variant**：全選択は `secondary`、全解除は `ghost`。どちらも `size="sm"` を既定とする。
- **disabled**：対象がすべて選択済みなら全選択を disabled、選択が 0 件なら全解除を disabled。読取専用・処理中は両方 disabled。
- **aria-label**：グループ単位の同名ボタンは `"{name} をすべて選択"` / `"{name} の選択をすべて解除"` のようにスコープ名を含める。
- **文言**：全範囲は `すべて選択` / `選択をすべて解除`、表示中の範囲は `表示中をすべて選択` / `表示中の選択をすべて解除` とし、`全選択` / `全解除` の短縮形は使わない。
- **レスポンシブ**：`flex-wrap` で折り返し、375px でも横スクロールを出さない。ボタンの文言は `whitespace-nowrap` を保ち、隣のテキストと重ねない。

---

## 6. Toggle / Segmented chip

絞り込み・モード・対象の切替は `aria-pressed` 付きの選択（`ToggleChip` または `<Button variant="secondary" aria-pressed={selected}>`）を使う。同じグループは `role="group"` + `aria-label` でまとめる。同じ対象の別の見方への切替は `Tabs` を使い、`ToggleChip` をタブの代わりにしない（デザインシステムの規約）。

ペイン・カードの中の見方の切替（原本の処理前 / 処理後、表示形式など）も `Tabs` + `TabPanel` にする。枠（`border` + `bg-surface-sunken`）の中に `Button` や素の `<button>` を並べたセグメントを手書きしない（枠線が二重になり、選択状態が読み上げられない。デザインシステム README §4「`Tabs`」、#396）。

タブ（`role="tab"`）、combobox、listbox の option、情報一覧の選択行、Sidebar のナビゲーションはアクションボタンと構造が違うため、各共通部品のレイアウトと ARIA を保つ。これらの生 `<button>` は明示的な適用除外であり、保存・コピー・開閉・削除を独自 CSS で作る例外ではない。

---

## 6.1 並べ替え列頭

- 並べ替えの列頭は `DataTable` の `sort`（`sortable` な列の列頭）を正とする。通常のアクションの `Button` や選択トグルの見た目を使わない。
- 文字と並べ替えアイコンだけを表示し、どの状態でも外枠・角丸・影・塗りの背景を付けない。選択中の方向は矢印とアクセシブルな状態で示す。
- 操作領域は desktop 32px / touch 44px を保ち、一覧の見える行数を変えない。
- Enter / Space / Tab とネイティブの disabled を保つため、内部は意味上の `<button type="button">` を使う。これは列頭専用の構造コントロールとしての明示的な適用除外である。
- キーボードフォーカスは列名の下線で示す。ボタン風の外枠を付けない。

---

## 7. 命名規則（naming）

- **コンポーネント**：アクションは常に `<Button>`。同じバーを再利用する場合は `XxxActionBar` のような名前で部品にする。
- **ラベルの文言 key**：`<domain>.<feature>.actions.<verb>`（例：`settings.model.actions.save` 相当）。汎用語は `common.*`（`common.confirm` / `common.cancel` / `common.delete` / `common.dismiss` / `common.undo`）。
- **初期化・クリアの操作**：handler が実際に変える状態に合わせ、空にする操作は `対象をクリア`、既定値へ戻す操作は `対象をリセット`、選択・絞り込みの解除は `対象を解除`、表示だけを消す操作は `対象表示を消去` とする。単独の `クリア` / `リセット` は使わない。共通 `ClearActionButton` の `label` は必須とし、ファイル用の既定ラベルを他の操作に流用しない。
- **ファイル選択の解除ボタン**：640px 未満ではファイル入力の下に置き、入力とボタンの間を 8px 以上空ける。名前を具体的にしても、選んだファイル名の表示幅を潰さない。desktop は同じ行に置く。
- **表示を取り直す操作**：今の表示（一覧・状態・ログ・参照情報など）をサーバーから取り直すだけの操作は、3 製品と共通パッケージで `最新情報を取得` にそろえる（#1797）。ページ全体（`PageHeader` の `utility`）でもカード・パネルの中でも `最新情報を取得` を基本にし、`表示を更新` / `再読込` / `状態を再取得` / `更新` などの言い換えを作らない。利用者の入力・選択・絞り込み・ページは残す（[workspace-state.md](./workspace-state.md)「入力を残した再取得」）。定義ファイルから一覧を読み直すだけの操作（Agent のスキルの読み直し）もこれに含める。
  - 対象外：サーバーで同期・再構築・保存を始める操作は対象を名前に含める（`DB 構造を再取得`・`DB Profile 一覧を再取得`・`スキーマを更新`・`インデックスを更新` など）。対象を選んでから初めて取る操作は `情報を取得` / `テーブル一覧を取得` のように取得の対象を書く。失敗の通知の取り直しは `再試行`（下の項目）、未保存の変更を捨てて読み直す競合の解消は `最新の内容を読み込む` とする。
  - **1 つの画面に同じ名前の `最新情報を取得` を 2 つ以上出さない**（#1868。どれが何を取り直すのか分からず、スクリーンリーダーでも区別できない）。画面全体を取り直す `PageHeader` のボタン（ページ全体）は `最新情報を取得` のまま、同じ画面のパネル・カード・一覧の中のボタンは `〈対象〉の最新情報を取得` と対象を書く（例: `訓練データの最新情報を取得`・`ログの最新情報を取得`・`エントリの最新情報を取得`・`生成状況の最新情報を取得`）。対象はパネルの見出しの語を短くしたもの（「〜一覧」は付けない）にし、375px で折り返し・はみ出しが無いことを確かめる。入らないほど長いときだけ、見える文言を短くして `aria-label` で対象を補う（見える文言はアクセシブルな名前の先頭に含める）。`PageHeader` のボタンが無く画面に 1 つだけなら `最新情報を取得` でよい。
  - **失敗の通知の取り直しは `再試行`**（共通の key は `common.retry`。#1868）：読み込み・処理に失敗したことを知らせる `PageNotice` / `Banner`（danger、または取得の失敗を知らせる warning）と `ErrorState` / `ApiErrorState` の取り直しは、`最新情報を取得` を使わずに `再試行` にする。失敗の文の案内（「〜で再試行してください」）もボタンの名前に合わせる。失敗ではない警告・案内（未確認・取得時点の内容など）から取り直すボタンは、上の書き分けに従う。
  - e2e は名前で区別できる locator で押す（`getByRole("button", { name: "最新情報を取得", exact: true })` はページ全体のボタン、パネルのボタンは対象を含めた名前、失敗の通知は `再試行`）。`exact` の無い `最新情報を取得` は `〈対象〉の最新情報を取得` にも当たるので、ページ全体のボタンを押すときは `exact: true` を付ける。
- **新しい作業を始める操作**：作業全体を初期化する操作は `新しい〇〇を開始` とする。破棄の確認では、何が消えて何が既定値に戻るかを説明し、確定ボタンも起点と同じ文言にする。
- **aria-label**：
  - グループ内で同じラベルが重なる操作は `「${セクション名}: ${ラベル}」`（例：`モデル設定: 保存`）。
  - 連番の要素の操作は末尾に番号（例：`モデルを削除 1`）。
  - **Icon-only は aria-label 必須**（例：閉じる＝`common.dismiss`）。
- **イベントハンドラ**：コンポーネント内は `handle<Verb>`（`handleSave`）、props 経由は `on<Verb>`（`onSave` / `onRetry` / `onRemove`）。

製品ごとの具体的なラベル（リセット操作の対象と範囲の表など）は、製品の `docs/frontend-button-spec.md` に書く。

---

## 8. アイコン / ローディング

- Lucide を使う。ラベル付きは**アイコンを左**に置き（共通 `Button` の `icon` prop）、`aria-hidden` を付ける。
- サイズは共通 `Button` の `icon` スロットが制御する。
- 非同期処理は `loading` prop を使う（共通 `Button` がアイコンをスピナーに置き換え、`aria-busy` と `aria-disabled` を付けてクリック・Enter / Space・form の送信を止める）。
  - **`loading` を渡すボタンは必ず `icon` を持つ**（アイコンが無いとスピナーの分だけ幅が変わる。adherence の lint が検出する）。
  - **`loading` 中もフォーカスはボタンに残る。** ネイティブの `disabled` を付けるとフォーカスが `body` へ外れるため、共通 `Button` は `loading` のときだけ `aria-disabled` にする。完了後もフォーカスはボタンのまま（#355）。製品側で `disabled={pending}` を足してフォーカスを外さない。
    - 処理中に他の操作をまとめて無効にするときも、押したボタンを `disabled` にしない。共有の `busy` を使うなら `disabled: busy && !loading` にする（`disabled` と `loading` が重なると `disabled` が優先され、ネイティブの disabled になる）。押したボタンを含む領域を `<fieldset disabled>` で包まない（中のボタンの prop に関係なくすべてが `:disabled` になる）。他の入力欄・操作は部品ごとに `disabled` を渡す（#835）。
  - **ボタン内の loading 表示は共通 `Button` に任せる。** `Loader2` や `Spinner` を子要素として描画しない。loading 中にラベルを「実行中…」などに差し替えない。
  - **同じ処理の動的なスピナーは 1 つだけ**にする。主ボタンが `loading` の場合、同じ処理を説明する `ProcessingIndicator` / `TimedLoadingState` は `activityIcon="none"` にして、静的なラベル・経過時間・slow hint だけを表示する。
  - 更新・同期などの busy 表示も `<Button loading>` を使い、ボタン内のアイコンに個別に `animate-spin` を付けない。
  - **`loading` は押したボタンだけが持つ（#819）。** `loading` を true にするのは、**そのボタンが始めた処理**の間だけ（Material・Carbon・Apple HIG と同じ。スピナーは利用者が操作した部品に出す）。同じ処理の間に使えない他のボタンは `disabled` にするだけで、スピナーを出さない。
    - 複数のボタンが 1 つの mutation・関数を共有するとき（「保存」と「情報を再取得」が同じ保存を呼ぶ、行ごとの同じ操作など）は、どのボタンが始めたかを `activeOperation` の state・`mutation.variables`（`isPending` のときだけ読む。完了後も前の値が残る）・行の id で区別する。共有の `busy` / `pending` を複数のボタンの `loading` に渡さない（`disabled` には渡してよい）。
    - 「最新情報を取得」などの再取得のボタンは、**押した取り直しの間だけ** `loading` にする。query の `isFetching` をそのまま渡すと、定期の取り直し（`refetchInterval`）・他の操作の後の invalidate・絞り込みやページの切り替え（`keepPreviousData`）でも回る。押した取り直しを state で持つ（共通の `useActionPending` など）。初回の読込でどちらがスピナーを出すかは [messaging.md §3.7](./messaging.md) に従う。
    - 例外: ボタンが表す状態そのものが進行中のとき（Autonomous Database の「起動」と `STARTING`、「停止」と `STOPPING`）は、押していなくても `loading` にしてよい。durable job の開始のボタンは送信の間だけ `loading`、job の間は `disabled`（[messaging.md §3.7](./messaging.md)）。
- emoji をアイコンに使わない（`no-emoji-icons`）。

---

## 9. アクセシビリティ チェックリスト（必須）

- [ ] Icon-only に `aria-label`。見える名前は共通 `Button` の Tooltip（`iconOnly` の既定で `aria-label` と同じ文言）が出す。HTML の `title` 属性で説明しない（キーボード・タッチで出ない）。文言を変えるときは `tooltip`（[デザインシステム README §4「`Tooltip`」](../design-system/README.md)）。
- [ ] 操作領域：desktop 32/36/40px、icon-only 36px、coarse pointer は入力欄・選択欄も含めて 44px。同じ行の入力欄・選択欄・ボタンは同じ段（#613）。
- [ ] `cursor-pointer` / `focus-visible` のリング（共通 `Button` 済み）。フォーカスの表示は outline 1 つ。`focus:ring-*` / `focus-visible:ring-*` を足したり、`focus(-visible):outline-none` で消したりしない（[デザインシステム README §4「フォーカスの表示」](../design-system/README.md)、adherence の lint が検出する）。
- [ ] disabled は `disabled` 属性 + disabled の意味の色（共通 `Button` 済み）。見た目だけの無効化をしない。
- [ ] `loading` 中は `aria-disabled="true"` + `aria-busy="true"`（共通 `Button` 済み）。Enter → loading → 完了でフォーカスがボタンに残り、`loading` 中の Enter / Space / クリック / 入力欄の Enter で二重に送信しない。
- [ ] `loading`（`aria-busy="true"`）になるのは押したボタンだけ。同じ処理で使えない他のボタンは `disabled` だけ（§8。#819）。
- [ ] トグルは `aria-pressed`、色だけで状態を伝えない。
- [ ] 破壊的操作は danger + 確認（`useConfirm`）、主アクションと分ける。確認の前の起点・取り消せる停止や拒否は赤塗りにしない（§3）。
- [ ] その場で結果を待つ操作は、実行と停止が同じ要素の 1 つのボタン。実行 → 停止 → 待機でフォーカスが残り、実行中の入力欄の Enter とダブルクリックの 2 回目で停止しない（§3.1）。

---

## 10. 使用例（正）

```tsx
// 主アクションバー（設定）
<div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
  <Button size="lg" icon={Save} loading={save.isPending} onClick={handleSave}>
    {t("settings.model.actions.save")}
  </Button>
  <Button size="lg" variant="secondary" icon={TestTube2} onClick={handleTest}>
    {t("settings.model.actions.test")}
  </Button>
  <FormStatus tone="success" message={saved ? t("...saved") : undefined} />
</div>

// 破壊的操作（行内の trigger → 確認ダイアログ）
<Button
  variant="ghost"
  size="sm"
  iconOnly
  icon={Trash2}
  aria-label={`${section}: ${t("common.delete")}`}
  onClick={async () => {
    if (await confirm({ title, tone: "danger", confirmLabel: t("common.delete") })) remove();
  }}
/>
```

---

## 12. 根拠と検証

- [Carbon Button usage](https://carbondesignsystem.com/components/button/usage/)：操作の重要度に合わせた variant、文脈に合わせた size、危険操作の段階的な強調。
- [Carbon Button style](https://carbondesignsystem.com/components/button/style/)：色・寸法・状態を部品側の token へ集める考え方。
- [WCAG 2.2 Target Size (Minimum)](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum/)：AA の 24 CSS px（例外あり）を下限として参照。3製品はモバイルを 44px にする。
- `ui-ux-pro-max`：touch target 44px、間隔 8px、キーボード focus、loading feedback と reduced motion。

各製品は、実際の部品を使って light / dark × desktop / mobile-375 の寸法・状態・操作・focus・danger の確認・overflow を Playwright で確認する（例：NL2SQL の `tests/e2e/button-standards.spec.ts`）。
