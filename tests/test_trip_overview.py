"""test_trip_overview.py - 시연 화면의 내 출장 정산 현황 계산 검증. LLM 호출 없음.

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


def row(employee_id, trip_id):
    """직원 현황에서 출장 1건을 찾는다."""
    return next(r for r in tools.my_trip_overview(employee_id) if r["trip_id"] == trip_id)


def test_only_own_trips():
    """본인 출장만 나온다 (김하늘: TRIP-001, TRIP-005)."""
    assert [r["trip_id"] for r in tools.my_trip_overview("EMP-001")] == ["TRIP-001", "TRIP-005"]


@pytest.mark.parametrize("employee_id, trip_id, kind, status, count, total", [
    ("EMP-001", "TRIP-001", "ok", "제출 가능", 3, "224,800원"),
    ("EMP-001", "TRIP-005", "done", "제출완료", 2, "235,000원"),
    ("EMP-004", "TRIP-002", "action", "조치 필요 2건", 4, "236,700원"),   # 미선택 EXP-008 50,000원 제외
    ("EMP-005", "TRIP-003", "action", "조치 필요 2건", 4, "508,800원"),
    ("EMP-006", "TRIP-004", "ok", "제출 가능", 3, "198,000원"),          # 기한 경과지만 APR-001 승인
    ("EMP-007", "TRIP-006", "empty", "결제 내역 없음", 0, "0원"),
])
def test_status_counts_totals(employee_id, trip_id, kind, status, count, total):
    """상태·정산 대상 건수·합계가 점검 계산과 같다."""
    r = row(employee_id, trip_id)
    assert (r["kind"], r["status"], r["selected_count"], r["total_amount"]) == (kind, status, count, total)


def test_detail_and_deadline():
    """TRIP-002 조치 내역(누락 1·예외 결재 필요 1)과 기한(10-29, 6영업일)."""
    r = row("EMP-004", "TRIP-002")
    assert r["detail"] == ["영수증 누락 1", "예외 결재 필요 1"]
    assert r["deadline"] == {"date": "2026-10-29", "passed": False, "remaining_business_days": 6}


def test_overview_follows_receipt_approval_and_submission():
    """첨부 → 조치 1건, 결재 요청 → 결재 대기, 부서장 승인 → 제출 가능, 제출 → 제출완료."""
    tools.attach_receipt("EXP-007", "EMP-004", "r.pdf", 1)
    assert row("EMP-004", "TRIP-002")["status"] == "조치 필요 1건"
    approval_id = tools.save_approval_request("TRIP-002", "한도초과", ["EXP-004", "EXP-005"], "미팅", "EMP-010")
    assert row("EMP-004", "TRIP-002")["status"] == "결재 대기 1건"
    tools.decide_approval(approval_id, "EMP-010", "승인")
    assert row("EMP-004", "TRIP-002")["kind"] == "ok"
    tools.save_submission("TRIP-002", "EMP-004")
    assert row("EMP-004", "TRIP-002")["kind"] == "done"
    assert row("EMP-004", "TRIP-002")["detail"] == []  # 제출완료 건에는 조치 내역을 표시하지 않는다
    assert row("EMP-001", "TRIP-005")["detail"] == []
