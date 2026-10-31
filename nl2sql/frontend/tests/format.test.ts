import assert from "node:assert/strict";
import test from "node:test";

import { setDisplayTimeZone } from "@production-ready/ui";

import { formatDateTime, formatDateTimeWithYear, identifierWrapSegments } from "../src/lib/format.ts";

test("formatDateTimeWithYear は年を含む日本語形式で日時を表示する", () => {
  const formatted = formatDateTimeWithYear("2026-07-19T00:00:00Z");

  assert.match(formatted, /^2026\/07\/19 \d{2}:\d{2}$/);
});

test("日時は端末ではなくシステムの設定の時間帯で書く（#3265）", () => {
  try {
    // 既定は UTC（#3274）。
    assert.equal(formatDateTimeWithYear("2026-07-18T15:30:00Z"), "2026/07/18 15:30");
    setDisplayTimeZone("Asia/Tokyo");
    assert.equal(formatDateTimeWithYear("2026-07-18T15:30:00Z"), "2026/07/19 00:30");
    assert.equal(formatDateTime("2026-07-18T15:30:00Z"), "07/19 00:30");
  } finally {
    setDisplayTimeZone(null);
  }
});

test("formatDateTimeWithYear は未設定または不正な日時をダッシュで表示する", () => {
  assert.equal(formatDateTimeWithYear(null), "—");
  assert.equal(formatDateTimeWithYear("not-a-date"), "—");
});

test("identifierWrapSegments は識別子を . _ $ # の直後で分割し、連結すると元の値に戻る", () => {
  assert.deepEqual(identifierWrapSegments("ADMIN.DENPYO_ACTIVITY_LOG"), ["ADMIN.", "DENPYO_", "ACTIVITY_", "LOG"]);
  assert.deepEqual(identifierWrapSegments("SYS$TAB#1"), ["SYS$", "TAB#", "1"]);
  assert.deepEqual(identifierWrapSegments("ORDERS"), ["ORDERS"]);
  assert.deepEqual(identifierWrapSegments("A__B."), ["A__", "B."]);
  assert.deepEqual(identifierWrapSegments(""), [""]);
  for (const value of ["ADMIN.DENPYO_ACTIVITY_LOG", "_X_", "日本語_テーブル", "A.B.C"]) {
    assert.equal(identifierWrapSegments(value).join(""), value);
  }
});

test("identifierWrapSegments は区切りの無い長い値・区切りの長い連続でも時間内に終わる（#1646）", () => {
  const started = performance.now();
  assert.deepEqual(identifierWrapSegments('"'.repeat(200_000)), ['"'.repeat(200_000)]);
  assert.deepEqual(identifierWrapSegments(`A${".".repeat(200_000)}B`), [`A${".".repeat(200_000)}`, "B"]);
  assert.ok(performance.now() - started < 1000);
});
