import { useCallback, useState } from "react";
import { DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS } from "@production-ready/ui";

/**
 * 共通の画面の一覧のページ番号と表示件数の作業状態（UX 契約 workspace-state.md・#1891）。
 *
 * - 保存先は同じタブの `sessionStorage` だけ（ブラウザを閉じれば消える）。保存するのはページ番号と表示件数だけで、
 *   一覧の中身・検索語・秘密は保存しない。
 * - 利用者が変わったら（ログアウト・別の利用者のログイン）消す（`AuthProvider` が `bindListPagingOwner` を呼ぶ）。
 * - storage が使えない環境（プライベートモード等）でも既定値（1 ページ目・25 件）で動く（読み書きはすべて try/catch）。
 * - 製品の作業状態（`production-ready-<製品>.workspace.v1:`）とは別の namespace（3 製品の共通の画面で同じ key）。
 */
export const LIST_PAGING_NAMESPACE = "production-ready-platform.list-paging.v1:";

/** 作業状態に残す一覧の名前。 */
export type ListPagingKey = "users" | "roles" | "permissionRoles" | "apiKeysAdmin" | "myApiKeys";

interface StoredPaging {
  page: number;
  pageSize: number;
}

const DEFAULT_PAGING: StoredPaging = { page: 1, pageSize: DEFAULT_PAGE_SIZE };

function isPageSize(value: unknown): value is number {
  return typeof value === "number" && (PAGE_SIZE_OPTIONS as readonly number[]).includes(value);
}

function isPage(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1;
}

export function readListPaging(key: ListPagingKey): StoredPaging {
  try {
    const raw = window.sessionStorage.getItem(`${LIST_PAGING_NAMESPACE}${key}`);
    if (!raw) return DEFAULT_PAGING;
    const value = JSON.parse(raw) as Partial<StoredPaging> | null;
    return {
      page: isPage(value?.page) ? value.page : DEFAULT_PAGING.page,
      pageSize: isPageSize(value?.pageSize) ? value.pageSize : DEFAULT_PAGING.pageSize,
    };
  } catch {
    return DEFAULT_PAGING;
  }
}

function writeListPaging(key: ListPagingKey, value: StoredPaging) {
  try {
    const storageKey = `${LIST_PAGING_NAMESPACE}${key}`;
    if (value.page === DEFAULT_PAGING.page && value.pageSize === DEFAULT_PAGING.pageSize) {
      window.sessionStorage.removeItem(storageKey);
    } else {
      window.sessionStorage.setItem(storageKey, JSON.stringify(value));
    }
  } catch {
    // storage が使えない環境では保存しない（画面の state だけで動く）。
  }
}

/** 共通の画面の一覧の作業状態をすべて消す（利用者が変わったとき）。 */
export function clearListPagingState() {
  try {
    const keys: string[] = [];
    for (let index = 0; index < window.sessionStorage.length; index += 1) {
      const key = window.sessionStorage.key(index);
      if (key?.startsWith(LIST_PAGING_NAMESPACE)) keys.push(key);
    }
    keys.forEach((key) => window.sessionStorage.removeItem(key));
  } catch {
    // storage が使えない環境では消すものもない。
  }
}

const OWNER_KEY = `${LIST_PAGING_NAMESPACE}owner`;

/**
 * 作業状態を今の利用者（`identityKey`。未認証は空）に結び付ける。前回の持ち主と違えば先に消す（再読込では消さない）。
 */
export function bindListPagingOwner(owner: string) {
  try {
    if (window.sessionStorage.getItem(OWNER_KEY) === owner) return;
    clearListPagingState();
    if (owner) window.sessionStorage.setItem(OWNER_KEY, owner);
  } catch {
    // storage が使えない環境では保存もしないため、結び付けるものもない。
  }
}

/**
 * 一覧のページ番号と表示件数（作業状態に残す）。一覧 → 別の画面 → 一覧、再読込でも同じページ・件数に戻る。
 * 表示件数を変えたら 1 ページ目へ戻す。行が減って範囲外になったページは、部品（`usePagination`）が表示だけ末尾に寄せる。
 */
export function usePersistedListPaging(key: ListPagingKey) {
  const [state, setState] = useState<StoredPaging>(() => readListPaging(key));
  const setPage = useCallback(
    (page: number) => {
      setState((current) => {
        const next = { ...current, page: Math.max(1, Math.floor(page)) };
        writeListPaging(key, next);
        return next;
      });
    },
    [key]
  );
  const setPageSize = useCallback(
    (pageSize: number) => {
      setState(() => {
        const next = { page: 1, pageSize: isPageSize(pageSize) ? pageSize : DEFAULT_PAGE_SIZE };
        writeListPaging(key, next);
        return next;
      });
    },
    [key]
  );
  return { page: state.page, pageSize: state.pageSize, setPage, setPageSize };
}
