import { Search } from "lucide-react";
import {
  type ChangeEvent,
  type CompositionEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { isImeComposing } from "../../lib/keyboard";

import { TextField, type TextFieldProps } from "./text-field";

/** 一覧の絞り込みの既定の debounce（ms）。入力が止まってからこの時間で絞り込む（#535）。 */
export const SEARCH_FIELD_DEBOUNCE_MS = 300;

/** 既定の正規化。前後の空白は絞り込みの意味を持たないので落とす。 */
export const trimSearchValue = (value: string) => value.trim();

export type SearchFieldProps = Omit<
  TextFieldProps,
  | "type"
  | "value"
  | "defaultValue"
  | "onChange"
  | "onValueChange"
  | "onClear"
  | "leadingIcon"
  | "required"
  | "requiredLabel"
> & {
  /**
   * 適用中の検索語（一覧の絞り込みに使っている値）。作業状態・URL に保存している値をそのまま渡す。
   * 入力中の文字（下書き）は SearchField が持ち、ここが外から変わったとき（条件のリセット・復元）だけ入力欄を合わせる。
   */
  value: string;
  /**
   * 検索語が確定したときに呼ぶ（正規化した値。既定は前後の空白を落とす）。次のときに呼ぶ:
   * - 入力が `debounceMs` 止まったとき（IME の変換中は呼ばない。変換を確定した値で呼ぶ）
   * - Enter（debounce を待たずにすぐ。変換を確定する Enter では呼ばない）
   * - 消去（× / Escape）
   * - 待っている値があるまま入力欄が外れる・ページを離れる（`pagehide`）とき（その場で確定する）
   * 前回呼んだ値と同じなら呼ばない（ページングを 1 ページ目へ戻す処理を空打ちしない）。
   */
  onSearch: (value: string) => void;
  /** 消去ボタンの読み上げ名と Tooltip（翻訳済み。例:「検索語をクリア」）。 */
  clearLabel: string;
  /** 入力が止まってから絞り込むまでの時間（既定 300ms）。0 なら入力のたびに（IME の確定後に）すぐ呼ぶ。 */
  debounceMs?: number;
  /** onSearch に渡す前の正規化（既定は trim。例: 所有者の接頭辞を大文字にする）。 */
  normalize?: (value: string) => string;
  /**
   * 入力中の文字の見せ方（任意。例: 大文字にする）。IME の変換中は適用しない（未確定の文字を書き換えない）。
   * 絞り込みに使う値は normalize で決める。
   */
  formatInput?: (value: string) => string;
  /**
   * 絞り込んだ結果の件数の文言（翻訳済み。例:「12 件中 3 件」）。検索語があるときだけ、
   * 入力欄の後ろの `role="status"`（aria-live="polite"）で読み上げる。画面に件数の表示がすでにある場合も、
   * 読み上げのために渡してよい（見た目には出さない）。
   */
  resultCountLabel?: string;
  /** 入力中の文字（下書き）が変わったとき（任意。件数の表示などに使う。絞り込みには onSearch を使う）。 */
  onDraftChange?: (draft: string) => void;
};

/**
 * 一覧の絞り込みの検索欄（#535）。3 製品と system-settings の「画面上の一覧・表を名前などで絞る」検索欄はすべてこれで作る。
 *
 * - 入力に合わせて絞り込む（debounce 300ms）。検索ボタンは置かない。Enter は debounce を待たずにすぐ反映する。
 * - 日本語入力の変換中（compositionstart〜compositionend / isComposing / keyCode 229）は、入力でも Enter でも絞り込まない。
 *   変換を確定した値で絞り込む。
 * - `type="search"`・先頭の虫眼鏡（16px）・値があるときの消去（×。Escape でも消す）は TextField の仕組みを使う。
 * - `resultCountLabel` で結果の件数を aria-live で伝える。
 *
 * LLM・ベクトル検索などを呼ぶ重い検索・問い合わせ（RAG の検索・チャット・NL2SQL の質問）には使わない。
 * それらは TextField と明示的な実行（ボタンと Enter。判定は `isSubmitEnter`）で作る（UX 契約 page-archetypes.md）。
 */
export function SearchField({
  value,
  onSearch,
  clearLabel,
  debounceMs = SEARCH_FIELD_DEBOUNCE_MS,
  normalize = trimSearchValue,
  formatInput,
  resultCountLabel,
  onDraftChange,
  onKeyDown,
  onCompositionStart,
  onCompositionEnd,
  autoComplete = "off",
  enterKeyHint = "search",
  ...props
}: SearchFieldProps) {
  const [draft, setDraft] = useState(value);
  // 最後に onSearch へ渡した値（= 親が value として返してくるはずの値）。
  // value がこれと違う値に変わったときだけ、外からの変更として入力欄を合わせる（入力中の文字を親の値で上書きしない）。
  const lastEmittedRef = useRef(value);
  const composingRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onSearchRef = useRef(onSearch);
  const normalizeRef = useRef(normalize);
  useEffect(() => {
    onSearchRef.current = onSearch;
    normalizeRef.current = normalize;
  });

  // 待っている絞り込みの値（debounce 中）。画面の切り替えで入力欄が外れるときに確定するために持つ。
  const pendingRef = useRef<string | null>(null);

  const cancelPending = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    pendingRef.current = null;
  }, []);

  const emit = useCallback(
    (raw: string) => {
      cancelPending();
      const next = normalizeRef.current(raw);
      if (next === lastEmittedRef.current) return;
      lastEmittedRef.current = next;
      onSearchRef.current(next);
    },
    [cancelPending]
  );

  const schedule = useCallback(
    (raw: string) => {
      cancelPending();
      if (debounceMs <= 0) {
        emit(raw);
        return;
      }
      pendingRef.current = raw;
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        emit(raw);
      }, debounceMs);
    },
    [cancelPending, debounceMs, emit]
  );

  // 外から検索語が変わったとき（条件のリセット・作業状態の復元）は、入力欄を合わせ、待っている絞り込みを捨てる。
  useEffect(() => {
    if (value === lastEmittedRef.current) return;
    lastEmittedRef.current = value;
    cancelPending();
    setDraft(value);
  }, [cancelPending, value]);

  // 入力欄が外れるとき（一覧 ⇄ 作成の切り替え・読込中の表示など）は、待っている絞り込みをその場で確定する。
  // 捨てると、消した・入力した検索語が一覧へ戻ったときに元に戻ってしまう。
  useEffect(
    () => () => {
      const pending = pendingRef.current;
      if (pending !== null) emit(pending);
    },
    [emit]
  );

  // ページを離れるとき（再読込・別の文書への移動・タブを閉じる）も、待っている絞り込みをその場で確定する（#3385）。
  // React の unmount は起きないため、入力してから debounce の間に再読込すると、作業状態・URL に残らず入力が消える。
  useEffect(() => {
    const flush = () => {
      const pending = pendingRef.current;
      if (pending !== null) emit(pending);
    };
    window.addEventListener("pagehide", flush);
    return () => window.removeEventListener("pagehide", flush);
  }, [emit]);

  const handleChange = (event: ChangeEvent<HTMLInputElement>) => {
    // 変換中の入力（未確定の読み・候補）では絞り込まない。確定は compositionend で拾う。
    const nativeComposing = (event.nativeEvent as { isComposing?: boolean }).isComposing === true;
    const composing = composingRef.current || nativeComposing;
    const next = formatInput && !composing ? formatInput(event.target.value) : event.target.value;
    setDraft(next);
    onDraftChange?.(next);
    if (composing) return;
    schedule(next);
  };

  const handleCompositionStart = (event: CompositionEvent<HTMLInputElement>) => {
    onCompositionStart?.(event);
    composingRef.current = true;
    cancelPending();
  };

  const handleCompositionEnd = (event: CompositionEvent<HTMLInputElement>) => {
    onCompositionEnd?.(event);
    composingRef.current = false;
    // 確定した値で絞り込む（ブラウザにより、確定後の input event が来ないことがあるため、ここで予約する）。
    const committed = formatInput ? formatInput(event.currentTarget.value) : event.currentTarget.value;
    if (committed !== event.currentTarget.value) setDraft(committed);
    schedule(committed);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    onKeyDown?.(event);
    if (event.defaultPrevented || event.key !== "Enter") return;
    // 変換を確定する Enter では絞り込まない（Safari は compositionend の後に keyCode 229 で届く）。
    if (composingRef.current || isImeComposing(event)) return;
    // 一覧の絞り込みはフォームを送信しない（囲む form があっても暗黙の送信をさせない）。
    event.preventDefault();
    emit(event.currentTarget.value);
  };

  const handleClear = () => {
    setDraft("");
    onDraftChange?.("");
    emit("");
  };

  const status = value.trim() && resultCountLabel ? resultCountLabel : "";

  return (
    <>
      <TextField
        {...props}
        type="search"
        value={draft}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        onCompositionStart={handleCompositionStart}
        onCompositionEnd={handleCompositionEnd}
        onClear={handleClear}
        clearLabel={clearLabel}
        leadingIcon={Search}
        autoComplete={autoComplete}
        enterKeyHint={enterKeyHint}
      />
      {resultCountLabel !== undefined ? (
        // 件数の読み上げ。領域は最初から置いておき（後から足した live region は読まれないことがある）、
        // 検索語があるときだけ文言を入れる。
        <p role="status" aria-live="polite" aria-atomic="true" className="sr-only" data-search-field-status="">
          {status}
        </p>
      ) : null}
    </>
  );
}
