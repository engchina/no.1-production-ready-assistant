import { describe, expect, it } from "vitest";

import {
  chunkPreviewForm,
  chunkPreviewValidationError,
  classificationPeriodIsValid,
  documentDisplayStatus,
  ingestConflictBannerIsStale,
  isIndexedTransition,
  phaseLabelKey,
  phaseRetryLabelKey,
  phaseRunningMessageKey,
  phaseStartedMessageKey,
  recipeIngestionActive,
  resolveDocumentActionPlan,
  resolveExtractStageProgress,
  resolveIngestionParserDisplay,
  resolveIngestionProgressSummary,
  resolvePhaseRows,
  resolveStatusMessageSlot,
  selectDocumentRecipe,
  shouldShowProcessingWatchBanner,
  shouldStopLocalProcessingWatch,
} from "./DocumentWorkspace.logic";
import {
  resolveDocumentFailureView,
  resolveIngestionErrorDisplayPlan,
} from "./ingestion-error-display";
import { t } from "@/lib/i18n";
import type {
  DocumentRecipeStep,
  DocumentRecipeView,
  FileStatus,
  IngestionJobPhase,
  IngestionJobProgress,
  IngestionVisionProgress,
} from "@/lib/api";

// #581: 文書詳細の見出し（PageHeader の状態）と本文は、同じレシピ・同じ状態を指す。
describe("文書詳細の見出しの状態", () => {
  const recipes = [
    { recipe_id: "recipe-1", status: "INDEXED" },
    { recipe_id: "recipe-2", status: "REVIEW" },
  ] as DocumentRecipeView[];

  it("?recipe= のレシピを選び、無い・見つからないときは先頭のレシピ", () => {
    expect(selectDocumentRecipe(recipes, "recipe-2")?.recipe_id).toBe("recipe-2");
    expect(selectDocumentRecipe(recipes, null)?.recipe_id).toBe("recipe-1");
    expect(selectDocumentRecipe(recipes, "missing")?.recipe_id).toBe("recipe-1");
    expect(selectDocumentRecipe(undefined, "recipe-1")).toBeNull();
  });

  it("状態はレシピの状態、無ければ文書の状態、どちらも無ければ UPLOADED", () => {
    expect(documentDisplayStatus(recipes[1], "INDEXED")).toBe("REVIEW");
    expect(documentDisplayStatus(null, "ERROR")).toBe("ERROR");
    expect(documentDisplayStatus(null, undefined)).toBe("UPLOADED");
  });
});

// #2687: 失敗したレシピを再処理すると、レシピは worker が claim するまで前回の ERROR と本文を持つ。
// 見出しは「〈工程〉の順番待ち」を出すため、本文も同じ規則で前回の失敗を出さない。
describe("再処理の job が動いている間の前回の失敗", () => {
  const failedRecipe = (steps: DocumentRecipeStep["status"][]) =>
    ({
      recipe_id: "recipe-1",
      status: "ERROR",
      failed_phase: "INDEX",
      error_message: "前回の失敗の本文",
      steps: (["PREPROCESS", "EXTRACT", "CHUNK", "INDEX"] as const).map((phase, index) => ({
        phase,
        status: steps[index],
        started_at: null,
        finished_at: null,
        error_message: steps[index] === "FAILED" ? "前回の失敗の本文" : null,
        queue_wait: null,
      })),
    }) as unknown as DocumentRecipeView;
  const failureView = (
    recipe: DocumentRecipeView,
    jobStatuses: Parameters<typeof recipeIngestionActive>[1],
    latestJob: { status: string; error_message: string | null }
  ) =>
    resolveDocumentFailureView({
      documentStatus: recipe.status,
      ingestionActive: recipeIngestionActive(recipe, jobStatuses),
      latestJobStatus: latestJob.status,
      latestJobPhase: "INDEX",
      failedRecipePhase: recipe.steps.find((step) => step.status === "FAILED")?.phase ?? null,
      latestJobErrorMessage: latestJob.error_message,
      segments: [],
      documentErrorMessage: recipe.error_message,
    });

  it("再処理の job が順番待ち（レシピの step が QUEUED）なら、レシピが ERROR でも失敗を出さない", () => {
    const recipe = failedRecipe(["SUCCEEDED", "SUCCEEDED", "SUCCEEDED", "QUEUED"]);
    expect(recipeIngestionActive(recipe, [])).toBe(true);
    expect(failureView(recipe, ["QUEUED"], { status: "QUEUED", error_message: null })).toEqual({
      errored: false,
      failedStep: null,
      primaryMessage: null,
    });
  });

  it("レシピの一覧の更新が遅れても、投入した job が QUEUED / RUNNING なら失敗を出さない", () => {
    // 投入の直後でレシピの step はまだ前回の FAILED のまま。この画面が投入した job は動いている。
    const recipe = failedRecipe(["SUCCEEDED", "SUCCEEDED", "SUCCEEDED", "FAILED"]);
    for (const status of ["QUEUED", "RUNNING"] as const) {
      expect(recipeIngestionActive(recipe, [undefined, status])).toBe(true);
      const view = failureView(recipe, [undefined, status], {
        status: "FAILED",
        error_message: "前回の失敗の本文",
      });
      expect(view.errored).toBe(false);
    }
  });

  it("新しい job が FAILED になれば、新しい失敗の本文と工程を出す", () => {
    const recipe = {
      ...failedRecipe(["SUCCEEDED", "SUCCEEDED", "SUCCEEDED", "FAILED"]),
      error_message: "新しい失敗の本文",
    } as DocumentRecipeView;
    expect(recipeIngestionActive(recipe, ["FAILED", "FAILED", null, undefined])).toBe(false);
    const view = failureView(recipe, ["FAILED", "FAILED"], {
      status: "FAILED",
      error_message: "新しい失敗の本文",
    });
    expect(view).toEqual({
      errored: true,
      failedStep: "INDEXING",
      primaryMessage: "新しい失敗の本文",
    });
  });

  it("レシピも job も動いていなければ、動いていると見なさない", () => {
    expect(recipeIngestionActive(null, [])).toBe(false);
    expect(
      recipeIngestionActive(failedRecipe(["SUCCEEDED", "SUCCEEDED", "SUCCEEDED", "FAILED"]), [
        "SUCCEEDED",
        "CANCELLED",
      ])
    ).toBe(false);
  });
});

