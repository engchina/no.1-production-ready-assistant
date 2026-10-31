import { useState, type ChangeEvent } from "react";

/**
 * 数値の欄の入力中の文字列を欄の中だけで持つ（#1103。フィードバック管理の数値の欄の直し方 #968 と同じ）。
 *
 * 打鍵ごとに `Number(value) || 既定値` や範囲への丸めで表示を上書きすると、欄を空にした瞬間に 0 や最小値に
 * 置き換わり、`50` のように打ち直せない。範囲内の数（`integer` なら整数）として読めたときだけ親の値を変え、
 * 欄を離れたら確定した値の表示に戻す。`TextField` に `{...numberDraft}` で渡す。
 */
export function useNumberDraft(
  value: number,
  {
    min,
    max,
    integer = false,
    onChange,
  }: { min: number; max: number; integer?: boolean; onChange: (value: number) => void },
) {
  const [draft, setDraft] = useState<string | null>(null);
  return {
    value: draft ?? String(value),
    onChange: (event: ChangeEvent<HTMLInputElement>) => {
      const text = event.currentTarget.value;
      setDraft(text);
      const next = Number(text);
      if (
        text.trim() !== "" &&
        Number.isFinite(next) &&
        (!integer || Number.isInteger(next)) &&
        next >= min &&
        next <= max
      ) {
        onChange(next);
      }
    },
    onBlur: () => setDraft(null),
  };
}
