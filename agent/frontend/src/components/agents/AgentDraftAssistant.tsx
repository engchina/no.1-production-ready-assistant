import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { WandSparkles } from "lucide-react";
import { ApiErrorBanner, Banner, Button, Card, CardContent, Section, TextareaField } from "@production-ready/ui";

import { agentApi, type AgentDraftSuggestion } from "@/lib/api";
import { t } from "@/lib/i18n";

/** 業務の説明の文字数の上限（backend の `drafting.MAX_DESCRIPTION_CHARS`）。 */
const MAX_DESCRIPTION_CHARS = 4000;

/**
 * 業務 Agent の新規作成の「説明から下書きを作る」（#2519）。業務の説明を AI に渡し、名前・説明・指示・スキル・
 * データの範囲・評価の問いの提案を受けて、親がフォームに入れる（保存はいつもの「作成」）。
 */
export function AgentDraftAssistant({
  disabled,
  onApply,
}: {
  disabled?: boolean;
  onApply: (suggestion: AgentDraftSuggestion) => void;
}) {
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const suggest = useMutation({
    mutationFn: (text: string) => agentApi.suggestAgentDraft(text),
    onSuccess: (suggestion) => onApply(suggestion),
  });

  function submit() {
    const text = description.trim();
    if (!text) {
      setError(t("agent.draftAssistant.required"));
      return;
    }
    setError(null);
    suggest.mutate(text);
  }

  const notices = suggest.data?.notices ?? [];
  return (
    <Section title={t("agent.draftAssistant.title")} description={t("agent.draftAssistant.description")}>
      <Card className="min-w-0">
        <CardContent className="space-y-3 pt-5" data-testid="agent-draft-assistant">
          <TextareaField
            id="agent-draft-description"
            label={t("agent.draftAssistant.label")}
            helper={t("agent.draftAssistant.helper")}
            placeholder={t("agent.draftAssistant.placeholder")}
            width="full"
            rows={4}
            maxLength={MAX_DESCRIPTION_CHARS}
            value={description}
            error={error ?? undefined}
            disabled={disabled}
            onValueChange={(value) => {
              setDescription(value);
              setError(null);
            }}
          />
          <div>
            <Button
              type="button"
              icon={WandSparkles}
              loading={suggest.isPending}
              disabled={disabled}
              onClick={submit}
              data-testid="agent-draft-suggest"
            >
              {t("agent.draftAssistant.submit")}
            </Button>
          </div>
          {suggest.error ? (
            <ApiErrorBanner error={suggest.error} fallback={t("agent.draftAssistant.failed")} testId="agent-draft-error" />
          ) : null}
          {notices.length ? (
            <div data-testid="agent-draft-notices">
              <Banner severity="info" title={t("agent.draftAssistant.noticesTitle")}>
                <ul className="list-disc space-y-0.5 pl-5">
                  {notices.map((notice) => (
                    <li key={notice}>{notice}</li>
                  ))}
                </ul>
              </Banner>
            </div>
          ) : null}
        </CardContent>
      </Card>
    </Section>
  );
}
