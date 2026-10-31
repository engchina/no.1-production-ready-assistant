import {
  parseAnswerPassageRecords,
  type AnswerPassageLabels,
  type AnswerPassageRecords,
} from "@production-ready/ui";

import { t } from "@/lib/i18n";

/**
 * 回答の段落の印・外した段落・回答全体の注意（#2738。契約は platform/contracts/answer-passages/passage-marks.json）。
 * 回答の記録の診断（`diagnostics.answer`。保存した回答は `answer_diagnostics`）の `passage_marks`・
 * `removed_passages`・`answer_warnings` を読む（回答の構造 `envelope` に置かれた記録も読む）。記録の無い過去の回答は
 * null（今までの表示のまま）。画面は Agent と同じ共有の `AnswerSections`・`AnswerPassageBadges` で描く。
 */
export function answerPassages(diagnostics: unknown): AnswerPassageRecords | null {
  if (!diagnostics || typeof diagnostics !== "object" || Array.isArray(diagnostics)) return null;
  const raw = diagnostics as Record<string, unknown>;
  return parseAnswerPassageRecords(raw) ?? parseAnswerPassageRecords(raw.envelope);
}

/**
 * 段落の印の記録のある回答の本文から、回答エンジンが本文の末尾に足した「根拠と矛盾し除いた内容」「確認が必要な点」の
 * 節（見出しの段落と、続く項目の段落）を除く（#3488）。画面はこの 2 つの節を記録から共有の `AnswerSections` で作るので、
 * 本文の節も描くと 2 回出る（Agent の `markedAnswer` と同じ扱い）。見出しは保存した表示形式（`removed_section`・
 * `marked_section`）と画面の文言で探す。本文だけを読む利用者（MCP など）向けの本文そのものは変えない。
 */
export function withoutPassageRecordSections(text: string, format?: Readonly<Record<string, string>>): string {
  const labels = answerPassageLabels();
  const titles = new Set(
    [format?.removed_section, format?.marked_section, labels.removedTitle, labels.marksTitle].filter(
      (title): title is string => Boolean(title)
    )
  );
  const parts = text.split(/\n{2,}/);
  const kept: string[] = [];
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index].trim();
    if (titles.has(part)) {
      index += 1; // 見出しの段落と、続く項目の段落を除く。
      continue;
    }
    if (titles.has(part.split("\n", 1)[0].trim())) continue; // 見出しと項目が同じ段落の形。
    kept.push(parts[index]);
  }
  return kept.join("\n\n");
}

/** 段落の印の文言（RAG のチャット・RAG 検索・実行履歴で同じ。Agent の同じ表示と同じ文言）。 */
export function answerPassageLabels(): AnswerPassageLabels {
  return {
    state: { unverified: t("search.answerText.passage.unverified"), warning: t("search.answerText.passage.warning") },
    stateDescription: {
      unverified: t("search.answerText.passage.unverifiedDescription"),
      warning: t("search.answerText.passage.warningDescription"),
    },
    toggleHint: t("search.answerText.passage.toggleHint"),
    removedTitle: t("search.answerText.passage.removedTitle"),
    removedNote: t("search.answerText.passage.removedNote"),
    marksTitle: t("search.answerText.passage.marksTitle"),
    marksNote: t("search.answerText.passage.marksNote"),
    jumpLabel: (quote) => t("search.answerText.passage.jump", { quote }),
    warningsTitle: t("search.answerText.passage.warningsTitle"),
    badge: {
      unverified: (count) => t("search.answerText.passage.badgeUnverified", { count }),
      warning: (count) => t("search.answerText.passage.badgeWarning", { count }),
      removed: (count) => t("search.answerText.passage.badgeRemoved", { count }),
    },
  };
}
