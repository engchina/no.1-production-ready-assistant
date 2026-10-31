import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  ActionResultRegion,
  BulkSelectionActions,
  ClearActionButton,
  ContentActionBar,
  FormActionBar,
  entityActionToFormAction,
} from "../src";

const noop = () => {};

describe("ContentActionBar", () => {
  it("操作は aria-label 付きの group にまとめ、情報がなければ右寄せ全幅にする", () => {
    const html = renderToStaticMarkup(
      <ContentActionBar ariaLabel="SQL の操作">
        <button type="button">コピー</button>
      </ContentActionBar>
    );
    expect(html).toContain('role="group"');
    expect(html).toContain('aria-label="SQL の操作"');
    expect(html).toContain("justify-end");
    expect(html).toContain("w-full");
  });

  it("title / description / meta を左の情報欄に描き、操作は ml-auto で右に寄せる", () => {
    const html = renderToStaticMarkup(
      <ContentActionBar ariaLabel="結果の操作" title="結果" description="説明" meta="3 件">
        <button type="button">出力</button>
      </ContentActionBar>
    );
    expect(html).toContain(">結果<");
    expect(html).toContain(">説明<");
    expect(html).toContain(">3 件<");
    expect(html).toContain("ml-auto");
  });
});

describe("BulkSelectionActions", () => {
  it("全選択は secondary、全解除は ghost で、スコープ付きの aria-label と test id を持つ", () => {
    const html = renderToStaticMarkup(
      <BulkSelectionActions
        selectLabel="すべて選択"
        clearLabel="選択をすべて解除"
        selectAriaLabel="表 をすべて選択"
        clearAriaLabel="表 の選択をすべて解除"
        onSelectAll={noop}
        onClearAll={noop}
        clearDisabled
        dataTestId="bulk"
      />
    );
    expect(html).toContain('aria-label="表 をすべて選択"');
    expect(html).toContain('aria-label="表 の選択をすべて解除"');
    expect(html).toContain('data-testid="bulk-select"');
    expect(html).toContain('data-testid="bulk-clear"');
    // 全解除だけ disabled
    expect(html.match(/disabled=""/g)).toHaveLength(1);
  });

  it("busy のときは両方 disabled にして aria-busy を立てる", () => {
    const html = renderToStaticMarkup(
      <BulkSelectionActions selectLabel="a" clearLabel="b" onSelectAll={noop} onClearAll={noop} busy />
    );
    expect(html).toContain('aria-busy="true"');
    expect(html.match(/disabled=""/g)).toHaveLength(2);
  });
});

describe("ClearActionButton", () => {
  it("label を必須の表示名にし、ariaLabel がなければ label を aria-label に使う", () => {
    const html = renderToStaticMarkup(<ClearActionButton label="SQL 入力・結果をリセット" onClick={noop} />);
    expect(html).toContain('aria-label="SQL 入力・結果をリセット"');
    expect(html).toContain('type="button"');
    expect(html).toContain("whitespace-nowrap");
  });
});

describe("ActionResultRegion", () => {
  it("結果もエラーもなければ何も描かない", () => {
    expect(renderToStaticMarkup(<ActionResultRegion loading={false} operationKey="a" />)).toBe("");
  });

  it("エラーは danger の Banner で描き、結果より優先する", () => {
    const html = renderToStaticMarkup(
      <ActionResultRegion loading={false} operationKey="a" errorMessage="失敗しました。" testId="run">
        <p>結果</p>
      </ActionResultRegion>
    );
    expect(html).toContain('data-testid="run-error"');
    expect(html).toContain("失敗しました。");
    expect(html).not.toContain("<p>結果</p>");
  });

  it("結果を描き、読込中の aria-busy は立てない", () => {
    const html = renderToStaticMarkup(
      <ActionResultRegion loading={false} operationKey="a" testId="run">
        <p>結果</p>
      </ActionResultRegion>
    );
    expect(html).toContain('data-testid="run-region"');
    expect(html).toContain("<p>結果</p>");
    expect(html).not.toContain("aria-busy");
  });
});