describe("分割プレビュー設定", () => {
  it("レシピ未指定時は現行の既定値を使う", () => {
    expect(chunkPreviewForm(null)).toEqual({
      chunking_strategy: "small_to_big",
      chunk_size: 800,
      chunk_overlap: 120,
      chunk_min_chars: 120,
      // 固定分割符の既定は構造化レコード JSONL の記録の境界（#2457）。
      chunk_delimiter: "<<<JSONL_RECORD_BOUNDARY>>>",
      chunk_child_target_chars: 1000,
      chunk_table_child_target_chars: 3000,
      chunk_parent_target_chars: 6000,
      chunk_parent_max_pages: 3,
      chunk_parent_max_children: 12,
    });
  });

  it("親子階層（small-to-big）は 5 項目の範囲だけを検証し、chunk サイズの制約は使わない", () => {
    const smallToBig = { ...chunkPreviewForm(null), chunking_strategy: "small_to_big" as const };
    expect(chunkPreviewValidationError({ ...smallToBig, chunk_overlap: 800 })).toBeNull();
    expect(chunkPreviewValidationError({ ...smallToBig, chunk_child_target_chars: 299 })).toBe(
      "子チャンク目標文字数: 300〜1,600"
    );
    expect(chunkPreviewValidationError({ ...smallToBig, chunk_parent_max_pages: 6 })).toBe(
      "親チャンク最大ページ数: 1〜5"
    );
    expect(
      chunkPreviewValidationError({ ...smallToBig, chunk_parent_max_children: Number.NaN })
    ).toBe("親チャンク最大 child 数: 3〜20");
  });

  it("overlap・最小文字数・分割符の不正値を実行前に止める", () => {
    const base = { ...chunkPreviewForm(null), chunking_strategy: "structure_aware" as const };
    expect(chunkPreviewValidationError({ ...base, chunk_overlap: 800 })).toContain("overlap");
    expect(chunkPreviewValidationError({ ...base, chunk_min_chars: 800 })).toContain(
      "最小 chunk"
    );
    expect(
      chunkPreviewValidationError({
        ...base,
        chunking_strategy: "fixed_delimiter",
        chunk_delimiter: " ",
      })
    ).toBe("固定分割符文字列");
    expect(chunkPreviewValidationError(base)).toBeNull();
  });

  it("chunk size と overlap の製品上限を検証する", () => {
    const base = { ...chunkPreviewForm(null), chunking_strategy: "structure_aware" as const };
    expect(
      chunkPreviewValidationError({
        ...base,
        chunk_size: 32_000,
        chunk_overlap: 8_000,
      })
    ).toBeNull();
    expect(chunkPreviewValidationError({ ...base, chunk_size: 32_001 })).toContain(
      "chunk サイズ"
    );
    expect(chunkPreviewValidationError({ ...base, chunk_overlap: 8_001 })).toContain(
      "overlap"
    );
  });
});

