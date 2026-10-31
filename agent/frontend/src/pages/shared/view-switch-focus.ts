import { useEffect, useRef } from "react";

/**
 * 一覧 ↔ 詳細の URL の切り替え（同じ画面の中の移動）でフォーカスを移す（#875）。
 * 詳細を開いたらページの見出しへ、一覧へ戻ったら `rowSelector` の行（選んでいた行）へ移す（無ければ見出し）。
 *
 * 画面を開いた最初の描画では動かさない（#1122）。動かすと最初の Tab が「本文へスキップ」と
 * サイドナビを飛ばし、サイドナビから開いたときもクリックした項目からフォーカスが外れる。
 * `viewKey` が前回と同じ再実行（StrictMode の effect の二重実行を含む）でも動かさない。
 */
export function useViewSwitchFocus(viewKey: string, rowSelector: string | null): void {
  const previous = useRef<string | null>(null);
  // 行の selector は切り替えの後の再描画（選択の書き戻し）でも最新を読む。selector の変化では動かさない。
  const selector = useRef(rowSelector);
  useEffect(() => {
    selector.current = rowSelector;
  });

  useEffect(() => {
    const switched = previous.current !== null && previous.current !== viewKey;
    previous.current = viewKey;
    if (!switched) return;
    const frame = requestAnimationFrame(() => {
      const heading = document.querySelector<HTMLElement>("main h1");
      if (heading) heading.tabIndex = -1;
      const row = selector.current ? document.querySelector<HTMLElement>(selector.current) : null;
      (row ?? heading)?.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  }, [viewKey]);
}
