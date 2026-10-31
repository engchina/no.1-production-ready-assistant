interface TimelineContainer {
  evaluate<R>(fn: (node: HTMLElement | SVGElement) => R): Promise<R>;
}

/** 操作画面と履歴の工程の内容・階層・状態・時間を、表示順のまま比較する。 */
export async function timelineSnapshot(container: TimelineContainer) {
  return container.evaluate((node) => Array.from(node.querySelectorAll("[data-run-timeline-step]"), (row) => ({
    name: row.querySelector("[data-run-timeline-label]")?.textContent ?? "",
    product: row.querySelector("[data-run-timeline-product]")?.textContent ?? "",
    level: Number(row.getAttribute("data-level") ?? 0),
    status: row.getAttribute("data-status"),
    text: (row.firstElementChild as HTMLElement).innerText.replace(/\s+/g, " ").trim(),
  })));
}

/** 進捗の文字の実測値。操作画面と履歴で同じ書体・大きさ・字重・行高かを比較する。 */
export async function timelineTypography(container: TimelineContainer) {
  return container.evaluate((node) => {
    const textStyle = (element: Element | null) => {
      if (!element) return null;
      const style = getComputedStyle(element);
      return {fontFamily: style.fontFamily, fontSize: style.fontSize, fontWeight: style.fontWeight, lineHeight: style.lineHeight};
    };
    return {
      summary: textStyle(node.querySelector("[data-run-progress-summary]")),
      steps: Array.from(node.querySelectorAll("[data-run-timeline-label]"), textStyle),
    };
  });
}
