import { APP_ROUTES } from "@/lib/routes";

import {
  EXTRACTION_FIELD_FILTER_KEY,
  extractionFieldFilterRowsFromConditions,
  parseExtractionFieldFilterValue,
  type ExtractionFieldFilterRow,
} from "./extraction-field-filters";

/**
 * RAG 検索の画面の条件と、URL で画面に入れる初期値（実行の詳細の「RAG 検索で開く」。#3309）。
 *
 * 実行履歴の詳細は、その実行の質問・検索・回答プロファイル・回答の生成の有無・絞り込みを URL に入れて
 * RAG 検索の画面を開く。画面は URL の値を入力欄と条件に一度だけ入れ（作業状態に残す）、URL から外す。
 * 検索はしない（利用者が「検索」を押す）。今は使えない検索・回答プロファイル・項目は、画面の通常の検証に任せる。
 */

/** 文書の分類（完全一致）と有効期間の基準日。backend の検索 filters のキーと同じ名前。 */
export const CLASSIFICATION_FILTER_KEYS = ["large_category", "middle_category", "small_category", "as_of"] as const;
export type ClassificationFilterValues = Record<(typeof CLASSIFICATION_FILTER_KEYS)[number], string>;
export const EMPTY_CLASSIFICATION_FILTERS: ClassificationFilterValues = {
  large_category: "",
  middle_category: "",
  small_category: "",
  as_of: "",
};

const QUESTION_PARAM = "question";
const PROFILE_PARAM = "search_answer_profile_id";
const GENERATE_ANSWER_PARAM = "generate_answer";
// 廃止した URL の値は読み取らず、他の初期値と合わせて除く。
const RETIRED_TOP_K_PARAM = "top_k";

/** URL で受け取る初期値のキー（一度だけ入れて URL から消す）。`question` が無い URL は初期値として扱わない。 */
export const SEARCH_PREFILL_KEYS = [
  QUESTION_PARAM,
  PROFILE_PARAM,
  GENERATE_ANSWER_PARAM,
  RETIRED_TOP_K_PARAM,
  ...CLASSIFICATION_FILTER_KEYS,
  EXTRACTION_FIELD_FILTER_KEY,
] as const;

/** 画面に入れる初期値（URL に無い条件は既定に戻す。前の作業の条件を混ぜない）。 */
export interface SearchPrefill {
  question: string;
  searchAnswerProfileId: string | null;
  generateAnswer: boolean;
  classification: ClassificationFilterValues;
  extractionRows: ExtractionFieldFilterRow[];
}

export interface SearchPrefillInput {
  question: string;
  searchAnswerProfileId: string | null;
  generateAnswer: boolean;
  /** 記録の絞り込み（`filters` のキー → 値）。画面が送れない条件は URL に入れない。 */
  filters?: Record<string, string>;
}

/** RAG 検索の画面を、質問と条件を入れて開く URL。 */
export function searchPrefillHref(input: SearchPrefillInput): string {
  const params = new URLSearchParams();
  params.set(QUESTION_PARAM, input.question);
  if (input.searchAnswerProfileId) params.set(PROFILE_PARAM, input.searchAnswerProfileId);
  params.set(GENERATE_ANSWER_PARAM, input.generateAnswer ? "true" : "false");
  for (const key of [...CLASSIFICATION_FILTER_KEYS, EXTRACTION_FIELD_FILTER_KEY]) {
    const value = input.filters?.[key]?.trim();
    if (value) params.set(key, value);
  }
  return `${APP_ROUTES.search}?${params.toString()}`;
}

/** URL の初期値を読む。質問の無い URL は null（初期値として扱わない）。値は書き換えずにそのまま入れる。 */
export function searchPrefillFromParams(params: URLSearchParams): SearchPrefill | null {
  const question = params.get(QUESTION_PARAM);
  if (question == null || !question.trim()) return null;
  const classification = { ...EMPTY_CLASSIFICATION_FILTERS };
  for (const key of CLASSIFICATION_FILTER_KEYS) classification[key] = params.get(key)?.trim() ?? "";
  return {
    question,
    searchAnswerProfileId: params.get(PROFILE_PARAM) || null,
    generateAnswer: params.get(GENERATE_ANSWER_PARAM) === "true",
    classification,
    extractionRows: extractionFieldFilterRowsFromConditions(
      parseExtractionFieldFilterValue(params.get(EXTRACTION_FIELD_FILTER_KEY) ?? undefined)
    ),
  };
}

/** URL に初期値のキーが残っているか（画面が入れた後に外す）。 */
export function hasSearchPrefillParams(params: URLSearchParams): boolean {
  return SEARCH_PREFILL_KEYS.some((key) => params.has(key));
}
