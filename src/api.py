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
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, Field

from agent import ROOT, ask, build_graph, current_turn, message_text, pending_interrupt, resume
from tools import get_employee
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
