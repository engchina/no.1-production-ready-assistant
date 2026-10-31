import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  MINERU_LICENSE_URL,
  MINERU_REPOSITORY_URL,
  MineruLicenseNotice,
  mineruLicenseAcknowledgedText,
} from "./MineruLicenseNotice";

const UNACKNOWLEDGED = { acknowledged: false, acknowledged_by: null, acknowledged_at: null };
const ACKNOWLEDGED = {
  acknowledged: true,
  acknowledged_by: "管理者 A",
  acknowledged_at: "2026-10-26T00:00:00+00:00",
};

function render(props: Parameters<typeof MineruLicenseNotice>[0]) {
  return renderToStaticMarkup(createElement(MineruLicenseNotice, props));
}

describe("MinerU のライセンスの案内", () => {
  it("主な条件・全文の確認・法的な助言ではないこと・公式のリンクを出す", () => {
    const html = render({ acknowledgement: UNACKNOWLEDGED });

    expect(html).toContain("MinerU Open Source License");
    expect(html).toContain("月間アクティブユーザーが 1 億を超える");
    expect(html).toContain("2,000 万米ドル");
    expect(html).toContain("MinerU を使っていることを明示");
    expect(html).toContain("自動で終わります");
    expect(html).toContain("MinerU2.5-2509-1.2B は AGPL-3.0");
    expect(html).toContain("MinerU2.5-Pro-2605-1.2B（Apache-2.0）");
    expect(html).toContain("利用の前に、ライセンスの全文を確認してください");
    expect(html).toContain("法的な助言ではありません");
    expect(html).toContain(`href="${MINERU_REPOSITORY_URL}"`);
    expect(html).toContain(`href="${MINERU_LICENSE_URL}"`);
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it("文書解析の画面では確認の checkbox を出す", () => {
    const html = render({
      acknowledgement: UNACKNOWLEDGED,
      acknowledge: {
        id: "ack",
        checked: false,
        disabled: false,
        onChange: () => undefined,
      },
    });

    expect(html).toContain('type="checkbox"');
    expect(html).toContain("MinerU のライセンスの条件を確認した");
    expect(html).not.toContain("checked=\"\"");
  });

  it("checkbox の無いレシピでは、未確認なら管理者の確認が要ることを出す", () => {
    const html = render({ acknowledgement: UNACKNOWLEDGED });

    expect(html).not.toContain('type="checkbox"');
    expect(html).toContain("システム管理者が「取込・検索・回答設定 › 文書解析」で");
  });

  it("確認済みなら確認した人を出す", () => {
    expect(mineruLicenseAcknowledgedText(ACKNOWLEDGED)).toContain("確認済み: 管理者 A");
    expect(mineruLicenseAcknowledgedText(UNACKNOWLEDGED)).toBeNull();
    expect(
      mineruLicenseAcknowledgedText({ ...ACKNOWLEDGED, acknowledged_by: null })
    ).toMatch(/^確認済み（/);
  });
});

describe("MinerU が準備中（ロードマップ）の間の短い案内", () => {
  it("1 文と公式のリンクだけを出し、確認の checkbox は出さない", () => {
    const html = render({ compact: true });

    expect(html).toContain("MinerU Open Source License");
    expect(html).toContain("利用の前に全文を確認してください");
    expect(html).toContain(`href="${MINERU_REPOSITORY_URL}"`);
    expect(html).toContain(`href="${MINERU_LICENSE_URL}"`);
    expect(html).not.toContain('type="checkbox"');
  });
});
