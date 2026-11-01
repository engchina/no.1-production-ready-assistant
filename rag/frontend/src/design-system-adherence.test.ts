/* eslint-disable no-restricted-syntax -- adherence の規則のテスト。検出させる違反の例を文字列で持つため。 */
import { ESLint } from "eslint";
import { describe, expect, it } from "vitest";

// platform の adherence（デザインシステム遵守ルール）の正本。RAG / Agent は ESLint の no-restricted-syntax に、
// NL2SQL は oxlint の JS プラグインに同じセレクタを渡す（platform AGENTS.md「lint」節）。
import adherence from "../../../platform/docs/design-system/adherence.oxlintrc.json" with { type: "json" };

const LOADING_ICON = "loading を渡す Button には icon を渡す";
const FOCUS_RING = "フォーカスの表示を ring";
const HANDWRITTEN_SEARCH = "アイコン付きの入力欄（検索欄）を手書きしない";
const HANDWRITTEN_DISCLOSURE = "開閉できる領域は <details> / <summary> を手書きせず";
const HANDWRITTEN_TEXTAREA = "複数行の入力欄は <textarea> を手書きせず";
const NATIVE_SELECT = "選択欄はネイティブの <select> を手書きせず";
const LIST_SEARCH = "一覧の絞り込みの検索欄は SearchField";
const OPTIONAL_MARKER = "任意の欄を「(任意)」や placeholder で示さない";
const HANDWRITTEN_REQUIRED = "必須の表示を手書きしない";
const TOUCH_TARGET = "製品で touchTarget を使わない";
const CONTROL_HEIGHT = "共有の操作部品（Button / TextField";
const FIELD_WIDTH = "入力欄・選択欄の幅を w-* / max-w-* で書かない";
const NATIVE_HEIGHT = "ネイティブの <input> / <select> の高さを";
const BACK_IN_ACTIONS = "「一覧へ戻る」を PageHeader の actions";
const CONFIRM_IN_HEADER = "詳細・作成・編集の画面の確定（作成・保存）と「変更を破棄」を PageHeader の actions に入れない";
const HANDWRITTEN_TABLE = "<table> を手書きしない";
const JSX_ACTIONS = "PageHeader の actions に JSX";
const RAW_COLOR_FUNCTION = "rgba() / rgb() / hsl() などの生の色を書かない";
const NUMERIC_TYPE = "文字サイズ・行間・字間を数値";
const NUMERIC_SPACING = "inline style の余白・角丸を数値";
const TEXTAREA_HEIGHT = "複数行の入力欄（TextareaField）の高さを";
const HANDWRITTEN_SPIN = "回転するアイコンを手書きしない";
const LOADER_ICON = "回転用の lucide のアイコン";
const INLINE_SPIN = "inline style の animation で回転";
const SURFACE_TINT = "淡い色の面（bg-accent-subtle";
const SELECT_SEARCH_ICON = "検索して選ぶ選択欄（SearchableSelectField）の閉じたボタンに虫眼鏡";

async function lint(code: string) {
  const eslint = new ESLint({
    overrideConfigFile: true,
    overrideConfig: [
      {
        files: ["**/*.tsx"],
        languageOptions: {
          parser: (await import("@typescript-eslint/parser")).default,
          parserOptions: { ecmaFeatures: { jsx: true }, ecmaVersion: "latest", sourceType: "module" },
        },
        rules: { "no-restricted-syntax": adherence.overrides[0].rules["design-system/restricted-syntax"] as never },
      },
    ],
  });
  const [result] = await eslint.lintText(code, { filePath: "probe.tsx" });
  return result.messages.map((message) => ({ line: message.line, message: message.message }));
}

function linesWith(messages: { line: number; message: string }[], text: string) {
  return messages.filter((message) => message.message.startsWith(text)).map((message) => message.line);
}

