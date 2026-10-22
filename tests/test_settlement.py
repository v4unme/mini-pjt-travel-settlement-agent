"""test_settlement.py - 정산 점검 계산(settlement.py) 검증. LLM 호출 없이 숫자만 맞춘다.

실행: pytest tests
"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from settlement import (  # noqa: E402
    STATUS_APPROVED, STATUS_NEEDED, STATUS_PENDING, check_trip, settlement_deadline,
)

BASE = date(2026, 10, 21)  # 평가 기준일 (CLAUDE.md 평가 규약)


def load(name):
    """data/ 의 더미 데이터를 읽는다."""
    return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))


HOLIDAYS = {date.fromisoformat(h["date"]) for h in load("holidays.json")["holidays"]}


def run_data_trip(trip_id, base=BASE):
    """data/ 의 실제 더미 데이터로 출장 1건을 점검한다."""
    trip = next(t for t in load("trips.json") if t["trip_id"] == trip_id)
    grade = next(e for e in load("employees.json") if e["employee_id"] == trip["employee_id"])["grade"]
    expenses = [e for e in load("expenses.json") if e["trip_id"] == trip_id]
    approvals = [a for a in load("approvals.json") if a["trip_id"] == trip_id]
    return check_trip(trip, expenses, grade, approvals, HOLIDAYS, base)


def make_trip(return_date="2026-10-14", status="미정산"):
    """경계 조건 시험용 출장 건을 만든다."""
    return {"trip_id": "TRIP-T", "destination": "시험", "start_date": "2026-10-12",
            "return_date": return_date, "status": status}


def make_expense(expense_id, day, category, amount, nights=None, selected=True, receipt=True):
    """경계 조건 시험용 결제 1건을 만든다."""
    return {"expense_id": expense_id, "trip_id": "TRIP-T", "date": day, "merchant": "시험",
            "category": category, "amount": amount, "nights": nights,
            "selected": selected, "receipt_attached": receipt}


def run_synthetic(expenses, grade="평직원", approvals=(), return_date="2026-10-14", base=date(2026, 10, 15)):
    """시험용 데이터로 점검한다. 기본값은 기한 내 상태."""
    return check_trip(make_trip(return_date), expenses, grade, list(approvals), HOLIDAYS, base)


# ---------------------------------------------------------------- 평가 문항 기대값

def test_case1_clean_trip():
    """#1 TRIP-001: 문제 없음, 마감 10-27."""
    r = run_data_trip("TRIP-001")
    assert r["missing_receipts"] == [] and r["over_limits"] == []
    assert r["deadline"]["deadline"] == "2026-10-27"
    assert r["action_needed_count"] == 0 and r["submittable"]


def test_case2_missing_and_food_over():
    """#2 TRIP-002: 누락 EXP-007, 10-19 식비 초과 13,000원, 미선택 EXP-008 제외, 조치 2건."""
    r = run_data_trip("TRIP-002")
    assert r["missing_receipts"] == ["EXP-007"]
    assert r["excluded_not_selected"] == ["EXP-008"]
    [over] = r["over_limits"]
    assert (over["date"], over["category"], over["over_amount"]) == ("2026-10-19", "식비", "13,000원")
    assert over["expense_ids"] == ["EXP-004", "EXP-005"] and over["status"] == STATUS_NEEDED
    assert r["action_needed_count"] == 2 and not r["submittable"]


def test_case3_and_11_transport_and_lodging_over():
    """#3 교통비 초과 13,800원, #11 숙박 2박 340,000원 → 1박 170,000원, 초과 40,000원."""
    overs = {o["category"]: o for o in run_data_trip("TRIP-003")["over_limits"]}
    assert set(overs) == {"교통", "숙박"}
    assert (overs["교통"]["date"], overs["교통"]["over_amount"]) == ("2026-10-12", "13,800원")
    assert overs["숙박"]["per_night"] == "170,000원" and overs["숙박"]["nights"] == 2
    assert overs["숙박"]["over_amount"] == "40,000원"


def test_case4_executive_limits():
    """#4 TRIP-007 임원: 식비 90,000원·숙박 180,000원 모두 한도 내."""
    r = run_data_trip("TRIP-007")
    assert r["limits"]["식비"] == "100,000원" and r["over_limits"] == [] and r["submittable"]


def test_case12_deadline_passed_with_approval():
    """#12 TRIP-004: 마감 10-20(한글날 제외) 경과, APR-001 기한초과 승인 → 제출 가능."""
    d = run_data_trip("TRIP-004")["deadline"]
    assert d["deadline"] == "2026-10-20" and d["passed"] and d["elapsed_business_days"] == 1
    assert d["status"] == STATUS_APPROVED and d["approval_ids"] == ["APR-001"]
    assert run_data_trip("TRIP-004")["submittable"]


