import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const read = (path: string) => readFileSync(new URL(`../src/styles/${path}`, import.meta.url), "utf8");
const colors = read("tokens/colors.css");

describe("tokens CSS", () => {
  it("旧トークン名の互換層（compat.css / 旧名ユーティリティ）を持たない", () => {
    const entry = read("tokens.css");
    expect(entry).not.toMatch(/compat\.css/);
    expect(entry).not.toMatch(/--color-(card|muted|primary|background|foreground):/);
    expect(() => read("tokens/compat.css")).toThrow();
  });

  it("light-dark() には色だけを渡す（混合率などを渡すと宣言ごと無効になる）", () => {
    expect(colors).not.toMatch(/light-dark\(\s*\d+%/);
  });

  it("意味トークンを color-scheme を切り替える要素すべてで宣言し直す（Lightning CSS 変換後もスコープ内でダーク値になる）", () => {
    expect(colors).toMatch(/:root, \[data-theme\], \[data-surface\] \{[^}]*--color-canvas:\s*light-dark/);
  });

  it("書体の並びは fonts.css が持ち、内容の言語（lang）で漢字の並びだけを替える（#3335）", () => {
    const fonts = read("tokens/fonts.css");
    expect(colors).not.toMatch(/--font-(sans|mono):/);
    expect(read("tokens.css")).toMatch(/@import "\.\/tokens\/fonts\.css";/);
    expect(fonts).toMatch(/--font-sans: var\(--font-han\), var\(--font-sans-tail\);/);
    for (const [lang, first] of [
      ["ja", "Noto Sans JP Variable"],
      ["zh-Hans", "Noto Sans SC Variable"],
      ["zh-Hant", "Noto Sans TC Variable"],
      ["zh-HK", "Noto Sans HK Variable"],
    ]) {
      expect(fonts).toMatch(new RegExp(`:where\\(\\[lang\\]:lang\\(${lang}\\)\\) \\{\\s*--font-han: "${first}",`));
    }
    // lang を付けた要素だけで組み直す（詳細度 0。子は継承する）。
    expect(fonts).toMatch(/:where\(\[lang\]\) \{\s*--font-sans: var\(--font-han\), var\(--font-sans-tail\);\s*--font-mono:[^;]+;/);
    // 書体の指定は Tailwind の base の層（preflight の code / pre と utility を上書きしない）。
    expect(fonts).toMatch(/@layer base \{\s*:where\(\[lang\]\) \{\s*font-family: var\(--font-sans\);\s*\}\s*\}/);
  });

  it("寸法を合わせた代わりの書体は local() だけで、Web の書体の直後に置く（#3335）", () => {
    const fonts = read("tokens/fonts.css");
    const faces = [...fonts.matchAll(/@font-face \{([^}]*)\}/g)].map((match) => match[1]);
    expect(faces).toHaveLength(8);
    for (const face of faces) {
      expect(face).toMatch(/font-family: "(Noto Sans JP|Google Sans Code) Fallback";/);
      expect(face).not.toMatch(/url\(/);
      expect(face).toMatch(/ascent-override: [\d.]+%;/);
      expect(face).toMatch(/descent-override: [\d.]+%;/);
      expect(face).toMatch(/line-gap-override: 0%;/);
    }
    expect(fonts).toMatch(/--font-han: "Noto Sans JP Variable", "Noto Sans JP Fallback",/);
    expect(fonts).toMatch(/--font-mono: "Google Sans Code", "Google Sans Code Fallback",/);
  });

  it("テーマ class（.light / .dark）はルート要素でだけ効く（外部ライブラリの同名 class に反応しない）", () => {
    for (const css of [colors, read("tokens/a11y.css")]) {
      expect(css.replace(/\/\*[\s\S]*?\*\//g, "")).not.toMatch(/(^|[\s,])\.(light|dark)\b/m);
    }
    expect(colors).toMatch(/:root\.dark \{ color-scheme: dark; \}/);
  });

  it("文字は px、ルートは 14px のまま（余白の rem を動かさない）", () => {
    expect(read("tokens/base.css")).toMatch(/html\s*\{\s*font-size:\s*14px;/);
    expect(read("tokens/typography.css")).toMatch(/--font-size-sm:\s*14px;/);
    expect(read("tokens/typography.css")).toMatch(/--font-size-xs:\s*12px;/);
    expect(read("tokens.css")).toMatch(/--text-sm:\s*var\(--font-size-sm\);/);
    expect(read("tokens.css")).toMatch(/--text-xs:\s*var\(--font-size-xs\);/);
  });

  it("通知（Toast）の幅は 22rem〜32rem、md 以上の下端からの距離は 8rem のトークンが正本（#899・#1728）", () => {
    const spacing = read("tokens/spacing.css");
    expect(spacing).toMatch(/--toast-width-min:\s*22rem;/);
    expect(spacing).toMatch(/--toast-width-max:\s*32rem;/);
    expect(spacing).toMatch(/--toast-offset-bottom:\s*8rem;/);
    const placement = readFileSync(new URL("../src/lib/toast-placement.ts", import.meta.url), "utf8");
    expect(placement).toContain('"var(--toast-width-min)"');
    expect(placement).toContain('"var(--toast-width-max)"');
    expect(placement).toContain('"var(--toast-offset-bottom)"');
    expect(placement).not.toMatch(/\d+rem, \$\{/);
  });

  it("サイドバー幅はトークンが正本で、日本語のナビ項目名が収まる 18rem（252px）", () => {
    expect(read("tokens/spacing.css")).toMatch(/--sidebar-width:\s*18rem;/);
    const sidebar = readFileSync(new URL("../src/components/app-shell/Sidebar.tsx", import.meta.url), "utf8");
    expect(sidebar).toContain('"w-[var(--sidebar-width-collapsed)]" : "w-[var(--sidebar-width)]"');
    expect(sidebar).not.toMatch(/\bw-60\b/);
  });

  it("通知はモーダルの下に重なる（toast < scrim < dialog < palette）", () => {
    const elevation = read("tokens/elevation.css");
    const z = (name: string) => Number(elevation.match(new RegExp(`--z-${name}:\\s*(\\d+);`))?.[1]);
    expect(z("sticky")).toBeLessThan(z("toast"));
    expect(z("toast")).toBeLessThan(z("scrim"));
    expect(z("scrim")).toBeLessThan(z("dialog"));
    expect(z("dialog")).toBeLessThan(z("palette"));
    const toast = readFileSync(new URL("../src/components/ui/toast.tsx", import.meta.url), "utf8");
    expect(toast).toContain("z-[var(--z-toast)]");
  });

  it("タッチ端末では操作部品（ボタン・入力欄・選択欄）の高さを 3 段とも 44px にする（#613）", () => {
    const spacing = read("tokens/spacing.css");
    expect(spacing).toMatch(
      /@media \(pointer: coarse\) \{\s*:root \{\s*--control-height-sm: var\(--control-height-touch\);\s*--control-height-md: var\(--control-height-touch\);\s*--control-height-lg: var\(--control-height-touch\);/
    );
    // ボタンと入力欄の高さは同じ段の別名（タッチ端末でも同じ行の高さがそろう）。
    expect(spacing).toMatch(/--button-height-sm: var\(--control-height-sm\);/);
    expect(spacing).toMatch(/--button-height-md: var\(--control-height-md\);/);
    expect(spacing).toMatch(/--button-height-lg: var\(--control-height-lg\);/);
    expect(spacing).toMatch(/--field-height: var\(--control-height-md\);/);
    expect(spacing).toMatch(/--control-height-sm: 32px;\s*--control-height-md: 36px;\s*--control-height-lg: 40px;\s*--control-height-touch: 44px;/);
  });

  it("入力欄・選択欄の幅は 4 段（xs 8rem / sm 12rem / md 20rem / lg 28rem、#613）", () => {
    expect(read("tokens/spacing.css")).toMatch(
      /--field-width-xs: 8rem;\s*--field-width-sm: 12rem;\s*--field-width-md: 20rem;\s*--field-width-lg: 28rem;/
    );
  });

  it("フォームの本文の読みやすい最大幅は 60rem（#1891）", () => {
    expect(read("tokens/spacing.css")).toMatch(/--form-max-width: 60rem;/);
  });

  it("フォーム入力のフォーカスリングは外側へはみ出さない（内側 1px のリング）", () => {
    const base = read("tokens/base.css");
    const rule = base.match(
      /:is\(input, textarea, select\):not\(\[type="checkbox"\]\):not\(\[type="radio"\]\):focus-visible\s*\{([^}]*)\}/
    )?.[1];
    expect(rule).toMatch(/outline:\s*none;/);
    expect(rule).toMatch(/box-shadow:\s*inset 0 0 0 1px var\(--color-focus-ring\);/);
  });

  describe("淡い色の面（[data-surface-tint]）の上の文字は両テーマで 4.5:1 以上（platform #64 / #1671）", () => {
    const palette = read("tokens/palette.css");
    const a11y = read("tokens/a11y.css");
    const rgb = (name: string) => {
      const hex = palette.match(new RegExp(`--${name}:\\s*#([0-9a-f]{6});`, "i"))?.[1];
      if (!hex) throw new Error(`--${name} が palette.css に無い`);
      return [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
    };
    const luminance = (c: number[]) => {
      const [r, g, b] = c.map((v) => (v / 255 <= 0.03928 ? v / 255 / 12.92 : ((v / 255 + 0.055) / 1.055) ** 2.4));
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const contrast = (a: number[], b: number[]) => {
      const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
      return (hi + 0.05) / (lo + 0.05);
    };
    const mix = (a: number[], ratio: number, b: number[]) => a.map((v, i) => v * ratio + b[i] * (1 - ratio));
    /** colors.css の `--<token>: light-dark(var(--<light>), var(--<dark>));` から原色の名前を取る */
    const lightDark = (css: string, token: string) => {
      const match = css.match(new RegExp(`--${token}:\\s*light-dark\\(var\\(--([\\w-]+)\\),\\s*var\\(--([\\w-]+)\\)\\);`));
      if (!match) throw new Error(`--${token} の light-dark() が見つからない`);
      return { light: match[1], dark: match[2] };
    };
    const tintBlock = colors.match(/\[data-surface-tint\] \{([^}]*)\}/)?.[1] ?? "";

    // 淡い面の地。accent はライト blue-500 / ダーク blue-600 を surface（neutral-0 / neutral-900）に混ぜる（colors.css）。
    const surfaces = {
      light: {
        "accent-subtle": mix(rgb("blue-500"), 0.08, rgb("neutral-0")),
        "accent-muted": mix(rgb("blue-500"), 0.12, rgb("neutral-0")),
        "info-subtle": rgb(lightDark(colors, "color-info-subtle").light),
        "success-subtle": rgb(lightDark(colors, "color-success-subtle").light),
        "warning-subtle": rgb(lightDark(colors, "color-warning-subtle").light),
        "danger-subtle": rgb(lightDark(colors, "color-danger-subtle").light),
        // 灰色の面（hover の地を常に塗るチップなど）。fg-muted は 4.39:1 で、同じ [data-surface-tint="neutral"] で深くする。
        "surface-hover": rgb(lightDark(colors, "color-surface-hover").light),
      },
      dark: {
        "accent-subtle": mix(rgb("blue-600"), 0.16, rgb("neutral-900")),
        "accent-muted": mix(rgb("blue-600"), 0.22, rgb("neutral-900")),
        "info-subtle": rgb(lightDark(colors, "color-info-subtle").dark),
        "success-subtle": rgb(lightDark(colors, "color-success-subtle").dark),
        "warning-subtle": rgb(lightDark(colors, "color-warning-subtle").dark),
        "danger-subtle": rgb(lightDark(colors, "color-danger-subtle").dark),
        "surface-hover": rgb(lightDark(colors, "color-surface-hover").dark),
      },
    } as const;
    type Theme = keyof typeof surfaces;
    type Surface = keyof (typeof surfaces)["light"];

    /** [data-surface-tint="<tone>"] の内側で、各文字のトークンが解決する原色 */
    const tintedText = (tone: string, theme: Theme) => {
      const pick = (token: string) => lightDark(colors, token)[theme];
      return {
        fg: pick("color-fg"),
        "fg-muted": lightDark(tintBlock, "color-fg-muted")[theme],
        "accent-fg": pick("color-accent-fg-strong"),
        "info-fg": pick("color-info-fg"),
        "danger-fg": pick("color-danger-fg"),
        "success-fg": tone === "success" ? pick("color-success-fg") : pick("color-success-fg-strong"),
        "warning-fg": tone === "warning" ? pick("color-warning-fg") : pick("color-warning-fg-strong"),
      };
    };
    const toneOf = (surface: Surface) => (surface === "surface-hover" ? "neutral" : surface.split("-")[0]);

    it("[data-surface-tint] は副次・アクセントの文字を深くし、他の系統の success / warning を深い値にする", () => {
      expect(tintBlock).toMatch(/--color-fg-muted:\s*light-dark\(var\(--neutral-650\), var\(--neutral-350\)\);/);
      expect(tintBlock).toMatch(/--color-accent-fg:\s*var\(--color-accent-fg-strong\);/);
      expect(colors).toMatch(/--color-accent-fg-strong:\s*light-dark\(var\(--blue-650\), var\(--blue-300\)\);/);
      expect(colors).toMatch(
        /\[data-surface-tint\]:not\(\[data-surface-tint="success"\]\) \{ --color-success-fg: var\(--color-success-fg-strong\); \}/
      );
      expect(colors).toMatch(
        /\[data-surface-tint\]:not\(\[data-surface-tint="warning"\]\) \{ --color-warning-fg: var\(--color-warning-fg-strong\); \}/
      );
    });

    for (const theme of ["light", "dark"] as const) {
      for (const surface of Object.keys(surfaces[theme]) as Surface[]) {
        it(`${theme}: ${surface} の上の文字（fg / fg-muted / accent-fg / 状態色の fg）`, () => {
          for (const [text, color] of Object.entries(tintedText(toneOf(surface), theme))) {
            expect(contrast(rgb(color), surfaces[theme][surface]), `${text}（--${color}）on ${surface}`).toBeGreaterThanOrEqual(4.5);
          }
        });
      }
    }

    it("変更前の値（fg-muted = neutral-600、accent-fg = blue-500、warning-fg = amber-700）はライトで不足していた", () => {
      const { light } = surfaces;
      for (const surface of Object.keys(light) as Surface[]) {
        expect(contrast(rgb("neutral-600"), light[surface]), surface).toBeLessThan(4.5);
        if (surface !== "surface-hover") expect(contrast(rgb("blue-500"), light[surface]), surface).toBeLessThan(4.5);
      }
      expect(contrast(rgb("amber-700"), light["info-subtle"])).toBeLessThan(4.5);
      expect(contrast(rgb("green-700"), light["danger-subtle"])).toBeLessThan(4.5);
    });

    it("prefers-contrast: more の値（[data-surface-tint] も対象）も淡い面の上で 4.5:1 以上", () => {
      const block = a11y.match(/:root, \[data-theme\], \[data-surface-tint\] \{([^}]*)\}/)?.[1] ?? "";
      for (const theme of ["light", "dark"] as const) {
        for (const token of ["color-fg-muted", "color-accent-fg"]) {
          const color = lightDark(block, token)[theme];
          for (const [surface, bg] of Object.entries(surfaces[theme])) {
            expect(contrast(rgb(color), bg), `${theme} ${token} on ${surface}`).toBeGreaterThanOrEqual(4.5);
          }
        }
      }
    });

    it("面のトークン一式は宣言し直さず、a11y の応答層（forced-colors / prefers-contrast）は対象に含める", () => {
      expect(colors).not.toMatch(/\[data-surface\], \[data-surface-tint\]/);
      expect(a11y.match(/:root, \[data-theme\], \[data-surface\], \[data-surface-tint\] \{/g)).toHaveLength(2);
      expect(a11y).toMatch(/:root, \[data-theme\], \[data-surface-tint\] \{\s*--color-fg-muted:/);
      // :not() の分だけ詳細度が高い success / warning の上書きも、強制カラーモードではシステム色になる
      expect(a11y).toMatch(/--color-success-fg-strong: CanvasText; --color-warning-fg-strong: CanvasText;/);
    });
  });

  it(".dark に色値の手書き宣言を持たない（テーマは light-dark() で解決する）", () => {
    for (const file of ["tokens.css", "tokens/colors.css", "tokens/base.css"]) {
      expect(read(file)).not.toMatch(/\.dark\s*\{[^}]*#[0-9a-f]{3,8}/i);
    }
  });
});