describe("文書処理の表示名", () => {
  it.each<[FileStatus, string]>([
    ["UPLOADED", "ファイル準備待ち"],
    ["PREPROCESSING", "ファイル準備中"],
    ["PREPROCESSED", "ファイル準備確認待ち"],
    ["INGESTING", "解析（抽出）中"],
    ["REVIEW", "抽出確認待ち"],
    ["CHUNKING", "Chunk 作成中"],
    ["CHUNKED", "Chunk 確認待ち"],
    ["INDEXING", "Embedding / 索引中"],
    ["INDEXED", "索引済み"],
    ["ERROR", "エラー"],
  ])("%s を %s と表示する", (status, label) => {
    expect(t(`status.${status}`)).toBe(label);
  });

  it.each<[IngestionJobPhase, string, string, string, string]>([
    [
      "PREPROCESS",
      "ファイル準備",
      "ファイル準備を開始しました。完了まで状態を更新します。",
      "ファイル準備を実行しています。完了まで状態を更新します。",
      "ファイル準備を再実行",
    ],
    [
      "EXTRACT",
      "抽出",
      "抽出を開始しました。完了まで状態を更新します。",
      "抽出を実行しています。完了まで状態を更新します。",
      "抽出を再実行",
    ],
    [
      "CHUNK",
      "Chunk 作成",
      "Chunk 作成を開始しました。完了まで状態を更新します。",
      "Chunk 作成を実行しています。完了まで状態を更新します。",
      "Chunk 作成を再実行",
    ],
    [
      "INDEX",
      "Embedding / 索引",
      "Embedding / 索引を開始しました。完了まで状態を更新します。",
      "Embedding / 索引を実行しています。完了まで状態を更新します。",
      "Embedding / 索引を再実行",
    ],
  ])("%s の phase 文言を統一する", (phase, label, started, running, retry) => {
    expect(t(phaseLabelKey(phase))).toBe(label);
    expect(t(phaseStartedMessageKey(phase))).toBe(started);
    expect(t(phaseRunningMessageKey(phase))).toBe(running);
    expect(t(phaseRetryLabelKey(phase))).toBe(retry);
  });
});

describe("resolveDocumentActionPlan", () => {
  const resolve = (
    status: FileStatus,
    overrides: Partial<Parameters<typeof resolveDocumentActionPlan>[0]> = {}
  ) =>
    resolveDocumentActionPlan({
      status,
      activeJob: false,
      latestFailedPhase: null,
      hasPreparedArtifact: true,
      hasExtraction: true,
      hasChunkSet: true,
      ...overrides,
    });

  it("UPLOADED はファイル準備だけを実行できる", () => {
    expect(resolve("UPLOADED")).toEqual({
      primary: { kind: "enqueue", phase: "PREPROCESS" },
      reprocessPhases: [],
    });
  });

  it.each<FileStatus>(["PREPROCESSING", "INGESTING", "CHUNKING", "INDEXING"])(
    "%s の処理中は操作を表示しない",
    (status) => {
      expect(resolve(status)).toEqual({
        primary: null,
        reprocessPhases: [],
      });
    }
  );

  it("active job があれば安定状態でも操作を表示しない", () => {
    expect(resolve("INDEXED", { activeJob: true })).toEqual({
      primary: null,
      reprocessPhases: [],
    });
  });

  it("選択中レシピが無ければ status に関わらず操作を表示しない", () => {
    expect(resolve("REVIEW", { hasSelectedRecipe: false })).toEqual({
      primary: null,
      reprocessPhases: [],
    });
    expect(resolve("UPLOADED", { hasSelectedRecipe: false })).toEqual({
      primary: null,
      reprocessPhases: [],
    });
  });

  it("PREPROCESSED の成果物が有効なら承認の直後にファイル準備再処理を出す", () => {
    expect(resolve("PREPROCESSED")).toEqual({
      primary: { kind: "approve" },
      reprocessPhases: ["PREPROCESS"],
    });
  });

  it("PREPROCESSED の成果物が無ければファイル準備の再実行だけを出す", () => {
    expect(resolve("PREPROCESSED", { hasPreparedArtifact: false })).toEqual({
      primary: { kind: "retry", phase: "PREPROCESS" },
      reprocessPhases: [],
    });
  });

  it("REVIEW は承認と、前提を満たす再処理だけを出す", () => {
    expect(resolve("REVIEW")).toEqual({
      primary: { kind: "approve" },
      reprocessPhases: ["PREPROCESS", "EXTRACT"],
    });
    expect(resolve("REVIEW", { hasPreparedArtifact: false })).toEqual({
      primary: { kind: "approve" },
      reprocessPhases: ["PREPROCESS"],
    });
  });

  it("CHUNKED と INDEXED は前提を満たす段階だけ再処理できる", () => {
    expect(resolve("CHUNKED")).toEqual({
      primary: { kind: "approve" },
      reprocessPhases: ["PREPROCESS", "EXTRACT", "CHUNK"],
    });
    expect(resolve("INDEXED")).toEqual({
      primary: null,
      reprocessPhases: ["PREPROCESS", "EXTRACT", "CHUNK", "INDEX"],
    });
    expect(
      resolve("INDEXED", {
        hasPreparedArtifact: false,
        hasExtraction: false,
        hasChunkSet: false,
      })
    ).toEqual({
      primary: null,
      reprocessPhases: ["PREPROCESS"],
    });
  });

  it.each<[IngestionJobPhase, IngestionJobPhase, IngestionJobPhase[]]>([
    ["PREPROCESS", "PREPROCESS", []],
    ["EXTRACT", "EXTRACT", ["PREPROCESS"]],
    ["CHUNK", "CHUNK", ["PREPROCESS", "EXTRACT"]],
    ["INDEX", "INDEX", ["PREPROCESS", "EXTRACT", "CHUNK"]],
  ])("ERROR(%s) は %s と前段の再処理を出す", (failedPhase, retryPhase, earlierPhases) => {
    expect(resolve("ERROR", { latestFailedPhase: failedPhase })).toEqual({
      primary: { kind: "retry", phase: retryPhase },
      reprocessPhases: earlierPhases,
    });
  });

  it("ERROR は不足成果物に応じて前段へ戻し、不明 phase はファイル準備へ戻す", () => {
    expect(
      resolve("ERROR", {
        latestFailedPhase: "INDEX",
        hasChunkSet: false,
      }).primary
    ).toEqual({ kind: "retry", phase: "CHUNK" });
    expect(
      resolve("ERROR", {
        latestFailedPhase: "CHUNK",
        hasExtraction: false,
      }).primary
    ).toEqual({ kind: "retry", phase: "EXTRACT" });
    expect(
      resolve("ERROR", {
        latestFailedPhase: "EXTRACT",
        hasPreparedArtifact: false,
      }).primary
    ).toEqual({ kind: "retry", phase: "PREPROCESS" });
    expect(resolve("ERROR", { latestFailedPhase: null }).primary).toEqual({
      kind: "retry",
      phase: "PREPROCESS",
    });
  });
});

