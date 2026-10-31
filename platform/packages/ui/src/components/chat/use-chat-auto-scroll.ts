import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from "react";

/** 末尾からこの距離（px）以内なら「末尾を見ている」とみなす（端数・行の余白で追従が外れないように）。 */
export const CHAT_AUTO_SCROLL_THRESHOLD_PX = 48;

export interface UseChatAutoScrollOptions {
  /** 会話の内容が変わったことを表す値（往復の数・最後の状態・仮の質問・受信中の本文など）。`Object.is` で比べる。 */
  contentKey: unknown;
  /** 会話そのものが変わったことを表す値（会話の ID・対象）。変わったら末尾から読み始める。 */
  resetKey?: unknown;
  /** 画面が見えている間だけ動かす（keep-alive で隠れている間は動かさない）。既定 true。 */
  enabled?: boolean;
  threshold?: number;
}

export interface ChatAutoScroll {
  /** 会話の欄（`ChatLayout` の `logRef`）に渡す ref。 */
  logRef: (element: HTMLDivElement | null) => void;
  /** 会話の欄の要素（引用・回答の位置への移動など、製品が会話の欄の中を動かすとき）。 */
  logElementRef: RefObject<HTMLDivElement | null>;
  /** 末尾より上を読んでいる間に新しい内容が届いた（「最新へ」を出す）。 */
  showLatest: boolean;
  /** 会話の欄を末尾へ動かす（送信の瞬間・「最新へ」）。祖先は動かさない。 */
  scrollToLatest: (behavior?: ScrollBehavior) => void;
}

function isAtBottom(element: HTMLElement, threshold: number) {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= threshold;
}

/**
 * 末尾を追っている間はブラウザの scroll anchoring を止める（#3245）。見えている範囲より上の内容が伸びると、
 * anchoring が `scrollTop` を動かして `scroll` を出し、利用者が動かしていないのに「末尾を見ていない」になって
 * 追うのをやめていた。上を読んでいる間は anchoring を戻し、読んでいる位置がずれないようにする。
 */
function setFollowingBottom(element: HTMLElement | null, following: boolean) {
  if (element) element.style.overflowAnchor = following ? "none" : "";
}

/**
 * チャットの会話の欄の自動スクロール（3 製品共通。UX 契約 page-archetypes.md §6、#1161）。
 *
 * - 末尾を見ている間は、新しい内容（回答の受信・処理の段階）に合わせて末尾へ追う。末尾へ動かした後、利用者が
 *   会話の欄を操作するまでは、描いた後の高さの変化（フォント・画像の読み込み）にも追う。
 * - 上を読んでいる間は引き戻さず、「最新へ」（`showLatest`）を出す。末尾まで戻ると消す。
 * - 会話を開いた・変えたとき（`resetKey`）は末尾から読み始める。送信の瞬間は製品が `scrollToLatest` を呼ぶ
 *   （messaging.md §11.1）。
 * - 動かすのは会話の欄の `scrollTo` だけで、祖先（ページ・document）は動かさない（README §4「AppShell」）。
 */
export function useChatAutoScroll({
  contentKey,
  resetKey,
  enabled = true,
  threshold = CHAT_AUTO_SCROLL_THRESHOLD_PX,
}: UseChatAutoScrollOptions): ChatAutoScroll {
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const logElementRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef(true);
  // 末尾へ動かした後、利用者が会話の欄を操作するまでは、描いた後の高さの変化にも末尾を追う。
  const followResizeRef = useRef(true);
  // この hook が最後に動かした位置。`scroll` は非同期に届くので、自分で動かした scroll が高さの伸びた後に届くと
  // 「末尾を見ていない」と測ってしまう（#3245）。その scroll では末尾を追うのをやめない。
  const pinnedTopRef = useRef<number | null>(null);
  const [showLatest, setShowLatest] = useState(false);

  const logRef = useCallback((next: HTMLDivElement | null) => {
    logElementRef.current = next;
    setElement(next);
  }, []);

  const scrollToLatest = useCallback((behavior: ScrollBehavior = "auto") => {
    const target = logElementRef.current;
    atBottomRef.current = true;
    followResizeRef.current = true;
    setShowLatest(false);
    setFollowingBottom(target, true);
    if (target) pinnedTopRef.current = Math.max(0, target.scrollHeight - target.clientHeight);
    target?.scrollTo({ top: target.scrollHeight, behavior });
  }, []);

  // 利用者のスクロールで「末尾を見ているか」を更新する。
  useLayoutEffect(() => {
    if (!element) return;
    const onScroll = () => {
      const pinnedTop = pinnedTopRef.current;
      if (followResizeRef.current && pinnedTop !== null && Math.abs(element.scrollTop - pinnedTop) <= 1) {
        // この hook が動かした scroll（利用者は操作していない）。届くまでに伸びていたら、末尾へ動かし直す。
        if (!isAtBottom(element, threshold)) {
          pinnedTopRef.current = Math.max(0, element.scrollHeight - element.clientHeight);
          element.scrollTo({ top: element.scrollHeight });
        }
        return;
      }
      pinnedTopRef.current = null;
      const atBottom = isAtBottom(element, threshold);
      atBottomRef.current = atBottom;
      if (atBottom) setShowLatest(false);
      else setFollowingBottom(element, false);
    };
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => element.removeEventListener("scroll", onScroll);
  }, [element, threshold]);

  // 描いた後の高さの変化（フォント・画像の読み込み）でも、末尾を見ていれば末尾に留める（#2479）。
  // 利用者が会話の欄を操作した後（折りたたみを開くなど）は、高さが変わっても引き戻さない。
  useLayoutEffect(() => {
    if (!enabled || !element || typeof ResizeObserver === "undefined") return;
    const resizeObserver = new ResizeObserver(() => {
      if (followResizeRef.current && atBottomRef.current) {
        pinnedTopRef.current = Math.max(0, element.scrollHeight - element.clientHeight);
        element.scrollTo({ top: element.scrollHeight });
      }
    });
    const observeChildren = () => {
      for (const child of Array.from(element.children)) resizeObserver.observe(child);
    };
    resizeObserver.observe(element);
    observeChildren();
    const mutationObserver = new MutationObserver(observeChildren);
    mutationObserver.observe(element, { childList: true });
    const onInteract = () => {
      followResizeRef.current = false;
      pinnedTopRef.current = null;
      setFollowingBottom(element, false);
    };
    const interactions = ["wheel", "touchstart", "pointerdown", "keydown"] as const;
    for (const type of interactions) element.addEventListener(type, onInteract, { passive: true });
    return () => {
      resizeObserver.disconnect();
      mutationObserver.disconnect();
      setFollowingBottom(element, false);
      for (const type of interactions) element.removeEventListener(type, onInteract);
    };
  }, [enabled, element]);

  // 会話を開いた・変えた・画面に戻った・会話の欄が描かれたときは末尾から。
  useLayoutEffect(() => {
    if (!enabled || !element) return;
    scrollToLatest();
  }, [resetKey, enabled, element, scrollToLatest]);

  // 新しい内容: 末尾を見ていれば追い、上を読んでいれば「最新へ」を出す。
  const previousContentKey = useRef(contentKey);
  useLayoutEffect(() => {
    if (Object.is(previousContentKey.current, contentKey)) return;
    previousContentKey.current = contentKey;
    if (!enabled || !element) return;
    if (atBottomRef.current) scrollToLatest();
    else setShowLatest(true);
  }, [contentKey, enabled, element, scrollToLatest]);

  return { logRef, logElementRef, showLatest, scrollToLatest };
}
