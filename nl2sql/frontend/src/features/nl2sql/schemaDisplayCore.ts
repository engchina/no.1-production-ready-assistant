export function formatSchemaCount(value: number | null | undefined) {
  return value == null ? "-" : new Intl.NumberFormat("ja-JP").format(value);
}

export function formatSampleValues(values: string[]) {
  return values.slice(0, 3).join(", ");
}
