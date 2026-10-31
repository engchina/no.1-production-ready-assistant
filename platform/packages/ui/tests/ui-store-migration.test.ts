import { afterEach, expect, it, vi } from "vitest";
import { createUiStore } from "../src/store/ui-store";

afterEach(() => vi.unstubAllGlobals());
it("ナビの旧 key を移して現在の設定を保持し、廃止した配色テーマ（#17）は読み捨てる", () => {
  const items = new Map<string, string>();
  const localStorage = {
    getItem: (key: string) => items.get(key) ?? null,
    setItem: (key: string, value: string) => { items.set(key, value); },
    removeItem: (key: string) => { items.delete(key); },
  };
  vi.stubGlobal("window", { localStorage, matchMedia: () => ({ matches: false }) });
  localStorage.setItem("rag-test", JSON.stringify({ state: {
    sidebarCollapsed: true, theme: "dark",
    collapsedSections: { "nav.section.rag": true, other: true },
  }, version: 0 }));
  const store = createUiStore({ storageKey: "rag-test", sectionKeyMigrations: { "nav.section.rag": "nav.section.use" } });
  expect(store.getState().collapsedSections).toEqual({ "nav.section.use": true, other: true });
  expect(store.getState()).not.toHaveProperty("theme");
  expect(store.getState().sidebarCollapsed).toBe(true);
  store.getState().setSectionCollapsed("nav.section.use", false);
  expect(localStorage.getItem("rag-test")).not.toContain("nav.section.rag");
  expect(localStorage.getItem("rag-test")).not.toContain("theme");
});
