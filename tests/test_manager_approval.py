"""test_manager_approval.py - 시연용 부서장 결재(결재함·승인·반려) 검증. LLM 호출 없음.

실행: pytest tests
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import tools  # noqa: E402
from settlement import STATUS_APPROVED, STATUS_NEEDED  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """테스트마다 빈 상태 폴더를 쓴다."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_DATE", "2026-10-21")


def request_trip3_lodging():
    """정하은 TRIP-003 숙박 초과(EXP-013)를 부서장 강태호(EMP-010)에게 요청해 둔다."""
    return tools.save_approval_request("TRIP-003", "한도초과", ["EXP-013"], "행사장 인근 숙소 부족", "EMP-010")


def check_trip3():
    """정하은 TRIP-003 을 현재 상태로 점검한다."""
    trip, _ = tools._find_trip("TRIP-003", "EMP-005")
    result, _ = tools._run_check(trip, "EMP-005")
    return result


def test_inbox_shows_request_with_amount():
    """결재함에는 결재자 본인에게 온 요청됨 건만, 초과 금액과 함께 나온다."""
    approval_id = request_trip3_lodging()
    [item] = tools.approver_inbox("EMP-010")
    assert item["approval_id"] == approval_id and item["requester"] == "정하은 (EMP-005)"
    assert item["targets"] == ["2026-10-14 숙박 초과 40,000원"]
    assert tools.approver_inbox("EMP-004") == []  # 결재자가 아니면 빈 결재함


def test_approve_makes_item_settleable():
    """부서장이 승인하면 재점검 시 해당 초과가 '예외 승인 완료' 가 되고 결재함에서 빠진다."""
    approval_id = request_trip3_lodging()
    record, error = tools.decide_approval(approval_id, "EMP-010", "승인")
    assert error is None and record["status"] == "승인"
    lodging = next(o for o in check_trip3()["over_limits"] if o["category"] == "숙박")
    assert lodging["status"] == STATUS_APPROVED
    assert tools.approver_inbox("EMP-010") == []


def test_reject_allows_new_request():
    """반려되면 '예외 결재 필요' 로 돌아가 다시 요청할 수 있다."""
    approval_id = request_trip3_lodging()
    tools.decide_approval(approval_id, "EMP-010", "반려")
    lodging = next(o for o in check_trip3()["over_limits"] if o["category"] == "숙박")
    assert lodging["status"] == STATUS_NEEDED
    targets, refusal = tools.select_exception_targets(check_trip3(), "한도초과", ["EXP-013"])
    assert refusal is None and targets


@pytest.mark.parametrize("approver, decision, expected", [
    ("EMP-004", "승인", "결재자만"),      # 결재자가 아닌 사람
    ("EMP-010", "보류", "'승인' 또는 '반려'"),  # 없는 결정
])
def test_invalid_decisions(approver, decision, expected):
    """결재자 본인만, 승인·반려만 할 수 있다."""
    approval_id = request_trip3_lodging()
    record, error = tools.decide_approval(approval_id, approver, decision)
    assert record is None and expected in error


def test_cannot_decide_twice_or_original_data():
    """이미 처리한 결재와 원본 데이터의 결재(APR-001)는 바꿀 수 없다."""
    approval_id = request_trip3_lodging()
    tools.decide_approval(approval_id, "EMP-010", "승인")
    assert "이미 '승인'" in tools.decide_approval(approval_id, "EMP-010", "반려")[1]
    assert "처리할 수 있는 결재 요청이 아닙니다" in tools.decide_approval("APR-001", "EMP-010", "반려")[1]


def test_full_flow_to_submittable():
    """TRIP-003: 교통·숙박 초과를 모두 요청 → 부서장 승인 → 조치 필요 0건, 제출 가능."""
    assert not check_trip3()["submittable"]
    approval_id = tools.save_approval_request("TRIP-003", "한도초과", ["EXP-011", "EXP-012", "EXP-013"],
                                              "현장 일정", "EMP-010")
    tools.decide_approval(approval_id, "EMP-010", "승인")
    result = check_trip3()
    assert result["action_needed_count"] == 0 and result["submittable"]