describe("FormActionBar", () => {
  it("primary → secondary の順に直置きし、danger は「その他の操作」メニューにまとめる", () => {
    const html = renderToStaticMarkup(
      <FormActionBar
        ariaLabel="ロール編集操作"
        testId="form-actions"
        primaryActions={[{ id: "save", label: "保存", onClick: noop }]}
        secondaryActions={[{ id: "cancel", label: "キャンセル", onClick: noop }]}
        dangerActions={[{ id: "delete", label: "削除", onClick: noop }]}
      />
    );
    expect(html).toContain('role="group"');
    expect(html).toContain('aria-label="ロール編集操作"');
    expect(html.indexOf(">保存<")).toBeLessThan(html.indexOf(">キャンセル<"));
    // danger は閉じたメニューの中にあり、赤いボタンとして直置きしない。
    expect(html).toContain(">その他の操作<");
    // 読み上げ名に対象（ariaLabel）を足す（#582）。
    expect(html).toContain('aria-label="その他の操作（ロール編集操作）"');
    expect(html).toContain('aria-haspopup="menu"');
    expect(html).not.toContain(">削除<");
  });

  it("メニューの見出しは moreLabel で差し替えられ、danger がなければメニューを出さない", () => {
    const withLabel = renderToStaticMarkup(
      <FormActionBar
        ariaLabel="操作"
        moreLabel="危険な操作"
        dangerActions={[{ id: "delete", label: "削除", onClick: noop }]}
      />
    );
    expect(withLabel).toContain(">危険な操作<");
    const without = renderToStaticMarkup(
      <FormActionBar ariaLabel="操作" primaryActions={[{ id: "save", label: "保存", onClick: noop }]} />
    );
    expect(without).not.toContain('aria-haspopup="menu"');
  });

  it("直置きのボタンは既定で type=button、type を渡すと submit にでき、loading でもラベルを変えない", () => {
    const html = renderToStaticMarkup(
      <FormActionBar
        ariaLabel="操作"
        primaryActions={[{ id: "save", label: "保存", type: "submit", loading: true, ariaLabel: "DB 設定: 保存" }]}
        secondaryActions={[{ id: "test", label: "接続テスト", onClick: noop }]}
      />
    );
    const [save, test] = html.match(/<button\b[^>]*>/g) ?? [];
    expect(save).toContain('type="submit"');
    expect(save).toContain('aria-busy="true"');
    expect(save).toContain('aria-label="DB 設定: 保存"');
    expect(test).toContain('type="button"');
    expect(test).toContain('data-form-action-id="test"');
    expect(html).toContain("<span>保存</span>");
    expect(html).not.toContain("保存中");
  });

  it("確定の行はフォームの末尾のふつうの行で、画面の下端に貼り付けない（#1786 / #2715）", () => {
    const html = renderToStaticMarkup(
      <FormActionBar
        ariaLabel="ユーザーの確定"
        primaryActions={[{ id: "save", label: "保存", type: "submit" }]}
        secondaryActions={[{ id: "discard", label: "変更を破棄", onClick: noop }]}
      />
    );
    // sticky・fixed で貼り付けず、重なり順も持たない（PageHeader に重ならない）。
    expect(html).not.toMatch(/\b(sticky|fixed)\b/);
    expect(html).not.toContain("z-[");
    expect(html).not.toContain("data-stuck");
    // フォームの末尾の区切り線の下に、primary → 変更を破棄 の順。狭い画面は縦に積み、sm 以上で横に並べる。
    expect(html).toMatch(/class="[^"]*\bborder-t border-border pt-4\b/);
    expect(html).toContain("flex-col");
    expect(html).toContain("sm:flex-row");
    expect(html.indexOf(">保存<")).toBeLessThan(html.indexOf(">変更を破棄<"));
  });

  it("entityActionToFormAction は一覧・詳細の EntityAction をそのまま移す", () => {
    const action = entityActionToFormAction({
      id: "archive",
      label: "アーカイブ",
      onSelect: noop,
      loading: true,
      disabled: false,
      ariaLabel: "ロールをアーカイブ",
      testId: "archive",
    });
    expect(action).toMatchObject({
      id: "archive",
      label: "アーカイブ",
      onClick: noop,
      loading: true,
      disabled: false,
      ariaLabel: "ロールをアーカイブ",
      testId: "archive",
    });
  });
});
