import { describe, expect, it } from "vitest";

import { revealWithinScrollContainer } from "./focus-scroll";

function box(top: number, height: number) {
  return { getBoundingClientRect: () => ({ top, bottom: top + height, height }) };
}

function container(top: number, height: number, scrollTop = 0) {
  return { ...box(top, height), scrollTop } as unknown as HTMLElement & { scrollTop: number };
}

describe("revealWithinScrollContainer", () => {
  it("見えている項目では一覧を動かさない", () => {
    const list = container(100, 300, 40);
    revealWithinScrollContainer(list, box(150, 80) as unknown as HTMLElement);
    expect(list.scrollTop).toBe(40);
  });

  it("下にはみ出した項目は、下端が一覧の下端にそろうまでだけ動かす", () => {
    const list = container(100, 300, 0);
    revealWithinScrollContainer(list, box(450, 100) as unknown as HTMLElement);
    expect(list.scrollTop).toBe(150);
  });

  it("上に隠れた項目は、上端が一覧の上端にそろうまで戻す", () => {
    const list = container(100, 300, 500);
    revealWithinScrollContainer(list, box(20, 60) as unknown as HTMLElement);
    expect(list.scrollTop).toBe(420);
  });

  it("一覧より高い項目は上端をそろえる", () => {
    const list = container(100, 200, 0);
    revealWithinScrollContainer(list, box(180, 400) as unknown as HTMLElement);
    expect(list.scrollTop).toBe(80);
  });
});
