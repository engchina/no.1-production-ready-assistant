import { useCallback, useEffect, useRef, useState } from "react";

export interface ActionPending {
  /** `track` に渡した処理のどれかが終わっていない間だけ true。 */
  pending: boolean;
  /** 処理を実行し、終わるまで `pending` を true にする。処理の結果・例外はそのまま返す。 */
  track: <T>(work: () => Promise<T>) => Promise<T>;
}

/**
 * 押したボタンが始めた処理の間だけ true になる状態（#819）。
 *
 * `Button` の `loading` は押したボタンだけが持つ（UX 契約 buttons.md §8）。「最新情報を取得」に
 * query の `isFetching` をそのまま渡すと、定期の取り直し・他の操作の後の invalidate・絞り込みの
 * 切り替えでも回るため、押した取り直しを `track(() => query.refetch())` で包み、`pending` を渡す。
 */
export function useActionPending(): ActionPending {
  const [count, setCount] = useState(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const track = useCallback(async <T,>(work: () => Promise<T>): Promise<T> => {
    setCount((current) => current + 1);
    try {
      return await work();
    } finally {
      if (mounted.current) setCount((current) => Math.max(0, current - 1));
    }
  }, []);
  return { pending: count > 0, track };
}
