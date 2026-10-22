"""test_exception_request.py - 예외 결재 요청 대상 검증·기록·사유서 숫자 검증. LLM 호출 없음.

실행: pytest tests
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import tools  # noqa: E402
from drafting import compose_request  # noqa: E402
from settlement import STATUS_PENDING  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """테스트마다 빈 상태 폴더를 쓴다."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_DATE", "2026-10-21")


def check(trip_id, employee_id):
    """현재 상태 기준으로 출장 1건을 점검한다."""
    trip, _ = tools._find_trip(trip_id, employee_id)
    result, _ = tools._run_check(trip, employee_id)
    return trip, result


# ---------------------------------------------------------------- 대상 검증

def test_limit_over_targets_all_needed_when_ids_omitted():
    """결제ID 를 안 주면 결재가 필요한 초과 항목 전부가 대상 (TRIP-003: 교통·숙박)."""
    _, result = check("TRIP-003", "EMP-005")
    targets, refusal = tools.select_exception_targets(result, "한도초과", None)
    assert refusal is None and {t["category"] for t in targets} == {"교통", "숙박"}


def test_limit_over_targets_by_expense_id():
    """결제ID 를 주면 그 결제가 속한 초과 항목만 대상 (EXP-013 → 숙박)."""
    _, result = check("TRIP-003", "EMP-005")
    targets, refusal = tools.select_exception_targets(result, "한도초과", ["EXP-013"])
    assert refusal is None and [t["category"] for t in targets] == ["숙박"]


@pytest.mark.parametrize("trip_id, employee_id, kind, ids, expected", [
    ("TRIP-001", "EMP-001", "한도초과", None, "한도 초과 항목이 없어"),        # 초과 없음
    ("TRIP-002", "EMP-004", "한도초과", ["EXP-006"], "한도 초과가 아닌 결제"),  # 교통비는 한도 내
    ("TRIP-002", "EMP-004", "기한초과", None, "지나지 않아"),                   # 기한 내
    ("TRIP-004", "EMP-006", "기한초과", None, "이미"),                          # APR-001 로 이미 승인
])
def test_refused_requests(trip_id, employee_id, kind, ids, expected):
    """초과가 아니거나 이미 결재된 건은 요청할 수 없다."""
    _, result = check(trip_id, employee_id)
    targets, refusal = tools.select_exception_targets(result, kind, ids)
    assert targets == [] and expected in refusal


# ---------------------------------------------------------------- 기록과 반영

def test_request_is_recorded_and_check_shows_pending():
    """요청을 기록하면 새 결재ID(APR-002)가 생기고, 재점검 시 해당 초과가 '결재 대기' 가 된다. 원본 데이터는 그대로."""
    approval_id = tools.save_approval_request("TRIP-002", "한도초과", ["EXP-004", "EXP-005"], "고객사 만찬", "EMP-010")
    assert approval_id == "APR-002"
    _, result = check("TRIP-002", "EMP-004")
    assert result["over_limits"][0]["status"] == STATUS_PENDING
    assert result["over_limits"][0]["approval_ids"] == ["APR-002"]
    assert len(tools._load("approvals.json")) == 1  # data/approvals.json 은 APR-001 하나 그대로


def test_pending_request_cannot_be_requested_again():
    """이미 요청된(결재 대기) 항목은 다시 요청할 수 없다."""
    tools.save_approval_request("TRIP-002", "한도초과", ["EXP-004", "EXP-005"], "고객사 만찬", "EMP-010")
    _, result = check("TRIP-002", "EMP-004")
    targets, refusal = tools.select_exception_targets(result, "한도초과", ["EXP-005"])
    assert targets == [] and "결재 대기" in refusal


def test_approval_ids_increase():
    """결재ID 는 기존 최대 번호 다음으로 붙는다."""
    first = tools.save_approval_request("TRIP-002", "한도초과", ["EXP-004"], "사유", "EMP-010")
    second = tools.save_approval_request("TRIP-003", "한도초과", ["EXP-013"], "사유", "EMP-010")
    assert (first, second) == ("APR-002", "APR-003")


# ---------------------------------------------------------------- 사유서 초안 숫자

def test_compose_request_uses_check_numbers():
    """사유서 초안의 대상·초과 금액·결제ID·결재자는 점검 결과 값 그대로다."""
    trip, result = check("TRIP-002", "EMP-004")
    targets, _ = tools.select_exception_targets(result, "한도초과", None)
    text = compose_request(trip, "한도초과", targets, result["deadline"], "강태호", "고객사 만찬 일정으로 초과했습니다.")
    assert "2026-10-19 식비 초과 13,000원" in text and "EXP-004, EXP-005" in text
    assert "부서장 강태호" in text and "'승인' 또는 '거절'" in text
