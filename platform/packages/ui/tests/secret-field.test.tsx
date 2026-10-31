import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { SecretField, type SecretFieldProps } from "../src";

const noop = () => {};

function render(props: Partial<SecretFieldProps> = {}) {
  return renderToStaticMarkup(
    <SecretField
      id="api-key"
      label="API key"
      value=""
      onValueChange={noop}
      hasSavedSecret={false}
      savedLabel="保存済み"
      notSetLabel="未設定"
      showLabel="API key を表示"
      hideLabel="API key を隠す"
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

const secretInput = (html: string) => openingTag(html, /^<input\b[^>]*id="api-key"/);
const toggleButton = (html: string) => openingTag(html, /^<button\b/);

describe("SecretField", () => {
  it("既定はマスク入力で、未設定のバッジ（アイコン付き）と表示の切り替えを入力欄の直後に出す", () => {
    const html = render({ helper: "Bearer 認証で使います。" });
    expect(html).toMatch(/<label for="api-key"[^>]*>API key<\/label>/);
    expect(html).toMatch(/data-status-variant="neutral"[^>]*><span class="pr-icon-line"><svg[^>]*>.*?<\/svg><\/span>未設定<\/span>/);
    const input = secretInput(html);
    expect(input).toContain('type="password"');
    expect(input).toContain('aria-describedby="api-key-hint"');
    expect(input).toContain('autoComplete="off"');
    // 入力欄の直後に切り替えボタン（`#api-key + button` で引ける）。
    expect(html).toMatch(/<input\b[^>]*id="api-key"[^>]*\/><button\b/);
    expect(toggleButton(html)).toContain('type="button"');
    expect(toggleButton(html)).toContain('aria-label="API key を表示"');
    expect(html).toContain("lucide-eye");
    expect(html).toContain('<p id="api-key-hint" class="text-xs leading-relaxed text-fg-muted">Bearer 認証で使います。</p>');
    // 保存済みの値がないときは削除の指定を出さない。
    expect(html).not.toContain('type="checkbox"');
  });

  it("保存済みなら success のバッジと削除の指定を出し、指定中は入力欄と切り替えを無効にする", () => {
    const clearOption = { label: "保存済み API key を削除", checked: false, onCheckedChange: noop };
    const unchecked = render({ hasSavedSecret: true, clearOption });
    expect(unchecked).toMatch(/data-status-variant="success"[^>]*>.*?保存済み<\/span>/);
    expect(unchecked).toContain('<label for="api-key-clear"');
    expect(openingTag(unchecked, /id="api-key-clear"/)).toContain('type="checkbox"');
    expect(unchecked).toContain("保存済み API key を削除");
    expect(secretInput(unchecked)).not.toContain('disabled=""');

    const checked = render({ hasSavedSecret: true, clearOption: { ...clearOption, checked: true } });
    expect(secretInput(checked)).toContain('disabled=""');
    expect(toggleButton(checked)).toContain('disabled=""');
    expect(openingTag(checked, /id="api-key-clear"/)).toContain('checked=""');
    // 保存済みの値がなければ、削除の指定を渡されても出さない。
    expect(render({ clearOption })).not.toContain('type="checkbox"');
  });

  it("visible で平文表示にし、切り替えボタンは隠す側のラベルになる", () => {
    const html = render({ visible: true, value: "secret" });
    expect(secretInput(html)).toContain('type="text"');
    expect(secretInput(html)).toContain('value="secret"');
    expect(toggleButton(html)).toContain('aria-label="API key を隠す"');
    expect(html).toContain("lucide-eye-off");
  });

  it("保存済みの値の取り出し中は切り替えがスピナーになり、取得中のラベルで押せなくする", () => {
    const html = render({ hasSavedSecret: true, revealPending: true, revealPendingLabel: "DB パスワードを取得中" });
    const button = toggleButton(html);
    // 取り出し中もフォーカスを保つため、ネイティブの disabled ではなく aria-disabled（#355）
    expect(button).toContain('aria-disabled="true"');
    expect(button).not.toMatch(/\sdisabled=""/);
    expect(button).toContain('aria-busy="true"');
    expect(button).toContain('aria-label="DB パスワードを取得中"');
    expect(html).toContain("animate-spin");
    expect(html).not.toContain("lucide-eye");
  });

  it("必須・エラー・取り出しの失敗を aria で入力欄と結ぶ", () => {
    const html = render({
      required: true,
      requiredLabel: "必須",
      helper: "補足",
      error: "パスワードを入力してください。",
      revealError: "保存済みパスワードを取得できませんでした。",
    });
    expect(html).toMatch(/<label for="api-key"[^>]*>API key<span aria-hidden="true"[^>]*>必須<\/span><\/label>/);
    const input = secretInput(html);
    expect(input).toContain('required=""');
    expect(input).toContain('aria-required="true"');
    expect(input).toContain('aria-invalid="true"');
    expect(input).toContain('aria-describedby="api-key-hint api-key-error api-key-reveal-error"');
    expect(input).toContain("border-danger-fg");
    expect(html).toMatch(/<p id="api-key-error" role="alert"[^>]*>/);
    expect(html).toContain('<div id="api-key-reveal-error">');
  });
});
