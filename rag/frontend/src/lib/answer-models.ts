import type { CompareModel } from "./api";
import { t, type I18nKey } from "./i18n";

/** 回答するモデルのチップの名前（「{name}（テキスト）」など。#675 / #888）。 */
export function answerModelLabel(model: CompareModel): string {
  return t(`answerModel.${model.kind}`, { name: model.display_name });
}

/**
 * 回答するモデルの説明の文言（#675 / #888）。ラベルの横の info アイコンの吹き出しに出す（#901）。
 *
 * 候補が 2 件（テキストと画像対応が別のモデル）なら画面ごとの説明（`multipleKey`）。1 件のときは、
 * そのモデルが画像対応モデルも兼ねるか（`text_vision`）で分け、画像対応モデルを隠さない。
 */
export function answerModelHelpKey(models: readonly CompareModel[], multipleKey: I18nKey): I18nKey {
  if (models.length > 1) return multipleKey;
  return models[0]?.kind === "text_vision"
    ? "search.answerModel.helpTextVision"
    : "search.answerModel.helpTextOnly";
}
