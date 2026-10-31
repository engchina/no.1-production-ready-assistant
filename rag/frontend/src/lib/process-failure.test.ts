import { describe, expect, it } from "vitest";

import { chatAnswerFailure, failedStageLabel, runFailure, storageFailureNotice } from "./process-failure";

describe("RAG の失敗を共通の失敗の表示に写す（Issue 2342）", () => {
  it("チャットの失敗は止まった工程を 1 文目に入れ、backend の文を原因、エラーの種類・コード・時刻を詳細にする", () => {
    const failure = chatAnswerFailure({
      message: "検索処理中にエラーが発生しました。",
      traceId: "trace-1",
      errorType: "DatabaseError",
      errorCode: "ORA-03146",
      stage: "文書検索",
      occurredAt: "2026-10-18T06:30:41Z",
    });
    expect(failure.summary).toBe("「検索文から根拠の候補を集める」で処理が止まりました。");
    expect(failure.cause).toBe("検索処理中にエラーが発生しました。");
    expect(failure.nextAction).toContain("トレース ID を添えて管理者に連絡");
    expect(failure).toMatchObject({ failedStep: "検索文から根拠の候補を集める", errorCode: "ORA-03146", traceId: "trace-1" });
  });

  it("工程の分からない失敗（保存済みの ERROR の回答）は定型の 1 文目にする", () => {
    expect(chatAnswerFailure({ message: "中断しました。", traceId: "t" }).summary).toBe("回答を作成できませんでした。");
  });

  it("止まった段はパイプラインの工程・回答フローの工程のどちらの名前でも引く", () => {
    expect(failedStageLabel("answer_record_save")).toBe("質問と回答と根拠の記録を保存する");
    expect(failedStageLabel("answer_step:文書検索（2回目）")).toBe("検索文から根拠の候補を集める（2 回目）");
    expect(failedStageLabel("文書検索")).toBe("検索文から根拠の候補を集める");
  });

  it("保存できなかった記録は、保存できなかったものごとの影響とコードを warning にする", () => {
    expect(storageFailureNotice([], "t")).toBeNull();
    const notice = storageFailureNotice(
      [
        { step: "answer_record_save", error_type: "DatabaseError", error_code: "ORA-03146" },
        { step: "chat_message_save", error_type: "DatabaseError", error_code: "ORA-40441" },
      ],
      "trace-1"
    );
    expect(notice?.summary).toBe("回答は表示していますが、一部を保存できませんでした。");
    expect(notice?.cause?.split("\n")).toHaveLength(2);
    expect(notice?.errorCode).toBe("ORA-03146・ORA-40441");
    expect(notice?.failedStep).toBe("質問と回答と根拠の記録を保存する・回答を会話に保存する");
  });

  it("実行の詳細は失敗した実行だけ、チャットと同じ 1 文目にする", () => {
    expect(runFailure({ status: "success", error_stage: null, error_type: null, error_code: null })).toBeNull();
    expect(
      runFailure({ status: "error", error_stage: "retrieval", error_type: "DatabaseError", error_code: "ORA-03146" })
    ).toMatchObject({ summary: "「文書から根拠の候補を検索する」で処理が止まりました。", errorCode: "ORA-03146", failedStep: "文書から根拠の候補を検索する" });
  });
});
