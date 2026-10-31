# フロントエンドのローカルフォント

Noto Sans JP / Noto Sans SC / Noto Sans TC / Noto Sans HK / Roboto / Google Sans Code は Fontsource パッケージ（各 5.3.x）として
`npm ci` 時にローカルの `node_modules/@fontsource/`（JP / SC / TC / HK は可変書体の `node_modules/@fontsource-variable/`）へダウンロードする。
`src/main.tsx` が Noto Sans JP の可変書体（`wght.css`）と Roboto・Google Sans Code の通常体の 400 / 500 / 600 / 700 を読み込み、Vite が CSS と
フォント実体を `dist/assets/` に同梱する。Noto Sans SC / TC / HK の `@font-face` は主の CSS に入れず、
`src/fonts-cjk.css` を最初の描画の後に `import()` で読む（別の CSS の chunk。共有の `loadStylesAfterFirstRender`。#3305）。JP / SC / TC / HK は
1 つの face が太さ 100〜900 を持つ可変書体（書体名は `Noto Sans JP Variable` など）にし、`@font-face` を slice の数に抑え、どの太さも合成の太字にせず本物の字形で出す
（静的な書体の slice × 太さの `@font-face` は、ページを読み込むたびに描画を数秒止めた。#3328・#3333）。実行時のフォント取得はアプリと
同一 origin へのリクエストのみで、Google Fonts / CDN や端末への事前インストールを必要としない。

日本語・中国語の文字範囲別サブセットは CSS の `unicode-range` により必要な分だけ取得する（preload しない）。
`font-display: swap` で初回ロード中も文字を表示する。
共有 UI の `"Noto Sans JP Variable", "Noto Sans SC Variable", "Noto Sans TC Variable", "Noto Sans HK Variable", "Roboto", system-ui, ...` の優先順位を維持し、
表頭もグローバルフォントを継承する。英数字も Noto Sans JP に収録される文字は同フォントが優先される。
日本語の字形を優先し、Noto Sans JP に無い簡体字・繁体字・香港の字だけを Noto Sans SC → TC → HK で補う。

| 用途 | 指定 |
| --- | --- |
| 通常 UI・日本語の業務名・件数・日時・割合・経過時間・サンプル値 | `font-sans`（Noto Sans JP → Noto Sans SC → TC → HK → Roboto） |
| SQL・コード・技術ログ・物理表/列/Schema 名・技術 ID | `font-mono`（Google Sans Code → Noto Sans JP → Noto Sans SC → TC → HK → Roboto） |

`code` / `pre` / `kbd` / `samp` もローカルコード用フォントを使う。
Google Sans Code に日本語は含まれないため Noto Sans JP（無い字は SC → TC → HK）で補う。
混在する日本語と英数字の幅比は固定されないため、空白文字による表レイアウトを前提としない。
業務数字の桁揃えが必要な場合は `tabular-nums` を併用する。
別の `font-family` を画面ごとに追加せず、この二つの共通スタックを使用する。

フォントのライセンス原文は、`npm run build` が `@fontsource*` の package の `LICENSE` から
`dist/licenses/fonts/<書体>-OFL.txt`（一覧は `dist/licenses/fonts/README.txt`）に出す（platform の共有の Vite の plugin
`@production-ready/ui/vite/third-party-licenses`。3 製品で同じ。#3392）。原文を repo に手で置かない。
第三者の部品の通知の全体は root の [THIRD-PARTY-NOTICES.md](../../THIRD-PARTY-NOTICES.md)。

検証:

```bash
npm run build
npx playwright test tests/e2e/profile-archive-reset.spec.ts --grep 'ローカルフォント'
npx playwright test tests/e2e/nl2sql-workflows.spec.ts --grep 'ローカルコードフォント'
```

Playwright は外部 origin を遮断し、フォントの全使用ウェイトのロード、
日本語表頭と英日混在 SQL の実描画フォントを確認する。
主要画面では可視テキスト・入力欄を走査して上記二つのスタック以外の使用を検出し、
desktop / mobile-375 の配置とキーボード操作を確認する。
