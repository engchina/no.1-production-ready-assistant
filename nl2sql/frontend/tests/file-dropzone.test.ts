import assert from "node:assert/strict";
import test from "node:test";

import {
  fileMatchesAccept,
  mergeUniqueFiles,
  validateFileDropzoneSelection,
  type FileDropzoneFile,
} from "../src/lib/file-dropzone.ts";
import {
  CORE_TABULAR_FILE_FORMATS,
  tabularFileFormatConfig,
  XLSX_TEMPLATE_FILE_FORMATS,
} from "../src/lib/tabular-file-formats.ts";

function file(
  name: string,
  type: string,
  size = 10,
  lastModified = 1
): FileDropzoneFile {
  return { name, type, size, lastModified };
}

test("file dropzone matches extensions case-insensitively and supports MIME wildcards", () => {
  assert.equal(fileMatchesAccept(file("QUERY.SQL", "text/plain"), ".sql,.txt"), true);
  assert.equal(fileMatchesAccept(file("payload.bin", "text/csv"), "text/csv"), true);
  assert.equal(fileMatchesAccept(file("photo.bin", "image/png"), "image/*"), true);
  assert.equal(fileMatchesAccept(file("cases.csv", "text/csv"), ".xlsx"), false);
});

test("tabular formats always expose CSV, XLSX, and XLS in the shared order", () => {
  assert.deepEqual(CORE_TABULAR_FILE_FORMATS, {
    accept: ".csv,.xlsx,.xls",
    formatLabel: ".CSV / .XLSX / .XLS",
  });
  assert.deepEqual(tabularFileFormatConfig([".XLSM", "txt", ".xls"]), {
    accept: ".csv,.xlsx,.xls,.xlsm,.txt",
    formatLabel: ".CSV / .XLSX / .XLS / .XLSM / .TXT",
  });
  assert.equal(
    fileMatchesAccept(file("legacy-data.XLS", "application/vnd.ms-excel"), CORE_TABULAR_FILE_FORMATS.accept),
    true
  );
});

test("template workbook format only exposes XLSX", () => {
  assert.deepEqual(XLSX_TEMPLATE_FILE_FORMATS, {
    accept: ".xlsx",
    formatLabel: ".XLSX",
  });
  assert.equal(
    fileMatchesAccept(file("template.xlsx", "application/octet-stream"), XLSX_TEMPLATE_FILE_FORMATS.accept),
    true
  );
  assert.equal(
    fileMatchesAccept(file("template.csv", "text/csv"), XLSX_TEMPLATE_FILE_FORMATS.accept),
    false
  );
});

test("single-file dropzone rejects multiple files without accepting a partial selection", () => {
  assert.deepEqual(
    validateFileDropzoneSelection(
      [file("one.sql", "text/plain"), file("two.sql", "text/plain")],
      { accept: ".sql", multiple: false }
    ),
    { accepted: false, reason: "multiple-files" }
  );
});

test("dropzone rejects the entire batch when one file type is unsupported", () => {
  assert.deepEqual(
    validateFileDropzoneSelection(
      [file("terms.xlsx", "application/octet-stream"), file("notes.exe", "application/x-msdownload")],
      { accept: ".xlsx,.csv", multiple: true }
    ),
    { accepted: false, reason: "unsupported-type" }
  );
});

test("multi-file dropzone deduplicates selections and preserves stable order", () => {
  const first = file("source.pdf", "application/pdf", 100, 10);
  const duplicate = file("source.pdf", "application/pdf", 100, 10);
  const second = file("notes.md", "text/markdown", 20, 11);
  const result = validateFileDropzoneSelection([first, duplicate, second], {
    accept: ".pdf,.md",
    multiple: true,
  });
  assert.equal(result.accepted, true);
  if (result.accepted) assert.deepEqual(result.files, [duplicate, second]);

  assert.deepEqual(mergeUniqueFiles([first], [duplicate, second]), [duplicate, second]);
});