describe("resolvePhaseRows", () => {
  it("工程順に各工程の最新(先頭一致)ジョブを返す", () => {
    const jobs = [
      { id: "j4", phase: "INDEX" as IngestionJobPhase },
      { id: "j3", phase: "CHUNK" as IngestionJobPhase },
      { id: "j2b", phase: "EXTRACT" as IngestionJobPhase },
      { id: "j2a", phase: "EXTRACT" as IngestionJobPhase },
      { id: "j1", phase: "PREPROCESS" as IngestionJobPhase },
    ];
    const rows = resolvePhaseRows([], jobs);
    expect(rows.map((row) => row.phase)).toEqual([
      "PREPROCESS",
      "EXTRACT",
      "CHUNK",
      "INDEX",
    ]);
    expect(rows.map((row) => row.job?.id ?? null)).toEqual(["j1", "j2b", "j3", "j4"]);
  });

  it("通しジョブで実行済みの工程は step 状態を保持する(job=null でも未実行にしない)", () => {
    // PREPROCESS 起点の通しジョブ 1 本で 4 工程完了 → steps は全 SUCCEEDED、
    // EXTRACT/CHUNK/INDEX にはジョブ行が無い。
    const steps = (["PREPROCESS", "EXTRACT", "CHUNK", "INDEX"] as IngestionJobPhase[]).map(
      (phase) => ({ phase, status: "SUCCEEDED" })
    );
    const rows = resolvePhaseRows(steps, [
      { id: "j1", phase: "PREPROCESS" as IngestionJobPhase },
    ]);
    expect(rows[0].job?.id).toBe("j1");
    expect(rows.slice(1).every((row) => row.job === null)).toBe(true);
    expect(rows.every((row) => row.step?.status === "SUCCEEDED")).toBe(true);
  });

  it("工程状態が無い工程は step=null の行になる", () => {
    const rows = resolvePhaseRows([{ phase: "PREPROCESS" as IngestionJobPhase }], []);
    expect(rows[0].step).not.toBeNull();
    expect(rows.slice(1).every((row) => row.step === null && row.job === null)).toBe(true);
  });
});

describe("resolveIngestionParserDisplay", () => {
  it("取込 segment の parser を source profile より優先して表示する", () => {
    const display = resolveIngestionParserDisplay({
      segments: [
        {
          status: "RUNNING",
          parser_backend: "unstructured",
          parser_profile: "unstructured",
        },
      ],
      extractionBackend: null,
      extractionProfile: null,
      loading: false,
    });

    expect(display).toEqual({
      backend: "unstructured",
      profile: "unstructured",
      source: "segment",
    });
  });

  it("segment がまだ無い取込中は初期判定 parser へ戻さず確認中にする", () => {
    const display = resolveIngestionParserDisplay({
      segments: [],
      extractionBackend: null,
      extractionProfile: null,
      loading: true,
    });

    expect(display).toEqual({
      backend: null,
      profile: null,
      source: "pending",
    });
  });

  it("保存済み extraction の parser 情報を segment 不在時の表示に使う", () => {
    const display = resolveIngestionParserDisplay({
      segments: [],
      extractionBackend: "docling",
      extractionProfile: "docling_adapter",
      loading: false,
    });

    expect(display).toEqual({
      backend: "docling",
      profile: "docling_adapter",
      source: "extraction",
    });
  });
});

