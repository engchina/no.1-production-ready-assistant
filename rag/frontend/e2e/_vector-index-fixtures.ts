export function vectorIndexEnvelope(profile: string, actual: [number, number] | null = [32, 500]) {
  const specs = [
    { name: "balanced", target_accuracy: 95, neighbors: 32, efconstruction: 500 },
    { name: "accurate", target_accuracy: 98, neighbors: 48, efconstruction: 800 },
    { name: "fast", target_accuracy: 85, neighbors: 16, efconstruction: 300 },
  ];
  const selected = specs.find((s) => s.name === profile) ?? specs[0];
  const statusOf = (spec: { neighbors: number; efconstruction: number }) =>
    actual === null
      ? "unknown"
      : actual[0] === spec.neighbors && actual[1] === spec.efconstruction
        ? "match"
        : "reprovision";
  const reindexSql =
    `DROP INDEX rag_chunks_embedding_hnsw_idx;\n` +
    `CREATE VECTOR INDEX rag_chunks_embedding_hnsw_idx\n` +
    `    ON rag_chunks (embedding)\n` +
    `    ORGANIZATION INMEMORY NEIGHBOR GRAPH\n` +
    `    DISTANCE COSINE\n` +
    `    WITH TARGET ACCURACY ${selected.target_accuracy}\n` +
    `    PARAMETERS (\n` +
    `        TYPE HNSW,\n` +
    `        NEIGHBORS ${selected.neighbors},\n` +
    `        EFCONSTRUCTION ${selected.efconstruction}\n` +
    `    );`;
  return {
    data: {
      profile,
      target_accuracy: selected.target_accuracy,
      neighbors: selected.neighbors,
      efconstruction: selected.efconstruction,
      distance: "COSINE",
      requires_reprovision: statusOf(selected) === "reprovision",
      index_status: statusOf(selected),
      actual_neighbors: actual?.[0] ?? null,
      actual_efconstruction: actual?.[1] ?? null,
      profiles: specs.map((s) => ({
        ...s,
        origin: "x",
        recommended_for: ["general"],
        distance: "COSINE",
        selected: s.name === profile,
        index_status: statusOf(s),
      })),
      reindex_sql: reindexSql,
      config_source: "runtime",
    },
    error_messages: [],
    warning_messages: [],
  };
}

