import { describe, expect, it } from "vitest";

import type { BatchUploadResult } from "./api";
import {
  DEFAULT_MAX_UPLOAD_BYTES,
  failedUploadItem,
  formatByteSize,
  mergeBatchUploadResults,
  planUploadRequests,
  totalUploadBytes,
  uploadFileProgresses,
  uploadProgressOf,
  uploadProgressPercent,
} from "./upload-requests";

const MB = 1024 * 1024;

function file(name: string, size: number) {
  return { name, size };
}

describe("planUploadRequests", () => {
  it("1 リクエストの合計が 1 ファイルの上限を超えないように分ける", () => {
    const files = [file("a", 120 * MB), file("b", 90 * MB), file("c", 60 * MB), file("d", 10 * MB)];

    const plan = planUploadRequests(files, 200 * MB);

    expect(plan.groups.map((group) => group.map((item) => item.name))).toEqual([["a"], ["b", "c", "d"]]);
    expect(plan.oversized).toEqual([]);
    for (const group of plan.groups) {
      expect(group.reduce((sum, item) => sum + item.size, 0)).toBeLessThanOrEqual(200 * MB);
    }
  });

  it("上限を超えるファイルは送らずに分け、残りは選択した順のまま送る", () => {
    const files = [file("small", 1 * MB), file("huge", 201 * MB), file("exact", 200 * MB)];

    const plan = planUploadRequests(files, 200 * MB);

    expect(plan.oversized.map((item) => item.name)).toEqual(["huge"]);
    expect(plan.groups.map((group) => group.map((item) => item.name))).toEqual([["small"], ["exact"]]);
  });

  it("小さなファイルが多いときはファイル数でも分ける", () => {
    const files = Array.from({ length: 7 }, (_, index) => file(`f${index}`, 1));

    const plan = planUploadRequests(files, 200 * MB, 3);

    expect(plan.groups.map((group) => group.length)).toEqual([3, 3, 1]);
  });

  it("上限が取れないときは backend の既定値で分ける", () => {
    const plan = planUploadRequests([file("a", DEFAULT_MAX_UPLOAD_BYTES + 1)], Number.NaN);

    expect(plan.oversized).toHaveLength(1);
    expect(plan.groups).toEqual([]);
  });
});

describe("mergeBatchUploadResults", () => {
  it("分けて送った結果と送らなかったファイルを 1 つの結果にまとめる", () => {
    const item = (id: string) =>
      ({ id, file_name: `${id}.txt` }) as unknown as BatchUploadResult["items"][number];
    const first: BatchUploadResult = {
      items: [item("a")],
      failed_items: [failedUploadItem({ name: "x.exe" }, 415, "対応していないファイル形式です。")],
      total_count: 2,
      uploaded_count: 1,
      failed_count: 1,
    };
    const second: BatchUploadResult = {
      items: [item("b")],
      failed_items: [],
      total_count: 1,
      uploaded_count: 1,
      failed_count: 0,
    };

    const merged = mergeBatchUploadResults(
      [first, second],
      [failedUploadItem({ name: "huge.pdf" }, 413, "上限超過")],
    );

    expect(merged.items.map((entry) => entry.id)).toEqual(["a", "b"]);
    expect(merged.failed_items.map((entry) => [entry.file_name, entry.status_code])).toEqual([
      ["x.exe", 415],
      ["huge.pdf", 413],
    ]);
    expect(merged).toEqual(
      expect.objectContaining({ total_count: 4, uploaded_count: 2, failed_count: 2 }),
    );
    // アップロードは取込 job を作らないため、job の状態を数える指標は持たない（#306）。
    expect(Object.keys(merged).sort()).toEqual(
      ["failed_count", "failed_items", "items", "total_count", "uploaded_count"],
    );
  });
});

describe("formatByteSize", () => {
  it("MiB 単位の上限を整数で示す", () => {
    expect(formatByteSize(200 * MB)).toBe("200 MB");
    expect(formatByteSize(1.5 * MB)).toBe("1.5 MB");
    expect(formatByteSize(512)).toBe("512 B");
  });
});

