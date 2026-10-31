import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DEFAULT_ROADMAP_LABEL, RoadmapBadge } from "../src/components/data/status-badge";
import { computeFloatingMenuLayout, floatingLayerZIndex } from "../src/components/ui/floating-menu";
import {
  findTypeaheadIndex,
  isInsideAny,
  nearestScrollTop,
  SELECT_TYPEAHEAD_RESET_MS,
  SelectField,
  selectPortalContainer,
  typeaheadStartIndex,
} from "../src/components/ui/select-field";

// 幅 1280 × 高さ 800 の画面で、高さ 36px のトリガー。
const viewport = { viewportWidth: 1280, viewportHeight: 800 };
function triggerAt(top: number, left = 100, width = 320) {
  return { top, bottom: top + 36, left, right: left + width, width };
}

describe("SelectField の一覧の位置（computeFloatingMenuLayout）", () => {
  it("下に収まるときは下に出し、トリガーと同じ幅・左端にそろえる", () => {
    const layout = computeFloatingMenuLayout({
      ...viewport,
      align: "stretch",
      triggerRect: triggerAt(100),
      menuWidth: 999,
      naturalHeight: 200,
      maxHeight: 224,
    });
    expect(layout.placement).toBe("bottom");
    expect(layout.constrained).toBe(false);
    expect(layout.style).toMatchObject({ top: 140, left: 100, width: 320 });
    expect(layout.style).not.toHaveProperty("maxHeight");
  });

  it("画面の下端では上に反転し、トリガーの上に接して置く", () => {
    const layout = computeFloatingMenuLayout({
      ...viewport,
      align: "stretch",
      triggerRect: triggerAt(700),
      menuWidth: 320,
      naturalHeight: 200,
      maxHeight: 224,
    });
    expect(layout.placement).toBe("top");
    expect(layout.style).toMatchObject({ top: 700 - 4 - 200 });
  });

  it("選択肢が多いときは maxHeight（16rem）で頭打ちにして内部スクロールにする", () => {
    const layout = computeFloatingMenuLayout({
      ...viewport,
      align: "stretch",
      triggerRect: triggerAt(100),
      menuWidth: 320,
      naturalHeight: 42 * 15 + 10,
      maxHeight: 224,
    });
    expect(layout.placement).toBe("bottom");
    expect(layout.constrained).toBe(true);
    expect(layout.style).toMatchObject({ maxHeight: 224 });
  });

  it("上下どちらにも収まらないときは広い側に出し、空きの高さまで縮める", () => {
    const layout = computeFloatingMenuLayout({
      viewportWidth: 375,
      viewportHeight: 400,
      align: "stretch",
      triggerRect: triggerAt(250, 16, 343),
      menuWidth: 343,
      naturalHeight: 600,
      maxHeight: 224,
    });
    // 上: 250 - 8 - 4 = 238、下: 400 - 8 - 286 - 4 = 102 → 上で 224 は収まる
    expect(layout.placement).toBe("top");
    expect(layout.style).toMatchObject({ maxHeight: 224, top: 250 - 4 - 224, width: 343 });

    const cramped = computeFloatingMenuLayout({
      viewportWidth: 375,
      viewportHeight: 300,
      align: "stretch",
      triggerRect: triggerAt(120, 16, 343),
      menuWidth: 343,
      naturalHeight: 600,
      maxHeight: 224,
    });
    // 上: 120 - 12 = 108、下: 300 - 8 - 156 - 4 = 132 → 下に 132px
    expect(cramped.placement).toBe("bottom");
    expect(cramped.style).toMatchObject({ maxHeight: 132, top: 160 });
  });

  it("操作メニュー（end 揃え）は従来どおりトリガーの右端にそろえ、画面の外に出さない", () => {
    const layout = computeFloatingMenuLayout({
      ...viewport,
      align: "end",
      triggerRect: { top: 100, bottom: 132, left: 1240, right: 1272, width: 32 },
      menuWidth: 200,
      naturalHeight: 120,
    });
    expect(layout.style).toMatchObject({ left: 1072, top: 136 });
    expect(layout.style).not.toHaveProperty("width");
  });
});

