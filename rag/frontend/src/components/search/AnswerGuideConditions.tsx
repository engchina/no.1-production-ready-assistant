import { CircleCheck, CircleHelp, ListChecks, Split, Target, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import type { AnswerGuide } from "@/lib/answer-diagnostics";
import { t } from "@/lib/i18n";

/**
 * 回答に使った業務ガイドの条件の状態（#1287）。どの条件を分かっているものとして答えたか（値と出所）、
 * 何が分からなかったか、質問から複数の候補が読み取れて決められなかった条件と、適用範囲を確かめたかを、
 * 業務の言葉で出す。業務ガイドの名前と版は回答の詳細の見出しのバッジが出すので、ここでは重ねない。
 */
export function AnswerGuideConditions({ guide }: { guide: AnswerGuide }) {
  const unknown = guide.unresolved.filter((item) => item.state === "unknown");
  const conflicting = guide.unresolved.filter((item) => item.state === "conflicting");
  if (!guide.known.length && !guide.unresolved.length && !guide.applicability.length) return null;
  return (
    <section
      className="space-y-3 rounded-md border border-border bg-surface p-3"
      aria-label={t("search.answerDetails.guideConditions.title")}
      data-testid="answer-guide-conditions"
    >
      <p className="flex items-center gap-1.5 text-xs font-medium text-fg-muted">
        <ListChecks size={14} aria-hidden />
        {t("search.answerDetails.guideConditions.title")}
      </p>
      {guide.known.length ? (
        <ConditionGroup
          testId="guide-conditions-known"
          icon={CircleCheck}
          iconClassName="text-success-fg"
          title={t("search.answerDetails.guideConditions.known", { count: guide.known.length })}
        >
          {guide.known.map((item) => (
            <li key={item.id} className="break-words">
              <span className="text-fg-muted">{item.label}: </span>
              <span className="font-medium text-fg">{item.value}</span>
              {item.source ? (
                <span className="text-xs text-fg-muted">
                  {t("search.answerDetails.guideConditions.sourceSuffix", {
                    source: t(`search.answerDetails.guideConditions.source.${item.source}`),
                  })}
                </span>
              ) : null}
            </li>
          ))}
        </ConditionGroup>
      ) : null}
      {conflicting.length ? (
        <ConditionGroup
          testId="guide-conditions-conflicting"
          icon={Split}
          iconClassName="text-warning-fg"
          title={t("search.answerDetails.guideConditions.conflicting", { count: conflicting.length })}
        >
          {conflicting.map((item) => (
            <li key={item.id} className="break-words">
              <span className="text-fg-muted">{item.label}: </span>
              <span className="font-medium text-fg">
                {item.candidates
                  .map((value) => t("search.answerDetails.guideConditions.candidate", { value }))
                  .join(t("search.answerDetails.guideConditions.candidateSeparator"))}
              </span>
              <Handling handling={item.handling} />
            </li>
          ))}
        </ConditionGroup>
      ) : null}
      {unknown.length ? (
        <ConditionGroup
          testId="guide-conditions-unknown"
          icon={CircleHelp}
          iconClassName="text-warning-fg"
          title={t("search.answerDetails.guideConditions.unknown", { count: unknown.length })}
        >
          {unknown.map((item) => (
            <li key={item.id} className="break-words">
              <span className="font-medium text-fg">{item.label}</span>
              <Handling handling={item.handling} />
            </li>
          ))}
        </ConditionGroup>
      ) : null}
      {guide.applicability.length ? (
        <ConditionGroup
          testId="guide-conditions-applicability"
          icon={Target}
          iconClassName="text-fg-muted"
          title={t("search.answerDetails.guideConditions.applicability")}
        >
          {guide.applicability.map((item) => (
            <li key={item.key} className="break-words">
              <span className="text-fg-muted">
                {t(`search.answerDetails.guideConditions.applicability.${item.key}`)}:{" "}
              </span>
              <span className="text-fg">
                {t(`search.answerDetails.guideConditions.applicability.${item.state}`)}
              </span>
            </li>
          ))}
        </ConditionGroup>
      ) : null}
    </section>
  );
}

function ConditionGroup({
  testId,
  icon: Icon,
  iconClassName,
  title,
  children,
}: {
  testId: string;
  icon: LucideIcon;
  iconClassName: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <div data-testid={testId} className="space-y-1">
      {/* 状態は色だけでなく、アイコンと見出しの文で示す。 */}
      <p className="flex items-center gap-1.5 text-xs font-semibold text-fg">
        <Icon size={14} className={`shrink-0 ${iconClassName}`} aria-hidden />
        {title}
      </p>
      <ul className="space-y-1 pl-5 text-sm leading-relaxed">{children}</ul>
    </div>
  );
}

function Handling({ handling }: { handling: AnswerGuide["unresolved"][number]["handling"] }) {
  if (!handling) return null;
  return (
    <span className="text-xs text-fg-muted">
      {t("search.answerDetails.guideConditions.handlingSuffix", {
        handling: t(`search.answerDetails.guideConditions.handling.${handling}`),
      })}
    </span>
  );
}