describe("送信の進み具合", () => {
  it("1 リクエストの送信割合をファイルのバイト数へ換算する", () => {
    // multipart の本文（境界などを含む）は 110 MB、ファイルは 100 MB。半分送った時点で 50 MB。
    const progress = uploadProgressOf(0, 100 * MB, { loaded: 55 * MB, total: 110 * MB }, 100 * MB);

    expect(progress).toEqual({ sentBytes: 50 * MB, totalBytes: 100 * MB });
    expect(uploadProgressPercent(progress)).toBe(50);
  });

  it("分けて送るときは送り終えたまとまりの分を足して全体の進み具合にする", () => {
    const groups = [[file("a", 150 * MB)], [file("b", 30 * MB), file("c", 20 * MB)]];
    const totalBytes = totalUploadBytes(groups.flat());
    const doneBytes = totalUploadBytes(groups[0]);

    const progress = uploadProgressOf(
      doneBytes,
      totalUploadBytes(groups[1]),
      { loaded: 1, total: 2 },
      totalBytes,
    );

    expect(totalBytes).toBe(200 * MB);
    expect(progress.sentBytes).toBe(175 * MB);
    expect(uploadProgressPercent(progress)).toBe(87);
  });

  it("割合は送り終えるまで 100% にしない。合計が 0 のときは 0%", () => {
    expect(uploadProgressPercent({ sentBytes: 999, totalBytes: 1000 })).toBe(99);
    expect(uploadProgressPercent({ sentBytes: 1000, totalBytes: 1000 })).toBe(100);
    expect(uploadProgressPercent({ sentBytes: 0, totalBytes: 0 })).toBe(0);
  });

  it("送信割合が範囲外でも合計を超えない", () => {
    const progress = uploadProgressOf(90, 20, { loaded: 5, total: 2 }, 100);

    expect(progress.sentBytes).toBe(100);
  });
});

describe("ファイルごとの送信の進み具合", () => {
  const files = [file("a.pdf", 30 * MB), file("b.pdf", 10 * MB), file("c.pdf", 20 * MB)];

  it("全体の送信済みバイト数を、送る順に先頭のファイルから割り振る", () => {
    expect(uploadFileProgresses(files, 35 * MB)).toEqual([
      { name: "a.pdf", sentBytes: 30 * MB, totalBytes: 30 * MB, state: "sent" },
      { name: "b.pdf", sentBytes: 5 * MB, totalBytes: 10 * MB, state: "sending" },
      { name: "c.pdf", sentBytes: 0, totalBytes: 20 * MB, state: "waiting" },
    ]);
  });

  it("送信前はすべて待機中、送り終えたらすべて送信済みにする", () => {
    expect(uploadFileProgresses(files, 0).map((item) => item.state)).toEqual([
      "waiting",
      "waiting",
      "waiting",
    ]);
    const done = uploadFileProgresses(files, 60 * MB);
    expect(done.map((item) => item.state)).toEqual(["sent", "sent", "sent"]);
    expect(done.map((item) => item.sentBytes)).toEqual([30 * MB, 10 * MB, 20 * MB]);
  });

  it("ちょうど境目では前のファイルを送信済みにし、次のファイルはまだ待機中にする", () => {
    expect(uploadFileProgresses(files, 30 * MB).map((item) => item.state)).toEqual([
      "sent",
      "waiting",
      "waiting",
    ]);
  });

  it("送信済みの量が合計を超えても、各ファイルの大きさを超えない", () => {
    const over = uploadFileProgresses(files, 999 * MB);
    expect(over.map((item) => item.sentBytes)).toEqual([30 * MB, 10 * MB, 20 * MB]);
  });

  it("0 バイトのファイルは、後ろのファイルを送り始めるか全体を送り終えたら送信済みにする", () => {
    const withEmpty = [file("a", 10), file("empty", 0), file("b", 10)];
    expect(uploadFileProgresses(withEmpty, 10).map((item) => item.state)).toEqual([
      "sent",
      "waiting",
      "waiting",
    ]);
    expect(uploadFileProgresses(withEmpty, 11).map((item) => item.state)).toEqual([
      "sent",
      "sent",
      "sending",
    ]);
    expect(uploadFileProgresses([file("a", 10), file("empty", 0)], 10).map((item) => item.state)).toEqual([
      "sent",
      "sent",
    ]);
  });
});
