import { useCallback, useRef } from "react";

/**
 * URL の検索パラメータを書き換える関数（React Router の `setSearchParams` と同じ形）。`packages/ui` は router に
 * 依存しないので、製品が `useSearchParams()` の戻り値を渡す。
 */
export type UrlSelectionSetSearchParams = (
  next: (previous: URLSearchParams) => URLSearchParams,
  options?: { replace?: boolean }
) => void;

export interface UseUrlSelectionOptions {
  /** 選んだ対象を持つ検索パラメータの名前（例: `object`・`user`・`feedback`）。 */
  param: string;
  /** 今の URL の検索パラメータ（`useSearchParams()[0]`）。 */
  searchParams: URLSearchParams;
  setSearchParams: UrlSelectionSetSearchParams;
  /**
   * 1 ペイン（`ListDetailLayout` の xl 未満）か。1 ペインで一覧から詳細へ入るときは履歴に積み（push）、
   * ブラウザの「戻る」と「一覧へ戻る」で一覧へ戻れるようにする。2 ペインで選び直すときは置き換える（replace）。
   */
  singlePane: boolean;
  /** `history.back()` の代わり（テスト用）。既定は `window.history.back()`。 */
  goBack?: () => void;
}

export interface UrlSelection {
  /** 選んでいる対象の ID（URL に無ければ null）。先頭の行を自動で選ばない。 */
  selectedId: string | null;
  /** 一覧から対象を選ぶ。1 ペインで未選択から選ぶときだけ push、それ以外は replace。 */
  select: (id: string) => void;
  /**
   * 選択を外して一覧へ戻る（1 ペインの「一覧へ戻る」）。この画面で push して詳細へ入ったときは `history.back()`
   * （ブラウザの「戻る」と同じ履歴の位置へ戻る）、URL から直接開いたときは検索パラメータを消して置き換える。
   */
  clear: () => void;
  /** 選択を置き換える（削除の後に外す・作成の後に選ぶなど）。履歴に積まない。 */
  replace: (id: string | null) => void;
}

function withParam(previous: URLSearchParams, param: string, value: string | null) {
  const next = new URLSearchParams(previous);
  if (value) next.set(param, value);
  else next.delete(param);
  return next;
}

/**
 * 一覧 + 詳細（`ListDetailLayout`）の選択を URL の検索パラメータに持つ（URL を唯一の情報源にする。#2281）。
 *
 * - 再読込・戻る / 進む・リンクの共有で同じ対象が開く。
 * - 先頭の行を自動で選ばない（未選択は詳細の空の状態。利用者の操作なしに詳細の取得を走らせない）。
 * - 1 ペインで一覧から詳細へ入るときは push（戻るで一覧）、2 ペインで選び直す・前へ / 次へは replace
 *   （行を見比べるたびに履歴を積まない）。
 */
export function useUrlSelection({
  param,
  searchParams,
  setSearchParams,
  singlePane,
  goBack,
}: UseUrlSelectionOptions): UrlSelection {
  const raw = searchParams.get(param);
  const selectedId = raw && raw.trim() ? raw : null;
  // この画面の操作で push して詳細へ入ったか（「一覧へ戻る」を history.back() にできるか）。
  const pushedRef = useRef(false);

  const select = useCallback(
    (id: string) => {
      if (id === selectedId) return;
      const push = singlePane && selectedId === null;
      if (push) pushedRef.current = true;
      setSearchParams((previous) => withParam(previous, param, id), { replace: !push });
    },
    [param, selectedId, setSearchParams, singlePane]
  );

  const clear = useCallback(() => {
    if (selectedId === null) return;
    if (pushedRef.current) {
      pushedRef.current = false;
      (goBack ?? (() => window.history.back()))();
      return;
    }
    setSearchParams((previous) => withParam(previous, param, null), { replace: true });
  }, [goBack, param, selectedId, setSearchParams]);

  const replace = useCallback(
    (id: string | null) => {
      if (id === selectedId) return;
      if (id === null) pushedRef.current = false;
      setSearchParams((previous) => withParam(previous, param, id), { replace: true });
    },
    [param, selectedId, setSearchParams]
  );

  return { selectedId, select, clear, replace };
}
