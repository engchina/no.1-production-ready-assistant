export function scrollFocusedControlIntoView(
  element: HTMLElement,
  { focus = false }: { focus?: boolean } = {}
): void {
  const reducedMotion =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  element.scrollIntoView({
    block: "center",
    inline: "nearest",
    behavior: reducedMotion ? "auto" : "smooth",
  });
  if (focus) {
    element.focus({ preventScroll: true });
  }
}

/**
 * スクロール領域（一覧）の中だけを動かして、項目を見える範囲に入れる（#403）。
 * `scrollIntoView` と違い、ページや外側のスクロール領域は動かさない。
 * 既に見えていれば何もしない。項目が領域より高いときは項目の上端を領域の上端にそろえる。
 */
export function revealWithinScrollContainer(container: HTMLElement, item: HTMLElement): void {
  const containerRect = container.getBoundingClientRect();
  const itemRect = item.getBoundingClientRect();
  const above = itemRect.top - containerRect.top;
  const below = itemRect.bottom - containerRect.bottom;
  if (above < 0 || itemRect.height > containerRect.height) {
    container.scrollTop += above;
  } else if (below > 0) {
    container.scrollTop += below;
  }
}

/**
 * `from` から親へたどり、縦にスクロールしている最初の領域を返す（`from` 自身を含む）。
 * ページ（body / html）まで無ければ null。幅によって一覧自身と外側のパネルのどちらがスクロールするかが変わるときに使う。
 */
export function nearestVerticalScrollContainer(from: HTMLElement | null): HTMLElement | null {
  for (let element = from; element && element !== document.body && element !== document.documentElement; element = element.parentElement) {
    const overflowY = getComputedStyle(element).overflowY;
    if ((overflowY === "auto" || overflowY === "scroll") && element.scrollHeight > element.clientHeight + 1) {
      return element;
    }
  }
  return null;
}
