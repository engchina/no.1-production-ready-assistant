import { SIMPLIFIED_ONLY_HAN, TRADITIONAL_ONLY_HAN } from "./han-script-sets";

/**
 * 利用者の内容（回答・根拠の抜粋・質問・文書の本文）の言語（#3335）。
 *
 * 共有の部品は内容の要素に `lang` を付け、共有の tokens（`fonts.css`）の `:lang()` が漢字の字形を言語で切り替える
 * （同じ符号の漢字でも、`zh-Hans` は Noto Sans SC、`zh-Hant` は TC、`zh-HK` は HK の字形。画面の文言は `ja` のまま）。
 * 読み上げ（スクリーンリーダー）も内容の言語で読む（WCAG 3.1.2 Language of Parts）。
 */
export type ContentLang = "ja" | "zh-Hans" | "zh-Hant" | "zh-HK";

/** 判定で読む先頭の文字数（長い文書でも描画のたびの判定を軽くする）。 */
export const CONTENT_LANG_SCAN_LIMIT = 4000;

/** かなが漢字とかなの合計のこの割合以上なら日本語（中国語の文に混ざる外来語のカタカナは日本語にしない）。 */
const KANA_RATIO_JA = 0.2;

const SIMPLIFIED_ONLY = new Set(SIMPLIFIED_ONLY_HAN);
const TRADITIONAL_ONLY = new Set(TRADITIONAL_ONLY_HAN);

function isKana(code: number): boolean {
  return (
    (code >= 0x3041 && code <= 0x309f) || // ひらがな
    (code >= 0x30a0 && code <= 0x30fa) || // カタカナ（中点 U+30FB は中国語の文にも使うので数えない）
    (code >= 0x30fc && code <= 0x30ff) ||
    (code >= 0x31f0 && code <= 0x31ff) || // カタカナ拡張
    (code >= 0xff66 && code <= 0xff9f) // 半角カタカナ
  );
}

function isHan(code: number): boolean {
  return (
    (code >= 0x3400 && code <= 0x4dbf) ||
    (code >= 0x4e00 && code <= 0x9fff) ||
    (code >= 0xf900 && code <= 0xfaff) ||
    (code >= 0x20000 && code <= 0x3ffff)
  );
}

/**
 * 文字だけから内容の言語を決める軽い判定（特定の文書・語に合わせない汎用の規則）。決められなければ `undefined`
 * （`lang` を付けず、画面の言語 `ja` のまま）。
 *
 * - かなが漢字とかなの 2 割以上 → `ja`。
 * - 簡体字だけ・繁体字だけの字（Unihan の符号化の表から作った集合。`scripts/generate-han-script-sets.py`）が
 *   あれば、多い方が 2 倍以上なら `zh-Hans` / `zh-Hant`（かなが少しあっても、外来語のカタカナとみなす）。
 *   ただし、かながあって印の字が 1 字だけなら日本語の文の旧字とみなして `ja`。
 * - かながあれば `ja`。漢字だけで印の字が無い（日本語とも中国語とも読める）・漢字が無いなら `undefined`。
 * - 香港の字形（`zh-HK`）は字だけでは台湾の繁体字と分けられないので判定しない（取込の言語の値でだけ付ける）。
 */
export function detectContentLang(text: string | null | undefined): ContentLang | undefined {
  if (!text) return undefined;
  let kana = 0;
  let han = 0;
  let simplified = 0;
  let traditional = 0;
  let scanned = 0;
  for (const char of text) {
    if (scanned >= CONTENT_LANG_SCAN_LIMIT) break;
    scanned += 1;
    const code = char.codePointAt(0) ?? 0;
    if (isKana(code)) {
      kana += 1;
    } else if (isHan(code)) {
      han += 1;
      if (SIMPLIFIED_ONLY.has(char)) simplified += 1;
      else if (TRADITIONAL_ONLY.has(char)) traditional += 1;
    }
  }
  if (kana + han === 0) return undefined;
  if (kana / (kana + han) >= KANA_RATIO_JA) return "ja";
  const marks = simplified + traditional;
  if (marks > 0 && !(kana > 0 && marks < 2)) {
    if (simplified >= traditional * 2) return "zh-Hans";
    if (traditional >= simplified * 2) return "zh-Hant";
    return kana > 0 ? "ja" : undefined;
  }
  return kana > 0 ? "ja" : undefined;
}

/**
 * 取込などで分かっている言語の値（BCP 47）を、`:lang()` の token に当たる形にそろえる。`zh-CN` → `zh-Hans`、
 * `zh-TW` → `zh-Hant`、`zh-HK` / `zh-MO` / `zh-Hant-HK` → `zh-HK`、`ja-JP` → `ja`。文字の種類が分からない
 * `zh` は `undefined`（呼び出し側が本文から判定する）。ほかの言語（`en` など）はそのまま返す。
 */
export function normalizeContentLang(value: string): string | undefined {
  const tags = value.trim().replaceAll("_", "-").split("-").filter(Boolean);
  const language = tags[0]?.toLowerCase();
  if (!language) return undefined;
  if (language === "ja") return "ja";
  if (language !== "zh") return value.trim();
  const rest = tags.slice(1).map((tag) => tag.toLowerCase());
  const region = rest.find((tag) => /^[a-z]{2}$/u.test(tag));
  if (region === "hk" || region === "mo") return "zh-HK";
  if (rest.includes("hant")) return "zh-Hant";
  if (rest.includes("hans")) return "zh-Hans";
  if (region === "tw") return "zh-Hant";
  if (region === "cn" || region === "sg" || region === "my") return "zh-Hans";
  return undefined;
}

/**
 * 画面の文言（部品・製品の文言）の言語。`<html lang>` の値（無ければ `ja`）。
 *
 * 共有の部品は内容の `lang` を内容だけを包む要素に付け、画面の文言はそれを継承しない所に置く（#3349）。内容の要素の
 * 中に置くしかない部品の文言（リンクの中の読み上げの補足など）にだけ、この値の `lang` を付ける。
 */
export function uiTextLang(): string {
  const lang = typeof document === "undefined" ? "" : document.documentElement.lang.trim();
  return lang || "ja";
}

/**
 * 内容の要素に付ける `lang` を決める。`lang` が文字列（取込で分かっている言語）ならそれを使い、`null` なら付けない、
 * 渡さなければ（`undefined`）本文から {@link detectContentLang} で判定する。`zh`（文字の種類が分からない）は本文の
 * 判定が中国語ならその値、そうでなければ `zh` のまま。
 */
export function resolveContentLang(lang: string | null | undefined, text: string | null | undefined): string | undefined {
  if (lang === null) return undefined;
  if (lang !== undefined && lang.trim() !== "") {
    const normalized = normalizeContentLang(lang);
    if (normalized) return normalized;
    const detected = detectContentLang(text);
    return detected && detected !== "ja" ? detected : lang.trim();
  }
  return detectContentLang(text);
}
