"""test_submission.py - 제출 승인 판정과 제출 상태 기록 검증. LLM 호출 없음.

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
    """테스트마다 빈 제출 기록 폴더를 쓴다 (실제 state/ 를 건드리지 않는다)."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_DATE", "2026-10-21")


@pytest.mark.parametrize("answer", ["승인", "승인합니다", "네", "네, 제출해 주세요", "예", "제출해줘", " 승인! "])
def test_approval_words(answer):
    """첫 단어가 승인 표현이면 승인이다."""
    assert tools.is_approval(answer)


@pytest.mark.parametrize("answer", ["거절", "아니요", "잠깐만", "예외 결재부터 할게요", "네가 알아서 해", "", "음… 승인?"])
def test_non_approval_is_rejection(answer):
    """승인 표현으로 시작하지 않으면(애매한 답 포함) 거절이다. '예외', '네가' 처럼 앞글자만 같은 말도 거절."""
    assert not tools.is_approval(answer)


def test_submission_overrides_status_without_touching_data():
    """제출 기록은 상태 파일에만 남고, 조회 시 원본 상태 위에 덮어씌워진다."""
    trip, _ = tools._find_trip("TRIP-001", "EMP-001")
    assert trip["status"] == "미정산"

    tools.save_submission("TRIP-001", "EMP-001")

    trip, _ = tools._find_trip("TRIP-001", "EMP-001")
    assert trip["status"] == "제출완료"
    original = next(t for t in tools._load("trips.json") if t["trip_id"] == "TRIP-001")
    assert original["status"] == "미정산"  # data/trips.json 은 그대로


def test_submitted_trip_is_not_submittable_again():
    """제출 기록이 생기면 점검 결과도 제출 불가가 된다 (재제출 차단)."""
    trip, _ = tools._find_trip("TRIP-001", "EMP-001")
    result, _ = tools._run_check(trip, "EMP-001")
    assert result["submittable"]

    tools.save_submission("TRIP-001", "EMP-001")
    trip, _ = tools._find_trip("TRIP-001", "EMP-001")
    result, _ = tools._run_check(trip, "EMP-001")
    assert result["settlement_status"] == "제출완료" and not result["submittable"]


def test_confirm_message_numbers():
    """제출 확인 문구의 건수·합계: TRIP-001 정산 대상 3건, 45,000 + 59,800 + 120,000 = 224,800원."""
    trip, _ = tools._find_trip("TRIP-001", "EMP-001")
    result, _ = tools._run_check(trip, "EMP-001")
    message = tools._confirm_message(trip, result)
    assert "정산 대상 3건, 합계 224,800원" in message and "'승인' 또는 '거절'" in message


def test_confirm_message_counts_approved_exceptions():
    """TRIP-004 는 기한초과 예외 승인 1건을 확인 문구에 표시한다."""
    trip, _ = tools._find_trip("TRIP-004", "EMP-006")
    result, _ = tools._run_check(trip, "EMP-006")
    assert result["submittable"]
    assert "예외 승인 완료 1건 포함" in tools._confirm_message(trip, result)


def test_submission_record_sets_submission_date():
    """화면에서 제출한 건은 제출 기록 시각의 날짜가 제출일이 되고, 기한은 그 날짜로 판정한다."""
    tools.save_submission("TRIP-001", "EMP-001")
    trip, _ = tools._find_trip("TRIP-001", "EMP-001")
    assert trip["status"] == "제출완료" and len(trip["submitted_date"]) == 10
    result, _ = tools._run_check(trip, "EMP-001")
    assert result["deadline"]["reference_date"] == trip["submitted_date"] and result["action_needed_count"] == 0
