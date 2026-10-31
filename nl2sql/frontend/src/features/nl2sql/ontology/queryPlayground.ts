import {
  GROUNDING_ATTRIBUTE_KINDS,
  GROUNDING_ENTITY_KINDS,
  matchQuestionToNodes,
  normalizeGroundingText,
  type GroundingCandidate,
} from "./groundingMatcher";
import { isGroundingContextEdge } from "./graphView";
import {
  ontologyRelationshipRows,
  type OntologyEdge,
  type OntologyGraph,
  type OntologyNode,
  type OntologyRelationshipRow,
} from "./types";

/**
 * 決定論 NL Query Playground。LLM を呼ばず、質問文をオントロジーの
 * ノード/エッジへ段階マッチングしてハイライト対象と説明文を返す。
 * SQL は生成しない(可視化・デバッグ用途)。照合は groundingMatcher の
 * 2 段階エンジン(名称包含+日本語トークン部分一致)を使う。
 */

export type PlaygroundStage =
  | "entity_definition"
  | "list_all"
  | "relationship"
  | "property"
  | "aggregate"
  | "no_match";

export interface PlaygroundResult {
  stage: PlaygroundStage;
  highlightNodeIds: string[];
  highlightEdgeIds: string[];
  explanationJa: string;
  matchedEntityNames: string[];
  suggestionsJa: string[];
  /** ランク付きの一致候補(下線表示・診断用)。 */
  candidates: GroundingCandidate[];
  /** 集計質問(平均/合計/〜ごと 等)を検出したか。 */
  aggregate: boolean;
}

const BUSINESS_ENTITY_KINDS = new Set(["business_entity", "business_event", "object_type", "interface", "function", "action_type", "object_set", "shared_property", "value_type", "enumeration", "business_rule"]);
const LIST_PATTERNS = [/一覧/, /すべて/, /全て/, /リスト/, /list/i, /show me all/i];
const DEFINITION_PATTERNS = [/とは/, /について/, /何ですか/, /どういう/, /what is/i];
const MAX_ENTITY_MATCHES = 3;
const MAX_ATTRIBUTE_MATCHES = 3;

export function normalizeQuestion(question: string): string {
  return normalizeGroundingText(question);
}

/** 接地結果に厳密に一致した関係行のみ返す(no_match 時は空。無関係な行を接地パスに混ぜない)。 */
export function groundedRelationshipRows(
  graph: OntologyGraph,
  result: Pick<PlaygroundResult, "highlightNodeIds" | "highlightEdgeIds"> | null
): OntologyRelationshipRow[] {
  if (!result) return [];
  const rows = ontologyRelationshipRows(graph);
  const highlightNodes = new Set(result.highlightNodeIds);
  const highlightEdges = new Set(result.highlightEdgeIds);
  return rows.filter(
    (row) =>
      highlightEdges.has(row.edge_id) ||
      (highlightNodes.has(row.source_node_id) && highlightNodes.has(row.target_node_id))
  );
}

/** 関係一覧(閲覧用): 接地一致があればそれを、なければ全件を表示する。 */
export function browseRelationshipRows(
  graph: OntologyGraph,
  grounded: OntologyRelationshipRow[]
): OntologyRelationshipRow[] {
  return grounded.length > 0 ? grounded : ontologyRelationshipRows(graph);
}

function relationshipLabel(edge: OntologyEdge): string {
  const cardinality =
    edge.cardinality && edge.cardinality !== "unknown" ? `(${edge.cardinality})` : "";
  return `${edge.relationship_name_ja}${cardinality}`;
}

function edgesBetween(edges: OntologyEdge[], nodeIds: Set<string>): OntologyEdge[] {
  return edges.filter(
    (edge) => nodeIds.has(edge.source_node_id) && nodeIds.has(edge.target_node_id)
  );
}

function edgesTouchingNode(edges: OntologyEdge[], nodeId: string): OntologyEdge[] {
  return edges.filter(
    (edge) => edge.source_node_id === nodeId || edge.target_node_id === nodeId
  );
}

