/**
 * キーボード入力の判定（IME 対応、#535）。
 *
 * 日本語入力の変換中（compositionstart〜compositionend）と、変換を確定する Enter では、
 * 送信・検索・追加などの操作を始めない。3 製品と system-settings はこの判定を使い、
 * `event.key === "Enter"` だけで判定しない。
 */

/** 判定に使う keydown event の最小形（React の KeyboardEvent と DOM の KeyboardEvent の両方を受ける）。 */
export interface KeyboardEventLike {
  key: string;
  keyCode?: number;
  isComposing?: boolean;
  nativeEvent?: { isComposing?: boolean };
}

/**
 * IME で変換中（日本語入力の確定の Enter を含む）の keydown か。
 * Chrome / Firefox は変換中の keydown が isComposing=true。Safari は確定の Enter が
 * compositionend の後に isComposing=false・keyCode=229 で届くため、keyCode も見る。
 */
export function isImeComposing(event: KeyboardEventLike): boolean {
  return Boolean(event.isComposing || event.nativeEvent?.isComposing || event.keyCode === 229);
}

/** 入力欄の Enter で送信・実行してよいか（IME の変換を確定する Enter では実行しない）。 */
export function isSubmitEnter(event: KeyboardEventLike): boolean {
  return event.key === "Enter" && !isImeComposing(event);
}
