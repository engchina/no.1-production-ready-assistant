import { afterEach, describe, expect, it, vi } from "vitest";

import type { ThemePreference } from "../src/store/ui-store";
import { initTheme, resolveDark } from "../src/theme";

// vitest は node 環境で動くため、テーマ反映に必要な最小限の window / document を差し込む。
function installDom(systemDark: boolean) {
  const classes = new Set<string>();
  const mqlListeners: Array<() => void> = [];
  const mql = {
    matches: systemDark,
    addEventListener: (_: string, fn: () => void) => mqlListeners.push(fn),
  };
  vi.stubGlobal("window", {
    matchMedia: () => mql,
    getComputedStyle: () => ({ color: "" }),
    setTimeout: (fn: () => void) => fn(),
  });
  vi.stubGlobal("document", {
    documentElement: {
      classList: {
        contains: (c: string) => classes.has(c),
        toggle: (c: string, on: boolean) => (on ? classes.add(c) : classes.delete(c)),
      },
    },
    head: { appendChild: () => undefined },
    createElement: () => ({ textContent: "", remove: () => undefined }),
  });
  return {
    isDark: () => classes.has("dark"),
    setSystemDark: (value: boolean) => {
      mql.matches = value;
      mqlListeners.forEach((fn) => fn());
    },
  };
}

function createStore(initial: ThemePreference) {
  let state = { theme: initial };
  const listeners: Array<(s: typeof state) => void> = [];
  return {
    getState: () => state,
    subscribe: (fn: (s: typeof state) => void) => listeners.push(fn),
    setTheme: (theme: ThemePreference) => {
      state = { theme };
      listeners.forEach((fn) => fn(state));
    },
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("resolveDark", () => {
  it("light / dark は OS 設定に関係なく固定、system は OS 設定に追従する", () => {
    installDom(true);
    expect(resolveDark("light")).toBe(false);
    expect(resolveDark("dark")).toBe(true);
    expect(resolveDark("system")).toBe(true);
    installDom(false);
    expect(resolveDark("system")).toBe(false);
  });
});

describe("initTheme", () => {
  it("初期値を反映し、ストアの変更と OS 設定の変更（system のときだけ）に追従する", () => {
    const dom = installDom(false);
    const store = createStore("dark");
    initTheme(store);
    expect(dom.isDark()).toBe(true);

    store.setTheme("light");
    expect(dom.isDark()).toBe(false);

    // light 固定中は OS がダークになっても変えない
    dom.setSystemDark(true);
    expect(dom.isDark()).toBe(false);

    store.setTheme("system");
    expect(dom.isDark()).toBe(true);
    dom.setSystemDark(false);
    expect(dom.isDark()).toBe(false);
  });
});