describe("adherence: loading を渡す Button の icon（#355）", () => {
  it("icon の無い loading を検出し、icon あり・spread・loading なしは許す", async () => {
    const messages = await lint(`
const a = <Button loading={busy}>保存</Button>;
const b = <Button loading={busy} icon={Save}>保存</Button>;
const c = <Button {...props} loading={busy}>保存</Button>;
const d = <Button>保存</Button>;
const e = <Button loading={busy} iconOnly icon={X} aria-label="閉じる" />;
const f = <Other loading={busy}>保存</Other>;
`);
    expect(linesWith(messages, LOADING_ICON)).toEqual([2]);
  });
});

describe("adherence: フォーカスの表示は outline（#355）", () => {
  it("focus / focus-visible / focus-within の ring と outline-none を検出する", async () => {
    const messages = await lint(`
const a = <button className="focus:outline-none focus:ring-2">x</button>;
const b = <button className={\`px-2 focus-visible:ring-focus-ring \${x}\`}>x</button>;
const c = <div className="md:focus-within:ring-2">x</div>;
const d = <div className="peer-focus-visible:ring-offset-2">x</div>;
const e = <div className="focus-visible:outline-none">x</div>;
`);
    expect(linesWith(messages, FOCUS_RING)).toEqual([2, 3, 4, 5, 6]);
  });

  it("outline の調整・フォーカス以外の ring・入力欄の outline-none は許す", async () => {
    const messages = await lint(`
const a = <div className="ring-2 ring-accent-emphasis">選択中</div>;
const b = <button className="focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus-ring">x</button>;
const c = <input className="outline-none focus:border-focus-ring" />;
const d = <div className="peer-focus-visible:outline-2 focus-within:outline-2">x</div>;
const e = "string with focus ring words";
`);
    expect(linesWith(messages, FOCUS_RING)).toEqual([]);
  });
});

describe("adherence: 手書きの検索欄（#384）", () => {
  it("アイコンの分の左の余白（pl-7〜12 / ps-* / 任意値）を持つ input を検出する", async () => {
    const messages = await lint(`
const a = <input className="h-9 w-full rounded-md border pl-9 pr-3" />;
const b = <input className={\`\${INPUT_CLASS} pl-9\`} />;
const c = <input className={cn("w-full", "pl-8")} />;
const d = <input className="md:pl-10" />;
const e = <input className="ps-9" />;
const f = <input className="pl-[2.25rem]" />;
`);
    expect(linesWith(messages, HANDWRITTEN_SEARCH)).toEqual([2, 3, 4, 5, 6, 7]);
  });

  it("TextField・小さい余白の input・input 以外は許す", async () => {
    const messages = await lint(`
const a = <TextField id="q" label="検索" leadingIcon={Search} />;
const b = <input className="px-3 pl-2" />;
const c = <div className="pl-9">入れ子の行</div>;
const d = <textarea className="pl-3" />;
`);
    expect(linesWith(messages, HANDWRITTEN_SEARCH)).toEqual([]);
  });
});

describe("adherence: 手書きの開閉（#397）", () => {
  it("<details> を検出し、Disclosure・DisclosureChevron・details 以外は許す", async () => {
    const messages = await lint(`
const a = <details><summary>詳細</summary>本文</details>;
const b = <details open={open} className="group/disclosure"><summary className="flex">詳細</summary></details>;
const c = <Disclosure summary="詳細">本文</Disclosure>;
const d = <button aria-expanded={open}>詳細<DisclosureChevron expanded={open} /></button>;
const e = <div data-details="x">詳細</div>;
`);
    expect(linesWith(messages, HANDWRITTEN_DISCLOSURE)).toEqual([2, 3]);
  });
});

describe("adherence: 手書きの複数行の入力欄（#584）", () => {
  it("<textarea> を検出し、TextareaField・textarea 以外は許す", async () => {
    const messages = await lint(`
const a = <textarea id="p" value={v} onChange={onChange} />;
const b = <textarea id="p" rows={3} className="w-full rounded-md border">{v}</textarea>;
const c = <TextareaField id="p" label="プロンプト" value={v} onValueChange={setV} />;
const d = <div data-textarea="x">本文</div>;
`);
    expect(linesWith(messages, HANDWRITTEN_TEXTAREA)).toEqual([2, 3]);
  });
});

