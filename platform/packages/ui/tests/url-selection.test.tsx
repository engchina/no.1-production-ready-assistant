// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useUrlSelection, type UrlSelection } from "../src/lib/url-selection";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

interface Navigation {
  search: string;
  replace: boolean;
}

let navigations: Navigation[];
let current: UrlSelection;
const goBack = vi.fn();

function Harness({ initial = "", singlePane = false }: { initial?: string; singlePane?: boolean }) {
  const [search, setSearch] = useState(initial);
  current = useUrlSelection({
    param: "object",
    searchParams: new URLSearchParams(search),
    setSearchParams: (next, options) => {
      const value = next(new URLSearchParams(search)).toString();
      navigations.push({ search: value, replace: Boolean(options?.replace) });
      setSearch(value);
    },
    singlePane,
    goBack: () => {
      goBack();
      setSearch(initial);
    },
  });
  return null;
}

beforeEach(() => {
  navigations = [];
  goBack.mockReset();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("useUrlSelection（#2281）", () => {
  it("URL の値を選択にし、無ければ null（先頭を自動で選ばない）", () => {
    act(() => root.render(<Harness initial="q=abc" />));
    expect(current.selectedId).toBeNull();
    expect(navigations).toHaveLength(0);
  });

  it("URL に値があれば、それを選択にする", () => {
    act(() => root.render(<Harness initial="q=abc&object=APP.T1" />));
    expect(current.selectedId).toBe("APP.T1");
  });

  it("2 ペインでは選び直しを replace にし、ほかの検索パラメータを残す", () => {
    act(() => root.render(<Harness initial="q=abc" />));
    act(() => current.select("APP.T1"));
    expect(navigations.at(-1)).toEqual({ search: "q=abc&object=APP.T1", replace: true });
    expect(current.selectedId).toBe("APP.T1");
    act(() => current.select("APP.T2"));
    expect(navigations.at(-1)).toEqual({ search: "q=abc&object=APP.T2", replace: true });
    act(() => current.select("APP.T2"));
    expect(navigations).toHaveLength(2);
  });

  it("1 ペインで一覧から入るときは push、詳細の中の選び直しは replace、戻るは history.back()", () => {
    act(() => root.render(<Harness singlePane />));
    act(() => current.select("APP.T1"));
    expect(navigations.at(-1)).toEqual({ search: "object=APP.T1", replace: false });
    act(() => current.select("APP.T2"));
    expect(navigations.at(-1)).toEqual({ search: "object=APP.T2", replace: true });
    act(() => current.clear());
    expect(goBack).toHaveBeenCalledTimes(1);
    expect(current.selectedId).toBeNull();
  });

  it("URL から直接開いた詳細の「一覧へ戻る」は、検索パラメータを消して置き換える", () => {
    act(() => root.render(<Harness singlePane initial="object=APP.T9&page=2" />));
    expect(current.selectedId).toBe("APP.T9");
    act(() => current.clear());
    expect(goBack).not.toHaveBeenCalled();
    expect(navigations.at(-1)).toEqual({ search: "page=2", replace: true });
    expect(current.selectedId).toBeNull();
  });

  it("replace は履歴に積まずに選択を置き換える（削除の後に外す）", () => {
    act(() => root.render(<Harness singlePane initial="object=APP.T1" />));
    act(() => current.replace(null));
    expect(navigations.at(-1)).toEqual({ search: "", replace: true });
    act(() => current.replace("APP.T3"));
    expect(navigations.at(-1)).toEqual({ search: "object=APP.T3", replace: true });
  });

  it("空白だけの値は未選択として扱う", () => {
    act(() => root.render(<Harness initial="object=%20%20" />));
    expect(current.selectedId).toBeNull();
  });
});
