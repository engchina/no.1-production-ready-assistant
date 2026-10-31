export const CORE_TABULAR_EXTENSIONS = [".csv", ".xlsx", ".xls"] as const;

export interface TabularFileFormatConfig {
  accept: string;
  formatLabel: string;
}

/**
 * 汎用の表形式入力は CSV / XLSX / XLS を必須の基底形式とし、
 * 画面固有の追加形式だけを引数で足す。
 */
export function tabularFileFormatConfig(
  extraExtensions: readonly string[] = []
): TabularFileFormatConfig {
  const extensions = [...CORE_TABULAR_EXTENSIONS, ...extraExtensions].map((extension) =>
    extension.startsWith(".") ? extension.toLowerCase() : `.${extension.toLowerCase()}`
  );
  const uniqueExtensions = [...new Set(extensions)];
  return {
    accept: uniqueExtensions.join(","),
    formatLabel: uniqueExtensions.map((extension) => extension.toUpperCase()).join(" / "),
  };
}

export const CORE_TABULAR_FILE_FORMATS = tabularFileFormatConfig();

export const XLSX_TEMPLATE_FILE_FORMATS: TabularFileFormatConfig = {
  accept: ".xlsx",
  formatLabel: ".XLSX",
};
