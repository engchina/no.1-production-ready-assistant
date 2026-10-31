"""根拠を明示して呼び出す回答生成サービス。"""

from rag_engine.generation.answer_labels import answer_labels
from rag_engine.models.contracts import AnswerResult, GenerationRequest
from rag_engine.ports import Generator


class GenerationService:
    """空検索のときは回答モデルを呼ばず不足を返す。"""

    def __init__(self, generator: Generator):
        self.generator = generator

    def generate(self, request: GenerationRequest) -> AnswerResult:
        """質問と根拠から回答する。空質問は ValueError。"""
        if not request.question.strip():
            raise ValueError("question must not be empty")
        if not request.evidence:
            return AnswerResult(
                text=answer_labels().insufficient_evidence_summary,
                confidence="low",
                insufficient_reason="retrieval returned no evidence",
                needs_human_review=True,
            )
        return self.generator.generate(request)
