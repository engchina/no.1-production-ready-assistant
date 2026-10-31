import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { create } from "zustand";
import { Banner, useListDetailSplit, useUrlSelection, type UrlSelection, DISPLAY_DATE_TIME_SECONDS, formatDisplayDateTime } from "@production-ready/ui";
import { useAuth } from "@/features/security/AuthProvider";
import { t } from "@/lib/i18n";
import { draftKey, draftValueOnScopeChange, readDraft, writeDraft, type DraftCarryOver, type DraftScope } from "@/lib/workspace-drafts";
import { FILL_HEIGHT_ROUTES } from "@/lib/routes";

const WorkspaceContext = createContext("legacy");
const ActivityContext = createContext({ active: true, page: "" });
const useDraftFailures = create<{ keys: Set<string>; mark: (key: string, failed: boolean) => void }>((set) => ({
  keys: new Set(),
  mark: (key, failed) => set((state) => {
    if (state.keys.has(key) === failed) return state;
    const keys = new Set(state.keys);
    if (failed) keys.add(key); else keys.delete(key);
    return { keys };
  }),
}));

// チャットの画面（FILL_HEIGHT_ROUTES）は、入れ物が本文（AppShell の main）の高さを画面まで渡す。渡さないと
// 入れ物が内容の高さに伸び、会話の欄ではなくページ全体がスクロールする（#2332）。
const FILL_HEIGHT_CLASS = "flex min-h-0 flex-1 flex-col";

export function WorkspaceBoundary({ contextId, children }: { contextId: string; children: ReactNode }) {
  const { pathname } = useLocation();
  return <WorkspaceContext.Provider value={contextId}><div key={contextId} className={FILL_HEIGHT_ROUTES.has(pathname) ? FILL_HEIGHT_CLASS : undefined}>{children}</div></WorkspaceContext.Provider>;
}
export function WorkspacePage({ active, page, children }: { active: boolean; page: string; children: ReactNode }) {
  return <ActivityContext.Provider value={{ active, page }}><div hidden={!active} className={active && FILL_HEIGHT_ROUTES.has(page) ? FILL_HEIGHT_CLASS : undefined}>{children}</div></ActivityContext.Provider>;
}
export function useWorkspaceActive() { return useContext(ActivityContext).active; }

/** タブ内の一時保存は草稿と同じユーザー・DB 境界に合わせる。 */
export function useWorkspaceIdentity() {
  const { user } = useAuth();
  return { owner: user?.user_uuid ?? "", context: useContext(WorkspaceContext) };
}

/** keep-alive 内でも離脱時に確認・破壊的選択だけを破棄する。 */
export function useResetExecutionConsent(reset: () => void, signature: string) {
  const active = useWorkspaceActive();
  const latest = useRef(reset);
  // 最新の reset を commit 時に入れる（render 中に ref を書かない）。
  useLayoutEffect(() => { latest.current = reset; });
  useEffect(() => { latest.current(); }, [active, signature]);
}

export interface WorkspaceStateOptions<T> {
  /**
   * field が変わった render で、変わる前の値を新しい field へ引き継ぐか（#3357）。利用者・DB・画面が変わったときは
   * 呼ばない。判定はその render の最新の値で行うので、field の変わる render と同じ render で起きた入力も失わない。
   */
  carryOver?: DraftCarryOver<T>;
}

