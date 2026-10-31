import { useCallback, useEffect, useMemo } from "react";
import { useLocation, useSearchParams } from "react-router-dom";

/**
 * A 型（一覧 → 全画面エディタ）の編集対象を URL の検索パラメータで持つ（platform UX 契約
 * page-archetypes.md §1 A。#147）。
 *
 * - `?id=` が唯一の情報源。無い = 一覧 / `new` = 新規 / それ以外 = その ID の対象。
 * - 開く・閉じるは履歴に積む（ブラウザの戻る / 進むで同じ対象が開く）。作成に成功して作成した対象へ
 *   移るときと、アーカイブ・削除の後に一覧へ戻るときは `replace` で積まない（戻るで空のフォームや
 *   消えた対象へ戻さない）。
 * - 対象が変わったら本文のスクロールを先頭へ戻す（一覧の下の方から開いてもエディタの先頭から読める）。
 * - 他の検索パラメータは残す。
 */
export const EDITOR_PARAM = "id";
export const NEW_TARGET = "new";

export type EditorTarget = { kind: "list" } | { kind: "new" } | { kind: "edit"; id: string };

export interface EditorRoute {
  target: EditorTarget;
  openNew: () => void;
  openItem: (id: string, options?: { replace?: boolean }) => void;
  backToList: (options?: { replace?: boolean }) => void;
  /**
   * その対象のエディタの URL（#583）。一覧の題名のリンク（`RowTitleButton` の `href`）に渡し、
   * Ctrl / ⌘ + クリック・中クリックで新しいタブに開けるようにする。他の検索パラメータは残す。
   */
  itemHref: (id: string) => string;
}

/** `pathname` + `search` に `?id=<id>` を足した URL（他の検索パラメータは残す）。 */
export function editorItemHref(pathname: string, search: string, id: string) {
  const next = new URLSearchParams(search);
  next.set(EDITOR_PARAM, id);
  return `${pathname}?${next.toString()}`;
}

/** `?id=` の値を編集対象へ読み替える（空白だけの値は一覧として扱う）。 */
export function parseEditorTarget(raw: string | null): EditorTarget {
  if (raw === null || raw.trim() === "") return { kind: "list" };
  if (raw === NEW_TARGET) return { kind: "new" };
  return { kind: "edit", id: raw };
}

export function useEditorRoute(): EditorRoute {
  const [searchParams, setSearchParams] = useSearchParams();
  const { pathname } = useLocation();
  const raw = searchParams.get(EDITOR_PARAM);
  const target = useMemo(() => parseEditorTarget(raw), [raw]);

  const setTarget = useCallback(
    (value: string | null, replace = false) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          if (value === null) next.delete(EDITOR_PARAM);
          else next.set(EDITOR_PARAM, value);
          return next;
        },
        { replace }
      );
    },
    [setSearchParams]
  );

  useEffect(() => {
    // AppShell の本文（<main>）が scroll container。対象の切替はページの切替と同じく先頭から見せる。
    document.querySelector("main")?.scrollTo({ top: 0 });
  }, [raw]);

  return useMemo(
    () => ({
      target,
      openNew: () => setTarget(NEW_TARGET),
      openItem: (id: string, options?: { replace?: boolean }) => setTarget(id, options?.replace),
      backToList: (options?: { replace?: boolean }) => setTarget(null, options?.replace),
      itemHref: (id: string) => editorItemHref(pathname, searchParams.toString(), id),
    }),
    [pathname, searchParams, setTarget, target]
  );
}
