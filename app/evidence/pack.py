"""Citation/Conflict — проверяемые по базе доказательства ответа (§11, §13 ТЗ).

`citationId` — порядковый номер цитаты в ЭТОМ конкретном ответе. `evidenceId` и
`sourceId` — реальные id из БД (BookChunk.id / Book.id соответственно), поэтому
выдуманная цитата в принципе не может туда попасть: id либо существует в базе
(и по нему поднимается точная страница/фрагмент через `/v1/evidence/{id}`),
либо чанка с таким id просто нет в текущей выдаче retrieval.
"""

from dataclasses import dataclass

from rag.retriever import ChunkResult


@dataclass
class Citation:
    citation_id: str
    evidence_id: str
    source_id: str
    source_title: str
    author: str
    subject: str
    page: int | None
    page_to: int | None
    section: str | None
    exact_supporting_text: str
    authority_level: str | None
    verification_status: str | None
    relevance: float | None

    def to_dict(self) -> dict:
        return {
            "citationId": self.citation_id,
            "evidenceId": self.evidence_id,
            "sourceId": self.source_id,
            "sourceTitle": self.source_title,
            "author": self.author,
            "subject": self.subject,
            "page": self.page,
            "pageTo": self.page_to,
            "section": self.section,
            "exactSupportingText": self.exact_supporting_text,
            "authorityLevel": self.authority_level,
            "verificationStatus": self.verification_status,
            "relevance": self.relevance,
        }


@dataclass
class Conflict:
    claim: str
    source_a_title: str
    source_b_title: str
    source_a_evidence_id: str
    source_b_evidence_id: str
    difference: str
    context_recommendation: str

    def to_dict(self) -> dict:
        return {
            "claim": self.claim,
            "sourceATitle": self.source_a_title,
            "sourceBTitle": self.source_b_title,
            "sourceAEvidenceId": self.source_a_evidence_id,
            "sourceBEvidenceId": self.source_b_evidence_id,
            "difference": self.difference,
            "contextRecommendation": self.context_recommendation,
        }


def _citation(index: int, chunk: ChunkResult) -> Citation:
    return Citation(
        citation_id=str(index),
        evidence_id=str(chunk.id),
        source_id=str(chunk.book_id) if chunk.book_id is not None else str(chunk.id),
        source_title=chunk.title,
        author=chunk.author,
        subject=chunk.subject,
        page=chunk.page_from,
        page_to=chunk.page_to,
        section=chunk.section,
        exact_supporting_text=chunk.content,
        authority_level=chunk.authority_level,
        verification_status=chunk.verification_status,
        relevance=chunk.rerank_score,
    )


def build_citations(cited_chunks: list[ChunkResult]) -> list[Citation]:
    return [_citation(i + 1, chunk) for i, chunk in enumerate(cited_chunks)]