/** 属性ノードの親エンティティ(表・業務概念)を包含/マッピング系エッジで逆引きする。 */
function attributeParents(
  graph: OntologyGraph,
  attribute: OntologyNode
): Array<{ parent: OntologyNode; edge: OntologyEdge }> {
  const parents: Array<{ parent: OntologyNode; edge: OntologyEdge }> = [];
  const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
  for (const edge of edgesTouchingNode(graph.edges, attribute.id)) {
    if (!isGroundingContextEdge(edge)) continue;
    const otherId =
      edge.source_node_id === attribute.id ? edge.target_node_id : edge.source_node_id;
    const other = nodeById.get(otherId);
    if (other && GROUNDING_ENTITY_KINDS.has(other.kind)) {
      parents.push({ parent: other, edge });
    }
  }
  return parents;
}

/** 業務概念と物理表が同じ文字列で一致した場合は業務概念を優先する。 */
function preferBusinessEntities(entities: GroundingCandidate[]): GroundingCandidate[] {
  const businessTexts = new Set(
    entities
      .filter((candidate) => BUSINESS_ENTITY_KINDS.has(candidate.node.kind))
      .map((candidate) => candidate.matchedText)
  );
  if (businessTexts.size === 0) return entities;
  return entities.filter(
    (candidate) =>
      BUSINESS_ENTITY_KINDS.has(candidate.node.kind) ||
      !businessTexts.has(candidate.matchedText)
  );
}

function quoteNames(nodes: OntologyNode[]): string {
  return nodes.map((node) => `「${node.business_name_ja}」`).join("と");
}