describe("adherence: ネイティブの選択欄（#631）", () => {
  it("<select> を検出し、SelectField・SearchableSelectField・option だけの要素は許す", async () => {
    const messages = await lint(`
const a = <select value={v} onChange={onChange}><option value="a">A</option></select>;
const b = <select className={fieldControlClassName({ width: "sm" })} disabled value={v} onChange={onChange} />;
const c = <SelectField id="r" label="リージョン" value={v} options={options} onValueChange={setV} disabled />;
const d = <SearchableSelectField id="kb" label="ナレッジベース" value={v} options={options} onValueChange={setV} />;
const e = <div data-select="x"><option value="a">A</option></div>;
`);
    expect(linesWith(messages, NATIVE_SELECT)).toEqual([2, 3]);
  });
});

describe("adherence: 一覧の絞り込みの検索欄は SearchField（#535）", () => {
  it("type=search の TextField と input を検出し、SearchField・重い検索の TextField は許す", async () => {
    const messages = await lint(`
const a = <TextField id="q" label="検索" type="search" value={q} onValueChange={setQ} />;
const b = <input type="search" value={q} onChange={onChange} />;
const c = <SearchField id="q" label="検索" value={q} onSearch={setQ} clearLabel="検索語をクリア" />;
const d = <TextField id="ask" label="質問" value={q} leadingIcon={Search} onKeyDown={onKeyDown} />;
const e = <input type="text" value={q} onChange={onChange} />;
`);
    expect(linesWith(messages, LIST_SEARCH)).toEqual([2, 3]);
  });
});

describe("adherence: 任意の表示（#531）", () => {
  it("placeholder の「任意」と、ラベル・文言の「(任意)」「（任意）」を検出する", async () => {
    const messages = await lint(`
const a = <TextField id="m" label="メモ" placeholder="メモ(任意)" />;
const b = <textarea placeholder={\`説明（任意）\`} />;
const c = { "searchAnswerProfiles.field.descriptionPlaceholder": "この検索・回答プロファイルの用途(任意)" };
const d = { descriptionPlaceholder: "任意で入力します" };
const e = { "feedback.controls.commentLabel": "コメント（任意）" };
const f = <label htmlFor="x">許可表(任意・カンマ区切り)</label>;
const g = { "settings.huggingface.field.token": "ダウンロード token( 任意 )" };
`);
    expect(linesWith(messages, OPTIONAL_MARKER)).toEqual([2, 3, 4, 5, 6, 7, 8]);
  });

  it("任意の意味の説明文・データの値・任意の欄の placeholder の例は許す", async () => {
    const messages = await lint(`
const a = { "settings.prompts.list.description": "任意の版を有効化(rollback)できます。" };
const b = { "ontologyResults.field.optional": "任意（Optional）" };
const c = <TextField id="m" label="メモ" placeholder="例: 月次の締め処理" />;
const d = { "evaluation.analyze.description": "任意の SQL を確認します。" };
`);
    expect(linesWith(messages, OPTIONAL_MARKER)).toEqual([]);
  });
});

describe("adherence: 手書きの必須表示（#531）", () => {
  it("「*」・「必須」の手書きと RequiredBadge の直接の使用を検出する", async () => {
    const messages = await lint(`
const a = <label htmlFor="n">名前 <span className="text-danger-fg">*</span></label>;
const b = <label htmlFor="n">名前 *</label>;
const c = <label htmlFor="n">名前{" *"}</label>;
const d = <span className="rounded-full">必須</span>;
const e = <StatusBadge variant="warning" label="必須" />;
const f = <label htmlFor="n">名前<RequiredBadge label={t("common.required")} /></label>;
const g = <span className="text-xs">{t("common.required")}</span>;
const h = <Badge label={t("settings.database.requiredMark")} />;
const i = <label htmlFor="n">名前{"※必須"}</label>;
`);
    expect(linesWith(messages, HANDWRITTEN_REQUIRED)).toEqual([2, 3, 4, 5, 6, 7, 8, 9, 10]);
  });

  it("共有部品の required・requiredLabel の上書き・エラー文言・SQL の * は許す", async () => {
    const messages = await lint(`
const a = <TextField id="n" label="名前" required />;
const b = <TextField id="r" label="リージョン" required requiredLabel="利用時必須" />;
const c = <SelectField id="s" label="種別" required requiredLabel={t("common.required")} />;
const d = <FieldLabel htmlFor="sql" label="SQL" required />;
const e = <Fieldset legend="ロール" required>…</Fieldset>;
const f = <FieldError id="e" message={t("profiles.error.nameRequired")} />;
const g = <p>{t("searchAnswerProfiles.scope.required")}</p>;
const h = <code>SELECT * FROM t</code>;
const i = { scope_code: "*" };
const j = <Route path="*" element={<Home />} />;
`);
    expect(linesWith(messages, HANDWRITTEN_REQUIRED)).toEqual([]);
  });
});