describe("resolveExtractStageProgress", () => {
  const updatedAt = "2026-10-15T08:37:27Z";
  const now = Date.parse(updatedAt) + 5_000;
  const vision = (overrides: Partial<IngestionVisionProgress> = {}): IngestionVisionProgress => ({
    phase: "pictures",
    succeeded: 0,
    failed: 0,
    skipped: 0,
    running: 0,
    pending: 0,
    total_known: 0,
    total_final: false,
    current_page: null,
    current_kind: null,
    finished: false,
    ...overrides,
  });
  const progress = (overrides: Partial<IngestionJobProgress>): IngestionJobProgress => ({
    stage: "parse",
    parse: null,
    vision: null,
    updated_at: updatedAt,
    ...overrides,
  });

  it("通しジョブ内の解析のページの進捗と、待っている解析サービスを出す", () => {
    const view = resolveExtractStageProgress(
      progress({
        parse: { engine: "docling", unit: "page", completed: 40, failed: 0, total: 82, finished: false },
      }),
      now
    );

    expect(view?.stages).toEqual([
      {
        key: "parse",
        label: "ページの解析（Docling）: 40 / 82 ページ",
        value: 40,
        max: 82,
        done: false,
      },
    ]);
    expect(view?.waiting).toContain("Docling");
    expect(view?.stalled).toBeNull();
  });

  it("ページ数が分かる前は不確定の bar にする", () => {
    const view = resolveExtractStageProgress(
      progress({
        parse: { engine: null, unit: "page", completed: 0, failed: 0, total: null, finished: false },
      }),
      now
    );

    expect(view?.stages[0]).toMatchObject({
      label: "ページの解析（解析サービス）: ページ数を確認しています",
      value: null,
      max: null,
    });
  });

  it("解析の後の図・表の説明の件数と、応答を待っている対象を出す", () => {
    const view = resolveExtractStageProgress(
      progress({
        stage: "vision",
        parse: { engine: "docling", unit: "page", completed: 82, failed: 0, total: 82, finished: true },
        vision: vision({ succeeded: 3, failed: 1, running: 1, total_known: 12, current_page: 5, current_kind: "picture" }),
      }),
      now
    );

    expect(view?.stages.map((stage) => stage.label)).toEqual([
      "ページの解析（Docling）: 完了（82 ページ）",
      "図・表の説明（Vision）: 4 / 12 件（表の対象を探しています）",
    ]);
    expect(view?.stages[1]).toMatchObject({ value: 4, max: 12, done: false });
    expect(view?.waiting).toBe(
      "Vision モデルの応答を待っています（p.5 の図）。1 件に数十秒かかることがあります。"
    );
  });

  it("対象が確定した後・完了・対象なしの文言を出し分ける", () => {
    const labels = [
      vision({ succeeded: 6, total_known: 8, total_final: true }),
      vision({ succeeded: 7, failed: 1, total_known: 8, total_final: true, finished: true }),
      vision({ total_final: true, finished: true }),
      vision(),
    ].map(
      (item) =>
        resolveExtractStageProgress(progress({ stage: "vision", vision: item }), now)?.stages[0]
          .label
    );

    expect(labels).toEqual([
      "図・表の説明（Vision）: 6 / 8 件",
      "図・表の説明（Vision）: 完了（8 件・失敗 1 件）",
      "図・表の説明（Vision）: 対象の図・表はありません",
      "図・表の説明（Vision）: 対象の図・表を探しています",
    ]);
  });

  it("長く進まないときは最後に進んでからの時間を出す", () => {
    const view = resolveExtractStageProgress(
      progress({
        stage: "vision",
        vision: vision({ running: 1, total_known: 3, current_page: 9, current_kind: "table" }),
      }),
      Date.parse(updatedAt) + 125_000
    );

    expect(view?.stalled).toBe("最後に進んでから 2 分 5 秒経過しています。");
    expect(view?.waiting).toContain("p.9 の表");
  });

  it("進捗が無ければ出さない", () => {
    expect(resolveExtractStageProgress(null, now)).toBeNull();
    expect(resolveExtractStageProgress(progress({}), now)).toBeNull();
  });
});

describe("resolveIngestionProgressSummary", () => {
  it("page segment を完了数と失敗数へ集計する", () => {
    const summary = resolveIngestionProgressSummary([
      { status: "SUCCEEDED", progress_unit: "page", progress_start: 1, progress_end: 5 },
      { status: "SUCCEEDED", progress_unit: "page", progress_start: 6, progress_end: 9 },
      { status: "RUNNING", progress_unit: "page", progress_start: 10, progress_end: 13 },
      { status: "QUEUED", progress_unit: "page", progress_start: 14, progress_end: 17 },
    ]);

    expect(summary).toEqual({
      kind: "determinate",
      unit: "page",
      completed: 9,
      failed: 0,
      total: 17,
    });
  });

  it("source segment は indeterminate progress にする", () => {
    const summary = resolveIngestionProgressSummary([
      { status: "RUNNING", progress_unit: "source", progress_start: null, progress_end: null },
    ]);

    expect(summary).toEqual({ kind: "indeterminate" });
  });
});