describe("Portal の重なり順（floatingLayerZIndex）", () => {
  it("モーダル（--z-dialog: 1000）の中では、モーダルより 1 段上に出す", () => {
    expect(floatingLayerZIndex([1000], 100)).toBe(1001);
    expect(floatingLayerZIndex([1, 1000], 100)).toBe(1001);
  });

  it("z-index を持つ層の外、または --z-dropdown より低い層では class の --z-dropdown のまま", () => {
    expect(floatingLayerZIndex([], 100)).toBeUndefined();
    expect(floatingLayerZIndex([10], 100)).toBeUndefined();
  });
});

describe("typeahead（findTypeaheadIndex）", () => {
  const regions = ["ap-tokyo-1", "ap-osaka-1", "us-chicago-1", "eu-frankfurt-1", "us-ashburn-1"];

  it("入力で始まる最初の選択肢へ、次の選択肢から末尾・先頭の順に探す", () => {
    expect(findTypeaheadIndex(regions, "u", 0)).toBe(2);
    expect(findTypeaheadIndex(regions, "u", 3)).toBe(4);
    expect(findTypeaheadIndex(regions, "a", 2)).toBe(0);
  });

  it("続けて入力した文字は前方一致で絞り込む", () => {
    expect(findTypeaheadIndex(regions, "us-a", 2)).toBe(4);
    expect(findTypeaheadIndex(regions, "ap-o", 0)).toBe(1);
  });

  it("同じ文字を繰り返すと、その文字で始まる選択肢を順に巡る", () => {
    expect(findTypeaheadIndex(regions, "aa", 1)).toBe(1);
    expect(findTypeaheadIndex(regions, "aaa", 2)).toBe(0);
  });

  it("大文字小文字と全角半角を区別しない。一致しなければ -1", () => {
    expect(findTypeaheadIndex(["MRR", "Recall@K", "Precision@K"], "ｒ", 1)).toBe(1);
    expect(findTypeaheadIndex(["MRR", "Recall@K"], "P", 1)).toBe(-1);
    expect(findTypeaheadIndex([], "a", 0)).toBe(-1);
    expect(findTypeaheadIndex(regions, "", 0)).toBe(-1);
  });

  it("1 文字目と同じ文字の繰り返しは次から、続く文字は今の選択肢から探す", () => {
    expect(typeaheadStartIndex("u", 2)).toBe(3);
    expect(typeaheadStartIndex("uu", 4)).toBe(5);
    expect(typeaheadStartIndex("us", 2)).toBe(2);
    expect(typeaheadStartIndex("u", -1)).toBe(0);
    // "u" → us-chicago、"us-a" → us-ashburn（今の選択肢から前方一致を続ける）
    const first = findTypeaheadIndex(regions, "u", typeaheadStartIndex("u", 0));
    expect(first).toBe(2);
    expect(findTypeaheadIndex(regions, "us", typeaheadStartIndex("us", first))).toBe(2);
    expect(findTypeaheadIndex(regions, "us-a", typeaheadStartIndex("us-a", first))).toBe(4);
  });

  it("入力のリセットは APG と同じ 500ms", () => {
    expect(SELECT_TYPEAHEAD_RESET_MS).toBe(500);
  });
});

describe("強調中の選択肢のスクロール（nearestScrollTop）", () => {
  const list = { viewportHeight: 224, itemHeight: 42, padding: 4 };

  it("下にはみ出た選択肢は、下端に合わせて見せる（7 件目以降）", () => {
    // 7 件目（index 6）: top = 4 + 42 * 6 = 256
    expect(nearestScrollTop({ ...list, scrollTop: 0, itemTop: 256 })).toBe(256 + 42 + 4 - 224);
  });

  it("上にはみ出た選択肢は、上端に合わせて見せる", () => {
    expect(nearestScrollTop({ ...list, scrollTop: 200, itemTop: 88 })).toBe(84);
    expect(nearestScrollTop({ ...list, scrollTop: 200, itemTop: 4 })).toBe(0);
  });

  it("見えている選択肢ではスクロールしない", () => {
    expect(nearestScrollTop({ ...list, scrollTop: 40, itemTop: 88 })).toBe(40);
  });
});

