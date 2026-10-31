/**
 * 利用者に見せる名前を並べるときの共通の比較（#2753）。3 製品の画面はこの比較だけで名前を並べる。
 *
 * Unicode の照合順序（UCA / CLDR の `ja`）に、数を数値として比べる自然な並びを足す:
 * 空白・記号 → 数字（`2` は `10` より前）→ ラテン文字 → かな → 漢字。
 * 全角 / 半角・大文字 / 小文字・ひらがな / カタカナは同じ文字として並べ、同じなら区別して順を決める
 * （同じ入力の並びが毎回同じになるように）。漢字どうしは読みではなく JIS の順（読みは持たないため）。
 *
 * ID・JSON の正規化など、表示に出ない並べ替えには使わない（locale に依らない `sort()` のままにする）。
 */

const NAME_COLLATOR = new Intl.Collator("ja", { numeric: true, sensitivity: "base" });
const NAME_TIEBREAK_COLLATOR = new Intl.Collator("ja", { numeric: true, sensitivity: "variant" });

export type NameSortDirection = "asc" | "desc";

/** 名前を比べる（`Array.prototype.sort` にそのまま渡せる）。`direction` が `desc` なら逆順。 */
export function compareNames(left: string, right: string, direction: NameSortDirection = "asc"): number {
  const result = NAME_COLLATOR.compare(left, right) || NAME_TIEBREAK_COLLATOR.compare(left, right);
  return direction === "desc" ? -result : result;
}