describe("adherence: 操作部品の高さと幅（#613）", () => {
  it("touchTarget・共有部品の h-* / min-h-*・欄の w-* / max-w-*・ネイティブの欄の高さを検出する", async () => {
    const messages = await lint(`
const a = <Button touchTarget icon={Save}>保存</Button>;
const b = <Button size="sm" className="h-11 sm:h-8" icon={Plus}>新規</Button>;
const c = <SelectField id="s" label="状態" value="" options={[]} onValueChange={f} buttonClassName="min-h-11" />;
const d = <TextField id="t" label="名前" inputClassName="h-[44px]" />;
const e = <SelectField id="s" label="状態" value="" options={[]} onValueChange={f} className="w-full @md:w-48" />;
const g = <SearchableSelectField id="k" label="KB" value="" options={[]} onValueChange={f} className={cn("md:w-[22rem]", x)} />;
const h = <select className="h-10 w-full rounded-md border">x</select>;
const i = <input className={\`min-h-11 \${x}\`} />;
`);
    expect(linesWith(messages, TOUCH_TARGET)).toEqual([2]);
    expect(linesWith(messages, CONTROL_HEIGHT)).toEqual([3, 4, 5]);
    expect(linesWith(messages, FIELD_WIDTH)).toEqual([6, 7]);
    expect(linesWith(messages, NATIVE_HEIGHT)).toEqual([8, 9]);
  });

  it("size / width・min-w-0・w-full・ボタンの幅・チェックボックスの寸法・fieldControlClassName は許す", async () => {
    const messages = await lint(`
const a = <Button size="lg" className="w-full sm:w-auto" icon={Save}>保存</Button>;
const b = <SelectField id="s" label="状態" value="" options={[]} onValueChange={f} size="lg" width="md" className="min-w-0" />;
const c = <TextField id="t" label="名前" width="full" className="w-full max-w-full" />;
const d = <input type="checkbox" className="h-4 w-4" />;
const e = <input type={multi ? "checkbox" : "radio"} className="mt-1 h-4 w-4" />;
const g = <input type="number" className={fieldControlClassName({ size: "lg", width: "xs" })} />;
const h = <div className="min-h-11 w-48">x</div>;
const i = <TextareaField id="q" label="質問" rows={2} />;
`);
    expect(messages).toEqual([]);
  });
});

describe("adherence: 一覧へ戻るは PageHeader の back（#618）", () => {
  it("actions の id: back を検出し、back・ほかの操作は許す", async () => {
    const messages = await lint(`
const a = <PageHeader title="x" actions={[{ id: "back", kind: "secondary", label: "一覧へ戻る", onClick: f }]} />;
const b = <PageHeader title="x" back={{ label: "一覧へ戻る", onClick: f }} actions={[{ id: "refresh", kind: "utility", label: "表示を更新", onClick: g }]} />;
const c = <Other actions={[{ id: "back" }]} />;
`);
    expect(linesWith(messages, BACK_IN_ACTIONS)).toEqual([2]);
  });
});

