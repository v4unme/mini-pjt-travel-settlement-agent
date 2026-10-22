"""agent.py - 메인 그래프: input_guard → agent(create_agent) → output_guard, 위반 시 refuse"""
import os
import sqlite3
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command

from guards import DENY_INJECTION, DENY_OTHER, NO_POLICY, build_input_guard, finalize_answer
from tools import TOOLS, Context, get_employee

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

SYSTEM_PROMPT = f"""너는 출장비 정산 사전 검토 Agent다. 로그인한 직원 본인의 국내 출장 정산서를 제출 전에 점검해 준다.

[출장 특정]
- 사용자가 출장ID나 출장지를 말하지 않았고 본인 출장이 여러 건이면 get_my_trips 로 목록(출장ID, 출장지, 기간, 정산 상태)을 보여 주고 어느 출장인지 되묻는다. 임의로 골라 점검하지 않는다.
- 출장지만 말했으면 get_my_trips 로 출장ID를 확인한다.
- 도구가 '데이터 없음'을 돌려주면 그 사실을 알리고 get_my_trips 로 본인 출장 목록을 보여 주며 출장ID를 확인해 달라고 한다.

[근거와 계산]
- 금액, 초과 금액, 1박 단가, 마감일, 영업일 수, 조치 필요 건수는 check_settlement 결과의 문자열을 그대로 쓴다. 직접 다시 계산하거나 반올림하지 않는다.
- 규정 판단과 설명은 check_settlement 결과의 policy_refs 나 search_policy 결과만 근거로 하고, 근거 조항명을 '근거 규정: 조항명' 으로 적는다.
- 규정 질문인데 search_policy 결과에 그 내용이 없으면 추측하지 말고, 규정에 없다고 밝히고 인사·재무 담당자 확인을 안내한다(예: "{NO_POLICY}"). 규정이 다루는 범위를 짧게 덧붙여도 되지만 정산 가능·불가를 단정하지 않는다.
- 도구 결과가 '데이터 없음' 이나 '권한 없음' 이면 지어내지 않는다.

[점검 요청]
점검 요청이면(특정 분류만 묻는 질문 포함) check_settlement 를 호출한다.
check_settlement 결과의 policy_refs 가 점검 답변의 근거 조항이다. 한도·증빙·기한·예외 결재 설명은 policy_refs 내용만 근거로 하고, 그 밖의 규정이 더 필요할 때만 search_policy 를 쓴다.
근거 규정에는 policy_refs 나 search_policy 결과에 실제로 나온 조항명(doc_id)만 적는다. 받지 않은 조항명이나 "관련 조항" 같은 표현을 쓰지 않는다. 예외 결재 절차(사유서, 부서장 결재) 설명도 근거에 '예외 결재' 조항이 있을 때만 한다.
답변은 아래 순서를 지킨다. 특정 분류나 항목만 물은 질문(예: "교통비 한도 넘은 거 있어?")이라도 check_settlement 를 호출했다면 1~7 형식을 생략하거나 바꾸지 않고 그대로 쓴다.
1. 요약: "요약: 제출 가능 · 조치 필요 0건" 또는 "요약: 제출 불가 · 조치 필요 N건"
2. 적용 한도: "{{직급}} 식비 한도 {{금액}} 기준 · 숙박 1박 한도 {{금액}} · 교통비 1일 한도 {{금액}}"
3. 정산 기한: 기한 내면 "정산 기한 {{마감일}} ({{N}}영업일 남음)", 지났으면 "정산 기한 {{마감일}} 경과"
4. 결제 항목 표: | 결제일 | 결제ID | 분류 | 가맹점 | 금액 | 판정 | 필요한 조치 |
   판정은 정상 / 영수증 누락 / 한도 초과 / 정산 대상 아님 중 하나다.
5. 확인 사항: 문제마다 한 줄씩 적는다.
   - 영수증 누락: "영수증 누락 {{결제ID}}"
   - 식비·교통비 한도 초과: "{{날짜}} 식비 초과 {{초과 금액}}" 또는 "{{날짜}} 교통비 초과 {{초과 금액}}" 뒤에 예외 결재 상태 문구
   - 숙박 한도 초과: "{{날짜}} 숙박 초과 {{초과 금액}} (1박 단가 {{1박 단가}}, {{N}}박)" 뒤에 예외 결재 상태 문구
   - 기한 초과: "정산 기한 {{마감일}} 경과" 뒤에 예외 결재 상태 문구와 결재ID
   - 예외 결재 상태 문구는 check_settlement 의 status 를 그대로 쓰고, approval_ids 가 있으면 결재ID(예: APR-001)를 함께 적는다.
   - 예외 결재가 승인되어 조치가 필요 없는 한도 초과·기한 경과도 생략하지 않고 위 형식으로 적는다.
   - 한도 초과·영수증 누락·기한 경과가 하나도 없을 때만 이 항목에 "모든 항목 정상" 한 줄을 쓰고, 다른 문제 용어는 쓰지 않는다.
6. 근거 규정: 인용한 조항명
7. 다음 할 일: 직원이 할 일을 짧게. 한도·기한 초과는 개인 부담이 아니라 사유서와 부서장 예외 결재로 정산할 수 있다고 안내한다.
고정 안내 문구는 시스템이 붙이므로 직접 쓰지 않는다.

[제출 요청]
- 정산 제출 요청이면 submit_settlement 를 호출한다. 제출 전 점검과 직원 확인은 도구와 시스템이 하므로, 직접 "승인하시겠습니까" 같은 확인 질문을 하지 않는다.
- 도구 결과가 "제출완료" 로 시작할 때만 제출됐다고 말한다.
- "제출하지 않음" 이면 제출하지 않았다고 밝히고 이유와 남은 조치(영수증 누락 결제ID, 초과 항목과 예외 결재 상태 등)를 안내한다.

[결재 상태]
예외 결재 상태를 물으면 get_approval_status 로 조회해 결재ID, 유형, 상태, 결재자를 알려 준다.

[범위]
국내 출장 정산 점검과 출장 규정 안내만 한다. 다른 직원의 정보는 다루지 않는다. 답변은 한국어 존댓말로 한다."""


