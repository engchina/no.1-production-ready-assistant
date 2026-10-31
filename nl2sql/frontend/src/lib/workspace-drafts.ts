/** 同一タブ専用の草稿。保存対象は各画面が明示登録した入力・表示条件・最小スナップショットだけ。 */
export const WORKSPACE_DRAFT_PREFIX = "production-ready-nl2sql.draft.v1:";
export const WORKSPACE_DRAFT_TTL_MS = 8 * 60 * 60 * 1000;
const OWNER_KEY = `${WORKSPACE_DRAFT_PREFIX}owner`;
export interface DraftStorage {
  length: number;
  key(index: number): string | null;
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}
export function clearWorkspaceDrafts(storage: DraftStorage): void {
  for (let index = storage.length - 1; index >= 0; index -= 1) {
    const key = storage.key(index);
    if (key?.startsWith(WORKSPACE_DRAFT_PREFIX)) storage.removeItem(key);
  }
}
export function bindWorkspaceOwner(storage: DraftStorage, owner: string): void {
  if (storage.getItem(OWNER_KEY) !== owner) clearWorkspaceDrafts(storage);
  storage.setItem(OWNER_KEY, owner);
}
export function isWorkspaceOwner(storage: Pick<DraftStorage, "getItem">, owner: string): boolean {
  return Boolean(owner) && storage.getItem(OWNER_KEY) === owner;
}
export function draftKey(owner: string, context: string, page: string, field: string): string {
  return WORKSPACE_DRAFT_PREFIX + JSON.stringify([owner, context, page, field]);
}
function compatible(value: unknown, initial: unknown): boolean {
  if (initial === null) return value === null;
  if (Array.isArray(initial)) return Array.isArray(value) && value.every((item) => typeof item === "string");
  if (typeof initial === "object") {
    if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
    return Object.entries(initial).every(([key, sample]) => compatible((value as Record<string, unknown>)[key], sample));
  }
  return typeof value === typeof initial && (typeof value !== "number" || Number.isFinite(value));
}
export function readDraft<T>(storage: DraftStorage, key: string, initial: T, now = Date.now()): T {
  try {
    const raw = storage.getItem(key);
    if (!raw) return initial;
    const data = JSON.parse(raw);
    if (typeof data.at !== "number" || data.at > now || now - data.at >= WORKSPACE_DRAFT_TTL_MS || !compatible(data.value, initial)) {
      storage.removeItem(key);
      return initial;
    }
    return data.value as T;
  } catch {
    return initial;
  }
}
export function writeDraft(storage: DraftStorage, owner: string, key: string, value: unknown, now = Date.now()): boolean {
  try {
    // logout 後の非同期完了による草稿の復活も拒否する。
    if (!isWorkspaceOwner(storage, owner)) return false;
    const encoded = JSON.stringify({ at: now, value });
    if (encoded.length > 200_000) return false;
    storage.setItem(key, encoded);
    return true;
  } catch {
    return false;
  }
}

/** 草稿の置き場所（利用者・DB・画面・field。`draftKey` の要素）。 */
export interface DraftScope {
  owner: string;
  context: string;
  page: string;
  field: string;
}

/** field が変わったときに、変わる前の値を新しい field へ引き継ぐかの判定（`useWorkspaceState` の `carryOver`）。 */
export type DraftCarryOver<T> = (change: { from: string; to: string; value: T; stored: T }) => boolean;

/**
 * 草稿の置き場所が変わった render の値（#3357）。新しい置き場所の草稿（`stored`）を読み直すのが既定で、
 * 利用者・DB・画面が同じまま field だけが変わり、`carryOver` が認めたときだけ、変わる前の値（`value`。その render の
 * 最新の値で、同じ render で起きた入力を含む）を引き継ぐ。利用者・DB・画面をまたいでは引き継がない。
 */
export function draftValueOnScopeChange<T>(
  previous: DraftScope,
  next: DraftScope,
  value: T,
  stored: T,
  carryOver?: DraftCarryOver<T>
): T {
  const sameBoundary =
    previous.owner === next.owner && previous.context === next.context && previous.page === next.page;
  if (!carryOver || !sameBoundary || previous.field === next.field) return stored;
  return carryOver({ from: previous.field, to: next.field, value, stored }) ? value : stored;
}
