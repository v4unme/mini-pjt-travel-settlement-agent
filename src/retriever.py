"""retriever.py - 출장 규정 문서 RAG 파이프라인 (조항 단위 청킹 → 벡터 검색 + BM25 하이브리드)"""
import os
import threading
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from kiwipiepy import Kiwi
from langchain_aws import BedrockEmbeddings
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter
from rank_bm25 import BM25Okapi

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
POLICY_DIR = ROOT / "data" / "policy"
EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"


def load_policy_chunks() -> list[Document]:
    """규정 문서를 '## ' 조항 단위로 나누고, 조항 제목을 doc_id 메타데이터로 붙인다."""
    splitter = MarkdownHeaderTextSplitter(headers_to_split_on=[("##", "section")])
    chunks = []
    for path in sorted(POLICY_DIR.glob("*.md")):
        for doc in splitter.split_text(path.read_text(encoding="utf-8")):
            section = doc.metadata.get("section")
            if not section:  # 첫 조항 앞의 문서 머리말은 검색 대상에서 뺀다
                continue
            doc.metadata = {"doc_id": section, "source": path.name}
            doc.page_content = f"{section}\n{doc.page_content}"
            chunks.append(doc)
    return chunks


_build_lock = threading.Lock()


@lru_cache(maxsize=1)
def _build_vectorstore() -> Chroma:
    """규정 조항을 임베딩해 메모리 벡터 저장소를 만든다."""
    embeddings = BedrockEmbeddings(
        model_id=EMBED_MODEL_ID,
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )
    chunks = load_policy_chunks()
    # 조항 제목을 ID 로 써서, 다시 넣어도 같은 조항이 중복 저장되지 않게 한다
    ids = [f"{c.metadata['source']}#{c.metadata['doc_id']}" for c in chunks]
    return Chroma.from_documents(chunks, embeddings, ids=ids, collection_name="travel_policy")


def get_vectorstore() -> Chroma:
    """벡터 저장소를 프로세스당 한 번만 만든다.

    Agent 가 search_policy 를 동시에 여러 번 호출해도 한 스레드만 만들도록 잠근다.
    """
    with _build_lock:
        return _build_vectorstore()


@lru_cache(maxsize=1)
def _sections() -> dict[str, dict]:
    """규정 조항을 {조항 제목: {"doc_id", "text"}} 로 읽어 둔다. 임베딩 호출 없음."""
    return {c.metadata["doc_id"]: {"doc_id": c.metadata["doc_id"], "text": c.page_content}
            for c in load_policy_chunks()}


def get_sections(doc_ids: list[str]) -> list[dict]:
    """조항 제목 목록에 해당하는 조항 본문을 순서대로 돌려준다 (필요한 조항을 이미 알 때 쓰는 결정적 조회)."""
    sections = _sections()
    return [sections[d] for d in doc_ids if d in sections]


# ---------------------------------------------------------------- BM25 (키워드 검색, 임베딩 호출 없음)

KEEP_TAGS = {"NNG", "NNP", "NNB", "NR", "NP", "VV", "VA", "SL", "SN"}  # 명사·어간·숫자·외래어 (Day 2 실습과 같은 기준)


@lru_cache(maxsize=1)
def _kiwi() -> Kiwi:
    """한국어 형태소 분석기. 만드는 비용이 커서 한 번만 만든다."""
    return Kiwi()


def tokenize(text: str) -> list[str]:
    """BM25 용 한국어 토큰화: 형태소 중 명사·어간·숫자·외래어만 남긴다."""
    return [t.form for t in _kiwi().tokenize(text) if t.tag in KEEP_TAGS]


@lru_cache(maxsize=1)
def _bm25() -> tuple[list[str], BM25Okapi]:
    """조항 제목 목록과 BM25 색인을 만든다."""
    chunks = load_policy_chunks()
    return [c.metadata["doc_id"] for c in chunks], BM25Okapi([tokenize(c.page_content) for c in chunks])


def bm25_rank(query: str) -> list[str]:
    """질의 키워드가 하나라도 맞는 조항을 BM25 점수 순으로 돌려준다."""
    with _build_lock:
        doc_ids, index = _bm25()
        scores = index.get_scores(tokenize(query))
    ranked = sorted(zip(doc_ids, scores), key=lambda pair: pair[1], reverse=True)
    return [doc_id for doc_id, score in ranked if score > 0]


def rrf_merge(rankings: list[list[str]], k: int = 60) -> list[str]:
    """여러 순위 목록을 순위 역수 합(RRF)으로 합친다. 점수 척도가 달라도 순위만 쓰므로 공정하게 섞인다."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1 / (k + rank)
    return sorted(scores, key=lambda doc_id: scores[doc_id], reverse=True)


def search(query: str, k: int = 3) -> list[dict]:
    """하이브리드 검색: 벡터 검색(의미) + BM25(키워드)를 RRF 로 합쳐 규정 조항 k개를 돌려준다.

    반환: [{"doc_id": 조항 제목, "text": 본문}]
    """
    total = len(_sections())
    vector_ids = [d.metadata["doc_id"] for d in get_vectorstore().similarity_search(query, k=total)]
    merged = rrf_merge([vector_ids, bm25_rank(query)])
    return get_sections(merged[:k])


if __name__ == "__main__":
    for q in ["출장비 정산은 언제까지 해야 하나요?", "식비 한도를 넘으면 어떻게 하나요?", "TR-102 양식"]:
        print(f"\n질의: {q}  (BM25: {bm25_rank(q)})")
        for hit in search(q):
            print(f"  - {hit['doc_id']}")
