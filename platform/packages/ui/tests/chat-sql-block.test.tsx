// @vitest-environment happy-dom
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ChatSqlBlock, SqlSafetyBadge, sqlBlockLabels } from "../src";

// #2346: 回答の中の SQL（NL2SQL のチャット・DB 管理チャット・Agent のチャットで共通）。

function html(node: React.ReactNode): HTMLElement {
  const host = document.createElement("div");
  host.innerHTML = renderToStaticMarkup(node);
  return host;
}

describe("ChatSqlBlock", () => {
  it("状態のバッジ・コピー・操作と SQL の本文を出す", () => {
    const host = html(
      <ChatSqlBlock
        sql="select 1 from dual"
        badges={<SqlSafetyBadge safe executed={false} />}
        actions={<button type="button">実行</button>}
        testId="sql"
      />,
    );
    expect(host.querySelector('[data-testid="sql"]')?.textContent).toBe("select 1 from dual");
    expect(host.querySelector("pre code")?.textContent).toBe("select 1 from dual");
    expect(host.textContent).toContain("安全検査済み・未実行");
    expect(Array.from(host.querySelectorAll("button")).map((button) => button.textContent)).toEqual([
      "SQL をコピー",
      "実行",
    ]);
  });

  it("安全検査と実行の状態を文言と色で分け、文言は上書きできる", () => {
    expect(html(<SqlSafetyBadge safe executed />).textContent).toBe("安全検査済み・実行済み");
    expect(html(<SqlSafetyBadge safe checked={false} executed />).textContent).toBe("安全確認を省略・実行済み");
    expect(html(<SqlSafetyBadge safe checked={false} executed={false} />).textContent).toBe("安全確認を省略・未実行");
    const blocked = html(<SqlSafetyBadge safe={false} checked={false} executed />);
    expect(blocked.textContent).toBe("安全検査でブロック");
    expect(blocked.querySelector("[data-status-variant]")?.getAttribute("data-status-variant")).toBe("danger");
    expect(html(<SqlSafetyBadge safe executed={false} labels={sqlBlockLabels({ safe: "OK" })} />).textContent).toBe("OK");
  });
});
