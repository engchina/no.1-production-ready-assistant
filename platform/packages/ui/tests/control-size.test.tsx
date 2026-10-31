import { SendHorizontal } from "lucide-react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  Button,
  CONTROL_HEIGHT_CLASS,
  CONTROL_MIN_HEIGHT_CLASS,
  ClearActionButton,
  FIELD_BOX_TEXT_MIN_WIDTH_CLASS,
  FIELD_BOX_TEXT_WIDTH_CLASS,
  FIELD_BOX_WIDTH_CLASS,
  FIELD_CONTROL_WIDTH_CLASS,
  FIELD_TEXT_CONTAIN_CLASS,
  FIELD_WIDTH_CLASS,
  FieldActionRow,
  fieldControlClassName,
  fieldWidthClass,
  labeledFieldWidthClasses,
  SearchableSelectField,
  SearchField,
  SecretField,
  SelectField,
  TextareaField,
  TextField,
} from "../src";

// #613: 操作部品（入力欄・選択欄・ボタン）の高さと、入力欄・選択欄の幅の段。

const noop = () => {};

/** 開始タグを取り出す（属性の並び順に依存しない検証のため）。 */
function openingTag(html: string, pattern: RegExp): string {
  const tag = (html.match(/<[a-z]+\b[^>]*>/g) ?? []).find((candidate) => pattern.test(candidate));
  if (!tag) throw new Error(`開始タグが見つかりません: ${pattern}`);
  return tag;
}
const classesOf = (tag: string) =>
  tag.match(/class="([^"]*)"/)?.[1].replaceAll("&gt;", ">").replaceAll("&amp;", "&").split(" ") ?? [];
/** 最初の要素（欄の外枠）のクラス。 */
const rootClasses = (html: string) => classesOf(openingTag(html, /^<[a-z]+/));

const textField = (props: Partial<Parameters<typeof TextField>[0]> = {}) =>
  renderToStaticMarkup(<TextField id="q" label="名前" value="" onChange={noop} {...props} />);
const selectField = (props: Partial<Parameters<typeof SelectField>[0]> = {}) =>
  renderToStaticMarkup(
    <SelectField id="s" label="状態" value="a" options={[{ value: "a", label: "有効" }]} onValueChange={noop} {...props} />
  );
const searchableSelect = (props: Partial<Parameters<typeof SearchableSelectField>[0]> = {}) =>
  renderToStaticMarkup(
    <SearchableSelectField id="ss" label="ナレッジベース" value="" options={[]} onValueChange={noop} {...props} />
  );
const secretField = (props: Partial<Parameters<typeof SecretField>[0]> = {}) =>
  renderToStaticMarkup(
    <SecretField
      id="secret"
      label="API key"
      value=""
      onValueChange={noop}
      hasSavedSecret={false}
      savedLabel="保存済み"
      notSetLabel="未設定"
      showLabel="表示"
      hideLabel="隠す"
      {...props}
    />
  );

