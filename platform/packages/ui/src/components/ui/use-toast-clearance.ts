import { useEffect, type RefObject } from "react";

/**
 * 画面の下端に固定された操作の領域（チャットの入力欄。`ChatLayout`）の上に、md 以上の通知を出す（#2103）。
 *
 * md 以上の通知（`<Toaster/>`）は画面の右下の決まった位置（下端から `--toast-offset-bottom`）に出す。下端に固定された
 * 入力欄がその高さを超える画面（質問の入力欄を 3.5 行にした #2008 の後のチャット）では、入力欄の上端から 1rem 上に
 * 持ち上げる（Material の Snackbar の「下端のバー・FAB の上に出す」と同じ考え方。Slack・Teams・ChatGPT も
 * 送信の欄を通知で覆わない）。入力欄が画面の下半分に見えている間だけ、入力欄の上端から画面の下端までの高さを
 * `--toast-clear-bottom` に書き、`<Toaster/>` の下端の距離はこれと `--toast-offset-bottom` の大きい方になる
 * （`lib/toast-placement.ts`）。入力欄の高さ（options・notice・footer の行）や画面の高さが変わっても追いかける。
 *
 * 書くのは `document.documentElement` の CSS 変数だけで、測る要素の大きさは変えない（書き戻しの揺れは起きない）。
 * 1 画面に 1 つ（下端に固定された入力欄）だけで使う。外れたら変数を消す。
 */
export const TOAST_CLEARANCE_VAR = "--toast-clear-bottom";

export function useToastClearance(ref: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const element = ref.current;
    if (!element || typeof window === "undefined") return;
    const root = document.documentElement;
    let frame = 0;
    const update = () => {
      frame = 0;
      const rect = element.getBoundingClientRect();
      const viewport = window.innerHeight;
      // 画面の下半分に見えているとき（下端に固定された入力欄）だけ持ち上げる。スクロールで画面の外・上半分へ
      // 動いた入力欄は、決まった位置の通知と重ならない。
      if (rect.height > 0 && rect.top < viewport && rect.bottom > viewport / 2) {
        root.style.setProperty(TOAST_CLEARANCE_VAR, `${Math.ceil(viewport - rect.top)}px`);
      } else {
        root.style.removeProperty(TOAST_CLEARANCE_VAR);
      }
    };
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(update);
    };
    update();
    // 入力欄の高さと、会話の欄の伸び縮みによる入力欄の位置の変化（md〜lg は画面ごとスクロールする）を追う。
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    observer?.observe(element);
    const layout = element.closest("[data-chat-layout]");
    if (layout) observer?.observe(layout);
    window.addEventListener("resize", schedule);
    document.addEventListener("scroll", schedule, { capture: true, passive: true });
    return () => {
      if (frame) window.cancelAnimationFrame(frame);
      observer?.disconnect();
      window.removeEventListener("resize", schedule);
      document.removeEventListener("scroll", schedule, { capture: true });
      root.style.removeProperty(TOAST_CLEARANCE_VAR);
    };
  }, [ref]);
}
