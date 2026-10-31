import { describe, expect, it } from "vitest";

import { computeFloatingMenuLayout } from "../src/components/ui/floating-menu";

// 操作メニュー（FloatingActionMenu）の左右の位置（#363）。375 × 812 の画面、高さ 36px のトリガー。
const phone = { viewportWidth: 375, viewportHeight: 812 };
function trigger(left: number, right: number, top = 120) {
  return { top, bottom: top + 36, left, right, width: right - left };
}

describe("操作メニューの左右の位置（computeFloatingMenuLayout）", () => {
  it("左寄りのトリガー（375px で操作が折り返した PageHeader）は、end 揃えが左外に切れるので左端にそろえる", () => {
    const layout = computeFloatingMenuLayout({
      ...phone,
      align: "end",
      triggerRect: trigger(16, 141),
      menuWidth: 250,
      naturalHeight: 120,
    });
    expect(layout.align).toBe("start");
    expect(layout.placement).toBe("bottom");
    expect(layout.style).toMatchObject({ left: 16, top: 160, transformOrigin: "top left" });
  });

  it("右寄りのトリガーは従来どおり右端にそろえる", () => {
    const layout = computeFloatingMenuLayout({
      ...phone,
      align: "end",
      triggerRect: trigger(240, 359),
      menuWidth: 250,
      naturalHeight: 120,
    });
    expect(layout.align).toBe("end");
    expect(layout.style).toMatchObject({ left: 359 - 250, transformOrigin: "top right" });
  });

  it("start 揃えでも、右外に切れて右端なら入るときは右端にそろえる", () => {
    const layout = computeFloatingMenuLayout({
      ...phone,
      align: "start",
      triggerRect: trigger(300, 359),
      menuWidth: 200,
      naturalHeight: 120,
    });
    expect(layout.align).toBe("end");
    expect(layout.style).toMatchObject({ left: 159 });
  });

  it("どちらの端でも入らなければ、指定の端のまま画面の内側（左右 8px）にずらす", () => {
    // 中央のトリガー: end → 200 - 300 = -100（左外）、start → 120（右端が 420 で右外）。
    const layout = computeFloatingMenuLayout({
      ...phone,
      align: "end",
      triggerRect: trigger(120, 200),
      menuWidth: 300,
      naturalHeight: 120,
    });
    expect(layout.align).toBe("end");
    expect(layout.style).toMatchObject({ left: 8, maxWidth: "calc(100vw - 16px)" });
    const left = layout.style.left as number;
    expect(left + 300).toBeLessThanOrEqual(375 - 8);

    // 右寄りのトリガーでも同じ（右端が画面の内側に収まる位置まで左へ寄せる）。
    const right = computeFloatingMenuLayout({
      ...phone,
      align: "start",
      triggerRect: trigger(300, 370),
      menuWidth: 360,
      naturalHeight: 120,
    });
    expect(right.style).toMatchObject({ left: 8 });
  });

  it("画面の下端では上に開き、左右の反転と組み合わせる", () => {
    const layout = computeFloatingMenuLayout({
      ...phone,
      align: "end",
      triggerRect: trigger(16, 141, 740),
      menuWidth: 250,
      naturalHeight: 120,
    });
    expect(layout.placement).toBe("top");
    expect(layout.align).toBe("start");
    expect(layout.style).toMatchObject({ left: 16, top: 740 - 4 - 120, transformOrigin: "bottom left" });
  });

  it("stretch（SelectField）は左右を反転せず、トリガーと同じ幅・左端のまま", () => {
    const layout = computeFloatingMenuLayout({
      ...phone,
      align: "stretch",
      triggerRect: trigger(16, 359),
      menuWidth: 999,
      naturalHeight: 120,
    });
    expect(layout.align).toBe("stretch");
    expect(layout.style).toMatchObject({ left: 16, width: 343 });
  });
});
