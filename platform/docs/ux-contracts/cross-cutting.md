# 横断的な保守・セキュリティ契約

> 3製品共通の原則。NL2SQL の再発防止策（engchina/no.1-production-ready-nl2sql#168–#185）から、製品に依存しない部分を移した。
> 製品固有の権限モデル（role の権限昇格の規則など）は各製品の `AGENTS.md`（とそこから案内する `docs/agents/`）に書く。

## 更新 API とデータの所有範囲

- `PATCH` / `PUT` の request schema と frontend の payload には、**そのユースケースが所有して更新する field だけ** を含める。別の画面・別の endpoint が管理する subresource を「今の値の送り返し」で兼ねない。受け取った所有外の field を tuple や簡略な DTO に写して組み直してはならない。
- 更新の対象外の subresource は、永続化された record をそのまま残す。特に ID・対象の owner / object・列・filter・外部 resource 名・checksum・apply / lifecycle の状態のような identity と適用状態を、欠けた DTO や既定値で `DELETE → INSERT` して置き換えない。
- identity から外部（Oracle など）の resource 名を導く object を変える・採番し直すときは、既存の resource の cleanup / migration / orphan の検出を同じ変更で設計する。外部の resource を残したまま UI・管理 record から見えなくしてはならない。
- 部分更新の回帰テストは「変えた field」だけでなく、**所有外の field のすべての不変条件** を更新の前後で確かめる。API の request / response、domain service、store の各境界をまたいで、情報の落ちる変換が起きないことを固定する。

## 認可と状態遷移をサーバー側で強制する

- ボタンの非表示・disabled は UX であり、認可の境界ではない。すべての mutation は、store を更新する前に backend の domain service で actor の実効権限と resource の状態を検証し、API を直接呼んでも迂回できないようにする。store の実装（InMemory / Oracle など）が複数あるときは、判定・status code・状態の保持をそろえる。
- archive 済みの resource は、明示的な restore / delete の流れを除いて変更できないものとする。通常の更新 endpoint は `409` で拒み、更新による暗黙の restore や、一部の field だけの書き換えを許さない。
- 権限の割当・変更では、actor が自分の持たない権限を他人（または自分）に与えられないようにする（権限昇格の防止）。規則の詳細は製品の権限モデルに合わせて各製品の `AGENTS.md`（とそこから案内する `docs/agents/`）に書く。

## i18n の変更と E2E の locator

- i18n の key / 値の変更は UI の変更として扱う。翻訳の diff から古い文言を挙げ、実装だけでなく frontend のテスト全体を検索して、`getByRole` / `getByLabel` / region / 空状態などの locator と期待する文言を同じ変更で直す。一部の spec だけ直して完了にしない。
- 文言の変更の検証では、その locator を使う Playwright の spec を desktop と `mobile-375` の両方で流す。既存の skip は理由を明記する。build / logic のテストだけでは locator の古さを見つけられない前提で進める。
- 共有パッケージの画面（`@production-ready/system-settings` など）の文言を変えるときは、3製品すべての E2E を同じ変更で直す。

## ロールの表示（#1682）

- 画面のロールは**ロール名（表示名。例「システム管理者」）**で出す。表示名が空ならロールコード。ロールの ID（UUID の `role_id`）とロールコードは、バッジ・チップ・表の列・選択肢・見出し・確認ダイアログ・`aria-label`・tooltip に出さない。ロールコードは、ロール管理と権限管理の編集の「ロールコード」の項目（属性）にだけ出す。
- 参照できない（削除済みの）ロールは、ID を出さずに「（削除済みのロール）」と出す。文字列は `@production-ready/system-settings` の `roleDisplayName`、バッジは `RoleNameBadges`、一覧の行は `RoleNameRowTitleButton` を使い、製品で書かない。backend はロールの ID だけを返さず、表示名を添える。

## ユーザーの表示（#1696）

- **サイドナビのアカウント欄の 1 行目だけはログイン ID（例 `system_admin`。2 行目はロール名）、それ以外のすべての画面はユーザーの表示名**（例「システム管理者」）で出す（実行履歴の実行者・承認の判断者・版の公開者・作成者 / 更新者・ユーザーの選択欄・ユーザー管理の一覧と見出し・確認ダイアログ・`aria-label`）。表示名が空ならログイン ID。UUID とログイン ID は出さない（ログイン ID はユーザー管理の「ログインユーザーID」の項目にだけ出す）。引けない利用者（削除済みなど）は「（削除済みのユーザー）」。文字列は `@production-ready/system-settings` の `userDisplayName` / `deletedUserLabel` を使い、backend はログイン ID・UUID だけを返さず表示名を添える。

## アカウントの画面（#1873）

- **個人の操作（API キー・パスワード変更）は、サイドナビの下部のアカウントの欄から開くアカウントの画面（`/account`）にまとめる。** 欄（名前・ロール）の行全体がリンク（高さ 44px 以上、読み上げ名「アカウント設定を開く（名前）」、開いている間は `aria-current="page"` と強調）。テーマ・ログアウトのボタンは欄の下のまま。375px のドロワーでは押すと閉じて移る。
- 画面は共通の `AccountPage`（3 製品で同じ）。見出し「アカウント」、説明に名前とロール、`PageHeader` の `tabs` に「API キー」「パスワード変更」。タブは URL（`?tab=api-keys` / `?tab=password`）に残す（戻る・直接開く・再読み込み）。
- ローカルの認証（ログインの省略）では「パスワード変更」のタブに説明（パスワードを使わない）だけを出す。初回・リセット後の強制の変更は単独の画面（`/password/change`）のまま。
- ログインの画面の「パスワードを忘れた場合」は、管理者に一時パスワードの発行を依頼する案内を同じ画面に開く（メールの送信は無い）。

## ページ遷移・未保存変更

[workspace-state.md](./workspace-state.md) を参照。
