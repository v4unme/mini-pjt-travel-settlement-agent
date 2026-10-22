"""test_receipt_attach.py - 시연용 영수증 첨부 기록과 점검 반영 검증. LLM 호출 없음.

실행: pytest tests
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import tools  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """테스트마다 빈 상태 폴더를 쓴다."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_DATE", "2026-10-21")


def check_trip2():
    """최민준 TRIP-002 를 현재 상태로 점검한다."""
    trip, _ = tools._find_trip("TRIP-002", "EMP-004")
    result, _ = tools._run_check(trip, "EMP-004")
    return result


def test_missing_list_shows_only_selected_unattached():
    """영수증 누락 목록에는 정산 대상이면서 미첨부인 결제만 (EXP-007). 미선택 EXP-008 은 제외."""
    assert [m["expense_id"] for m in tools.missing_receipts_for("EMP-004")] == ["EXP-007"]
    assert tools.missing_receipts_for("EMP-001") == []


def test_attach_clears_missing_and_keeps_data():
    """첨부하면 점검에서 누락이 사라지고, 원본 데이터(expenses.json)는 그대로다."""
    record, error = tools.attach_receipt("EXP-007", "EMP-004", "receipt.pdf", 1234)
    assert error is None and record["file_name"] == "receipt.pdf"
    assert check_trip2()["missing_receipts"] == []
    assert tools.missing_receipts_for("EMP-004") == []
    original = next(e for e in tools._load("expenses.json") if e["expense_id"] == "EXP-007")
    assert original["receipt_attached"] is False


@pytest.mark.parametrize("expense_id, employee_id, expected", [
    ("EXP-007", "EMP-008", "권한 없음"),               # 다른 직원의 결제
    ("EXP-008", "EMP-004", "정산 대상으로 선택한"),     # 정산 대상 아님
    ("EXP-001", "EMP-001", "이미 영수증이 첨부"),       # 이미 첨부됨
    ("EXP-019", "EMP-001", "이미 제출완료"),            # 제출완료 출장 (TRIP-005)
    ("EXP-999", "EMP-004", "결제가 없습니다"),
])
def test_attach_refused(expense_id, employee_id, expected):
    """본인·정산 대상·미첨부·제출 전 결제만 첨부할 수 있다."""
    record, error = tools.attach_receipt(expense_id, employee_id, "receipt.pdf", 10)
    assert record is None and expected in error


def test_cannot_attach_twice():
    """같은 결제에 두 번 첨부할 수 없다."""
    tools.attach_receipt("EXP-007", "EMP-004", "a.pdf", 1)
    assert "이미 영수증이 첨부" in tools.attach_receipt("EXP-007", "EMP-004", "b.pdf", 1)[1]


def test_full_flow_receipt_and_approval_to_submittable():
    """TRIP-002: 영수증 첨부 + 식비 초과 예외 결재 요청 → 부서장 승인 → 조치 필요 0건, 제출 가능."""
    assert check_trip2()["action_needed_count"] == 2
    tools.attach_receipt("EXP-007", "EMP-004", "receipt.pdf", 1234)
    assert check_trip2()["action_needed_count"] == 1
    approval_id = tools.save_approval_request("TRIP-002", "한도초과", ["EXP-004", "EXP-005"], "고객사 미팅", "EMP-010")
    tools.decide_approval(approval_id, "EMP-010", "승인")
    result = check_trip2()
    assert result["action_needed_count"] == 0 and result["submittable"]