export function answerOntologyQuestion(
  graph: OntologyGraph,
  question: string
): PlaygroundResult {
  const { candidates, normalizedQuestion, aggregate } = matchQuestionToNodes(graph, question);

  const businessSuggestions = [
    ...new Set(
      graph.nodes
        .filter((node) => BUSINESS_ENTITY_KINDS.has(node.kind))
        .map((node) => node.business_name_ja)
    ),
  ];
  const physicalSuggestions = [
    ...new Set(
      graph.nodes
        .filter((node) => node.kind === "table" || node.kind === "view")
        .map((node) => node.business_name_ja)
    ),
  ];
  const suggestions = (
    businessSuggestions.length > 0 ? businessSuggestions : physicalSuggestions
  ).slice(0, 5);

  const noMatch = (explanationJa: string): PlaygroundResult => ({
    stage: "no_match",
    highlightNodeIds: [],
    highlightEdgeIds: [],
    explanationJa,
    matchedEntityNames: [],
    suggestionsJa: suggestions,
    candidates,
    aggregate,
  });

  if (!normalizedQuestion) return noMatch("質問を入力してください。");

  const links = graph.edges.filter(edge => {
    const definition = edge.metadata?.definition;
    const aliases = definition && typeof definition === "object" && !Array.isArray(definition)
      && Array.isArray(definition.aliases) ? definition.aliases.filter((v): v is string => typeof v === "string") : [];
    return edge.kind === "link_type" && [edge.relationship_name_ja, String(edge.metadata?.api_name ?? ""), ...aliases]
      .some(label => Boolean(label) && label!.length >= 2 && normalizedQuestion.includes(normalizeGroundingText(label!)));
  });
  if (links.length) {
    const ids = [...new Set(links.flatMap(edge=>[edge.source_node_id, edge.target_node_id]))];
    return {stage:"relationship",highlightNodeIds:ids,highlightEdgeIds:links.map(e=>e.id),explanationJa:links.map(e=>`${relationshipLabel(e)} ${e.description_ja ?? ""}`).join("、"),matchedEntityNames:graph.nodes.filter(n=>ids.includes(n.id)).map(n=>n.business_name_ja),suggestionsJa:[],candidates,aggregate};
  }

  const entityCandidates = preferBusinessEntities(
    candidates.filter((candidate) => GROUNDING_ENTITY_KINDS.has(candidate.node.kind))
  ).slice(0, MAX_ENTITY_MATCHES);
  const entityTexts = new Map(
    entityCandidates.map((candidate) => [candidate.matchedText, candidate.score])
  );
  // エンティティを接地させたのと同じ文字列で(同点以下の)属性が重複一致した場合は落とす
  // (例:「部署」が 部署情報(表)と 部署ID(列)の両方に前方一致するケース)。
  const attributeCandidates = candidates
    .filter((candidate) => GROUNDING_ATTRIBUTE_KINDS.has(candidate.node.kind))
    .filter(
      (candidate) => (entityTexts.get(candidate.matchedText) ?? -1) < candidate.score
    )
    .slice(0, MAX_ATTRIBUTE_MATCHES);

  const entities = entityCandidates.map((candidate) => candidate.node);
  const attributes = attributeCandidates.map((candidate) => candidate.node);
  const matchedEntityNames = entities.map((node) => node.business_name_ja);

  if (entities.length === 0 && attributes.length === 0) {
    return noMatch("質問に一致するエンティティが見つかりませんでした。");
  }

  // --- 属性単独接地: 親エンティティを自動連結する(「給与とは」) --------------------------
  if (entities.length === 0) {
    const primary = attributes[0];
    const parents = attributeParents(graph, primary);
    const highlightNodeIds = [primary.id, ...parents.map(({ parent }) => parent.id)];
    const highlightEdgeIds = parents.map(({ edge }) => edge.id);
    const parentText =
      parents.length > 0
        ? `「${parents[0].parent.business_name_ja}」が持つ`
        : "";
    return {
      stage: aggregate ? "aggregate" : "property",
      highlightNodeIds,
      highlightEdgeIds,
      explanationJa:
        `「${primary.business_name_ja}」は${parentText}` +
        `${primary.kind === "metric" ? "指標" : "属性・列"}です。` +
        (primary.description_ja ? ` ${primary.description_ja}` : "") +
        (aggregate ? " 集計(平均・合計・グループ化など)の対象になります。" : ""),
      matchedEntityNames: parents.map(({ parent }) => parent.business_name_ja),
      suggestionsJa: [],
      candidates,
      aggregate,
    };
  }

  // --- 複数エンティティ: 関係辿り(直接辺 → 1-hop) ------------------------------------
  if (entities.length >= 2) {
    const matchedIds = new Set(entities.map((node) => node.id));
    const attributeExtras = attributes.flatMap((attribute) => {
      const parents = attributeParents(graph, attribute);
      return [
        attribute.id,
        ...parents.map(({ parent }) => parent.id),
      ];
    });
    const attributeEdges = attributes.flatMap((attribute) =>
      attributeParents(graph, attribute).map(({ edge }) => edge.id)
    );
    const direct = edgesBetween(graph.edges, matchedIds);
    if (direct.length > 0) {
      return {
        stage: aggregate ? "aggregate" : "relationship",
        highlightNodeIds: [...new Set([...matchedIds, ...attributeExtras])],
        highlightEdgeIds: [...new Set([...direct.map((edge) => edge.id), ...attributeEdges])],
        explanationJa:
          `${quoteNames(entities)}は ` +
          `${direct.map(relationshipLabel).join("、")} で結ばれています。` +
          (aggregate ? " 集計(平均・合計・グループ化など)を検出しました。" : ""),
        matchedEntityNames,
        suggestionsJa: [],
        candidates,
        aggregate,
      };
    }
    const [first, second] = entities;
    for (const node of graph.nodes) {
      if (node.id === first.id || node.id === second.id) continue;
      const viaFirst = graph.edges.filter(
        (edge) =>
          (edge.source_node_id === first.id && edge.target_node_id === node.id) ||
          (edge.target_node_id === first.id && edge.source_node_id === node.id)
      );
      const viaSecond = graph.edges.filter(
        (edge) =>
          (edge.source_node_id === second.id && edge.target_node_id === node.id) ||
          (edge.target_node_id === second.id && edge.source_node_id === node.id)
      );
      if (viaFirst.length > 0 && viaSecond.length > 0) {
        return {
          stage: aggregate ? "aggregate" : "relationship",
          highlightNodeIds: [first.id, node.id, second.id],
          highlightEdgeIds: [viaFirst[0].id, viaSecond[0].id],
          explanationJa:
            `「${first.business_name_ja}」と「${second.business_name_ja}」は` +
            `「${node.business_name_ja}」を経由してつながります。`,
          matchedEntityNames,
          suggestionsJa: [],
          candidates,
          aggregate,
        };
      }
    }
    return {
      stage: "relationship",
      highlightNodeIds: [...matchedIds],
      highlightEdgeIds: [],
      explanationJa:
        `${quoteNames(entities)}の間に承認済みの関係が見つかりませんでした。`,
      matchedEntityNames,
      suggestionsJa: [],
      candidates,
      aggregate,
    };
  }

  // --- 単一エンティティ ---------------------------------------------------------------
  const entity = entities[0];
  const connectedEdges = edgesTouchingNode(graph.edges, entity.id);

  if (attributes.length > 0) {
    const attribute = attributes[0];
    const directEdges = connectedEdges.filter(
      (edge) => edge.source_node_id === attribute.id || edge.target_node_id === attribute.id
    );
    // 属性がエンティティと直接つながらない場合は親エンティティ経由の接地パスを組み立てる
    // (例:「部署ごとの平均給与」→ 部署情報 ↔ 従業員情報 が持つ 給与)。
    let bridgeNodeIds: string[] = [];
    let bridgeEdgeIds: string[] = [];
    let bridgeText = "";
    if (directEdges.length === 0) {
      const parents = attributeParents(graph, attribute).filter(
        ({ parent }) => parent.id !== entity.id
      );
      for (const { parent, edge } of parents) {
        const joinEdges = edgesBetween(
          graph.edges,
          new Set([entity.id, parent.id])
        );
        if (joinEdges.length === 0) continue;
        bridgeNodeIds = [parent.id];
        bridgeEdgeIds = [edge.id, ...joinEdges.map((item) => item.id)];
        bridgeText =
          `「${attribute.business_name_ja}」は「${parent.business_name_ja}」の` +
          `${attribute.kind === "metric" ? "指標" : "属性"}で、` +
          `「${entity.business_name_ja}」とは ${joinEdges
            .map(relationshipLabel)
            .join("、")} で結ばれています。`;
        break;
      }
    }
    return {
      stage: aggregate ? "aggregate" : "property",
      highlightNodeIds: [...new Set([entity.id, attribute.id, ...bridgeNodeIds])],
      highlightEdgeIds: [
        ...new Set([...directEdges.map((edge) => edge.id), ...bridgeEdgeIds]),
      ],
      explanationJa:
        (bridgeText ||
          `「${attribute.business_name_ja}」は「${entity.business_name_ja}」に関連する` +
            `${attribute.kind === "metric" ? "指標" : "属性・用語"}です。` +
            (attribute.description_ja ? ` ${attribute.description_ja}` : "")) +
        (aggregate ? " 集計(平均・合計・グループ化など)を検出しました。" : ""),
      matchedEntityNames,
      suggestionsJa: [],
      candidates,
      aggregate,
    };
  }

  if (LIST_PATTERNS.some((pattern) => pattern.test(normalizedQuestion))) {
    return {
      stage: "list_all",
      highlightNodeIds: [entity.id],
      highlightEdgeIds: [],
      explanationJa: `「${entity.business_name_ja}」の一覧照会に対応するエンティティです。`,
      matchedEntityNames,
      suggestionsJa: [],
      candidates,
      aggregate,
    };
  }

  const isDefinition = DEFINITION_PATTERNS.some((pattern) => pattern.test(normalizedQuestion));
  return {
    stage: "entity_definition",
    highlightNodeIds: [entity.id],
    highlightEdgeIds: isDefinition ? [] : connectedEdges.map((edge) => edge.id),
    explanationJa:
      `「${entity.business_name_ja}」${entity.description_ja ? `: ${entity.description_ja}` : "に対応するエンティティです。"}` +
      (connectedEdges.length > 0 ? ` 関係が ${connectedEdges.length} 件あります。` : ""),
    matchedEntityNames,
    suggestionsJa: [],
    candidates,
    aggregate,
  };
}
