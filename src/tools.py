"""tools.py - 출장 정산 도메인 도구 (로그인 직원 본인의 데이터만 다룬다)"""
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from langchain.tools import ToolRuntime, tool
from langgraph.types import interrupt

import retriever
from drafting import compose_request, polish_reason
from guards import mask_card
from settlement import STATUS_NEEDED, check_trip

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

NO_DATA = "데이터 없음"
NO_PERMISSION = "권한 없음: 본인 출장 건만 조회할 수 있습니다."
NOT_SUBMITTED = "제출하지 않음"
SUBMITTED = "제출완료"
NOT_REQUESTED = "결재요청안함"
REQUESTED = "결재요청완료"

# 직원 답변의 첫 단어가 이 중 하나면 승인으로 본다. 그 밖(애매한 답 포함)은 모두 거절로 처리한다.
APPROVAL_WORDS = {"승인", "승인합니다", "승인해", "승인해줘", "승인해주세요",
                  "네", "예", "제출", "제출해", "제출해줘", "제출해주세요", "제출합니다"}


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


# ---------------------------------------------------------------- 제출 상태 (원본 데이터는 바꾸지 않는다)

def _submissions_path() -> Path:
    """제출 기록 파일. STATE_DIR 이 있으면 그 폴더(평가는 임시 폴더), 없으면 state/."""
    return Path(os.getenv("STATE_DIR") or ROOT / "state") / "submissions.json"


def load_submissions() -> dict:
    """제출 기록 {출장ID: {employee_id, submitted_at}} 을 읽는다."""
    path = _submissions_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_submission(trip_id: str, employee_id: str):
    """출장 건을 제출완료로 기록한다."""
    records = load_submissions()
    records[trip_id] = {"employee_id": employee_id, "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    path = _submissions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")


def _state_file(name: str) -> Path:
    """상태 폴더 안의 파일 경로."""
    return _submissions_path().parent / name


def _read_state(name: str, default):
    """상태 파일을 읽는다. 없으면 default."""
    path = _state_file(name)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write_state(name: str, value):
    """상태 파일을 쓴다."""
    path = _state_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


def load_approvals(trip_id: str) -> list[dict]:
    """출장 건의 예외 결재 = 원본 데이터(data/approvals.json) + 이 Agent 로 요청한 결재(상태 파일)."""
    records = _load("approvals.json") + _read_state("approvals.json", [])
    return [a for a in records if a["trip_id"] == trip_id]


