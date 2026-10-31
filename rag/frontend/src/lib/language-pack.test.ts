import { describe, expect, it } from "vitest";

import type { LanguagePackField, LanguagePackSections } from "./api";
import {
  changesIngestFields,
  isChanged,
  languagePackErrors,
  overridesFromTexts,
  textsFromSections,
} from "./language-pack";

const FIELDS: LanguagePackField[] = [
  { section: "numbers", key: "article_units", kind: "words", choices: [], ingest: true, max_chars: 40 },
  { section: "numbers", key: "kanji_digits", kind: "char_values", choices: [], ingest: true, max_chars: 40 },
  { section: "symbols", key: "normalization", kind: "choice", choices: ["NFKC", "NFC", "none"], ingest: true, max_chars: 40 },
  { section: "symbols", key: "bracket_pairs", kind: "pairs", choices: [], ingest: true, max_chars: 40 },
  {
    section: "english",
    key: "en_section_words",
    kind: "enum_map",
    choices: ["chapter", "section", "article", "appendix"],
    ingest: true,
    max_chars: 40,
  },
  { section: "symbols", key: "name_chars", kind: "char_ranges", choices: [], ingest: true, max_chars: 40 },
];
const BUILTIN: LanguagePackSections = {
  numbers: { article_units: ["条"], kanji_digits: ["一=1"] },
  symbols: { normalization: "NFKC", bracket_pairs: ["()"], name_chars: ["一-鿿"] },
  english: { en_section_words: ["section=section"] },
};

describe("言語資源の画面の値（Issue 2181）", () => {
  it("組み込みと違う項目だけを、空行・前後の空白・重複を除いて保存の形にする", () => {
    const texts = { ...textsFromSections(FIELDS, BUILTIN), "numbers.article_units": " 条\n\n款\n款\n" };
    expect(overridesFromTexts(FIELDS, texts, BUILTIN)).toEqual({ numbers: { article_units: ["条", "款"] } });
    expect(isChanged(FIELDS[0], texts, BUILTIN)).toBe(true);
    expect(isChanged(FIELDS[1], texts, BUILTIN)).toBe(false);
    expect(overridesFromTexts(FIELDS, textsFromSections(FIELDS, BUILTIN), BUILTIN)).toEqual({});
  });

  it("項目の種類ごとに形の誤りを欄のエラーにする（backend と同じ規則）", () => {
    const texts = {
      ...textsFromSections(FIELDS, BUILTIN),
      "numbers.kanji_digits": "一=いち",
      "symbols.normalization": "NFKD",
      "symbols.bracket_pairs": "()\n(",
      "english.en_section_words": "part=volume",
      "symbols.name_chars": "あいう",
    };
    expect(Object.keys(languagePackErrors(FIELDS, texts)).sort()).toEqual([
      "english.en_section_words",
      "numbers.kanji_digits",
      "symbols.bracket_pairs",
      "symbols.name_chars",
      "symbols.normalization",
    ]);
    expect(languagePackErrors(FIELDS, textsFromSections(FIELDS, BUILTIN))).toEqual({});
  });

  it("文書の分割し直しの案内は、取込の項目を変えたときだけ出す（検索の語だけなら出さない）", () => {
    const fields: LanguagePackField[] = [
      ...FIELDS,
      { section: "search_terms", key: "stopwords", kind: "words", choices: [], ingest: false, max_chars: 40 },
    ];
    const builtin: LanguagePackSections = { ...BUILTIN, search_terms: { stopwords: ["こと"] } };
    const saved = textsFromSections(fields, builtin);
    expect(changesIngestFields(fields, { ...saved, "search_terms.stopwords": "こと\nもの" }, saved)).toBe(false);
    expect(changesIngestFields(fields, { ...saved, "numbers.article_units": "条\n箇条" }, saved)).toBe(true);
  });
});


it("数値マッピングは API の下限に従い、変換不能な数字を拒否する", () => {
  const field: LanguagePackField = { ...FIELDS[1], key: "roman_digits", min_value: 1 };
  for (const value of ["v=0", "v=²", "v=１", "v=-1"]) {
    expect(languagePackErrors([field], { "numbers.roman_digits": value })).toHaveProperty("numbers.roman_digits");
  }
  expect(languagePackErrors([field], { "numbers.roman_digits": "i=1\nv=5" })).toEqual({});
  expect(languagePackErrors([FIELDS[1]], { "numbers.kanji_digits": "〇=0" })).toEqual({});
});