def test_submitted_trip_is_not_submittable():
    """TRIP-005: 이미 제출완료면 문제가 없어도 다시 제출할 수 없다."""
    r = run_data_trip("TRIP-005")
    assert r["settlement_status"] == "제출완료" and not r["submittable"]


# ---------------------------------------------------------------- 숙박 1박 단가

@pytest.mark.parametrize("amount, nights, expected_over", [
    (340000, 2, "40,000원"),   # 나눠떨어지는 경우
    (335001, 2, "35,001원"),   # 나눠떨어지지 않아도 원 단위 오차 없이
    (300001, 2, "1원"),        # 1박 단가 내림 시 놓치는 1원 초과
    (150001, 1, "1원"),
])
def test_lodging_over_amount_is_exact(amount, nights, expected_over):
    """숙박 초과 금액 = 결제 금액 − 1박 한도 × 박수 (오차 0원)."""
    [over] = run_synthetic([make_expense("E1", "2026-10-14", "숙박", amount, nights)])["over_limits"]
    assert over["over_amount"] == expected_over and over["nights"] == nights


@pytest.mark.parametrize("amount, nights", [(300000, 2), (150000, 1), (149999, 1)])
def test_lodging_at_or_under_limit_is_not_over(amount, nights):
    """1박 금액이 한도와 같거나 낮으면 초과가 아니다."""
    assert run_synthetic([make_expense("E1", "2026-10-14", "숙박", amount, nights)])["over_limits"] == []


def test_lodging_executive_limit():
    """임원 숙박 1박 한도는 200,000원이다."""
    r = run_synthetic([make_expense("E1", "2026-10-14", "숙박", 420000, 2)], grade="임원")
    assert r["over_limits"][0]["over_amount"] == "20,000원"


# ---------------------------------------------------------------- 식비·교통비 1일 합계

def test_daily_limit_boundary():
    """같은 날 합계가 한도와 같으면 정상, 1원 넘으면 초과."""
    assert run_synthetic([make_expense("E1", "2026-10-13", "식비", 70000)])["over_limits"] == []
    [over] = run_synthetic([make_expense("E1", "2026-10-13", "식비", 30000),
                            make_expense("E2", "2026-10-13", "식비", 40001)])["over_limits"]
    assert over["over_amount"] == "1원" and over["expense_ids"] == ["E1", "E2"]


def test_daily_sum_is_per_date_and_category():
    """다른 날짜·다른 분류는 합산하지 않는다."""
    r = run_synthetic([make_expense("E1", "2026-10-12", "식비", 60000),
                       make_expense("E2", "2026-10-13", "식비", 60000),
                       make_expense("E3", "2026-10-12", "교통", 60000)])
    assert r["over_limits"] == []


def test_unselected_expense_is_excluded():
    """정산 대상으로 선택하지 않은 결제는 한도 합계와 누락 판정에서 빠진다."""
    r = run_synthetic([make_expense("E1", "2026-10-13", "식비", 60000),
                       make_expense("E2", "2026-10-13", "식비", 50000, selected=False, receipt=False)])
    assert r["over_limits"] == [] and r["missing_receipts"] == [] and r["excluded_not_selected"] == ["E2"]


# ---------------------------------------------------------------- 7영업일 기한

@pytest.mark.parametrize("return_date, expected", [
    ("2026-10-16", "2026-10-27"),  # 금요일 복귀 → 월요일이 1일째
    ("2026-10-17", "2026-10-27"),  # 토요일 복귀도 같은 마감
    ("2026-10-20", "2026-10-29"),  # 화요일 복귀
    ("2026-10-08", "2026-10-20"),  # 10-09 한글날 제외
    ("2026-10-02", "2026-10-15"),  # 10-05 개천절 대체공휴일 + 10-09 한글날 제외
])
def test_deadline_with_holidays(return_date, expected):
    """복귀일 다음 영업일부터 7영업일째가 마감이다 (주말·공휴일 제외)."""
    assert settlement_deadline(date.fromisoformat(return_date), HOLIDAYS).isoformat() == expected


def test_holiday_changes_deadline():
    """공휴일 목록이 실제로 반영되는지: 한글날을 빼지 않으면 마감이 하루 당겨진다."""
    assert settlement_deadline(date(2026, 10, 8), set()) == date(2026, 10, 19)
    assert settlement_deadline(date(2026, 10, 8), HOLIDAYS) == date(2026, 10, 20)