describe("adherence: 詳細・作成・編集の画面の確定はフォームの末尾の FormActionBar（#1786）", () => {
  it("PageHeader の actions の保存・変更を破棄と、back のある画面の作成を検出し、一覧の新規作成・FormActionBar は許す", async () => {
    const messages = await lint(`
const a = <PageHeader title="x" back={{ label: "一覧へ戻る", onClick: f }} actions={[{ id: "save", kind: "primary", label: "保存", onClick: g }]} />;
const b = <PageHeader title="x" actions={[{ id: "discard", kind: "secondary", label: "変更を破棄", onClick: g }]} />;
const c = <PageHeader title="x" back={{ label: "一覧へ戻る", onClick: f }} actions={[{ id: "create", kind: "primary", label: "作成", onClick: g }]} />;
const d = <PageHeader title="一覧" actions={[{ id: "create", kind: "primary", label: "新規作成", onClick: g }]} />;
const e = <FormActionBar ariaLabel="確定" primaryActions={[{ id: "save", label: "保存" }]} secondaryActions={[{ id: "discard", label: "変更を破棄" }]} />;
`);
    expect(linesWith(messages, CONFIRM_IN_HEADER)).toEqual([2, 3, 4]);
  });
});

describe("adherence: 表は DataTable（#129 / #530 / #800）", () => {
  it("手書きの table / thead / tbody / tr / th / td を検出し、DataTable と大文字の部品は許す", async () => {
    const messages = await lint(`
const a = <table className="w-full"><tbody><tr><td>x</td></tr></tbody></table>;
const b = <thead><tr><th>見出し</th></tr></thead>;
const c = <DataTable columns={columns} rows={rows} getRowKey={key} />;
const d = <TableSkeleton rows={5} />;
`);
    expect(linesWith(messages, HANDWRITTEN_TABLE)).toEqual([2, 2, 2, 2, 3, 3, 3]);
  });
});

describe("adherence: PageHeader の actions は配列（#800）", () => {
  it("actions の中の JSX（要素・Fragment・条件の中）を検出し、配列・ほかの部品は許す", async () => {
    const messages = await lint(`
const a = <PageHeader title="x" actions={<Button icon={RefreshCw}>表示を更新</Button>} />;
const b = <PageHeader title="x" actions={<><Button>a</Button><Button>b</Button></>} />;
const c = <PageHeader title="x" actions={busy ? <Spinner /> : [{ id: "save", kind: "primary", label: "保存", icon: Save, onClick: f }]} />;
const d = <PageHeader title="x" actions={[{ id: "save", kind: "primary", label: "保存", icon: Save, onClick: f }]} />;
const e = <PageHeader title="x" status={<StatusBadge variant="success" label="稼働中" />} tabs={<Tabs items={items} />} />;
const g = <Other actions={<Button>x</Button>} />;
`);
    // Fragment は Fragment と中の要素をそれぞれ報告する（3 行目は 3 件）。
    expect(linesWith(messages, JSX_ACTIONS)).toEqual([2, 3, 3, 3, 4]);
  });
});

describe("adherence: 生の色の関数（#800）", () => {
  it("rgba / rgb / hsl / oklch を className・style・テンプレートで検出し、トークンと color-mix は許す", async () => {
    const messages = await lint(`
const a = <span className="shadow-[0_0_0_1px_rgba(255,255,255,0.9)]" />;
const b = <span style={{ color: "rgb(0 0 0)" }} />;
const c = <span style={{ background: \`hsl(\${hue} 50% 50%)\` }} />;
const d = <span style={{ color: "oklch(0.7 0.1 200)" }} />;
const e = <span className="ring-1 ring-surface/90 bg-accent-emphasis/15" />;
const g = <span className="bg-[color-mix(in_srgb,var(--color-fg)_12%,var(--color-surface))]" />;
const h = <span style={{ color: "var(--color-fg-muted)" }} />;
`);
    expect(linesWith(messages, RAW_COLOR_FUNCTION)).toEqual([2, 3, 4, 5]);
  });
});

