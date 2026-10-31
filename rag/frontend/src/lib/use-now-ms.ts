import { useMemo, useState, useSyncExternalStore } from "react";

/** 現在時刻（外部の値）を `useSyncExternalStore` で読むための、コンポーネントごとの時計。 */
function createClock(intervalMs: number) {
  let now = Date.now();
  return {
    /**
     * 刻み直しの区切り（`restartKey`）ごとに別の購読関数を返す。React は購読関数が変わると
     * 購読し直すので、そのたびに購読した時点の時刻から刻み直す（経過時間が前回の値から始まらない）。
     */
    subscribeFor(_restartKey: unknown) {
      return (onChange: () => void) => {
        now = Date.now();
        onChange();
        const timer = window.setInterval(() => {
          now = Date.now();
          onChange();
        }, intervalMs);
        return () => window.clearInterval(timer);
      };
    },
    getSnapshot: () => now,
  };
}

function subscribeNothing(): () => void {
  return () => {};
}

/**
 * `active` の間だけ `intervalMs` ごとに更新される現在時刻（ms）を返す。
 * `active` でない間は最後の値を保つ。`restartKey` が変わると、その時点の時刻から刻み直す。
 * 経過時間の表示用。effect で `setState(Date.now())` する代わりに使う。
 */
export function useNowMs(
  active: boolean,
  restartKey: string | number | null | undefined,
  intervalMs = 1000
): number {
  const [clock] = useState(() => createClock(intervalMs));
  const subscribe = useMemo(
    () => (active ? clock.subscribeFor(restartKey) : subscribeNothing),
    [active, clock, restartKey]
  );
  return useSyncExternalStore(subscribe, clock.getSnapshot);
}
