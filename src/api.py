"""api.py - FastAPI 서버: POST /query → {answer, contexts, trace}

실행: uvicorn src.api:app --reload  (프로젝트 루트에서)
"""
import os
import sqlite3
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # src 안의 모듈을 바로 import 하기 위해

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, Field

from agent import ROOT, ask, build_graph, current_turn, message_text, pending_interrupt, resume
from tools import (
    _load as load_data, approver_inbox, attach_receipt, decide_approval, get_employee, missing_receipts_for,
    my_trip_overview,
)
from tracer import TraceRecorder

load_dotenv()


class QueryRequest(BaseModel):
    """요청 본문 (제출 규약: question 하나만 받는다)."""
    question: str = Field(min_length=1, description="사용자 질의")


class QueryResponse(BaseModel):
    """응답 본문 (제출 규약: answer, contexts, trace)."""
    answer: str
    contexts: list[dict]
    trace: list[dict]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버 시작 시 그래프와 체크포인터를 한 번만 만든다."""
    conn = sqlite3.connect(ROOT / "checkpoints.sqlite", check_same_thread=False)
    app.state.graph = build_graph(SqliteSaver(conn))
    yield
    conn.close()


app = FastAPI(title="출장비 정산 사전 검토 Agent", lifespan=lifespan)


CONTEXT_TOOLS = {"search_policy", "check_settlement"}  # 규정 조항을 artifact 로 돌려주는 도구


def _contexts(turn: list) -> list[dict]:
    """이번 질문에서 규정 검색·정산 점검이 근거로 쓴 조항을 중복 없이 모은다."""
    seen, contexts = set(), []
    for m in turn:
        if isinstance(m, ToolMessage) and m.name in CONTEXT_TOOLS:
            for hit in m.artifact or []:
                if hit["doc_id"] not in seen:
                    seen.add(hit["doc_id"])
                    contexts.append(hit)
    return contexts


def _is_throttled(error: Exception) -> bool:
    """Bedrock 호출 한도 초과 오류인지 확인한다."""
    return "Throttling" in type(error).__name__ or "Too many tokens" in str(error)


@app.post("/query", response_model=QueryResponse)
def query(
    body: QueryRequest,
    response: Response,
    x_employee_id: str | None = Header(default=None),
    x_thread_id: str | None = Header(default=None),
):
    """질문을 받아 정산 검토 그래프를 실행한다. 직원은 X-Employee-Id, 대화는 X-Thread-Id 헤더로 구분한다."""
    employee_id = x_employee_id or os.getenv("DEMO_EMPLOYEE_ID", "EMP-001")
    if get_employee(employee_id) is None:
        raise HTTPException(status_code=403, detail="등록되지 않은 직원입니다.")
    thread_id = x_thread_id or str(uuid.uuid4())
    response.headers["X-Thread-Id"] = thread_id

    graph = app.state.graph
    tracer = TraceRecorder(request_id=thread_id)
    waiting = pending_interrupt(graph, thread_id)
    if waiting and waiting.get("employee_id") != employee_id:
        raise HTTPException(status_code=403, detail="승인 대기 중인 요청은 요청한 직원만 답할 수 있습니다.")
    try:
        if waiting:  # 승인 대기 중이면 이번 질문을 직원의 승인/거절 답변으로 보고 이어서 실행한다
            state = resume(graph, body.question, employee_id, thread_id, callbacks=[tracer])
            first_step = {"step": "resume", "input": body.question, "output": waiting.get("type", "")}
        else:
            state = ask(graph, body.question, employee_id, thread_id, callbacks=[tracer])
            first_step = {"step": "input_guard", "input": body.question, "output": state["verdict"]}
    except Exception as error:
        if _is_throttled(error):
            raise HTTPException(status_code=503, detail="모델 호출 한도를 넘었습니다. 잠시 후 다시 시도해 주세요.")
        raise

    turn = current_turn(state["messages"])
    trace = [first_step] + tracer.steps
    if state.get("__interrupt__"):  # 사람 승인을 기다리며 멈춤: 확인 문구를 답으로 돌려준다
        request = state["__interrupt__"][0].value
        trace.append({"step": "interrupt", "input": request["type"], "output": request["message"]})
        return QueryResponse(answer=request["message"], contexts=_contexts(turn), trace=trace)
    return QueryResponse(answer=message_text(turn[-1]), contexts=_contexts(turn), trace=trace)


@app.get("/health")
def health():
    """서버 상태 확인."""
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def demo_page():
    """시연용 채팅 화면 (web/index.html). /query 를 그대로 호출한다."""
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/employees")
def employees():
    """시연 화면의 직원 선택 목록 (직원ID·이름·부서·직급, 역할: 직원 / 부서장)."""
    staff = load_data("employees.json")
    managers = {e["manager_id"] for e in staff if e.get("manager_id")}
    return [{**{k: e[k] for k in ("employee_id", "name", "department", "grade")},
             "role": "부서장" if e["employee_id"] in managers else "직원"} for e in staff]


@app.get("/trips/mine")
def trips_mine(x_employee_id: str | None = Header(default=None)):
    """시연 화면의 내 출장 정산 현황 표. 본인 출장만, 점검 계산 결과 그대로 (모델 호출 없음)."""
    if not x_employee_id or get_employee(x_employee_id) is None:
        raise HTTPException(status_code=403, detail="등록되지 않은 직원입니다.")
    return my_trip_overview(x_employee_id)


# ---------------------------------------------------------------- 시연용 영수증 첨부 (Agent 범위 밖, 모델 호출 없음)

class ReceiptRequest(BaseModel):
    """첨부 기록. 파일 내용은 받지 않고 이름·크기만 받는다 (더미 파일, 올리는 행위만 의미가 있다)."""
    file_name: str = Field(min_length=1, max_length=200)
    size: int = Field(default=0, ge=0)


@app.get("/receipts/missing")
def receipts_missing(x_employee_id: str | None = Header(default=None)):
    """로그인 직원 본인의 영수증 누락 결제 목록 (제출 전 출장만)."""
    if not x_employee_id or get_employee(x_employee_id) is None:
        raise HTTPException(status_code=403, detail="등록되지 않은 직원입니다.")
    return missing_receipts_for(x_employee_id)


@app.post("/receipts/{expense_id}")
def receipts_attach(expense_id: str, body: ReceiptRequest, x_employee_id: str | None = Header(default=None)):
    """영수증 첨부를 기록한다. 본인 결제만, 정산 대상·미첨부·제출 전 건만 가능하다."""
    record, error = attach_receipt(expense_id, x_employee_id or "", body.file_name, body.size)
    if error:
        raise HTTPException(status_code=403 if error.startswith("권한 없음") else 400, detail=error)
    return record


# ---------------------------------------------------------------- 시연용 부서장 결재함 (Agent 범위 밖, 모델 호출 없음)

class DecisionRequest(BaseModel):
    """부서장 결정."""
    decision: str = Field(pattern="^(승인|반려)$", description="승인 또는 반려")


@app.get("/approvals/inbox")
def approvals_inbox(x_employee_id: str | None = Header(default=None)):
    """로그인한 결재자에게 올라온 '요청됨' 예외 결재 목록."""
    if not x_employee_id or get_employee(x_employee_id) is None:
        raise HTTPException(status_code=403, detail="등록되지 않은 직원입니다.")
    return approver_inbox(x_employee_id)


@app.post("/approvals/{approval_id}/decision")
def approvals_decide(approval_id: str, body: DecisionRequest, x_employee_id: str | None = Header(default=None)):
    """결재자가 예외 결재를 승인·반려한다. 결재자 본인만, 요청됨 상태만 처리할 수 있다."""
    record, error = decide_approval(approval_id, x_employee_id or "", body.decision)
    if error:
        raise HTTPException(status_code=403 if "결재자만" in error else 400, detail=error)
    return record