describe("高さの段（size）: 入力欄・選択欄・ボタンで同じトークン", () => {
  it("段は --control-height-sm / md / lg（タッチ端末では 3 段とも 44px になる）", () => {
    expect(CONTROL_HEIGHT_CLASS).toEqual({
      sm: "h-[var(--control-height-sm)]",
      md: "h-[var(--control-height-md)]",
      lg: "h-[var(--control-height-lg)]",
    });
    expect(CONTROL_MIN_HEIGHT_CLASS.lg).toBe("min-h-[var(--control-height-lg)]");
  });

  it.each(["sm", "md", "lg"] as const)("TextField / SelectField / SearchableSelectField / SecretField の %s", (size) => {
    expect(classesOf(openingTag(textField({ size }), /^<input\b/))).toContain(CONTROL_MIN_HEIGHT_CLASS[size]);
    expect(classesOf(openingTag(selectField({ size }), /role="combobox"/))).toContain(CONTROL_HEIGHT_CLASS[size]);
    expect(classesOf(openingTag(searchableSelect({ size }), /aria-haspopup="dialog"/))).toContain(
      CONTROL_MIN_HEIGHT_CLASS[size]
    );
    expect(classesOf(openingTag(secretField({ size }), /^<input\b/))).toContain(CONTROL_MIN_HEIGHT_CLASS[size]);
  });

  it("既定は md（36px）。md 以外の高さのクラスと重ならない", () => {
    const input = classesOf(openingTag(textField(), /^<input\b/));
    expect(input).toContain("min-h-[var(--control-height-md)]");
    expect(input.filter((name) => name.startsWith("min-h-"))).toHaveLength(1);
    const trigger = classesOf(openingTag(selectField(), /role="combobox"/));
    expect(trigger).toContain("h-[var(--control-height-md)]");
  });

  it("SearchField も TextField と同じ size を受け取る（一覧のツールバーの欄を隣のボタンとそろえる）", () => {
    const html = renderToStaticMarkup(
      <SearchField id="f" label="検索" labelHidden value="" onSearch={noop} clearLabel="クリア" size="sm" />
    );
    expect(classesOf(openingTag(html, /^<input\b/))).toContain("min-h-[var(--control-height-sm)]");
  });

  it("Button の段は同じ値の別名（--button-height-*）を使う", () => {
    for (const size of ["sm", "md", "lg"] as const) {
      const html = renderToStaticMarkup(<Button size={size}>保存</Button>);
      expect(classesOf(openingTag(html, /^<button\b/))).toContain(`h-[var(--button-height-${size})]`);
    }
  });

  it("ClearActionButton は既定 sm で、マウス環境で 44px にしない（並べる入力欄とは size でそろえる）", () => {
    const html = renderToStaticMarkup(<ClearActionButton label="クリア" onClick={noop} />);
    const classes = classesOf(openingTag(html, /^<button\b/));
    expect(classes).toContain("h-[var(--button-height-sm)]");
    expect(classes).not.toContain("h-[var(--control-height-touch)]");
    const lg = classesOf(openingTag(renderToStaticMarkup(<ClearActionButton label="クリア" size="lg" />), /^<button\b/));
    expect(lg).toContain("h-[var(--button-height-lg)]");
  });
});

