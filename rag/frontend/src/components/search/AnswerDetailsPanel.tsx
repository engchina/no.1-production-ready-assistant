import { AnswerCheckPanel, Banner, RunUsedModels } from "@production-ready/ui";
import { ragAnswerCheck } from "@/lib/answer-check";
import { parseAnswerDiagnostics, type AnswerDiagnostics } from "@/lib/answer-diagnostics";
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
 * 資料との照合の行（RAG・Agent 共通の 5 状態と部品。#141）。回答の本文の直下に出す（回答 → 資料との照合 → 回答の
 * 注意書き → 出典 → 評価。page-archetypes.md §6.1）。照合の記録は回答の記録の診断から読み、診断の無い保存した回答
 * （会話を開き直したとき）は `passageSource`（会話のメッセージ。照合の状態と段落の印を持つ）から読む。照合の記録の
 * 無い過去の回答には出さない。
 */
export function AnswerCheckRow({ diagnostics, passageSource }: { diagnostics: unknown; passageSource?: unknown }) {
  return <AnswerCheckPanel check={ragAnswerCheck(diagnostics) ?? ragAnswerCheck(passageSource)} testId="answer-check" />;
}

/**
 * 回答を読むときに要る案内（資料では決まらない値がある）。回答の本文の下に出す
 * （「詳細」に畳まない。#2096）。段落の印の数は資料との照合の行（`AnswerCheckRow`）に出す（#141）。
 */
export function AnswerNotices({ diagnostics }: { diagnostics: unknown }) {
  const data = parseAnswerDiagnostics(diagnostics);
  if (!data || !(data.externalDataRequired || data.externalDataItems.length > 0)) return null;
  return (
    <div className="space-y-2">
      <AnswerNoticeBanners data={data} />
    </div>
  );
}

function AnswerNoticeBanners({ data }: { data: AnswerDiagnostics }) {
  return (
    <>
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