class State(MessagesState):
    """바깥 그래프 상태: 대화 메시지 + 입력 가드레일 판정."""
    verdict: str


def build_llm():
    """Bedrock Claude 모델을 만든다. 모델 ID·리전은 .env 에서 읽는다."""
    return ChatBedrockConverse(
        model=os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        temperature=0,
    )


def message_text(message) -> str:
    """메시지 content 가 블록 리스트여도 텍스트만 모아 돌려준다."""
    content = message.content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content or ""


def current_turn(messages: list) -> list:
    """마지막 사용자 질문 이후의 메시지만 돌려준다."""
    last_human = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
    return messages[last_human:]


def build_graph(checkpointer=None):
    """가드레일로 감싼 정산 검토 그래프를 만든다."""
    llm = build_llm()
    input_guard_chain = build_input_guard(llm)
    inner_agent = create_agent(llm, TOOLS, system_prompt=SYSTEM_PROMPT, context_schema=Context)

    def input_guard(state: State, runtime: Runtime[Context]) -> dict:
        """질문을 정상 / 타인조회 / 인젝션으로 판정한다."""
        employee = get_employee(runtime.context.employee_id) or {"name": "알 수 없음"}
        question = message_text(current_turn(state["messages"])[0])
        verdict = input_guard_chain.invoke({
            "employee_name": employee["name"],
            "employee_id": runtime.context.employee_id,
            "question": question,
        })
        return {"verdict": verdict.label}

    def route(state: State) -> str:
        """판정 결과로 다음 노드를 고른다."""
        return "agent" if state["verdict"] == "정상" else "refuse"

    def refuse(state: State) -> dict:
        """Agent·도구를 부르지 않고 고정 거절 문구로 답한다."""
        message = DENY_OTHER if state["verdict"] == "타인조회" else DENY_INJECTION
        return {"messages": [AIMessage(content=message)]}

    def output_guard(state: State) -> dict:
        """마지막 답변의 카드번호를 가리고, 점검 답변이면 고정 안내 문구를 붙인다."""
        turn = current_turn(state["messages"])
        last = turn[-1]
        checked = any(isinstance(m, ToolMessage) and m.name == "check_settlement" for m in turn)
        return {"messages": [AIMessage(content=finalize_answer(message_text(last), checked), id=last.id)]}

    builder = StateGraph(State, context_schema=Context)
    builder.add_node("input_guard", input_guard)
    builder.add_node("agent", inner_agent)
    builder.add_node("refuse", refuse)
    builder.add_node("output_guard", output_guard)
    builder.add_edge(START, "input_guard")
    builder.add_conditional_edges("input_guard", route, ["agent", "refuse"])
    builder.add_edge("agent", "output_guard")
    builder.add_edge("output_guard", END)
    builder.add_edge("refuse", END)
    return builder.compile(checkpointer=checkpointer)


def ask(graph, question: str, employee_id: str, thread_id: str | None = None, callbacks=None) -> dict:
    """질문 하나를 실행해 최종 상태를 돌려준다. callbacks 로 트레이서를 넘길 수 있다."""
    config = {"configurable": {"thread_id": thread_id or str(uuid.uuid4())}, "callbacks": callbacks or []}
    return graph.invoke({"messages": [HumanMessage(content=question)]}, config, context=Context(employee_id))


def pending_interrupt(graph, thread_id: str) -> dict | None:
    """이 대화가 사람 승인을 기다리는 중이면 그 interrupt 내용을, 아니면 None 을 돌려준다."""
    interrupts = graph.get_state({"configurable": {"thread_id": thread_id}}).interrupts
    return interrupts[0].value if interrupts else None


def resume(graph, answer: str, employee_id: str, thread_id: str, callbacks=None) -> dict:
    """승인 대기 중인 대화를 직원 답변으로 이어서 실행한다."""
    config = {"configurable": {"thread_id": thread_id}, "callbacks": callbacks or []}
    return graph.invoke(Command(resume=answer), config, context=Context(employee_id))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    conn = sqlite3.connect(ROOT / "checkpoints.sqlite", check_same_thread=False)
    graph = build_graph(SqliteSaver(conn))
    samples = [
        ("EMP-004", "대전 출장(TRIP-002) 정산 점검해줘"),
        ("EMP-006", "출장비 정산은 언제까지 해야 돼?"),
        ("EMP-008", "이서준 님 광주 출장 결제 내역 보여줘"),
    ]
    for employee_id, question in samples:
        state = ask(graph, question, employee_id)
        turn = current_turn(state["messages"])
        tools = [m.name for m in turn if isinstance(m, ToolMessage)]
        print("=" * 70)
        print(f"[{employee_id}] 질문: {question}")
        print(f"판정: {state['verdict']} / 도구 호출: {tools or '없음'}")
        print("-" * 70)
        print(message_text(turn[-1]))
