export interface FileDropzoneFile {
  name: string;
  type: string;
  size: number;
  lastModified: number;
}

export type FileDropzoneRejectReason = "multiple-files" | "unsupported-type";

export type FileDropzoneValidationResult<T extends FileDropzoneFile> =
  | { accepted: true; files: T[] }
  | { accepted: false; reason: FileDropzoneRejectReason };

/** input accept と同じ拡張子 / MIME / wildcard 規則でファイルを判定する。 */
export function fileMatchesAccept(file: Pick<FileDropzoneFile, "name" | "type">, accept: string) {
  const acceptedTypes = accept
    .split(",")
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean);
  if (acceptedTypes.length === 0) return true;

  const filename = file.name.toLowerCase();
  const mimeType = file.type.toLowerCase();
  return acceptedTypes.some((acceptedType) => {
    if (acceptedType.startsWith(".")) return filename.endsWith(acceptedType);
    if (acceptedType.endsWith("/*")) return mimeType.startsWith(acceptedType.slice(0, -1));
    return mimeType === acceptedType;
  });
}

function fileIdentity(file: FileDropzoneFile) {
  return `${file.name}\u0000${file.size}\u0000${file.lastModified}`;
}

/** 既存選択と新規選択を安定順で結合し、同一ファイルを重複させない。 */
export function mergeUniqueFiles<T extends FileDropzoneFile>(
  current: readonly T[],
  incoming: readonly T[]
) {
  const unique = new Map<string, T>();
  for (const file of [...current, ...incoming]) {
    unique.set(fileIdentity(file), file);
  }
  return [...unique.values()];
}

/**
 * drop / file picker の候補を一括検証する。
 * エラー時は accepted files を返さないため、呼出側の現在選択を安全に保持できる。
 */
export function validateFileDropzoneSelection<T extends FileDropzoneFile>(
  files: readonly T[],
  options: { accept: string; multiple: boolean }
): FileDropzoneValidationResult<T> {
  if (!options.multiple && files.length > 1) {
    return { accepted: false, reason: "multiple-files" };
  }
  if (files.some((file) => !fileMatchesAccept(file, options.accept))) {
    return { accepted: false, reason: "unsupported-type" };
  }
  return {
    accepted: true,
    files: options.multiple ? mergeUniqueFiles([], files) : [...files],
  };
}
