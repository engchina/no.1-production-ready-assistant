import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { FieldLabel, FieldLegend, Fieldset } from "../src/components/ui/field-label";
import { DEFAULT_REQUIRED_LABEL, RequiredBadge } from "../src/components/ui/required-badge";
import { SecretField } from "../src/components/ui/secret-field";
import { SelectField } from "../src/components/ui/select-field";
import { TextField } from "../src/components/ui/text-field";

// #531: 必須の表示を 3 製品で統一する。required だけで「必須」のタグを出し、支援技術には aria-required で伝える。

function badges(html: string) {
  return html.match(/<span[^>]*>[^<]*必須<\/span>/g) ?? [];
}

describe("必須の表示の既定（#531）", () => {
  it("既定の文言は「必須」", () => {
    expect(DEFAULT_REQUIRED_LABEL).toBe("必須");
    expect(renderToStaticMarkup(<RequiredBadge />)).toContain(">必須</span>");
  });

  it("TextField は required だけで「必須」のタグと aria-required を出す（タグは読み上げない）", () => {
    const html = renderToStaticMarkup(<TextField id="name" label="名前" required value="" readOnly />);
    expect(html).toContain('aria-required="true"');
    expect(badges(html)).toHaveLength(1);
    expect(badges(html)[0]).toContain('aria-hidden="true"');
  });

  it("SelectField・SecretField も required だけで「必須」のタグを出す", () => {
    const select = renderToStaticMarkup(
      <SelectField
        id="region"
        label="リージョン"
        value="a"
        options={[{ value: "a", label: "A" }]}
        onValueChange={() => {}}
        required
      />
    );
    expect(badges(select)).toHaveLength(1);
    expect(select).toContain('aria-required="true"');

    const secret = renderToStaticMarkup(
      <SecretField
        id="key"
        label="API キー"
        value=""
        onValueChange={() => {}}
        hasSavedSecret={false}
        savedLabel="保存済み"
        notSetLabel="未設定"
        showLabel="表示"
        hideLabel="隠す"
        required
      />
    );
    expect(badges(secret)).toHaveLength(1);
  });

  it("requiredLabel で条件付きの必須の文言に上書きできる", () => {
    const html = renderToStaticMarkup(
      <TextField id="region" label="リージョン" required requiredLabel="OCI 運用時必須" value="" readOnly />
    );
    expect(html).toContain(">OCI 運用時必須</span>");
  });

  it("required が無い欄にはタグも aria-required も出さない（任意の欄には何も付けない）", () => {
    const html = renderToStaticMarkup(<TextField id="memo" label="メモ" value="" readOnly />);
    expect(badges(html)).toHaveLength(0);
    expect(html).not.toContain("aria-required");
    const select = renderToStaticMarkup(
      <SelectField id="region" label="リージョン" value="a" options={[{ value: "a", label: "A" }]} onValueChange={() => {}} />
    );
    expect(select).not.toContain("aria-required");
  });
});

describe("FieldLabel / FieldLegend / Fieldset（#531）", () => {
  it("FieldLabel は htmlFor で入力と結び、必須のタグは既定で読み上げない（入力の aria-required が伝える）", () => {
    const html = renderToStaticMarkup(<FieldLabel htmlFor="sql" label="SQL" required />);
    expect(html).toMatch(/^<label for="sql"/);
    const [badge] = badges(html);
    expect(badge).toContain('aria-hidden="true"');
    expect(badge).toContain("text-fg-muted");
    expect(badge).not.toMatch(/warning|danger/);
  });

  it("FieldLabel は aria-required を持てない入力のためにタグを読み上げ対象に残せる", () => {
    const html = renderToStaticMarkup(
      <FieldLabel htmlFor="grid" label="対象" required requiredAnnouncedByControl={false} />
    );
    expect(badges(html)[0]).not.toContain("aria-hidden");
  });

  it("任意の FieldLabel / FieldLegend には何も付けない", () => {
    expect(badges(renderToStaticMarkup(<FieldLabel htmlFor="memo" label="メモ" />))).toHaveLength(0);
    expect(badges(renderToStaticMarkup(<FieldLegend>ロール</FieldLegend>))).toHaveLength(0);
  });

  it("FieldLegend は fieldset が aria-required を持てないので、既定でタグを読み上げる", () => {
    const html = renderToStaticMarkup(<FieldLegend required>ロール</FieldLegend>);
    expect(html).toMatch(/^<legend/);
    expect(badges(html)[0]).not.toContain("aria-hidden");
  });

  it("Fieldset（group）は legend のタグで必須を伝え、aria-required を付けず、補足とエラーを aria-describedby で結ぶ", () => {
    const html = renderToStaticMarkup(
      <Fieldset id="roles" legend="ロール" required helper="1 つ以上選びます。" error="ロールを選択してください。">
        <input type="checkbox" />
      </Fieldset>
    );
    expect(html).not.toContain("aria-required");
    expect(html).toContain('aria-describedby="roles-hint roles-error"');
    expect(html).toContain('<p id="roles-hint"');
    expect(html).toMatch(/<p id="roles-error" role="alert"[^>]*>.*ロールを選択してください。.*<\/p>/);
    expect(badges(html)[0]).not.toContain("aria-hidden");
  });

  it("Fieldset（radiogroup）は aria-required で伝え、タグは読み上げない", () => {
    const html = renderToStaticMarkup(
      <Fieldset id="mode" legend="モード" role="radiogroup" required>
        <input type="radio" name="mode" />
      </Fieldset>
    );
    expect(html).toMatch(/<fieldset id="mode" role="radiogroup" aria-required="true"/);
    expect(badges(html)[0]).toContain('aria-hidden="true"');
  });
});
