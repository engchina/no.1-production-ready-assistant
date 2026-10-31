import { Card, CardHeader, CardTitle, CardDescription, CardContent, Disclosure, TextareaField } from "@production-ready/ui";
import { Fragment } from "react";
import { FileText } from "lucide-react";

import type { AnswerPromptStage } from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";

import { AnswerPromptCard, AnswerPromptPanel } from "./AnswerPromptEditor";
import { groupStagesByPhase } from "./answer-prompt.logic";

/**
 * backend の順序で各段を分類し、使用条件と原文を示す。
 * 回答テンプレートの編集欄は、生成の system 指示の後に置く。
 */
export function AnswerPromptStages({
  stages,
  titleKey = "settings.answerPrompts.stages.title",
  descriptionKey = "settings.answerPrompts.stages.description",
}: {
  stages: AnswerPromptStage[];
  titleKey?: I18nKey;
  descriptionKey?: I18nKey;
}) {
  return (
    <Card role="region" aria-labelledby="answer-prompt-stages-title" data-testid="answer-prompt-stages-card">
      <CardHeader>
        <CardTitle id="answer-prompt-stages-title" className="flex items-center gap-2">
          <FileText size={16} className="text-accent-fg" aria-hidden />
          {t(titleKey)}
        </CardTitle>
        <CardDescription>{t(descriptionKey)}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
      {groupStagesByPhase(stages).map((group, index) => {
        const headingId = `answer-prompt-phase-${index}`;
        return (
          <section key={`${group.phase}-${index}`} className="space-y-2" aria-labelledby={headingId}>
            <h3 id={headingId} className="text-sm font-semibold text-fg">
              {t(`settings.answerPrompts.phase.${group.phase}` as I18nKey)}
            </h3>
            <p className="text-xs leading-relaxed text-fg-muted">
              {t(`settings.answerPrompts.phase.${group.phase}.description` as I18nKey)}
            </p>
            {group.stages.map((stage) => (
              <Fragment key={stage.id}>
                {stage.prompts.length > 0 ? (
                  <Disclosure
                    summary={t(`settings.answerPrompts.stage.${stage.id}` as I18nKey)}
                    description={t("settings.answerPrompts.condition", {
                      condition: t(`settings.answerPrompts.stage.${stage.id}.condition` as I18nKey),
                    })}
                    surface="sunken"
                    contentClassName="space-y-3"
                    data-testid={`answer-prompt-stage-${stage.id}`}
                  >
                    {stage.prompts.map((part) => (
                      part.prompt_key ? <AnswerPromptPanel
                        key={part.id}
                        promptKey={part.prompt_key}
                        fieldLabel={t(`settings.answerPrompts.part.${part.id}` as I18nKey)}
                      /> : <TextareaField
                        key={part.id}
                        id={`answer-prompt-${stage.id}-${part.id}`}
                        label={t(`settings.answerPrompts.part.${part.id}` as I18nKey)}
                        value={part.content}
                        readOnly
                        monospace
                        rows={12}
                      />
                    ))}
                  </Disclosure>
                ) : null}
                {stage.id === "generation" ? <AnswerPromptCard promptKey="vlm_answer" /> : null}
              </Fragment>
            ))}
          </section>
        );
      })}
      </CardContent>
    </Card>
  );
}
