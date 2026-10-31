import { AnswerPassageBadges, Banner, RunUsedModels, type AnswerPassageRecords } from "@production-ready/ui";
import { parseAnswerDiagnostics, type AnswerDiagnostics } from "@/lib/answer-diagnostics";
import { answerPassageLabels, answerPassages } from "@/lib/answer-passages";
import { t } from "@/lib/i18n";

/** 処理の詳細に出す、回答時点のモデルの記録。 */
export function AnswerDetailsPanel({ diagnostics, recordedModels = [] }: { diagnostics: unknown; recordedModels?: readonly string[] }) {
  const data = parseAnswerDiagnostics(diagnostics);
  const models = [
    ...(data?.models.llm ? [{ label: t("search.answerDetails.model.llm"), value: data.models.llm.label, title: data.models.llm.modelId }] : []),
    ...(data?.models.embedding ? [{ label: t("search.answerDetails.model.embedding"), value: data.models.embedding }] : []),
    ...(data?.models.rerank ? [{ label: t("search.answerDetails.model.rerank"), value: data.models.rerank }] : []),
  ];
  return <RunUsedModels models={[...models, ...recordedModels.map(value => ({ label: t("search.answerDetails.model.llm"), value }))]} />;
}

/**
 * 回答を読むときに要る案内（段落の印の数のバッジ・公開の業務ガイドを読めなかった・資料では決まらない値がある）。
 * 回答の本文の下に出す（「詳細」に畳まない。#2096）。段落の印の記録（#2738）は回答の記録の診断から読み、診断の無い
 * 保存した回答（会話を開き直したとき）は `passageSource`（会話のメッセージ）から読む。
 */
export function AnswerNotices({ diagnostics, passageSource }: { diagnostics: unknown; passageSource?: unknown }) {
  const data = parseAnswerDiagnostics(diagnostics);
  const passages = answerPassages(diagnostics) ?? answerPassages(passageSource);
  const notices = data && (data.guideLoadFailed || data.externalDataRequired || data.externalDataItems.length > 0);
  if (!notices && !passages) return null;
  return (
    <div className="space-y-2">
      {data ? <AnswerNoticeBanners data={data} passages={passages} /> : <PassageBadges passages={passages} />}
    </div>
  );
}

/**
 * 段落の印の数のバッジ（「未確認 N 件」「要注意 N 件」「矛盾で除外 N 件」。0 件は出さない。Agent の「資料との照合」と
 * 同じ共有の部品。#2738）。
 */
function PassageBadges({ passages }: { passages: AnswerPassageRecords | null }) {
  return <AnswerPassageBadges records={passages} labels={answerPassageLabels()} testId="answer-passage-badges" />;
}

function AnswerNoticeBanners({ data, passages }: { data: AnswerDiagnostics; passages: AnswerPassageRecords | null }) {
  return (
    <>
      <PassageBadges passages={passages} />
      {data.guideLoadFailed ? (
        // 公開の業務ガイドを読めず、使わずに答えた（ガイドが無かったときと区別する。#1278 / #1287）。
        <div data-testid="answer-guide-load-failed">
          <Banner severity="warning" title={t("search.answerDetails.guideLoadFailed.title")}>
            <p>{t("search.answerDetails.guideLoadFailed.body")}</p>
          </Banner>
        </div>
      ) : null}
      {data.externalDataRequired || data.externalDataItems.length ? (
        // 資料では決まらない値（rag_poc の「外部データ確認」。#651）。
        <Banner severity="warning" title={t("search.answerDetails.externalData.title")}>
          <p>{t("search.answerDetails.externalData.hint")}</p>
          {data.externalDataItems.length ? (
            <ul className="mt-1 list-disc pl-5">
              {data.externalDataItems.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
        </Banner>
      ) : null}
    </>
  );
}
