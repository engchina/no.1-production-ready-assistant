import { BookCheck, MessageSquareText } from "lucide-react";

import {
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  StatusBadge,
} from "@production-ready/ui";

import type { ApprovedFaqSuggestionData } from "@/lib/api";
import { t } from "@/lib/i18n";

/**
 * 回答前に提示する類似の承認済み FAQ。
 * RAG 検索（search）は選ぶと LLM を使わず FAQ の回答を表示する。チャット（chat）は選んだ類似問を質問と
 * 一緒に LLM へ渡して回答する（#684）。どちらも「類似問を使わない」以外に飛ばす操作は無い。
 */
export function ApprovedFaqSuggestions({
  suggestions,
  onUse,
  onSkip,
  mode = "search",
  disabled = false,
}: {
  suggestions: ApprovedFaqSuggestionData[];
  onUse: (suggestion: ApprovedFaqSuggestionData) => void;
  onSkip: () => void;
  mode?: "search" | "chat";
  disabled?: boolean;
}) {
  return (
    <Card data-testid={`${mode}-approved-faq-suggestions`}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <BookCheck size={16} className="text-accent-fg" aria-hidden />
          {t(`${mode}.faq.title`)}
        </CardTitle>
        <CardDescription>{t(`${mode}.faq.description`)}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <ul className="space-y-2">
          {suggestions.map((suggestion) => (
            <li
              key={suggestion.id}
              className="flex min-w-0 flex-col gap-2 rounded-md border border-border bg-surface p-3 sm:flex-row sm:items-start sm:justify-between"
            >
              <div className="min-w-0 space-y-1">
                <p className="break-words text-sm font-medium text-fg">
                  {suggestion.question}
                </p>
                <p className="line-clamp-2 whitespace-pre-wrap break-words text-xs text-fg-muted">
                  {suggestion.answer}
                </p>
                <StatusBadge
                  variant={suggestion.direct ? "success" : "neutral"}
                  label={t(`${mode}.faq.score`, {
                    value: Math.round(suggestion.score * 100),
                  })}
                />
              </div>
              <Button
                size="sm"
                variant={suggestion.direct ? "primary" : "secondary"}
                className="shrink-0"
                disabled={disabled}
                onClick={() => onUse(suggestion)}
              >
                {t(`${mode}.faq.use`)}
              </Button>
            </li>
          ))}
        </ul>
        <Button
          size="sm"
          variant="ghost"
          icon={MessageSquareText}
          disabled={disabled}
          onClick={onSkip}
        >
          {t(`${mode}.faq.skip`)}
        </Button>
      </CardContent>
    </Card>
  );
}

/** 選んだ承認済み FAQ の回答(LLM 不使用)。 */
export function ApprovedFaqAnswer({
  suggestion,
}: {
  suggestion: ApprovedFaqSuggestionData;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <BookCheck size={16} className="text-accent-fg" aria-hidden />
          {t("search.faq.answerTitle")}
        </CardTitle>
        <CardDescription>{t("search.faq.answerDescription")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="break-words text-xs text-fg-muted">
          {t("search.faq.matched", { question: suggestion.question })}
        </p>
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-fg">
          {suggestion.answer}
        </p>
      </CardContent>
    </Card>
  );
}