def test_deadline_day_itself_is_within_deadline():
    """기준일이 마감일 당일이면 기한 내, 다음 영업일이면 경과다."""
    on_day = run_synthetic([make_expense("E1", "2026-10-16", "식비", 10000)],
                           return_date="2026-10-16", base=date(2026, 10, 27))["deadline"]
    assert not on_day["passed"] and on_day["remaining_business_days"] == 0
    after = run_synthetic([make_expense("E1", "2026-10-16", "식비", 10000)],
                          return_date="2026-10-16", base=date(2026, 10, 28))["deadline"]
    assert after["passed"] and after["elapsed_business_days"] == 1 and after["status"] == STATUS_NEEDED


def test_remaining_business_days():
    """TRIP-002(마감 10-29) 기준일 10-21 → 22·23·26·27·28·29 6영업일 남음."""
    assert run_data_trip("TRIP-002")["deadline"]["remaining_business_days"] == 6


# ---------------------------------------------------------------- 예외 결재

def approval(kind, status, expense_ids=(), approval_id="APR-T"):
    """시험용 예외 결재 1건을 만든다."""
    return {"approval_id": approval_id, "trip_id": "TRIP-T", "type": kind,
            "expense_ids": list(expense_ids), "status": status, "approver_id": "EMP-010"}


@pytest.mark.parametrize("status, expected_label, actions", [
    ("승인", STATUS_APPROVED, 0),
    ("요청됨", STATUS_PENDING, 1),
    ("반려", STATUS_NEEDED, 1),
])
def test_limit_over_approval_status(status, expected_label, actions):
    """한도초과 결재 상태별 판정 문구와 조치 건수."""
    r = run_synthetic([make_expense("E1", "2026-10-13", "식비", 80000)],
                      approvals=[approval("한도초과", status, ["E1"])])
    assert r["over_limits"][0]["status"] == expected_label
    assert r["action_needed_count"] == actions and r["submittable"] == (actions == 0)


def test_approval_for_other_expense_does_not_apply():
    """다른 결제를 대상으로 한 한도초과 결재는 적용되지 않는다."""
    r = run_synthetic([make_expense("E1", "2026-10-13", "식비", 80000)],
                      approvals=[approval("한도초과", "승인", ["E9"])])
    assert r["over_limits"][0]["status"] == STATUS_NEEDED


def test_approval_types_do_not_cross():
    """기한초과 승인은 한도 초과를, 한도초과 승인은 기한 초과를 해결하지 않는다."""
    expenses = [make_expense("E1", "2026-10-16", "식비", 80000)]
    late = dict(return_date="2026-10-16", base=date(2026, 10, 28))
    r = run_synthetic(expenses, approvals=[approval("기한초과", "승인")], **late)
    assert r["deadline"]["status"] == STATUS_APPROVED and r["over_limits"][0]["status"] == STATUS_NEEDED
    r = run_synthetic(expenses, approvals=[approval("한도초과", "승인", ["E1"])], **late)
    assert r["over_limits"][0]["status"] == STATUS_APPROVED and r["deadline"]["status"] == STATUS_NEEDED


def test_missing_receipt_blocks_submission_even_if_approved():
    """한도초과가 승인돼도 영수증 누락이 있으면 제출할 수 없다."""
    r = run_synthetic([make_expense("E1", "2026-10-13", "식비", 80000, receipt=False)],
                      approvals=[approval("한도초과", "승인", ["E1"])])
    assert r["missing_receipts"] == ["E1"] and r["action_needed_count"] == 1 and not r["submittable"]


# ---------------------------------------------------------------- 점검 근거 조항 선택 (tools.policy_topics)

def test_policy_topics_by_result():
    """점검 결과에 따라 근거 조항이 정해진다: 기본 한도·기한, 누락이면 증빙, 초과·기한 경과면 예외 결재."""
    from tools import policy_topics

    assert policy_topics(run_data_trip("TRIP-001")) == ["경비 한도", "정산 기한"]
    assert policy_topics(run_data_trip("TRIP-002")) == ["경비 한도", "정산 기한", "증빙", "예외 결재"]
    assert policy_topics(run_data_trip("TRIP-003")) == ["경비 한도", "정산 기한", "예외 결재"]
    assert policy_topics(run_data_trip("TRIP-004")) == ["경비 한도", "정산 기한", "예외 결재"]


def test_get_sections_returns_policy_text():
    """조항 제목으로 규정 본문을 순서대로 꺼낸다 (임베딩 호출 없음). 없는 제목은 건너뛴다."""
    from retriever import get_sections

    refs = get_sections(["예외 결재", "없는 조항", "증빙"])
    assert [r["doc_id"] for r in refs] == ["예외 결재", "증빙"]
    assert "개인 부담으로 처리하지 않습니다" in refs[0]["text"]
