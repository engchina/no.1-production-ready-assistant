import assert from "node:assert/strict";
import test from "node:test";

import {
  selectedVisibleKey,
  selectedVisibleStringKey,
} from "../src/lib/visible-selection.ts";

const rows = [
  { id: "row-2", label: "Second" },
  { id: "row-1", label: "First" },
];

test("visible selection keeps the current key when it is still visible", () => {
  assert.equal(selectedVisibleStringKey(rows, "row-1", (row) => row.id), "row-1");
});

test("visible selection falls back to the first visible item", () => {
  assert.equal(selectedVisibleStringKey(rows, "missing", (row) => row.id), "row-2");
});

test("visible selection can ignore the current key when it was not manually selected", () => {
  assert.equal(
    selectedVisibleStringKey(rows, "row-1", (row) => row.id, { preserveSelected: false }),
    "row-2"
  );
});

test("visible selection clears when no items are visible", () => {
  assert.equal(selectedVisibleStringKey([], "row-1", (row: { id: string }) => row.id), "");
  assert.equal(selectedVisibleKey([], 10, (row: { id: number }) => row.id), null);
});

test("visible selection supports numeric keys", () => {
  assert.equal(selectedVisibleKey([{ id: 2 }, { id: 1 }], 3, (row) => row.id), 2);
  assert.equal(selectedVisibleKey([{ id: 2 }, { id: 1 }], 1, (row) => row.id), 1);
});
