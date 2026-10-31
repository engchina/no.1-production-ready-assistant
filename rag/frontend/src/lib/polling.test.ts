import { describe, expect, it } from "vitest";

import {
  DOCUMENT_ACTIVE_STATUSES,
  documentWorkspaceShouldRefresh,
  documentsHaveActiveWork,
  ingestionJobIsActive,
} from "./queries";
import type { DocumentSummary, FileStatus } from "./api";

function doc(status: FileStatus): Pick<DocumentSummary, "status" | "ingestion_active"> {
  return { status };
}

describe("documentsHaveActiveWork", () => {
  it("空配列/undefined は false", () => {
    expect(documentsHaveActiveWork([])).toBe(false);
    expect(documentsHaveActiveWork(undefined)).toBe(false);
  });

  it("取込/Chunk/索引中があれば true", () => {
    expect(documentsHaveActiveWork([doc("INDEXED"), doc("PREPROCESSING")])).toBe(true);
    expect(documentsHaveActiveWork([doc("INDEXED"), doc("INGESTING")])).toBe(true);
    expect(documentsHaveActiveWork([doc("CHUNKING")])).toBe(true);
    expect(documentsHaveActiveWork([doc("INDEXING")])).toBe(true);
  });

  it("安定状態のみなら false(UPLOADED/REVIEW/CHUNKED/INDEXED/ERROR)", () => {
    expect(
      documentsHaveActiveWork([
        doc("UPLOADED"),
        doc("REVIEW"),
        doc("CHUNKED"),
        doc("INDEXED"),
        doc("ERROR"),
      ])
    ).toBe(false);
  });

  // #2012: 投入直後の job は worker が拾うまで状態が変わらない。
  it("待機中・実行中の job がある文書は、状態が変わる前でも true", () => {
    expect(
      documentsHaveActiveWork([doc("INDEXED"), { status: "UPLOADED", ingestion_active: true }])
    ).toBe(true);
    expect(documentsHaveActiveWork([{ status: "REVIEW", ingestion_active: false }])).toBe(false);
  });

  it("DOCUMENT_ACTIVE_STATUSES は実行中ステータスのみ", () => {
    expect([...DOCUMENT_ACTIVE_STATUSES].sort()).toEqual([
      "CHUNKING",
      "INDEXING",
      "INGESTING",
      "PREPROCESSING",
    ]);
  });
});

describe("ingestionJobIsActive", () => {
  it("QUEUED/RUNNING のみ true", () => {
    expect(ingestionJobIsActive("QUEUED")).toBe(true);
    expect(ingestionJobIsActive("RUNNING")).toBe(true);
    expect(ingestionJobIsActive("SUCCEEDED")).toBe(false);
    expect(ingestionJobIsActive("FAILED")).toBe(false);
    expect(ingestionJobIsActive(undefined)).toBe(false);
  });
});

describe("documentWorkspaceShouldRefresh", () => {
  it("取込/索引中の文書では true", () => {
    expect(documentWorkspaceShouldRefresh({ documentStatus: "PREPROCESSING" })).toBe(true);
    expect(documentWorkspaceShouldRefresh({ documentStatus: "INGESTING" })).toBe(true);
    expect(documentWorkspaceShouldRefresh({ documentStatus: "INDEXING" })).toBe(true);
  });

  it("ERROR/REVIEW の画面でも投入済み job が動いていれば true", () => {
    expect(
      documentWorkspaceShouldRefresh({
        documentStatus: "ERROR",
        jobStatuses: ["QUEUED"],
      })
    ).toBe(true);
    expect(
      documentWorkspaceShouldRefresh({
        documentStatus: "REVIEW",
        jobStatuses: ["RUNNING"],
      })
    ).toBe(true);
  });

  it("投入直後の watch 窓では安定状態に入るまで true", () => {
    expect(
      documentWorkspaceShouldRefresh({
        documentStatus: "UPLOADED",
        localWatchProcessing: true,
      })
    ).toBe(true);
    expect(
      documentWorkspaceShouldRefresh({
        documentStatus: "REVIEW",
        localWatchProcessing: true,
      })
    ).toBe(false);
  });

  it("segment が進行中なら true", () => {
    expect(
      documentWorkspaceShouldRefresh({
        documentStatus: "UPLOADED",
        segmentStatuses: ["RUNNING"],
      })
    ).toBe(true);
  });
});