def save_approval_request(trip_id: str, kind: str, expense_ids: list[str], reason: str, approver_id: str) -> str:
    """예외 결재 요청을 '요청됨' 상태로 기록하고 새 결재ID 를 돌려준다."""
    requested = _read_state("approvals.json", [])
    numbers = [int(a["approval_id"].split("-")[1]) for a in _load("approvals.json") + requested]
    approval_id = f"APR-{max(numbers, default=0) + 1:03d}"
    requested.append({"approval_id": approval_id, "trip_id": trip_id, "type": kind, "expense_ids": expense_ids,
                      "reason": reason, "status": "요청됨", "approver_id": approver_id,
                      "requested_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    _write_state("approvals.json", requested)
    return approval_id


def _with_status(trip: dict) -> dict:
    """원본 출장 데이터에 제출 기록을 덮어씌운 사본을 돌려준다."""
    return {**trip, "status": SUBMITTED} if trip["trip_id"] in load_submissions() else trip


def is_approval(answer: str) -> bool:
    """직원 답변이 승인인지 판정한다. 첫 단어만 보고, 애매하면 거절로 본다."""
    words = re.split(r"[\s,.!~]+", str(answer).strip())
    return bool(words) and words[0] in APPROVAL_WORDS


def _find_trip(trip_id: str, employee_id: str) -> tuple[dict | None, str | None]:
    """출장 건을 찾고 본인 소유인지 확인한다. (출장, 오류 메시지) 를 돌려준다."""
    trip = next((t for t in _load("trips.json") if t["trip_id"] == trip_id), None)
    if trip is None:
        return None, f"{NO_DATA}: {trip_id} 출장 건이 없습니다."
    if trip["employee_id"] != employee_id:
        return None, NO_PERMISSION
    return _with_status(trip), None


@tool
def get_my_trips(runtime: ToolRuntime[Context]) -> str:
    """로그인한 직원 본인의 출장 건 목록을 조회한다.

    사용자가 출장ID를 말하지 않았거나 출장지만 말했을 때, 또는 없는 출장ID를 말했을 때 먼저 호출해 출장ID를 확인한다.
    반환: 출장ID, 출장지, 출발일, 복귀일, 정산 상태(미정산/제출완료) 목록
    """
    employee_id = runtime.context.employee_id
    trips = [_with_status(t) for t in _load("trips.json") if t["employee_id"] == employee_id]
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
    result, error = _run_check(trip, employee_id)
    if error:
        return error, []
    # 근거 조항은 모델이 검색할지 고르지 않고 점검 결과로 정해 붙인다
    refs = retriever.get_sections(policy_topics(result))
    result["policy_refs"] = refs
    return _dump(result), refs


def _run_check(trip: dict, employee_id: str) -> tuple[dict | None, str | None]:
    """출장 1건을 점검한다. (점검 결과, 오류 메시지) 를 돌려준다."""
    expenses = [e for e in _load("expenses.json") if e["trip_id"] == trip["trip_id"]]
    if not expenses:
        return None, f"{NO_DATA}: {trip['trip_id']} 출장 건에 결제 내역이 없습니다."
    approvals = load_approvals(trip["trip_id"])
    holidays = {date.fromisoformat(h["date"]) for h in _load("holidays.json")["holidays"]}
    grade = get_employee(employee_id)["grade"]
    result = check_trip(trip, expenses, grade, approvals, holidays, _base_date())
    result["total_amount"] = f"{sum(e['amount'] for e in expenses if e['selected']):,}원"
    for over in result["over_limits"]:
        over.pop("over_value")  # 모델에는 표기용 금액 문자열만 넘긴다
    return result, None


@tool
def get_approval_status(trip_id: str, runtime: ToolRuntime[Context]) -> str:
    """본인 출장 건 하나의 예외 결재(한도초과·기한초과) 상태를 조회한다.

    입력: trip_id (예: TRIP-004)
    반환: 결재ID, 유형(한도초과/기한초과), 대상 결제ID(한도초과만), 상태(요청됨/승인/반려), 결재자 이름
    """
    trip, error = _find_trip(trip_id, runtime.context.employee_id)
    if error:
        return error
    approvals = load_approvals(trip["trip_id"])
    if not approvals:
        return f"{NO_DATA}: {trip_id} 출장 건에 예외 결재 내역이 없습니다."
    return _dump([
        {"approval_id": a["approval_id"], "type": a["type"], "expense_ids": a["expense_ids"],
         "status": a["status"], "approver": (get_employee(a["approver_id"]) or {}).get("name", a["approver_id"])}
        for a in approvals
    ])


def _confirm_message(trip: dict, result: dict) -> str:
    """제출 직전에 직원에게 보여 줄 확인 문구를 만든다."""
    approved = sum(1 for o in result["over_limits"]) + (1 if result["deadline"]["passed"] else 0)
    lines = [
        "정산서 제출 전 확인",
        f"- 출장: {trip['trip_id']} {trip['destination']} ({result['period']})",
        f"- 정산 대상 {len(result['items'])}건, 합계 {result['total_amount']}",
        f"- 조치 필요 0건 (예외 승인 완료 {approved}건 포함)" if approved else "- 조치 필요 0건",
        "",
        "이대로 정산서를 제출할까요? '승인' 또는 '거절'로 답해 주세요.",
    ]
    return "\n".join(lines)


@tool
def submit_settlement(trip_id: str, runtime: ToolRuntime[Context]) -> str:
    """본인 출장 건의 정산서를 최종 제출한다. 직원이 승인해야만 제출된다 (사람 승인 필요).

    입력: trip_id (예: TRIP-001)
    제출 직전에 다시 점검해, 영수증 누락이나 예외 승인 전 초과가 남아 있으면 제출하지 않고 남은 조치를 돌려준다.
    문제가 없으면 시스템이 직원에게 제출 확인을 받는다. 반환이 '제출완료'일 때만 제출된 것이다.
    """
    employee_id = runtime.context.employee_id
    trip, error = _find_trip(trip_id, employee_id)
    if error:
        return error
    if trip["status"] == SUBMITTED:
        return f"{NOT_SUBMITTED}: {trip_id} 출장 건은 이미 제출완료 상태입니다. 다시 제출하지 않습니다."
    result, error = _run_check(trip, employee_id)
    if error:
        return f"{NOT_SUBMITTED}: {error}"
    if not result["submittable"]:
        return f"{NOT_SUBMITTED}: 조치가 필요한 항목이 남아 제출하지 않았습니다. " + _dump({
            "action_needed_count": result["action_needed_count"],
            "missing_receipts": result["missing_receipts"],
            "over_limits": [{k: o[k] for k in ("date", "category", "over_amount", "status")} for o in result["over_limits"]],
            "deadline": result["deadline"],
        })

    # 사람 승인 (HITL): 여기서 그래프가 멈추고, 같은 대화로 직원 답변이 오면 이어서 실행된다
    answer = interrupt({"type": "submit_confirm", "trip_id": trip_id, "employee_id": employee_id,
                        "message": _confirm_message(trip, result)})
    if not is_approval(answer):
        return f"{NOT_SUBMITTED}: 직원이 승인하지 않아 {trip_id} 정산서를 제출하지 않았습니다."
    save_submission(trip_id, employee_id)
    return f"{SUBMITTED}: {trip_id} 정산서를 제출했습니다 (합계 {result['total_amount']})."


def select_exception_targets(result: dict, kind: str, expense_ids: list[str] | None) -> tuple[list[dict], str | None]:
    """예외 결재를 요청할 수 있는 대상인지 점검 결과로 확인한다. (대상 초과 항목, 거절 사유) 를 돌려준다.

    한도초과: 실제로 한도를 넘었고 아직 결재가 없거나 반려된 항목만. 결제ID 를 안 주면 그런 항목 전부.
    기한초과: 실제로 기한이 지났고 아직 결재가 없거나 반려된 경우만.
    """
    if kind == "기한초과":
        deadline = result["deadline"]
        if not deadline["passed"]:
            return [], f"정산 기한({deadline['deadline']})이 지나지 않아 기한초과 결재가 필요 없습니다."
        if deadline["status"] != STATUS_NEEDED:
            return [], f"기한초과 결재가 이미 '{deadline['status']}' 상태입니다."
        return [], None
    if kind != "한도초과":
        return [], "결재 유형은 한도초과 또는 기한초과입니다."
    overs = result["over_limits"]
    if not overs:
        return [], "한도 초과 항목이 없어 한도초과 결재가 필요 없습니다."
    if expense_ids:
        over_ids = {i for o in overs for i in o["expense_ids"]}
        unknown = [i for i in expense_ids if i not in over_ids]
        if unknown:
            return [], f"한도 초과가 아닌 결제입니다: {', '.join(unknown)}"
        chosen = [o for o in overs if set(o["expense_ids"]) & set(expense_ids)]
    else:
        chosen = [o for o in overs if o["status"] == STATUS_NEEDED]
    done = [o for o in chosen if o["status"] != STATUS_NEEDED]
    if done:
        return [], "이미 결재가 진행 중이거나 승인된 항목입니다: " + ", ".join(
            f"{o['date']} {o['category']}({o['status']})" for o in done)
    if not chosen:
        return [], "결재가 필요한 한도 초과 항목이 없습니다 (이미 요청됐거나 승인됨)."
    return chosen, None


@tool
def request_exception_approval(trip_id: str, kind: Literal["한도초과", "기한초과"], reason: str,
                               runtime: ToolRuntime[Context], expense_ids: list[str] | None = None) -> str:
    """한도 초과나 기한 초과 건의 사유서 초안을 만들어 부서장에게 예외 결재를 요청한다 (사람 승인 필요).

    입력: trip_id, kind(한도초과/기한초과), reason(직원이 직접 말한 사유), expense_ids(한도초과 대상 결제ID, 생략하면 결재가 필요한 초과 항목 전부)
    직원이 사유를 말하지 않았으면 호출하지 말고 먼저 사유를 묻는다.
    시스템이 직원에게 사유서 초안을 보여 주고 확인을 받는다. 반환이 '결재요청완료' 일 때만 요청된 것이다.
    """
    employee_id = runtime.context.employee_id
    trip, error = _find_trip(trip_id, employee_id)
    if error:
        return f"{NOT_REQUESTED}: {error}"
    if len((reason or "").strip()) < 2:
        return f"{NOT_REQUESTED}: 사유가 없습니다. 직원에게 예외 결재 사유를 먼저 물어보세요."
    result, error = _run_check(trip, employee_id)
    if error:
        return f"{NOT_REQUESTED}: {error}"
    targets, refusal = select_exception_targets(result, kind, expense_ids)
    if refusal:
        return f"{NOT_REQUESTED}: {refusal}"

    employee = get_employee(employee_id)
    approver = get_employee(employee["manager_id"]) or {"name": employee["manager_id"]}
    target_ids = sorted({i for t in targets for i in t["expense_ids"]})
    target_text = ", ".join(f"{t['date']} {t['category']} 초과" for t in targets) or "정산 기한 경과"

    # 재개 시 도구가 처음부터 다시 실행되므로, 직원에게 보여 준 사유 문단을 보관해 같은 문구로 저장한다
    key = "|".join([employee_id, trip_id, kind, ",".join(target_ids), reason.strip()])
    drafts = _read_state("drafts.json", {})
    if key not in drafts:
        drafts[key] = polish_reason(kind, target_text, reason.strip())
        _write_state("drafts.json", drafts)
    reason_text = drafts[key]

    # 사람 승인 (HITL): 사유서 초안을 보여 주고 직원 답변을 기다린다
    answer = interrupt({"type": "exception_request_confirm", "trip_id": trip_id, "employee_id": employee_id,
                        "message": compose_request(trip, kind, targets, result["deadline"], approver["name"], reason_text)})
    drafts = _read_state("drafts.json", {})
    drafts.pop(key, None)
    _write_state("drafts.json", drafts)
    if not is_approval(answer):
        return f"{NOT_REQUESTED}: 직원이 승인하지 않아 예외 결재를 요청하지 않았습니다."
    approval_id = save_approval_request(trip_id, kind, target_ids, reason_text, employee["manager_id"])
    return f"{REQUESTED}: {approval_id} {kind} 예외 결재를 부서장 {approver['name']}에게 요청했습니다 (상태: 요청됨)."


TOOLS = [get_my_trips, get_trip_expenses, search_policy, check_settlement, get_approval_status, submit_settlement,
         request_exception_approval]
