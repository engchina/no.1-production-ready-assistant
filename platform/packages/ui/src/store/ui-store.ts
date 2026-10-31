import { create } from "zustand";
import { createJSONStorage, persist, type StateStorage } from "zustand/middleware";

/**
 * サイドナビの開閉状態（折りたたみ / セクション開閉）を保持する UI ストアの「ファクトリ」。
 *
 * アプリごとに永続化キーが異なる（RAG / NL2SQL / Agent で別 localStorage namespace）ため、
 * `createUiStore({ storageKey })` で各アプリが自分のインスタンスを生成する。
 * パッケージ内の Sidebar はストアに依存せず props で値を受け取るので、
 * このストアは「アプリ側の利便フック」という位置づけ。
 */

/** 配色テーマ。"system" は OS の prefers-color-scheme に追従。既定は "light"。 */
export type ThemePreference = "light" | "dark" | "system";

export interface UiState {
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (collapsed: boolean) => void;
  toggleSidebarCollapsed: () => void;
  /**
   * サイドナビのセクション折りたたみの保存値。キーは NavSection.key、true で折りたたむ。
   * 未指定（キー無し）のセクションは NavSection.defaultCollapsed（既定は展開）に従う（#1563）。
   * 開閉の切り替えは既定を知る Sidebar が `setSectionCollapsed` で保存する。
   */
  collapsedSections: Record<string, boolean>;
  setSectionCollapsed: (key: string, collapsed: boolean) => void;
  /** 配色テーマの選好（永続化）。適用（html.dark 付与）はアプリ側で行う。 */
  theme: ThemePreference;
  setTheme: (theme: ThemePreference) => void;
}

export interface CreateUiStoreOptions {
  /** localStorage の永続化キー（アプリごとに一意）。 */
  storageKey: string;
  /** セクション改名の対応（旧 key → 現行 key）。既存の折りたたみ状態を保持する。 */
  sectionKeyMigrations?: Readonly<Record<string, string>>;
  /**
   * 旧バージョンが単独で持っていた「サイドバー折りたたみ真偽値」の localStorage キー。
   * 指定すると初期値を移行する（任意）。
   */
  legacyCollapsedKey?: string;
  /** 初期状態でモバイル幅なら畳む判定の閾値（px, 既定 640）。 */
  mobileBreakpoint?: number;
}

const memoryStorage = new Map<string, string>();

const fallbackStorage: StateStorage = {
  getItem: (name) => memoryStorage.get(name) ?? null,
  setItem: (name, value) => memoryStorage.set(name, value),
  removeItem: (name) => memoryStorage.delete(name),
};

function resolveStorage(): StateStorage {
  if (typeof window === "undefined") {
    return fallbackStorage;
  }
  return window.localStorage;
}

function initialSidebarCollapsed(options: CreateUiStoreOptions): boolean {
  if (typeof window === "undefined") {
    return false;
  }
  const breakpoint = options.mobileBreakpoint ?? 640;
  if (window.matchMedia(`(max-width: ${breakpoint}px)`).matches) {
    return true;
  }
  if (options.legacyCollapsedKey) {
    return window.localStorage.getItem(options.legacyCollapsedKey) === "true";
  }
  return false;
}

export function createUiStore(options: CreateUiStoreOptions) {
  // 戻り値の型は推論に委ねる（persist ミドルウェアが付与する `.persist` API を保持するため）。
  return create<UiState>()(
    persist(
      (set) => ({
        sidebarCollapsed: initialSidebarCollapsed(options),
        setSidebarCollapsed: (collapsed) => set({ sidebarCollapsed: collapsed }),
        toggleSidebarCollapsed: () =>
          set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
        collapsedSections: {},
        setSectionCollapsed: (key, collapsed) =>
          set((state) => ({
            collapsedSections: { ...state.collapsedSections, [key]: collapsed },
          })),
        theme: "light",
        setTheme: (theme) => set({ theme }),
      }),
      {
        name: options.storageKey,
        merge: (persisted, current) => {
          const saved = persisted && typeof persisted === "object"
            ? persisted as Partial<UiState> : {};
          const collapsedSections = { ...saved.collapsedSections };
          for (const [oldKey, newKey] of Object.entries(options.sectionKeyMigrations ?? {})) {
            if (Object.hasOwn(collapsedSections, oldKey)) {
              if (!Object.hasOwn(collapsedSections, newKey)) collapsedSections[newKey] = collapsedSections[oldKey];
              delete collapsedSections[oldKey];
            }
          }
          return { ...current, ...saved, collapsedSections };
        },
        partialize: (state) => ({
          sidebarCollapsed: state.sidebarCollapsed,
          collapsedSections: state.collapsedSections,
          theme: state.theme,
        }),
        storage: createJSONStorage(resolveStorage),
      }
    )
  );
}
