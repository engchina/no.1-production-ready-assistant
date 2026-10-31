import type { ThemePreference } from "./store/ui-store";

/**
 * テーマ選好（light / dark / system）を `<html>` に反映する。
 *
 * 反映は `<html class="dark">`（デザインシステムの互換 API。`color-scheme` と `light-dark()` トークンを
 * 共有 CSS の `.dark` が切り替える）。NL2SQL の lib/theme.ts から移設した（#95）。
 */

const DARK_QUERY = "(prefers-color-scheme: dark)";

/**
 * テーマ切替の瞬間だけ CSS transition を止める。
 * 行・ボタン等の `transition-colors`（150ms）が旧テーマの色から補間し、ダークへ切り替えた直後に
 * 明るい地と暗い地の中間色が一瞬描かれる（NL2SQL #571）。テーマは状態の切替であって動きではないため、
 * 補間せずに一度で切り替える。
 */
const THEME_SWITCH_STYLE = "*,*::before,*::after{transition:none!important}";

/** `initTheme` が必要とする最小のストア契約（`createUiStore` の戻り値がそのまま渡せる）。 */
export interface ThemeStore {
  getState: () => { theme: ThemePreference };
  subscribe: (listener: (state: { theme: ThemePreference }) => void) => unknown;
}

function prefersDark(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia(DARK_QUERY).matches
    : false;
}

/** テーマ選好を実効値（dark かどうか）へ解決する。"system" は OS 設定に追従。 */
export function resolveDark(pref: ThemePreference): boolean {
  if (pref === "dark") return true;
  if (pref === "light") return false;
  return prefersDark();
}

/** テーマ選好を `<html>` に反映する。実効値が変わらない場合は何もしない。 */
export function applyTheme(pref: ThemePreference): void {
  if (typeof document === "undefined") return;
  const dark = resolveDark(pref);
  const root = document.documentElement;
  if (root.classList.contains("dark") === dark) return;
  const pause = document.createElement("style");
  pause.textContent = THEME_SWITCH_STYLE;
  document.head.appendChild(pause);
  root.classList.toggle("dark", dark);
  // transition を止めたまま新しいテーマの computed style を確定させてから戻す。
  void window.getComputedStyle(root).color;
  window.setTimeout(() => pause.remove(), 1);
}

/**
 * 永続化されたテーマ選好を即時適用し、ストアの変更と OS 設定の変更を購読する。
 * FOUC を避けるため、各アプリの main.tsx から React の描画前に1回だけ呼ぶ。
 */
export function initTheme(store: ThemeStore): void {
  applyTheme(store.getState().theme);
  store.subscribe((state) => applyTheme(state.theme));
  if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
    window.matchMedia(DARK_QUERY).addEventListener("change", () => {
      if (store.getState().theme === "system") applyTheme("system");
    });
  }
}
