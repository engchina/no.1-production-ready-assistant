"use client";

import { useCallback, useState } from "react";

/**
 * 一覧の行選択を管理するフック（参照実装の useSelection の設計を踏襲）。
 */
export function useSelection<T extends string>() {
  const [selected, setSelected] = useState<ReadonlySet<T>>(new Set());

  const isSelected = useCallback((id: T) => selected.has(id), [selected]);

  const toggle = useCallback((id: T) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // 件数ではなく「表示中の ids がすべて選ばれているか」で判定する。再取得で一覧から消えた
  // id が選択に残っていても、全選択 / 全解除が逆にならないようにする（#281）。
  const toggleAll = useCallback((ids: T[]) => {
    setSelected((prev) =>
      ids.length > 0 && ids.every((id) => prev.has(id)) ? new Set<T>() : new Set(ids)
    );
  }, []);

  const clear = useCallback(() => setSelected(new Set<T>()), []);

  // 一括操作の後に、失敗した行だけを選択に残す（#2674）。
  const replace = useCallback((ids: Iterable<T>) => setSelected(new Set<T>(ids)), []);

  return {
    selected,
    count: selected.size,
    isSelected,
    toggle,
    toggleAll,
    clear,
    replace,
  };
}