describe("外側クリックの判定（isInsideAny）", () => {
  const node = (children: unknown[]) => ({ contains: (target: unknown) => children.includes(target) });
  const option = {} as Node;
  const outside = {} as Node;

  it("Portal で body に出した一覧の中は内側とみなす", () => {
    const root = node([]);
    const listbox = node([option]);
    expect(isInsideAny(option, [root, listbox])).toBe(true);
  });

  it("フィールドと一覧のどちらでもなければ外側。一覧が閉じていても判定できる", () => {
    expect(isInsideAny(outside, [node([]), null])).toBe(false);
    expect(isInsideAny(null, [node([option])])).toBe(false);
  });
});

describe("一覧を描く先（selectPortalContainer）", () => {
  it("モーダル（aria-modal / dialog）の中では、そのモーダルに描く", () => {
    const modal = {} as HTMLElement;
    const selectors: string[] = [];
    const trigger = {
      closest: (selector: string) => {
        selectors.push(selector);
        return modal;
      },
    } as unknown as Element;
    expect(selectPortalContainer(trigger)).toBe(modal);
    expect(selectors).toEqual(['[aria-modal="true"], dialog[open]']);
  });

  it("モーダルの外では body（DOM が無い環境では描かない）", () => {
    const trigger = { closest: () => null } as unknown as Element;
    expect(selectPortalContainer(trigger)).toBeNull();
    expect(selectPortalContainer(null)).toBeNull();
  });
});

describe("SelectField の aria（互換）", () => {
  it("閉じているときは combobox だけを描き、一覧の id と aria を保つ", () => {
    const html = renderToStaticMarkup(
      <SelectField
        id="region"
        label="リージョン"
        value="b"
        options={[
          { value: "a", label: "A" },
          { value: "b", label: "B" },
        ]}
        onValueChange={() => {}}
        helper="説明"
      />
    );
    expect(html).toMatch(/<button[^>]*id="region"[^>]*role="combobox"/);
    expect(html).toMatch(/aria-controls="region-[^"]*-listbox"/);
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain('aria-haspopup="listbox"');
    expect(html).toMatch(/aria-describedby="region-[^"]*-hint"/);
    expect(html).not.toContain('role="listbox"');
    expect(html).toContain(">B<");
  });
});

describe("準備中（ロードマップ）のバッジ", () => {
  it("既定のラベルを中立色のバッジとアイコンで出し、目印の data 属性を付ける", () => {
    const html = renderToStaticMarkup(<RoadmapBadge />);
    expect(DEFAULT_ROADMAP_LABEL).toBe("準備中（ロードマップ）");
    expect(html).toContain("data-roadmap-badge");
    expect(html).toContain('data-status-variant="neutral"');
    expect(html).toContain(DEFAULT_ROADMAP_LABEL);
    expect(html).toContain("<svg");
  });

  it("翻訳済みのラベルで上書きできる", () => {
    expect(renderToStaticMarkup(<RoadmapBadge label="Roadmap" />)).toContain("Roadmap");
  });

  it("閉じた選択欄は、無効の選択肢を選んでいても値のラベルを出す（保存済みの値の表示）", () => {
    const html = renderToStaticMarkup(
      <SelectField
        id="engine"
        label="エンジン"
        value="b"
        options={[
          { value: "a", label: "A" },
          { value: "b", label: "B", disabled: true, disabledReason: "理由", badge: DEFAULT_ROADMAP_LABEL },
        ]}
        onValueChange={() => {}}
      />
    );
    expect(html).toContain(">B<");
  });
});
