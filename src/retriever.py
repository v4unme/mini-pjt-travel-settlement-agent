"""retriever.py - 출장 규정 문서 RAG 파이프라인 (청킹 → 임베딩 → 벡터 검색)"""
import os
import threading
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from langchain_aws import BedrockEmbeddings
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter

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


def search(query: str, k: int = 3) -> list[dict]:
    """질의와 가까운 규정 조항 k개를 [{"doc_id": 조항 제목, "text": 본문}] 으로 돌려준다."""
    docs = get_vectorstore().similarity_search(query, k=k)
    return [{"doc_id": d.metadata["doc_id"], "text": d.page_content} for d in docs]


if __name__ == "__main__":
    for q in ["출장비 정산은 언제까지 해야 하나요?", "식비 한도를 넘으면 어떻게 하나요?"]:
        print(f"\n질의: {q}")
        for hit in search(q):
            print(f"  - {hit['doc_id']}")
