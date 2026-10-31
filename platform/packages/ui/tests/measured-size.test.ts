import { describe, expect, it } from "vitest";

import { stabilizeMeasuredBox, stabilizeMeasuredSize } from "../src/lib/measured-size";

describe("stabilizeMeasuredSize（測った大きさの書き戻しの揺れ止め。#1222）", () => {
  it("1px の往復は止まり、larger は大きい方・smaller は小さい方に寄せる", () => {
    let larger = stabilizeMeasuredSize(undefined, 158);
    let smaller = stabilizeMeasuredSize(undefined, 158, { prefer: "smaller" });
    for (const measured of [159, 158, 159, 158]) {
      larger = stabilizeMeasuredSize(larger, measured);
      smaller = stabilizeMeasuredSize(smaller, measured, { prefer: "smaller" });
    }
    expect(larger).toBe(159);
    expect(smaller).toBe(158);
  });

  it("差が tolerance を超える変化はそのまま反映する", () => {
    expect(stabilizeMeasuredSize(159, 175)).toBe(175);
    expect(stabilizeMeasuredSize(175, 159, { prefer: "smaller" })).toBe(159);
    expect(stabilizeMeasuredSize(100, 103, { tolerance: 3 })).toBe(103);
    expect(stabilizeMeasuredSize(100, undefined)).toBeUndefined();
  });

  it("幅・高さの組は、変わらなければ同じ object を返す（React の再描画をしない）", () => {
    const current = { width: 640, height: 480 };
    // 1px 大きく測れても（smaller なので）今の値を保ち、同じ object を返す。
    expect(stabilizeMeasuredBox(current, { width: 641, height: 481 }, { prefer: "smaller" })).toBe(current);
    // 1px 小さく測れたら小さい方に寄せ、その後に 1px 大きく戻っても動かない。
    const shrunk = stabilizeMeasuredBox(current, { width: 640, height: 479 }, { prefer: "smaller" });
    expect(shrunk).toEqual({ width: 640, height: 479 });
    expect(stabilizeMeasuredBox(shrunk, { width: 640, height: 480 }, { prefer: "smaller" })).toBe(shrunk);
    expect(stabilizeMeasuredBox(current, { width: 600, height: 480 }, { prefer: "smaller" })).toEqual({
      width: 600,
      height: 480,
    });
    expect(stabilizeMeasuredBox(null, { width: 1, height: 2 })).toEqual({ width: 1, height: 2 });
  });
});
