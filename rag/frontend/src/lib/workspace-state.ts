import { useEffect, useState, type Dispatch, type SetStateAction } from "react";

/**
 * ページ遷移・再読込のときの作業状態（platform `docs/ux-contracts/workspace-state.md`）。
 *
 * 同じタブの sessionStorage に、allowlist の field だけを期限付きで保存する。
 * 秘密情報・サーバー応答の全体・結果行・破壊的な選択は登録しない。
 * storage が使えない環境でも画面は初期値で動く（すべての access を try/catch で囲む）。
 */
export const WORKSPACE_NAMESPACE = "production-ready-rag.workspace.v1:";
const OWNER_KEY = `${WORKSPACE_NAMESPACE}owner`;
/** 最終保存からの有効期限。 */
export const WORKSPACE_TTL_MS = 8 * 60 * 60 * 1000;
/** 1 field の JSON の上限（評価リクエストのケースなどの大きな入力で storage を圧迫しない）。 */
const MAX_JSON_CHARS = 200_000;

/** 保存してよい field の allowlist。新しい field はここへ明示登録する。 */
export type WorkspaceField =
  | "search.query"
  | "search.searchAnswerProfileId"
  | "search.classification"
  | "search.extractionFields"
  | "search.advancedOpen"
  | "search.generateAnswer"
  | "chat.searchAnswerProfileId"
  | "chat.conversationId"
  | "chat.composer"
  | "chat.conversationsPage"
  | "chat.historyOpen"
  | "chat.faqChoice"
  | "chat.clarifyChoice"
  | "fileList.view"
  | "knowledgeBases.view"
  | "knowledgeBases.documentsPage"
  | "knowledgeBases.draft"
  | "searchAnswerProfiles.view"
  | "searchAnswerProfiles.draft"
  // 品質評価（#2065）: 検証した評価リクエストの Excel（ケースとプレビュー。200,000 文字を超えると
  // 保存しない）、KB スコープ、評価の job id と、その評価のケースの見出し（結果の表の質問・根拠）。
  | "evaluation.import"
  | "evaluation.profileId"
  | "evaluation.runProfileName"
  | "evaluation.runInput"
  | "evaluation.runJobId"
  | "evaluation.runCaseLabels"
  | "evaluation.compareEnabled"
  | "evaluation.compareProfileId"
  | "evaluation.runCompareName"
  | "evaluation.processDrafts"
  | "evaluation.useProcessDrafts"
  // 判定の校正（#3578）: 選んだ記入の集合・手順のタブ・記入の観点と状態の絞り込み・校正の job id。
  | "evaluation.calibration.setsPaging"
  | "evaluation.calibration.labelSetId"
  | "evaluation.calibration.step"
  | "evaluation.calibration.aspect"
  | "evaluation.calibration.rowFilter"
  | "evaluation.calibration.jobId"
  | "feedback.search"
  | "history.view"
  | "language-releases.paging"
  // クライアント側で全件を持つ一覧のページと表示件数（#1891。lib/list-paging.ts の useClientListPaging）
  | "evaluation.casesPaging"
  | "evaluation.previewPaging"
  | "supportGuides.includeArchived"
  | "supportGuides.listPaging"
  | "supportGuides.revisionsPaging"
  | "runtimeKnowledge.listPaging"
  | "approvedFaqs.listPaging"
  | "documentCategories.searchPaging";

interface StoredRecord {
  value: unknown;
  savedAt: number;
}

function storage(): Storage | null {
  try {
    const store = window.sessionStorage;
    migrateWorkspaceNames(store);
    return store;
  } catch {
    return null;
  }
}

/** 更新境界にだけ旧名を置き、owner / TTL / scoped ID / 自由入力は変えない。 */
function migrateWorkspaceNames(store: Storage): void {
  store.removeItem(`${WORKSPACE_NAMESPACE}search.topK`);
  const migrations: Record<string, string> = {
    "search.businessViewId": "search.searchAnswerProfileId",
    "chat.businessViewId": "chat.searchAnswerProfileId",
    "businessViews.view": "searchAnswerProfiles.view",
    "businessViews.draft": "searchAnswerProfiles.draft",
  };
  const keys: string[] = [];
  for (let index = 0; index < store.length; index += 1) {
    const key = store.key(index);
    if (key) keys.push(key);
  }
  for (const key of keys) {
    for (const [oldField, newField] of Object.entries(migrations)) {
      const oldPrefix = `${WORKSPACE_NAMESPACE}${oldField}`;
      if (key !== oldPrefix && !key.startsWith(`${oldPrefix}:`)) continue;
      const next = `${WORKSPACE_NAMESPACE}${newField}${key.slice(oldPrefix.length)}`;
      const raw = store.getItem(key);
      if (raw != null && store.getItem(next) == null) store.setItem(next, raw);
      store.removeItem(key);
    }
  }
}