export function useWorkspaceState<T>(
  field: string,
  initial: T,
  options: WorkspaceStateOptions<T> = {}
): [T, Dispatch<SetStateAction<T>>] {
  const { user } = useAuth();
  const context = useContext(WorkspaceContext);
  const activity = useContext(ActivityContext);
  const { pathname } = useLocation();
  const owner = user?.user_uuid ?? "";
  const page = activity.page || pathname;
  const key = draftKey(owner, context, page, field);
  const mark = useDraftFailures((state) => state.mark);
  const read = () => { try { return readDraft(window.sessionStorage, key, initial); } catch { return initial; } };
  const [value, setValue] = useState<T>(read);
  // key が変わった render で、その key の草稿を読み直す（前回の置き場所は state で持ち、render 中に ref を読まない）。
  // 引き継ぎ（`carryOver`）は、この render の最新の値で判定する（#3357）。
  const [previous, setPrevious] = useState<DraftScope & { key: string }>({ key, owner, context, page, field });
  if (previous.key !== key) {
    const next = { owner, context, page, field };
    setPrevious({ key, ...next });
    setValue(draftValueOnScopeChange(previous, next, value, read(), options.carryOver));
  }
  useEffect(() => {
    let saved = false;
    try { saved = writeDraft(window.sessionStorage, owner, key, value); } catch { /* storage disabled */ }
    mark(key, !saved);
    return () => mark(key, false);
  }, [key, mark, owner, value]);
  // setter は effect（commit の後）を待たずに草稿を書く（#3385）。ページを離れる直前（pagehide。SearchField が
  // 待っている検索語を確定するときなど）の更新は commit されないまま文書が破棄されるため、effect だけでは残らない。
  // 最新の値と置き場所は commit 時に入れ、続けて呼ばれた setter は直前の setter の値から計算する。
  const latest = useRef({ key, owner, value });
  useLayoutEffect(() => { latest.current = { key, owner, value }; });
  const update = useCallback<Dispatch<SetStateAction<T>>>((action) => {
    const current = latest.current;
    const next = typeof action === "function" ? (action as (previous: T) => T)(current.value) : action;
    latest.current = { ...current, value: next };
    setValue(next);
    try { writeDraft(window.sessionStorage, current.owner, current.key, next); } catch { /* storage disabled */ }
  }, [setValue]);
  return [value, update];
}

export function WorkspaceDraftWarning() {
  const failed = useDraftFailures((state) => state.keys.size > 0);
  useEffect(() => {
    if (!failed) return;
    const protect = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [failed]);
  return failed ? <div className="px-4 pt-4"><Banner severity="warning">{t("workspace.storageFailed")}</Banner></div> : null;
}

/** 再表示時は stale な読み取り query のみ更新する。生成・実行 mutation は再送しない。 */
export function useWorkspaceRevalidation() {
  const active = useWorkspaceActive();
  const queryClient = useQueryClient();
  const visited = useRef(false);
  useEffect(() => {
    if (active && visited.current) void queryClient.refetchQueries({
      type: "active", stale: true,
      // job は既存 coordinator に任せ、完了済み job を復帰時に再ポーリングしない。
      predicate: (query) => query.queryKey[0] === "schema"
        ? ["catalog", "objects"].includes(String(query.queryKey[1]))
        : query.queryKey[0] === "nl2sql" && ["profiles", "db-admin"].includes(String(query.queryKey[1])),
    });
    if (active) visited.current = true;
  }, [active, queryClient]);
}

export function useWorkspaceActivation(onActivate: () => void) {
  const active = useWorkspaceActive();
  const wasActive = useRef(false);
  const callback = useRef(onActivate);
  // 最新の onActivate を commit 時に入れる（render 中に ref を書かない）。
  useLayoutEffect(() => { callback.current = onActivate; });
  useEffect(() => {
    if (active && !wasActive.current) callback.current();
    wasActive.current = active;
  }, [active]);
}

const NO_SEARCH_PARAMS_UPDATE = () => undefined;

/**
 * 一覧 + 詳細（`ListDetailLayout`）の選択を URL の検索パラメータに持つ（共有の `useUrlSelection`。#2281）。
 *
 * NL2SQL の画面は keep-alive（離れても描いたまま隠す）なので、次の 2 つを足す。
 * - 隠れている間は、ほかの画面の URL を読まず、最後に開いていた選択のまま止める（詳細を消さない）。
 * - サイドナビから戻る（URL に選択が無いまま画面が表に出る）ときは、最後の選択を URL に戻す（replace）。
 *   作業状態の「対象の選択は残して検証し直す」（workspace-state.md）。「一覧へ戻る」で外した選択は戻さない。
 *
 * 画面の中のタブの 1 つだけが一覧 + 詳細のとき（データの管理のデータの表示。#2315）は `enabled` にそのタブが
 * 開いているかを渡す。ほかのタブの間は隠れている画面と同じに扱い（URL を読まず書かない）、タブへ戻ると最後の選択を
 * URL に戻す。タブを離れるときに URL からパラメータを外すのは呼び出し側が行う。
 */
export function useWorkspaceUrlSelection(
  param: string,
  options: { enabled?: boolean } = {}
): UrlSelection & { split: boolean } {
  const active = useWorkspaceActive() && (options.enabled ?? true);
  const split = useListDetailSplit();
  const [searchParams, setSearchParams] = useSearchParams();
  const [remembered, setRemembered] = useWorkspaceState(`selection:${param}`, "");
  const urlValue = searchParams.get(param)?.trim() ?? "";
  const frozenParams = useMemo(
    () => new URLSearchParams(remembered ? { [param]: remembered } : {}),
    [param, remembered]
  );
  const selection = useUrlSelection({
    param,
    searchParams: active ? searchParams : frozenParams,
    setSearchParams: active ? setSearchParams : NO_SEARCH_PARAMS_UPDATE,
    singlePane: !split,
  });
  const wasActive = useRef(false);
  useEffect(() => {
    const activated = active && !wasActive.current;
    wasActive.current = active;
    if (!active) return;
    if (activated && !urlValue && remembered) {
      setSearchParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          next.set(param, remembered);
          return next;
        },
        { replace: true }
      );
      return;
    }
    if (urlValue !== remembered) setRemembered(urlValue);
  }, [active, param, remembered, setRemembered, setSearchParams, urlValue]);
  return { ...selection, split };
}