describe("resolveIngestionErrorDisplayPlan", () => {
  it("job の原因を優先し、同じ原因の segment と document banner を隠す", () => {
    const plan = resolveIngestionErrorDisplayPlan({
      latestJobErrorMessage: " unstructured_adapter_failed ",
      segments: [
        {
          segment_id: "segment-1",
          status: "FAILED",
          error_message: "unstructured_adapter_failed",
        },
      ],
      documentErrorMessage: "unstructured_adapter_failed",
      queuedJobErrorMessage: "unstructured_adapter_failed",
      retriedSegmentJobErrorMessage: "unstructured_adapter_failed",
    });

    expect(plan.jobMessage).toBe("unstructured_adapter_failed");
    expect(Array.from(plan.segmentIds)).toEqual([]);
    expect(plan.documentMessage).toBeNull();
    expect(plan.queuedJobMessage).toBeNull();
    expect(plan.retriedSegmentJobMessage).toBeNull();
  });

  it("同じ segment 原因は最初の1件だけ表示する", () => {
    const plan = resolveIngestionErrorDisplayPlan({
      segments: [
        { segment_id: "segment-1", status: "FAILED", error_message: "parser failed" },
        { segment_id: "segment-2", status: "FAILED", error_message: " parser failed " },
        { segment_id: "segment-3", status: "FAILED", error_message: "ocr failed" },
      ],
    });

    expect(Array.from(plan.segmentIds)).toEqual(["segment-1", "segment-3"]);
  });

  it("segment と同じ document 原因は document banner に出さない", () => {
    const plan = resolveIngestionErrorDisplayPlan({
      segments: [
        { segment_id: "segment-1", status: "FAILED", error_message: "parser failed" },
      ],
      documentErrorMessage: " parser failed ",
    });

    expect(Array.from(plan.segmentIds)).toEqual(["segment-1"]);
    expect(plan.documentMessage).toBeNull();
  });

  it("異なる原因はそれぞれ表示する", () => {
    const plan = resolveIngestionErrorDisplayPlan({
      latestJobErrorMessage: "job failed",
      segments: [
        { segment_id: "segment-1", status: "FAILED", error_message: "segment failed" },
      ],
      documentErrorMessage: "document failed",
      queuedJobErrorMessage: "queued job failed",
      retriedSegmentJobErrorMessage: "retried job failed",
    });

    expect(plan.jobMessage).toBe("job failed");
    expect(Array.from(plan.segmentIds)).toEqual(["segment-1"]);
    expect(plan.documentMessage).toBe("document failed");
    expect(plan.queuedJobMessage).toBe("queued job failed");
    expect(plan.retriedSegmentJobMessage).toBe("retried job failed");
  });

  it("suppressMessages の原因は詳細側で再掲しない（§9 P2）", () => {
    const plan = resolveIngestionErrorDisplayPlan({
      segments: [
        { segment_id: "segment-1", status: "FAILED", error_message: " parser failed " },
        { segment_id: "segment-2", status: "FAILED", error_message: "ocr failed" },
      ],
      documentErrorMessage: "parser failed",
      queuedJobErrorMessage: "parser failed",
      suppressMessages: ["parser failed"],
    });

    // 上部バナーで出した "parser failed" は segment/document/queued から消える。
    expect(Array.from(plan.segmentIds)).toEqual(["segment-2"]);
    expect(plan.documentMessage).toBeNull();
    expect(plan.queuedJobMessage).toBeNull();
  });
});