function storageKey(field: WorkspaceField, scope?: string) {
  return `${WORKSPACE_NAMESPACE}${field}${scope ? `:${scope}` : ""}`;
}

/** 保存値を読む。期限切れ・型不一致・壊れた JSON は初期値にして消す。 */
export function readWorkspace<T>(
  field: WorkspaceField,
  fallback: T,
  isValid: (value: unknown) => value is T = (value): value is T => sameShape(value, fallback),
  scope?: string
): T {
  const store = storage();
  if (!store) return fallback;
  const key = storageKey(field, scope);
  try {
    const raw = store.getItem(key);
    if (raw == null) return fallback;
    const record = JSON.parse(raw) as StoredRecord;
    if (
      !record ||
      typeof record.savedAt !== "number" ||
      Date.now() - record.savedAt > WORKSPACE_TTL_MS ||
      !isValid(record.value)
    ) {
      store.removeItem(key);
      return fallback;
    }
    return record.value;
  } catch {
    try {
      store.removeItem(key);
    } catch {
      // storage が使えない場合は何もしない。
    }
    return fallback;
  }
}

/** 値を保存する。保存できなかった場合は false（上限超過・storage 無効）。 */
export function writeWorkspace(field: WorkspaceField, value: unknown, scope?: string): boolean {
  const store = storage();
  if (!store) return false;
  try {
    const json = JSON.stringify({ value, savedAt: Date.now() } satisfies StoredRecord);
    if (json.length > MAX_JSON_CHARS) {
      store.removeItem(storageKey(field, scope));
      return false;
    }
    store.setItem(storageKey(field, scope), json);
    return true;
  } catch {
    return false;
  }
}

export function removeWorkspace(field: WorkspaceField, scope?: string): void {
  try {
    storage()?.removeItem(storageKey(field, scope));
  } catch {
    // storage が使えない場合は何もしない。
  }
}

/** namespace 配下の一時保存をすべて消す（logout・ユーザー切替）。 */
export function clearWorkspace(): void {
  const store = storage();
  if (!store) return;
  try {
    const keys: string[] = [];
    for (let index = 0; index < store.length; index += 1) {
      const key = store.key(index);
      if (key?.startsWith(WORKSPACE_NAMESPACE)) keys.push(key);
    }
    keys.forEach((key) => store.removeItem(key));
  } catch {
    // storage が使えない場合は何もしない。
  }
}

/** 保存した作業状態を今のユーザーに結び付ける。別ユーザーの状態は使い回さずに消す。 */
export function bindWorkspaceOwner(owner: string): void {
  const store = storage();
  if (!store) return;
  try {
    if (store.getItem(OWNER_KEY) === owner) return;
    clearWorkspace();
    store.setItem(OWNER_KEY, owner);
  } catch {
    // storage が使えない場合は何もしない。
  }
}

/**
 * useState と同じ使い方で、値を sessionStorage に保存・復元する。
 * 保存できない間に初期値から変わった入力は、`beforeunload` で再読込・タブを閉じる操作を確認する。
 */
export function useWorkspaceState<T>(
  field: WorkspaceField,
  initial: T,
  isValid?: (value: unknown) => value is T,
  /** URL の deep-link など、保存値より優先する初期値。undefined なら保存値を使う。 */
  override?: T
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() =>
    override !== undefined ? override : readWorkspace(field, initial, isValid)
  );
  const unchanged = JSON.stringify(value) === JSON.stringify(initial);
  // 値を保存し、保存できず初期値から変わっていれば、再読込・タブを閉じる操作を確認する。
  // 保存の成否は state に持たず、同じ effect で確認の登録まで決める。
  useEffect(() => {
    const saved = writeWorkspace(field, value);
    if (saved || unchanged) return;
    const protect = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [field, value, unchanged]);
  return [value, setValue];
}

/** 保存値が選択肢のどれかか（古い版・改ざんした値は初期値に戻す）。 */
export function isOneOf<T>(options: readonly T[]) {
  return (value: unknown): value is T => (options as readonly unknown[]).includes(value);
}

export function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

/** 初期値と同じ形か（string / number / boolean / null / 配列 / object の区別）。 */
export function sameShape(value: unknown, fallback: unknown): boolean {
  if (fallback === null) return value === null || typeof value === "string";
  if (Array.isArray(fallback)) {
    return (
      Array.isArray(value) &&
      (fallback.length === 0
        ? value.every((item) => typeof item === "string")
        : value.every((item) => typeof item === typeof fallback[0]))
    );
  }
  if (typeof fallback === "object") {
    return (
      typeof value === "object" &&
      value !== null &&
      !Array.isArray(value) &&
      Object.keys(fallback as object).every((key) =>
        sameShape((value as Record<string, unknown>)[key], (fallback as Record<string, unknown>)[key])
      )
    );
  }
  return typeof value === typeof fallback;
}