describe("adherence: inline style の数値（#800）", () => {
  it("文字サイズ・行間・字間の数値と、JSX の style の余白・角丸の数値を検出する", async () => {
    const messages = await lint(`
const a = <div style={{ fontSize: 11 }}>x</div>;
const b = { labelStyle: { fill: "var(--color-fg-muted)", fontSize: 10 } };
const c = <div style={{ lineHeight: 1.4, letterSpacing: 0.5 }}>x</div>;
const d = <div style={{ marginTop: 2, paddingInline: 6, gap: 4, borderRadius: 8 }}>x</div>;
`);
    expect(linesWith(messages, NUMERIC_TYPE)).toEqual([2, 3, 4, 4]);
    expect(linesWith(messages, NUMERIC_SPACING)).toEqual([5, 5, 5, 5]);
  });

  it("トークン・0・寸法と座標・グラフのライブラリの設定・fontWeight は許す", async () => {
    const messages = await lint(`
const a = <div style={{ fontSize: "var(--font-size-xs)", marginTop: 0, padding: "var(--space-2)" }}>x</div>;
const b = <div style={{ width: 120, height: 48, left: x, top: 0, fontWeight: 600, opacity: 0.5 }}>x</div>;
const c = { padding: 0.18, duration: 300 };
const d = { pathOptions: { borderRadius: 8, offset: 12 } };
`);
    expect(messages).toEqual([]);
  });
});

describe("adherence: 複数行の入力欄の高さは rows（#613 / #800）", () => {
  it("textareaClassName の h-* / min-h-* を検出し、rows・max-h-*・幅は許す", async () => {
    const messages = await lint(`
const a = <TextareaField id="a" label="a" textareaClassName="min-h-24" />;
const b = <TextareaField id="b" label="b" textareaClassName="h-44 font-mono" />;
const c = <TextareaField id="c" label="c" textareaClassName={\`md:min-h-40 \${x}\`} />;
const d = <TextareaField id="d" label="d" rows={6} textareaClassName="max-h-[16.625rem] min-w-0 max-w-full" />;
const e = <TextareaField id="e" label="e" rows={9} monospace />;
`);
    expect(linesWith(messages, TEXTAREA_HEIGHT)).toEqual([2, 3, 4]);
  });
});

describe("adherence: 回転するアイコンは共有の Spinner だけ（#395 / #1180）", () => {
  it("animate-spin（変種・任意値・テンプレートを含む）・回転用の lucide のアイコン・inline style の spin を検出する", async () => {
    const messages = await lint(`
import { Loader2, LoaderCircle as Busy, Loader2Icon, LucideLoader, LoaderPinwheel, RefreshCw } from "lucide-react";
const a = <RefreshCw className="h-4 w-4 animate-spin" />;
const b = <RefreshCw className="motion-safe:animate-spin" />;
const c = <span className={\`\${busy ? "animate-spin" : ""} text-fg\`} />;
const d = <span className="animate-[spin_2s_linear_infinite]" />;
const e = <span style={{ animation: "spin 1s linear infinite" }} />;
const f = <span style={{ animationName: "spin" }} />;
`);
    expect(linesWith(messages, LOADER_ICON)).toEqual([2, 2, 2, 2, 2]);
    expect(linesWith(messages, HANDWRITTEN_SPIN)).toEqual([3, 4, 5, 6]);
    expect(linesWith(messages, INLINE_SPIN)).toEqual([7, 8]);
  });

  it("共有の Spinner・loading・回さないアイコン・spin を含む別の語は許す", async () => {
    const messages = await lint(`
import { RefreshCw, RotateCcw, Clock3 } from "lucide-react";
import { Spinner, Button } from "@production-ready/ui";
import { Loader2 } from "./local-loader";
const a = <Spinner size={14} className="text-accent-fg" />;
const b = <Button loading={busy} icon={RefreshCw}>再読み込み</Button>;
const c = <RotateCcw size={16} aria-hidden />;
const d = <span className="animate-pulse spinner-like" />;
const e = <span style={{ animation: "toast-in 200ms ease-out" }} />;
const f = { label: "spin" };
`);
    expect(messages).toEqual([]);
  });
});