describe("resolveDocumentFailureView", () => {
  it("ERROR でなければ errored=false で何も出さない", () => {
    const view = resolveDocumentFailureView({
      documentStatus: "INDEXED",
      segments: [],
    });
    expect(view).toEqual({ errored: false, failedStep: null, primaryMessage: null });
  });

  it("最新 FAILED ジョブの phase から失敗工程を導出し、原因を 1 本化する", () => {
    const view = resolveDocumentFailureView({
      documentStatus: "ERROR",
      latestJobStatus: "FAILED",
      latestJobPhase: "PREPROCESS",
      latestJobErrorMessage: " ファイル準備に失敗しました。再処理してください。 ",
      segments: [{ status: "FAILED", error_message: "別の原因" }],
      documentErrorMessage: "文書側の原因",
    });
    expect(view.errored).toBe(true);
    expect(view.failedStep).toBe("PREPROCESSING");
    // job → segment → document の順で最具体を採用（job を採る）。
    expect(view.primaryMessage).toBe("ファイル準備に失敗しました。再処理してください。");
  });

  it("レシピの step が失敗した工程を、ジョブの phase より優先する（ステップ表示と一致させる。Issue 329）", () => {
    // レシピの CHUNK ジョブは索引まで行う。索引で失敗したら、ジョブの phase は CHUNK でも失敗工程は索引。
    const view = resolveDocumentFailureView({
      documentStatus: "ERROR",
      latestJobStatus: "FAILED",
      latestJobPhase: "CHUNK",
      failedRecipePhase: "INDEX",
      latestJobErrorMessage: "取込処理に失敗しました。",
      segments: [],
    });
    expect(view.failedStep).toBe("INDEXING");
    // レシピの step に失敗が無ければ、今までどおりジョブの phase を使う。
    expect(
      resolveDocumentFailureView({
        documentStatus: "ERROR",
        latestJobStatus: "FAILED",
        latestJobPhase: "CHUNK",
        failedRecipePhase: null,
        segments: [],
      }).failedStep
    ).toBe("CHUNKING");
  });

  it("ジョブ原因が無ければ失敗セグメント→文書の順にフォールバックする", () => {
    const view = resolveDocumentFailureView({
      documentStatus: "ERROR",
      latestJobStatus: "FAILED",
      latestJobPhase: "EXTRACT",
      latestJobErrorMessage: "   ",
      segments: [
        { status: "SUCCEEDED", error_message: null },
        { status: "FAILED", error_message: "segment 由来の原因" },
      ],
      documentErrorMessage: "文書側の原因",
    });
    expect(view.failedStep).toBe("INGESTING");
    expect(view.primaryMessage).toBe("segment 由来の原因");
  });

  it("ジョブが FAILED でなければ失敗工程は不明(null)", () => {
    const view = resolveDocumentFailureView({
      documentStatus: "ERROR",
      latestJobStatus: "SUCCEEDED",
      latestJobPhase: "INDEX",
      segments: [],
      documentErrorMessage: "文書側の原因",
    });
    expect(view.errored).toBe(true);
    expect(view.failedStep).toBeNull();
    expect(view.primaryMessage).toBe("文書側の原因");
  });
});

describe("shouldShowProcessingWatchBanner", () => {
  it("FAILED job があれば文書が一時的に INGESTING でも取込中 banner を出さない", () => {
    expect(
      shouldShowProcessingWatchBanner({
        watchProcessing: true,
        documentStatus: "INGESTING",
        latestJobStatus: "FAILED",
      })
    ).toBe(false);
  });

  it("ERROR は安定状態として取込中 banner を出さない", () => {
    expect(
      shouldShowProcessingWatchBanner({
        watchProcessing: true,
        documentStatus: "ERROR",
        latestJobStatus: "FAILED",
      })
    ).toBe(false);
  });
});

describe("ingestConflictBannerIsStale", () => {
  it("取込進行中の 409 は stale ではない(banner を残す)", () => {
    expect(
      ingestConflictBannerIsStale({ errorStatus: 409, hasActiveJob: true, observedActiveJob: true })
    ).toBe(false);
  });

  it("進行中ジョブを観測した後、それが無くなった 409 は stale として消す", () => {
    expect(
      ingestConflictBannerIsStale({ errorStatus: 409, hasActiveJob: false, observedActiveJob: true })
    ).toBe(true);
  });

  it("進行中ジョブをまだ観測していない 409 は出した瞬間に消さない", () => {
    expect(
      ingestConflictBannerIsStale({ errorStatus: 409, hasActiveJob: false, observedActiveJob: false })
    ).toBe(false);
  });

  it("409 以外のエラーは消さない", () => {
    expect(
      ingestConflictBannerIsStale({ errorStatus: 500, hasActiveJob: false, observedActiveJob: true })
    ).toBe(false);
    expect(
      ingestConflictBannerIsStale({ errorStatus: null, hasActiveJob: false, observedActiveJob: true })
    ).toBe(false);
  });
});

