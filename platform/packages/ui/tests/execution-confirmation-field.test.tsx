import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  Button,
  DEFAULT_EXECUTION_CONFIRMATION_LABELS,
  ExecutionConfirmationField,
  executionConfirmationStatus,
  type ExecutionConfirmationFieldProps,
} from "../src";

// #379: NL2SQL の ExecutionConfirmationField と system-settings の確認語欄を packages/ui に一本化した。

const PHRASE = "ADMIN_EXECUTE";
const noop = () => {};

function render(props: Partial<ExecutionConfirmationFieldProps> = {}) {
  const value = props.value ?? "";
  return renderToStaticMarkup(
    <ExecutionConfirmationField
      id="confirm"
      value={value}
      onChange={noop}
      confirmed={value.trim() === PHRASE}
      expectedLabel={PHRASE}
      helper="ADMIN_EXECUTE を入力すると実行できます。"
      {...props}
    />
  );
}

/** 開始タグを取り出す（属性の並び順に依存しない検証のため）。 */
function openingTag(html: string, pattern: RegExp): string {
  const tag = (html.match(/<[a-z]+\b[^>]*>/g) ?? []).find((candidate) => pattern.test(candidate));
  if (!tag) throw new Error(`開始タグが見つかりません: ${pattern}`);
  return tag;
}

const input = (html: string) => openingTag(html, /^<input\b/);
const helper = (html: string) => openingTag(html, /^<p\b[^>]*id="confirm-helper"/);
const statusBadge = (html: string) => openingTag(html, /^<span\b[^>]*aria-live="polite"/);

describe("executionConfirmationStatus", () => {
  it("一致は確認済み、空白だけは未入力、それ以外は不一致", () => {
    expect(executionConfirmationStatus("", false)).toBe("pending");
    expect(executionConfirmationStatus("   ", false)).toBe("pending");
    expect(executionConfirmationStatus("wrong", false)).toBe("mismatch");
    expect(executionConfirmationStatus("ADMIN_EXECUTE", true)).toBe("confirmed");
  });
});

describe("ExecutionConfirmationField", () => {
  it("未入力: 中立の面・通常色のラベル・補助色の説明で、danger 色を使わない", () => {
    const html = render();
    const root = openingTag(html, /^<div\b[^>]*data-testid="execution-confirmation-field"/);
    expect(root).toContain("border border-border bg-surface-sunken p-3");
    expect(root).toContain('data-confirmation-status="pending"');
    expect(root).not.toMatch(/danger/);
    expect(html).toMatch(/<label for="confirm" class="text-sm font-semibold text-fg">実行確認語<span aria-hidden="true"[^>]*>必須<\/span><\/label>/);
    expect(helper(html)).toContain("text-fg-muted");
    expect(statusBadge(html)).toContain("border-border bg-surface text-fg-muted");
    expect(html).toContain(">未入力</span>");
    expect(input(html)).not.toContain("aria-invalid");
  });

  it("入力欄は必須・説明付きで、自動補完や自動修正をしない", () => {
    const tag = input(render());
    expect(tag).toContain('id="confirm"');
    expect(tag).toContain('aria-required="true"');
    expect(tag).toContain("required");
    expect(tag).toContain('aria-describedby="confirm-helper"');
    expect(tag).toContain('placeholder="ADMIN_EXECUTE"');
    expect(tag).toContain('autoComplete="off"');
    expect(tag).toContain('autoCapitalize="off"');
    expect(tag).toContain('spellCheck="false"');
  });

  it("フォーカス: lg（--control-height-lg）の入力欄で、フォーカス中は枠線を danger 色にし、ring を重ねない", () => {
    const tag = input(render());
    expect(tag).toContain("h-[var(--control-height-lg)]");
    expect(tag).toContain("border border-border-control bg-surface");
    expect(tag).toContain("focus:border-danger-fg");
    expect(tag).not.toMatch(/focus(-visible)?:ring-/);
  });

  it("不一致: aria-invalid を付け、説明文とバッジだけを danger 色にする", () => {
    const html = render({ value: "wrong" });
    expect(input(html)).toContain('aria-invalid="true"');
    expect(helper(html)).toContain("text-danger-fg");
    expect(statusBadge(html)).toContain("border-danger-border bg-danger-subtle text-danger-fg");
    expect(html).toContain(">不一致</span>");
    expect(openingTag(html, /data-testid="execution-confirmation-field"/)).toContain('data-confirmation-status="mismatch"');
  });

  it("一致: 確認済みのバッジ（success）になり、aria-invalid を外す", () => {
    const html = render({ value: " ADMIN_EXECUTE " });
    expect(input(html)).not.toContain("aria-invalid");
    expect(helper(html)).toContain("text-fg-muted");
    expect(statusBadge(html)).toContain("border-success-border bg-success-subtle text-success-fg");
    expect(html).toContain(">確認済み</span>");
  });

  it("クリア: 空に戻すと未入力の表示に戻る", () => {
    expect(render({ value: "wrong" })).toContain(">不一致</span>");
    const cleared = render({ value: "" });
    expect(cleared).toContain(">未入力</span>");
    expect(input(cleared)).not.toContain("aria-invalid");
  });

  it("入力条件の確認語を等幅で出し、識別子の区切りの後で折り返せる", () => {
    const html = render({ expectedLabel: "ADMIN.DENPYO_ACTIVITY_LOG", confirmed: false });
    expect(html).toContain(
      '入力条件: <span class="font-mono font-semibold [overflow-wrap:anywhere]">ADMIN.<wbr/>DENPYO_<wbr/>ACTIVITY_<wbr/>LOG</span>'
    );
  });

  it("文言は props で差し替えられ、未指定の項目は既定の文言を使う", () => {
    const html = render({
      labels: { label: "確認入力", expected: "「{phrase}」と入力", mismatch: "違います" },
      value: "x",
    });
    expect(html).toMatch(/<label[^>]*>確認入力<span[^>]*>必須<\/span><\/label>/);
    expect(html).toContain('「<span class="font-mono font-semibold [overflow-wrap:anywhere]">ADMIN_<wbr/>EXECUTE</span>」と入力');
    expect(html).toContain(">違います</span>");
    expect(DEFAULT_EXECUTION_CONFIRMATION_LABELS.confirmed).toBe("確認済み");
  });

  it("操作は区切り線の下に置き、無いときは区切りを出さない。disabled は入力欄に渡す", () => {
    const withActions = render({
      disabled: true,
      actions: (
        <Button variant="danger" size="lg">
          実行
        </Button>
      ),
    });
    expect(withActions).toMatch(/<div class="flex min-w-0 flex-col gap-\[8px\] border-t border-border pt-3[^"]*"><button/);
    expect(input(withActions)).toContain("disabled");
    expect(render()).not.toContain("border-t border-border pt-3");
  });
});
