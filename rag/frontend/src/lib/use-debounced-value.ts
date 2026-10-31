import { useEffect, useState } from "react";

/** 値が delayMs の間変わらなかったときだけ更新される値（入力ごとの API 呼び出しを抑える）。 */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}