/** Profile の推薦を明示適用するときだけ、そのクエリを移動先の草稿へ引き継ぐ。 */
export function useWorkspaceDraftWriter() {
  const { user } = useAuth();
  const context = useContext(WorkspaceContext);
  const activity = useContext(ActivityContext);
  const { pathname } = useLocation();
  return (field: string, value: string) => {
    const owner = user?.user_uuid ?? "";
    try { return writeDraft(window.sessionStorage, owner, draftKey(owner, context, activity.page || pathname, field), value); } catch { return false; }
  };
}

export function WorkspaceResultNotice({ result, inputSignature, finishedAt, restored = false }: { result: object | null; inputSignature: string; finishedAt?: string | number | null; restored?: boolean }) {
  const active = useWorkspaceActive();
  // 結果が変わった時点の入力と時刻を state で持つ（render 中に ref を読み書きしない）。
  const [stored, setStored] = useState(() => ({ result, inputSignature, at: new Date().toISOString() }));
  const [previous, setPrevious] = useState<object | null>(null);
  let snapshot = stored;
  if (stored.result !== result) {
    snapshot = { result, inputSignature, at: new Date().toISOString() };
    setStored(snapshot);
  }
  // 非表示の間に来た結果を「前回の結果」として覚える（effect ではなく render 中に同期する）。
  if (!active && previous !== result) setPrevious(result);
  if (!result) return null;
  const changed = snapshot.inputSignature !== inputSignature;
  if (!restored && previous !== result && !changed) return null;
  return <Banner severity="info" title={t("workspace.previousResult")}>
    {t("workspace.executedAt", { date: formatDisplayDateTime(finishedAt || snapshot.at, DISPLAY_DATE_TIME_SECONDS) })}
    {changed ? ` — ${t("workspace.inputChanged")}` : ""}
  </Banner>;
}

/** ファイル内容など、一時保存の対象外である未保存入力を再読込から保護する。 */
export function useTransientDraftGuard(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return;
    const protect = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [enabled]);
}