describe("adherence: 淡い色の面には data-surface-tint（#1671）", () => {
  it("淡い面（accent の subtle / muted、状態色の subtle、灰色の surface-hover）を className に持ち data-surface-tint の無い要素を検出する", async () => {
    const messages = await lint(`
const a = <div className="rounded-md bg-info-subtle p-3 text-fg-muted">x</div>;
const b = <li className={cn("px-3", selected && "bg-accent-subtle")}>x</li>;
const c = <span className={\`rounded \${tone} bg-warning-subtle/60\`}>x</span>;
const d = <label className="border has-[:checked]:bg-accent-muted">x</label>;
const e = <div className={selected ? "bg-success-subtle" : "bg-surface"}>x</div>;
const f = <div className="md:bg-danger-subtle aria-selected:bg-accent-subtle">x</div>;
const g = <span className="rounded bg-surface-hover px-1.5 text-xs text-fg-muted">x</span>;
`);
    expect(linesWith(messages, SURFACE_TINT)).toEqual([2, 3, 4, 5, 6, 7, 8]);
  });

  it("data-surface-tint（式でも可）・spread・hover だけの面・別の className 属性・面でない class は許す", async () => {
    const messages = await lint(`
const a = <div data-surface-tint="info" className="rounded-md bg-info-subtle p-3 text-fg-muted">x</div>;
const b = <li data-surface-tint={selected ? "accent" : undefined} className={cn(selected && "bg-accent-subtle")}>x</li>;
const c = <div {...rowProps} className="bg-accent-subtle">x</div>;
const d = <div className="hover:bg-accent-subtle group-hover:bg-info-subtle focus-visible:bg-accent-subtle hover:bg-surface-hover">x</div>;
const e = <div className="bg-accent-emphasis text-fg-on-accent border-info-border text-danger-fg">x</div>;
const f = <Disclosure summaryClassName="bg-warning-subtle">x</Disclosure>;
const g = <div className="bg-surface-sunken bg-accent-subtle-like">x</div>;
`);
    expect(linesWith(messages, SURFACE_TINT)).toEqual([]);
  });
});

describe("adherence: 検索して選ぶ選択欄の閉じたボタンに虫眼鏡を付けない（#1771）", () => {
  it("SearchableSelectField の leadingIcon に Search 系のアイコン（別名・条件の式を含む）を渡すと検出する", async () => {
    const messages = await lint(`
const a = <SearchableSelectField id="p" label="L" value={v} options={o} onValueChange={f} leadingIcon={Search} />;
const b = <SearchableSelectField id="p" label="L" value={v} options={o} onValueChange={f} leadingIcon={SearchIcon} />;
const c = <SearchableSelectField id="p" label="L" value={v} options={o} onValueChange={f} leadingIcon={LucideSearch} />;
const d = <SearchableSelectField id="p" label="L" value={v} options={o} onValueChange={f} leadingIcon={TextSearch} />;
const e = <SearchableSelectField id="p" label="L" value={v} options={o} onValueChange={f} leadingIcon={kind ? ScanSearch : Database} />;
`);
    expect(linesWith(messages, SELECT_SEARCH_ICON)).toEqual([2, 3, 4, 5, 6]);
  });

  it("対象の種類のアイコン・leadingIcon なし・ほかの部品の Search（TextField の検索欄）は許す", async () => {
    const messages = await lint(`
const a = <SearchableSelectField id="p" label="L" value={v} options={o} onValueChange={f} leadingIcon={Database} />;
const b = <SearchableSelectField id="p" label="L" value={v} options={o} onValueChange={f} leadingIcon={isSearchMode ? Bot : Database} />;
const c = <SearchableSelectField id="p" label="L" value={v} options={o} onValueChange={f} />;
const d = <TextField id="q" label="RAG 検索" labelHidden size="lg" value={q} onValueChange={setQ} leadingIcon={Search} />;
const e = <Button icon={Search}>検索</Button>;
`);
    expect(linesWith(messages, SELECT_SEARCH_ICON)).toEqual([]);
  });
});
