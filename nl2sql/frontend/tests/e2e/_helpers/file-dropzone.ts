import type { Locator, Page } from "@playwright/test";

export interface DropzoneTestFile {
  name: string;
  type: string;
  content: string;
  lastModified?: number;
}

export async function createFileDataTransfer(page: Page, files: DropzoneTestFile[]) {
  return page.evaluateHandle((items) => {
    const dataTransfer = new DataTransfer();
    for (const item of items) {
      dataTransfer.items.add(
        new File([item.content], item.name, {
          type: item.type,
          lastModified: item.lastModified,
        })
      );
    }
    return dataTransfer;
  }, files);
}

export async function dropFiles(
  page: Page,
  dropzone: Locator,
  files: DropzoneTestFile[]
) {
  const dataTransfer = await createFileDataTransfer(page, files);
  try {
    await dropzone.dispatchEvent("dragenter", { dataTransfer });
    await dropzone.dispatchEvent("dragover", { dataTransfer });
    await dropzone.dispatchEvent("drop", { dataTransfer });
  } finally {
    await dataTransfer.dispose();
  }
}