describe("shouldStopLocalProcessingWatch", () => {
  it("進行中の job がある間はやめない", () => {
    expect(
      shouldStopLocalProcessingWatch({
        activeSubmittedJob: true,
        documentStatus: "UPLOADED",
        submittedJobStatus: "QUEUED",
      })
    ).toBe(false);
  });

  it("確認待ち・完了・失敗になったらやめる", () => {
    for (const documentStatus of ["INDEXED", "ERROR", "PREPROCESSED", "REVIEW"] as const) {
      expect(
        shouldStopLocalProcessingWatch({
          activeSubmittedJob: false,
          documentStatus,
          submittedJobStatus: "SUCCEEDED",
        })
      ).toBe(true);
    }
  });

  it("待機中にキャンセルされ UPLOADED のままになったらやめる", () => {
    expect(
      shouldStopLocalProcessingWatch({
        activeSubmittedJob: false,
        documentStatus: "UPLOADED",
        submittedJobStatus: "CANCELLED",
      })
    ).toBe(true);
  });

  it("job が失敗・成功しただけで、文書の状態がまだ UPLOADED ならやめない（次の取得で ERROR などへ進む）", () => {
    for (const submittedJobStatus of ["FAILED", "SUCCEEDED"] as const) {
      expect(
        shouldStopLocalProcessingWatch({
          activeSubmittedJob: false,
          documentStatus: "UPLOADED",
          submittedJobStatus,
        })
      ).toBe(false);
    }
  });

  it("投入した job がまだ分からない UPLOADED と、処理中の状態ではやめない", () => {
    expect(
      shouldStopLocalProcessingWatch({
        activeSubmittedJob: false,
        documentStatus: "UPLOADED",
        submittedJobStatus: null,
      })
    ).toBe(false);
    expect(
      shouldStopLocalProcessingWatch({
        activeSubmittedJob: false,
        documentStatus: "CHUNKING",
        submittedJobStatus: "SUCCEEDED",
      })
    ).toBe(false);
  });
});

describe("resolveStatusMessageSlot(状態メッセージ単一スロット)", () => {
  const base = {
    errored: false,
    processingVisible: false,
    documentStatus: "INDEXED",
    preparedArtifactMissing: false,
  };

  it("失敗原因が実行中・ゲート案内より優先される", () => {
    expect(
      resolveStatusMessageSlot({
        ...base,
        errored: true,
        processingVisible: true,
        documentStatus: "REVIEW",
      })
    ).toEqual({ kind: "failure" });
  });

  it("実行中はゲート案内より優先される", () => {
    expect(
      resolveStatusMessageSlot({ ...base, processingVisible: true, documentStatus: "REVIEW" })
    ).toEqual({ kind: "processing" });
  });

  it.each(["PREPROCESSED", "REVIEW", "CHUNKED"] as const)(
    "%s は承認待ちゲート案内を出す",
    (status) => {
      expect(resolveStatusMessageSlot({ ...base, documentStatus: status })).toEqual({
        kind: "gate",
        status,
        artifactMissing: false,
      });
    }
  );

  it("PREPROCESSED で処理後ファイル欠落なら artifactMissing を立てる", () => {
    expect(
      resolveStatusMessageSlot({
        ...base,
        documentStatus: "PREPROCESSED",
        preparedArtifactMissing: true,
      })
    ).toEqual({ kind: "gate", status: "PREPROCESSED", artifactMissing: true });
  });

  it("INDEXED では常設メッセージを出さない(StatusBadge に委ねる)", () => {
    expect(resolveStatusMessageSlot(base)).toBeNull();
    expect(
      resolveStatusMessageSlot({ ...base, documentStatus: "PREPROCESSED", preparedArtifactMissing: true, errored: false, processingVisible: false })
    ).not.toBeNull();
  });
});

describe("isIndexedTransition(索引完了 toast)", () => {
  it("同一レシピの INDEXING → INDEXED でのみ true", () => {
    expect(
      isIndexedTransition(
        { recipeId: "r1", status: "INDEXING" },
        { recipeId: "r1", status: "INDEXED" }
      )
    ).toBe(true);
  });

  it("初回マウント(prev なし)では発火しない", () => {
    expect(isIndexedTransition(null, { recipeId: "r1", status: "INDEXED" })).toBe(false);
  });

  it("レシピ切替による INDEXED 表示では発火しない", () => {
    expect(
      isIndexedTransition(
        { recipeId: "r1", status: "INDEXING" },
        { recipeId: "r2", status: "INDEXED" }
      )
    ).toBe(false);
  });

  it("INDEXING 以外からの遷移では発火しない", () => {
    expect(
      isIndexedTransition(
        { recipeId: "r1", status: "REVIEW" },
        { recipeId: "r1", status: "INDEXED" }
      )
    ).toBe(false);
  });
});

// 分類の有効期間の確認（Issue 944）。
describe("classificationPeriodIsValid", () => {
  it("終了日が開始日より後なら有効", () => {
    expect(classificationPeriodIsValid({ effective_from: "2026-01-01", effective_to: "2026-01-02" })).toBe(true);
  });
  it("終了日が開始日と同じか前なら無効（終了日は排他的）", () => {
    expect(classificationPeriodIsValid({ effective_from: "2026-01-01", effective_to: "2026-01-01" })).toBe(false);
    expect(classificationPeriodIsValid({ effective_from: "2026-02-01", effective_to: "2026-01-01" })).toBe(false);
  });
  it("どちらかが未入力なら比べない", () => {
    expect(classificationPeriodIsValid({ effective_from: null, effective_to: "2026-01-01" })).toBe(true);
    expect(classificationPeriodIsValid({ effective_from: "2026-01-01", effective_to: null })).toBe(true);
  });
});
