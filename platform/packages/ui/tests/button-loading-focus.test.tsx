import { Save } from "lucide-react";
import type { KeyboardEvent, ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { Button, buttonVariants, type ButtonProps } from "../src/components/ui/button";
import { PageHeader, groupBoundaries, orderActions, type PageHeaderAction } from "../src/components/app-shell/PageHeader";

type ButtonElementProps = {
  disabled?: boolean;
  "aria-disabled"?: boolean | "true" | "false";
  "aria-busy"?: boolean;
  onClick?: (event: unknown) => void;
  onPointerDown?: (event: unknown) => void;
  onMouseDown?: (event: unknown) => void;
  onKeyDown?: (event: KeyboardEvent<HTMLButtonElement>) => void;
};

/** Button はフックを持たないので、関数として呼んで <button> 要素の props を直接確かめる。 */
function buttonElement(props: ButtonProps) {
  return Button(props) as ReactElement<ButtonElementProps>;
}

function fakeEvent(key?: string) {
  return { key, preventDefault: vi.fn(), stopPropagation: vi.fn() };
}

describe("Button の loading（#355: フォーカスを保つ）", () => {
  it("loading 中はネイティブの disabled を付けず aria-disabled / aria-busy にする（フォーカスが body へ外れない）", () => {
    const html = renderToStaticMarkup(
      <Button loading icon={Save}>
        保存
      </Button>
    );
    const tag = html.slice(0, html.indexOf(">") + 1);
    expect(tag).toContain('aria-disabled="true"');
    expect(tag).toContain('aria-busy="true"');
    expect(tag).not.toMatch(/\sdisabled=""/);
  });

  it("disabled はネイティブのまま（loading と重なっても disabled を優先し、aria-disabled は付けない）", () => {
    const tag = (html: string) => html.slice(0, html.indexOf(">") + 1);
    const disabledOnly = tag(renderToStaticMarkup(<Button disabled>保存</Button>));
    expect(disabledOnly).toMatch(/\sdisabled=""/);
    expect(disabledOnly).not.toMatch(/\saria-disabled=/);

    const both = tag(
      renderToStaticMarkup(
        <Button disabled loading icon={Save}>
          保存
        </Button>
      )
    );
    expect(both).toMatch(/\sdisabled=""/);
    expect(both).toContain('aria-busy="true"');
    expect(both).not.toMatch(/\saria-disabled=/);
  });

  it("loading 中のクリックは呼び出し側の onClick を呼ばず、既定動作（form の submit）を取り消す", () => {
    const onClick = vi.fn();
    const element = buttonElement({ loading: true, icon: Save, type: "submit", onClick, children: "保存" });
    const event = fakeEvent();
    element.props.onClick?.(event);
    expect(onClick).not.toHaveBeenCalled();
    expect(event.preventDefault).toHaveBeenCalledTimes(1);
    expect(event.stopPropagation).toHaveBeenCalledTimes(1);
  });

  it("loading でないときは onClick をそのまま渡す", () => {
    const onClick = vi.fn();
    expect(buttonElement({ onClick, children: "保存" }).props.onClick).toBe(onClick);
  });

  it("loading 中は押下で処理を始める onPointerDown / onMouseDown も呼ばない", () => {
    const onPointerDown = vi.fn();
    const onMouseDown = vi.fn();
    const element = buttonElement({ loading: true, icon: Save, onPointerDown, onMouseDown, children: "検索" });
    expect(element.props.onPointerDown).toBeUndefined();
    expect(element.props.onMouseDown).toBeUndefined();
    const idle = buttonElement({ onPointerDown, onMouseDown, children: "検索" });
    expect(idle.props.onPointerDown).toBe(onPointerDown);
    expect(idle.props.onMouseDown).toBe(onMouseDown);
  });

  it("loading 中の Enter / Space は既定動作ごと止め、Tab など移動のキーは呼び出し側へ渡す", () => {
    const onKeyDown = vi.fn();
    const element = buttonElement({ loading: true, icon: Save, onKeyDown, children: "保存" });
    for (const key of ["Enter", " "]) {
      const event = fakeEvent(key);
      element.props.onKeyDown?.(event as unknown as KeyboardEvent<HTMLButtonElement>);
      expect(event.preventDefault).toHaveBeenCalledTimes(1);
    }
    expect(onKeyDown).not.toHaveBeenCalled();
    const tab = fakeEvent("Tab");
    element.props.onKeyDown?.(tab as unknown as KeyboardEvent<HTMLButtonElement>);
    expect(onKeyDown).toHaveBeenCalledTimes(1);
    expect(tab.preventDefault).not.toHaveBeenCalled();
  });

  it("aria-disabled の見た目は disabled と同じで、hover / active の塗りは aria-disabled では効かない", () => {
    for (const variant of ["primary", "secondary", "ghost", "danger"] as const) {
      const classes = buttonVariants({ variant }).split(/\s+/);
      for (const token of ["cursor-not-allowed", "border-border", "bg-surface-disabled", "text-fg-disabled", "shadow-none"]) {
        expect(classes).toContain(`disabled:${token}`);
        expect(classes).toContain(`aria-disabled:${token}`);
      }
      const interactive = classes.filter((token) => /^(hover|active):/.test(token));
      expect(interactive.length).toBeGreaterThan(0);
      for (const token of interactive) expect(token).toMatch(/^(hover|active):enabled:not-aria-disabled:/);
    }
  });
});

describe("Button の variant と tone（#355）", () => {
  it("variant='danger' と tone='danger' の同時指定は型で禁止する（赤地に赤文字になる）", () => {
    // @ts-expect-error variant="danger" に tone="danger" は渡せない
    const invalid = <Button variant="danger" tone="danger">削除</Button>;
    const allowed = [
      <Button key="a" variant="danger">削除</Button>,
      <Button key="b" variant="secondary" tone="danger">キャンセル</Button>,
      <Button key="c" variant="ghost" tone="danger">削除</Button>,
      <Button key="d" tone="danger">削除</Button>,
    ];
    expect(invalid).toBeTruthy();
    expect(allowed).toHaveLength(4);
  });
});

describe("PageHeader の操作のグループの区切り（buttons.md §5）", () => {
  const noop = () => {};
  const actions: PageHeaderAction[] = [
    { id: "create", kind: "primary", label: "作成", onClick: noop },
    { id: "refresh", kind: "utility", label: "最新情報を取得", onClick: noop },
    { id: "import", kind: "secondary", label: "取込", onClick: noop },
    { id: "purge", kind: "danger", label: "全削除", onClick: noop },
  ];

  it("danger | utility | secondary + primary の境界にだけ区切りを置く", () => {
    const ordered = orderActions(actions);
    expect(ordered.map((action) => action.id)).toEqual(["purge", "refresh", "import", "create"]);
    expect([...groupBoundaries(ordered)]).toEqual([1, 2]);
    // secondary と primary は同じ「作業開始」のグループ
    expect([...groupBoundaries(orderActions([actions[0], actions[2]]))]).toEqual([]);
  });

  it("区切りは読み上げない装飾として描画する", () => {
    const html = renderToStaticMarkup(<PageHeader title="一覧" actions={actions} />);
    const separators = html.match(/<span aria-hidden="true"[^>]*data-testid="page-actions-separator"/g) ?? [];
    expect(separators).toHaveLength(2);
  });
});
