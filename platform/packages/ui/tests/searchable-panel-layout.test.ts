import { describe, expect, it } from "vitest";

import { computeSearchablePanelLayout } from "../src/components/ui/searchable-multi-select";

// #2091: 検索つきの複数選択の開いた一覧（パネル）の幅と左へのずらし。
// 欄の幅（lg 28rem = 392px）に縛らず、読みやすい幅（36rem = 504px）までカードの内容の端の中で右へ広げる。
// 下部の行（N 件選択中・表示中をすべて選択・クリア・完了）が 1 行に入らないときだけ、画面の余白まで広げ、左へずらす。
describe("computeSearchablePanelLayout", () => {
  const base = { fieldLeft: 300, fieldWidth: 392, preferred: 504, required: 420, boundLeft: 266, boundRight: 1410 };

  it("右に空きがあれば、欄の左端から読みやすい幅まで広げ、ずらさない", () => {
    expect(computeSearchablePanelLayout(base)).toEqual({ width: 504, offsetLeft: 0 });
  });

  it("読みやすい幅はカードの内容の端（softRight）で止め、下部の行が入るならずらさない", () => {
    expect(computeSearchablePanelLayout({ ...base, softRight: 750 })).toEqual({ width: 450, offsetLeft: 0 });
  });

  it("右の空きに下部の行が入らないときは、置いてよい範囲の中で右へ広げ、足りなければ左へずらす", () => {
    // 375px: 欄 33〜343、画面の余白の内側 14〜361。下部の 1 行に 400px 要る → 範囲いっぱいの 347px、19px 左へ。
    expect(
      computeSearchablePanelLayout({
        fieldLeft: 33,
        fieldWidth: 310,
        preferred: 504,
        required: 400,
        boundLeft: 14,
        boundRight: 361,
        softRight: 343,
      })
    ).toEqual({ width: 347, offsetLeft: 19 });
  });

  it("隣の欄の手前（boundRight）までしか広げず、下部の行が入れば左へはずらさない", () => {
    expect(computeSearchablePanelLayout({ ...base, required: 400, fieldLeft: 325, boundRight: 734 })).toEqual({
      width: 409,
      offsetLeft: 0,
    });
    // 下部の行が入らなければ、その分だけ左へずらす。
    expect(computeSearchablePanelLayout({ ...base, fieldLeft: 325, boundRight: 734 })).toEqual({
      width: 420,
      offsetLeft: 11,
    });
  });

  it("欄より狭くはしない。測れない（幅 0）ときは null", () => {
    expect(computeSearchablePanelLayout({ ...base, preferred: 100, required: 100 })).toEqual({
      width: 392,
      offsetLeft: 0,
    });
    expect(computeSearchablePanelLayout({ ...base, fieldWidth: 0 })).toBeNull();
    expect(computeSearchablePanelLayout({ ...base, boundLeft: 0, boundRight: 0 })).toBeNull();
  });

  it("小数の幅は切り上げる（1 行に入る幅にわずかに足りず折り返さない）", () => {
    expect(computeSearchablePanelLayout({ ...base, preferred: 0, required: 420.4 })?.width).toBe(421);
  });
});
