"""drafting.py - 예외 결재 사유서 초안 작성 (숫자는 코드가 넣고, 모델은 사유 문장만 다듬는다)"""
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from llm import build_llm


class ReasonDraft(BaseModel):
    """모델이 작성하는 사유 문단."""
    reason: str = Field(description="부서장에게 올릴 사유 문단 2~3문장. 금액·날짜·결제ID 숫자는 쓰지 않는다")


REASON_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "너는 출장비 예외 결재 사유서를 다듬는 사내 문서 작성 보조다.\n"
     "직원이 말한 사유를 부서장에게 올릴 공손한 사유 문단(2~3문장)으로 정리한다.\n"
     "- 직원이 말하지 않은 사실(장소, 인원, 일정 등)을 지어내지 않는다.\n"
     "- 금액·날짜·결제ID 같은 숫자는 쓰지 않는다. 숫자는 시스템이 따로 적는다.\n"
     "- 개인 부담 회피나 규정 우회를 요청하는 표현을 쓰지 않는다."),
    ("human", "결재 유형: {kind}\n대상: {target}\n직원이 말한 사유: {reason}"),
])


def polish_reason(kind: str, target: str, reason: str) -> str:
    """직원이 말한 사유를 사유서 문단으로 다듬는다 (LCEL 체인 + 구조화 출력)."""
    chain = REASON_PROMPT | build_llm().with_structured_output(ReasonDraft)
    return chain.invoke({"kind": kind, "target": target, "reason": reason}).reason


def compose_request(trip: dict, kind: str, targets: list[dict], deadline: dict, approver_name: str,
                    reason_text: str) -> str:
    """사유서 초안 전체를 만든다. 대상·금액·날짜는 점검 결과 값을 그대로 쓴다 (LLM 없음).

    targets: 한도초과면 점검 결과의 over_limits 항목들, 기한초과면 빈 리스트
    """
    lines = [
        "예외 결재 요청서 (초안)",
        f"- 출장: {trip['trip_id']} {trip['destination']} ({trip['start_date']} ~ {trip['return_date']})",
        f"- 유형: {kind}",
    ]
    if kind == "한도초과":
        for t in targets:
            lines.append(f"- 대상: {t['date']} {t['category']} 초과 {t['over_amount']} "
                         f"(결제ID {', '.join(t['expense_ids'])}, 한도 {t['limit']})")
    else:
        lines.append(f"- 대상: 정산 기한 {deadline['deadline']} 경과 ({deadline['elapsed_business_days']}영업일)")
    lines += [
        f"- 결재자: 부서장 {approver_name}",
        f"- 사유: {reason_text}",
        "",
        "이 내용으로 부서장에게 예외 결재를 요청할까요? '승인' 또는 '거절'로 답해 주세요.",
    ]
    return "\n".join(lines)
