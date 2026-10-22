"""test_retriever.py - 하이브리드 검색의 BM25·순위 융합 검증. 임베딩·LLM 호출 없음.

실행: pytest tests
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from retriever import bm25_rank, rrf_merge, tokenize  # noqa: E402


def test_tokenize_keeps_keywords():
    """명사·숫자·외래어는 남기고 조사는 버린다."""
    tokens = tokenize("출장비 정산은 언제까지 해야 하나요? 양식 TR-102")
    assert {"정산", "양식", "TR", "102"} <= set(tokens)
    assert "은" not in tokens


def test_bm25_finds_keyword_sections():
    """키워드형 질의는 BM25 가 해당 조항을 1순위로 찾는다."""
    assert bm25_rank("TR-102 양식")[0] == "신청과 승인"
    assert bm25_rank("영수증 스캔본 첨부")[0] == "증빙"
    assert bm25_rank("정산 기한 영업일")[0] == "정산 기한"


def test_bm25_ignores_unrelated_query():
    """규정에 없는 키워드만 있으면 BM25 결과가 비어 있다 (지어낸 근거를 만들지 않는다)."""
    assert bm25_rank("골프장 이용료") == []


def test_rrf_merge_rewards_agreement():
    """두 목록에서 모두 상위인 조항이 1순위가 되고, 한쪽에만 있는 조항도 결과에 남는다."""
    merged = rrf_merge([["예외 결재", "경비 한도", "증빙"], ["경비 한도", "예외 결재", "정산 기한"]])
    assert set(merged[:2]) == {"예외 결재", "경비 한도"}
    assert set(merged) == {"예외 결재", "경비 한도", "증빙", "정산 기한"}


def test_rrf_merge_keeps_single_list_order():
    """목록이 하나면 원래 순서를 유지한다."""
    assert rrf_merge([["a", "b", "c"]]) == ["a", "b", "c"]