describe("幅の段（width）", () => {
  it("xs / sm / md / lg は sm（640px）未満で全幅、sm 以上で段の幅（親より広くしない）", () => {
    expect(FIELD_WIDTH_CLASS.xs).toBe("w-full sm:w-[var(--field-width-xs)] sm:max-w-full");
    expect(FIELD_WIDTH_CLASS.lg).toBe("w-full sm:w-[var(--field-width-lg)] sm:max-w-full");
    expect(FIELD_WIDTH_CLASS.full).toBe("w-full");
    expect(fieldWidthClass(undefined)).toBeUndefined();
  });

  it("段の幅は入力欄・選択欄のボタンに付き、外枠はラベルの 1 行の幅まで広がる（#1746）", () => {
    expect(FIELD_BOX_WIDTH_CLASS.xs).toBe("w-full sm:w-fit sm:max-w-full");
    expect(FIELD_CONTROL_WIDTH_CLASS.xs).toBe("sm:w-[var(--field-width-xs)] sm:max-w-full");
    expect(FIELD_CONTROL_WIDTH_CLASS.full).toBeUndefined();

    // TextField: 補足・エラーの無い外枠は w-fit（ラベルを入力欄の幅で折り返さない）、入力欄の枠（relative）が段の幅。
    const plain = textField({ width: "xs", required: true });
    expect(rootClasses(plain)).toEqual(expect.arrayContaining(["w-full", "sm:w-fit", "sm:max-w-full"]));
    expect(rootClasses(plain).some((name) => name.startsWith("sm:w-[var(--field-width-"))).toBe(false);
    expect(classesOf(openingTag(plain, /class="relative /))).toContain("sm:w-[var(--field-width-xs)]");
    // 補足・エラーのある外枠は置いた枠の幅（auto）。入力欄の枠は段の幅のまま（#2063）。
    const text = textField({ width: "xs", required: true, helper: "1〜100000 の整数。", error: "範囲外です。" });
    expect(rootClasses(text)).toEqual(expect.arrayContaining(["w-full", "sm:w-auto", "sm:max-w-full"]));
    expect(rootClasses(text)).not.toContain("sm:w-fit");
    expect(classesOf(openingTag(text, /class="relative /))).toContain("sm:w-[var(--field-width-xs)]");
    // 補足・エラーは外枠を広げない（外枠の幅で折り返し、1 行はフォームの最大幅まで）。
    const textClasses = FIELD_TEXT_CONTAIN_CLASS.split(" ");
    expect(textClasses).toEqual(["sm:[contain:inline-size]", "sm:max-w-[var(--form-max-width)]"]);
    expect(classesOf(openingTag(text, /^<p\b[^>]*-hint"/))).toEqual(expect.arrayContaining(textClasses));
    expect(classesOf(openingTag(text, /role="alert"/))).toEqual(expect.arrayContaining(textClasses));

    const select = selectField({ width: "sm" });
    expect(rootClasses(select)).toContain("sm:w-fit");
    expect(classesOf(openingTag(select, /class="relative /))).toContain("sm:w-[var(--field-width-sm)]");

    const searchable = searchableSelect({ width: "lg" });
    expect(rootClasses(searchable)).toContain("sm:w-fit");
    expect(classesOf(openingTag(searchable, /aria-haspopup="dialog"/))).toContain("sm:w-[var(--field-width-lg)]");

    const secret = secretField({ width: "lg" });
    expect(rootClasses(secret)).toContain("sm:w-fit");
    expect(classesOf(openingTag(secret, /class="relative /))).toContain("sm:w-[var(--field-width-lg)]");
  });

  it("補足・エラーのある段の欄は、外枠が置いた枠の幅（auto）で、横に並べる行でも min(20rem, 親の幅) 以上（#1891 段 5・#2063）", () => {
    expect(FIELD_BOX_TEXT_WIDTH_CLASS).toBe("w-full sm:w-auto sm:max-w-full");
    expect(FIELD_BOX_TEXT_MIN_WIDTH_CLASS).toBe("sm:min-w-[min(var(--field-width-md),100%)]");
    // 補足・エラーの無い欄は外枠を広げない（ツールバーの xs / sm の欄の並びを変えない）。
    expect(labeledFieldWidthClasses("xs").box).not.toContain(FIELD_BOX_TEXT_MIN_WIDTH_CLASS);
    expect(labeledFieldWidthClasses("xs").box).toBe(FIELD_BOX_WIDTH_CLASS.xs);
    expect(labeledFieldWidthClasses("xs", "viewport", true).box).toContain(FIELD_BOX_TEXT_MIN_WIDTH_CLASS);
    expect(labeledFieldWidthClasses("xs", "viewport", true).box).toBe(
      `${FIELD_BOX_TEXT_WIDTH_CLASS} ${FIELD_BOX_TEXT_MIN_WIDTH_CLASS}`
    );
    expect(labeledFieldWidthClasses("sm", "container", true).box).toBe(
      "w-full @md:w-auto @md:max-w-full @md:min-w-[min(var(--field-width-md),100%)]"
    );
    expect(labeledFieldWidthClasses("full", "viewport", true).box).toBe("w-full");
    expect(labeledFieldWidthClasses(undefined, "viewport", true).box).toBeUndefined();

    // 入力欄の幅は段のまま（外枠だけが文の幅まで広がる）。
    const helper = textField({ width: "xs", helper: "前後の子チャンクを何件まで加えるか。0 で加えない。" });
    expect(rootClasses(helper)).toContain(FIELD_BOX_TEXT_MIN_WIDTH_CLASS);
    expect(classesOf(openingTag(helper, /class="relative /))).toContain("sm:w-[var(--field-width-xs)]");
    expect(rootClasses(textField({ width: "xs" }))).not.toContain(FIELD_BOX_TEXT_MIN_WIDTH_CLASS);
    expect(rootClasses(textField({ width: "sm", error: "1 以上の整数を入力してください。" }))).toContain(
      FIELD_BOX_TEXT_MIN_WIDTH_CLASS
    );

    expect(rootClasses(selectField({ width: "sm", helper: "補足" }))).toContain(FIELD_BOX_TEXT_MIN_WIDTH_CLASS);
    expect(rootClasses(selectField({ width: "sm" }))).not.toContain(FIELD_BOX_TEXT_MIN_WIDTH_CLASS);
    expect(rootClasses(searchableSelect({ width: "xs", error: "選んでください。" }))).toContain(
      FIELD_BOX_TEXT_MIN_WIDTH_CLASS
    );
    expect(rootClasses(secretField({ width: "sm", revealError: "取り出せませんでした。" }))).toContain(
      FIELD_BOX_TEXT_MIN_WIDTH_CLASS
    );
    expect(
      rootClasses(renderToStaticMarkup(<TextareaField id="t" label="メモ" value="" onChange={noop} width="sm" maxLength={20} showCount />))
    ).toContain(FIELD_BOX_TEXT_MIN_WIDTH_CLASS);

    // 補足・エラーは aria-describedby で欄に結んだまま。
    const described = textField({ width: "xs", helper: "補足", error: "誤り" });
    const describedBy = openingTag(described, /^<input\b/).match(/aria-describedby="([^"]*)"/)?.[1].split(" ") ?? [];
    expect(describedBy).toHaveLength(2);
    expect(describedBy[0]).toMatch(/-hint$/);
    expect(describedBy[1]).toMatch(/-error$/);
  });

  it("full は外枠も入力欄も親の幅いっぱい（補足の文の扱いも変えない）", () => {
    const html = textField({ width: "full", helper: "補足" });
    expect(rootClasses(html)).toContain("w-full");
    expect(rootClasses(html)).not.toContain("sm:w-fit");
    expect(classesOf(openingTag(html, /class="relative/))).toEqual(["relative"]);
    expect(html).not.toContain("[contain:inline-size]");
  });

  it("指定しなければ幅のクラスを付けない（フォームの grid のセルいっぱい）", () => {
    expect(rootClasses(selectField()).some((name) => name.includes("--field-width-"))).toBe(false);
  });
});

describe("fieldControlClassName（ネイティブの select / input）", () => {
  it("TextField と同じ見た目・高さ・エラーの枠線を付ける", () => {
    const classes = fieldControlClassName().split(" ");
    expect(classes).toContain("rounded-control");
    expect(classes).toContain("border-border-control");
    expect(classes).toContain("aria-[invalid=true]:border-danger-fg");
    expect(classes).toContain("min-h-[var(--control-height-md)]");
  });

  it("size と width を受け取る", () => {
    const classes = fieldControlClassName({ size: "lg", width: "xs" }).split(" ");
    expect(classes).toContain("min-h-[var(--control-height-lg)]");
    expect(classes).not.toContain("min-h-[var(--control-height-md)]");
    expect(classes).toContain("sm:w-[var(--field-width-xs)]");
  });
});

describe("FieldActionRow（入力欄と操作の行）", () => {
  it("操作を入力欄の下端にそろえ、sm 未満は縦に積んで操作を全幅にする", () => {
    const html = renderToStaticMarkup(
      <FieldActionRow actions={<Button icon={SendHorizontal}>送信</Button>} footer={<p>補足</p>}>
        <TextareaField id="composer" label="質問" labelHidden value="" onChange={noop} rows={2} />
      </FieldActionRow>
    );
    const row = classesOf(openingTag(html, /data-field-action-row/));
    expect(row).toEqual(expect.arrayContaining(["flex-col", "sm:flex-row", "sm:items-end"]));
    const actions = classesOf(openingTag(html, /\[&amp;&gt;\*\]:w-full sm:/));
    expect(actions).toEqual(expect.arrayContaining(["[&>*]:w-full", "sm:[&>*]:w-auto", "shrink-0"]));
    // 補足は行の外（下）に置く。
    expect(html.indexOf("<p>補足</p>")).toBeGreaterThan(html.indexOf("送信"));
  });

  it("操作が無い（null）ときは操作の列を描かず、入力欄が行の幅いっぱいになる（#631）", () => {
    const html = renderToStaticMarkup(
      <FieldActionRow actions={null}>
        <TextField id="password" label="一時パスワード" value="" onChange={noop} />
      </FieldActionRow>
    );
    expect(html).not.toContain("shrink-0");
    expect(html).toContain('id="password"');
  });
});
