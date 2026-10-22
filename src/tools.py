"""tools.py - 출장 정산 도메인 도구 (로그인 직원 본인의 데이터만 다룬다)"""
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from langchain.tools import ToolRuntime, tool

import retriever
from guards import mask_card
from settlement import check_trip

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

NO_DATA = "데이터 없음"
NO_PERMISSION = "권한 없음: 본인 출장 건만 조회할 수 있습니다."


@dataclass
class Context:
    """그래프 실행 시 주입되는 로그인 정보. 모델이 바꿀 수 없다."""
    employee_id: str


def _load(name: str):
    """data/ 폴더의 JSON 파일을 읽는다."""
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def _dump(obj) -> str:
    """도구 결과를 모델이 읽을 JSON 문자열로 바꾼다."""
    return json.dumps(obj, ensure_ascii=False)


def _base_date() -> date:
    """점검 기준일. .env 의 BASE_DATE 가 있으면 그 값, 없으면 오늘."""
    value = os.getenv("BASE_DATE")
    return date.fromisoformat(value) if value else date.today()


def get_employee(employee_id: str) -> dict | None:
    """직원ID로 직원 정보를 찾는다. 없으면 None."""
    return next((e for e in _load("employees.json") if e["employee_id"] == employee_id), None)


def _find_trip(trip_id: str, employee_id: str) -> tuple[dict | None, str | None]:
    """출장 건을 찾고 본인 소유인지 확인한다. (출장, 오류 메시지) 를 돌려준다."""
    trip = next((t for t in _load("trips.json") if t["trip_id"] == trip_id), None)
    if trip is None:
        return None, f"{NO_DATA}: {trip_id} 출장 건이 없습니다."
    if trip["employee_id"] != employee_id:
        return None, NO_PERMISSION
    return trip, None


@tool
def get_my_trips(runtime: ToolRuntime[Context]) -> str:
    """로그인한 직원 본인의 출장 건 목록을 조회한다.

    사용자가 출장ID를 말하지 않았거나 출장지만 말했을 때, 또는 없는 출장ID를 말했을 때 먼저 호출해 출장ID를 확인한다.
    반환: 출장ID, 출장지, 출발일, 복귀일, 정산 상태(미정산/제출완료) 목록
    """
    employee_id = runtime.context.employee_id
    trips = [t for t in _load("trips.json") if t["employee_id"] == employee_id]
    if not trips:
        return f"{NO_DATA}: 등록된 출장 건이 없습니다."
    return _dump([{k: t[k] for k in ("trip_id", "destination", "start_date", "return_date", "status")} for t in trips])


@tool
def get_trip_expenses(trip_id: str, runtime: ToolRuntime[Context]) -> str:
    """본인 출장 건 하나의 법인카드 결제 내역을 조회한다.

    입력: trip_id (예: TRIP-001)
    반환: 결제ID, 결제일, 가맹점, 분류(식비/숙박/교통), 금액, 숙박 박수, 정산 대상 선택 여부, 영수증 첨부 여부, 카드번호(끝 4자리)
    한도·누락·기한 판정은 하지 않는다. 판정이 필요하면 check_settlement 를 쓴다.
    """
    trip, error = _find_trip(trip_id, runtime.context.employee_id)
    if error:
        return error
    expenses = [e for e in _load("expenses.json") if e["trip_id"] == trip["trip_id"]]
    if not expenses:
        return f"{NO_DATA}: {trip_id} 출장 건에 결제 내역이 없습니다."
    return _dump([
        {**{k: e[k] for k in ("expense_id", "date", "merchant", "category", "nights", "selected", "receipt_attached")},
         "amount": f"{e['amount']:,}원", "card_no": mask_card(e["card_no"])}
        for e in expenses
    ])


@tool(response_format="content_and_artifact")
def search_policy(query: str) -> tuple[str, list[dict]]:
    """출장 규정 문서에서 질의와 관련된 조항을 검색한다 (RAG).

    입력: query (예: "정산 기한", "식비 한도 초과 시 처리")
    반환: 관련 조항 제목(doc_id)과 본문. 규정 판단과 근거 인용은 반드시 이 결과만 사용한다.
    """
    hits = retriever.search(query)
    if not hits:
        return f"{NO_DATA}: 관련 규정을 찾지 못했습니다.", []
    return _dump(hits), hits


def policy_topics(result: dict) -> list[str]:
    """점검 결과에 따라 답변 근거로 필요한 규정 조항 제목을 고른다."""
    topics = ["경비 한도", "정산 기한"]  # 점검 답변에는 항상 한도·기한 줄이 들어간다
    if result["missing_receipts"]:
        topics.append("증빙")
    if result["over_limits"] or result["deadline"]["passed"]:
        topics.append("예외 결재")
    return topics


@tool(response_format="content_and_artifact")
def check_settlement(trip_id: str, runtime: ToolRuntime[Context]) -> tuple[str, list[dict]]:
    """본인 출장 건 하나를 정산 규정과 대조해 사전 점검한다.

    입력: trip_id (예: TRIP-001)
    반환: 직급별 한도, 정산 대상 결제 항목, 영수증 누락 결제ID, 한도 초과(날짜·분류·초과 금액·예외 결재 상태),
    정산 기한(마감일·경과 여부·예외 결재 상태), 조치 필요 건수, 제출 가능 여부,
    policy_refs(이 점검 결과의 근거 규정 조항. 근거 인용은 policy_refs 를 쓴다).
    금액과 날짜는 이 결과를 그대로 쓰고 다시 계산하지 않는다.
    """
    employee_id = runtime.context.employee_id
    trip, error = _find_trip(trip_id, employee_id)
    if error:
        return error, []
    expenses = [e for e in _load("expenses.json") if e["trip_id"] == trip["trip_id"]]
    if not expenses:
        return f"{NO_DATA}: {trip_id} 출장 건에 결제 내역이 없습니다.", []
    approvals = [a for a in _load("approvals.json") if a["trip_id"] == trip["trip_id"]]
    holidays = {date.fromisoformat(h["date"]) for h in _load("holidays.json")["holidays"]}
    grade = get_employee(employee_id)["grade"]
    result = check_trip(trip, expenses, grade, approvals, holidays, _base_date())
    for over in result["over_limits"]:
        over.pop("over_value")  # 모델에는 표기용 금액 문자열만 넘긴다
    # 근거 조항은 모델이 검색할지 고르지 않고 점검 결과로 정해 붙인다
    refs = retriever.get_sections(policy_topics(result))
    result["policy_refs"] = refs
    return _dump(result), refs


TOOLS = [get_my_trips, get_trip_expenses, search_policy, check_settlement]
