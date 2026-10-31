import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { defaultTextareaCount, TextareaField, type TextareaFieldProps, TextField } from "../src";

// #584: 複数行の入力欄（TextareaField）。見た目と API は TextField とそろえる。

const noop = () => {};

function render(props: Partial<TextareaFieldProps> = {}) {
  return renderToStaticMarkup(<TextareaField id="prompt" label="システムプロンプト" value="" onChange={noop} {...props} />);
}

function openingTag(html: string, pattern: RegExp): string {
  const tag = (html.match(/<[a-z]+\b[^>]*>/g) ?? []).find((candidate) => pattern.test(candidate));
  if (!tag) throw new Error(`開始タグが見つかりません: ${pattern}`);
  return tag;
}

const classesOf = (tag: string) => tag.match(/class="([^"]*)"/)?.[1].replaceAll("&amp;", "&").split(" ") ?? [];
const textarea = (html: string) => openingTag(html, /^<textarea\b/);
const describedBy = (tag: string) => tag.match(/aria-describedby="([^"]*)"/)?.[1].split(" ") ?? [];

describe("TextareaField", () => {
  it("ラベルと textarea を for / id で結び、TextField と同じ枠線・角丸・地・フォーカスの見た目にする", () => {
    const html = render();
    expect(html).toContain('for="prompt"');
    const tag = textarea(html);
    expect(tag).toContain('id="prompt"');
    expect(tag).toContain('rows="3"');
    const inputClasses = classesOf(openingTag(renderToStaticMarkup(<TextField id="n" label="名前" />), /^<input\b/));
    const classes = classesOf(tag);
    for (const shared of ["rounded-control", "bg-surface", "border-border-control", "focus-visible:border-focus-ring", "read-only:bg-surface-sunken"]) {
      expect(inputClasses).toContain(shared);
      expect(classes).toContain(shared);
    }
    expect(classes).toContain("resize-y");
    expect(classes).not.toContain("font-mono");
    // フォーカスはグローバルの :focus-visible（outline）。ring も outline-none も書かない（#355）。
    expect(tag).not.toMatch(/focus(-visible)?:(ring|outline-none)/);
  });

  it("required は aria-required と「必須」のタグ（ネイティブの required 検証はしない）", () => {
    const html = render({ required: true });
    expect(textarea(html)).toContain('aria-required="true"');
    expect(textarea(html)).not.toMatch(/\srequired=/);
    expect(html).toContain("必須");
    expect(render()).not.toContain("必須");
  });

  it("requiredAnnouncedByControl=false は aria-required を付けず、条件付きのタグをラベルの一部として読ませる", () => {
    const html = render({ required: true, requiredLabel: "「違う」のとき必須", requiredAnnouncedByControl: false });
    expect(textarea(html)).not.toContain("aria-required");
    expect(html).toContain("「違う」のとき必須");
    expect(openingTag(html, /^<label\b/)).toBeTruthy();
    expect(html).not.toMatch(/aria-hidden="true"[^>]*>[^<]*「違う」のとき必須/);
  });

  it("error は aria-invalid・枠線の色・欄の直下の FieldError（role=alert）を aria-describedby で結ぶ", () => {
    const html = render({ error: "入力してください。" });
    const tag = textarea(html);
    expect(tag).toContain('aria-invalid="true"');
    expect(classesOf(tag)).toContain("border-danger-fg");
    expect(classesOf(tag)).not.toContain("border-border-control");
    const errorTag = openingTag(html, /role="alert"/);
    const errorId = errorTag.match(/id="([^"]*)"/)?.[1];
    expect(describedBy(tag)).toContain(errorId);
  });

  it("helper・文字数・呼び出し側の aria-describedby をまとめて結ぶ", () => {
    const html = render({ helper: "回答の方針を書きます。", showCount: true, maxLength: 1000, value: "あいう", "aria-describedby": "note" });
    const ids = describedBy(textarea(html));
    expect(ids).toHaveLength(3);
    expect(ids).toContain("note");
    expect(html).toContain("回答の方針を書きます。");
    expect(html).toContain("3 / 1,000");
    expect(openingTag(html, /tabular-nums/)).toContain("text-fg-muted");
  });

  it("文字数は関数で翻訳済みの文言にでき、上限が無ければ「n 文字」", () => {
    expect(defaultTextareaCount(12)).toBe("12 文字");
    expect(defaultTextareaCount(1200, 20000)).toBe("1,200 / 20,000");
    const html = render({ value: "abcd", showCount: (count) => `${count} 字` });
    expect(html).toContain("4 字");
  });

  it("monospace は等幅の 12px、resize=none は高さを変えない。labelHidden はラベルを読み上げだけにする", () => {
    const html = render({ monospace: true, resize: "none", labelHidden: true, readOnly: true });
    const classes = classesOf(textarea(html));
    expect(classes).toContain("font-mono");
    expect(classes).toContain("text-xs");
    expect(classes).toContain("resize-none");
    expect(openingTag(html, /^<label\b/)).toContain("sr-only");
    expect(textarea(html)).toContain("readOnly");
  });

  it("surface=code は暗いコードの面（data-surface）で等幅、read-only でもコードの地のまま", () => {
    const tag = textarea(render({ surface: "code", readOnly: true }));
    expect(tag).toContain('data-surface="code"');
    const classes = classesOf(tag);
    expect(classes).toContain("font-mono");
    expect(classes).toContain("read-only:bg-surface");
    expect(classes).not.toContain("read-only:bg-surface-sunken");
  });
});
