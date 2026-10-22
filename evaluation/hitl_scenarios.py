"""hitl_scenarios.py - 사람 승인(HITL)이 필요한 여러 턴 대화를 실제 API 로 검증한다.

test_queries.csv 는 질문 1개 → 답 1개 구조라 "제출 요청 → 승인" 같은 대화를 담을 수 없어 따로 검증한다.
판정은 모델 문장이 아니라 결정적인 신호(승인 대기 여부, 제출 기록 파일, 도구 반환값, 숫자)로 한다.

실행 (프로젝트 루트에서): python evaluation/hitl_scenarios.py
"""
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("BASE_DATE", "2026-10-21")
os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="hitl_state_")  # 빈 제출 기록에서 시작

from fastapi.testclient import TestClient  # noqa: E402

from api import app  # noqa: E402
from tools import load_submissions  # noqa: E402


class Session:
    """한 직원의 한 대화(X-Thread-Id)를 이어 가며 API 를 호출한다."""

    def __init__(self, client: TestClient, employee_id: str):
        self.client, self.employee_id, self.thread_id = client, employee_id, str(uuid.uuid4())

    def say(self, question: str, employee_id: str | None = None):
        """질문(또는 승인 답변)을 보내고 응답을 돌려준다. employee_id 로 다른 직원인 척 보낼 수 있다."""
        return self.client.post("/query", json={"question": question},
                                headers={"X-Employee-Id": employee_id or self.employee_id, "X-Thread-Id": self.thread_id})


def waiting(body: dict) -> bool:
    """응답이 사람 승인을 기다리는 상태인지 (trace 에 interrupt 단계가 있는지)."""
    return any(step["step"] == "interrupt" for step in body["trace"])


def tool_outputs(body: dict, name: str) -> list[str]:
    """trace 에서 특정 도구의 반환값을 모은다."""
    return [step["output"] for step in body["trace"] if step["step"] == name]


def scenario_submit_and_approve(client):
    """김하늘: 부산 출장 제출 요청 → 확인 문구(3건, 224,800원) → '승인' → 제출 기록 생성."""
    s = Session(client, "EMP-001")
    first = s.say("부산 출장(TRIP-001) 정산서 제출해줘").json()
    second = s.say("승인").json()
    return [
        ("제출 요청에 승인 대기로 멈춤", waiting(first)),
        ("확인 문구에 정산 대상 3건, 합계 224,800원", "3건" in first["answer"] and "224,800원" in first["answer"]),
        ("'승인' 후 제출 기록에 TRIP-001", "TRIP-001" in load_submissions()),
        ("도구가 제출완료 반환", any(o.startswith("제출완료") for o in tool_outputs(second, "submit_settlement"))),
        ("승인 후에는 대기 상태가 아님", not waiting(second)),
    ]


def scenario_resubmit_blocked(client):
    """김하늘: 이미 제출한 부산 출장을 다시 제출 요청 → 확인 없이 거절."""
    body = Session(client, "EMP-001").say("부산 출장(TRIP-001) 정산서 다시 제출해줘").json()
    outputs = tool_outputs(body, "submit_settlement")
    return [
        ("승인 대기로 멈추지 않음", not waiting(body)),
        ("도구가 '이미 제출완료' 로 제출하지 않음 반환", any("이미 제출완료" in o for o in outputs)),
    ]


def scenario_reject(client):
    """한지우: 울산 출장(기한초과 예외 승인) 제출 요청 → 확인 문구 → '거절' → 제출 기록 없음."""
    s = Session(client, "EMP-006")
    first = s.say("울산 출장(TRIP-004) 정산서 제출해줘").json()
    second = s.say("거절").json()
    return [
        ("제출 요청에 승인 대기로 멈춤", waiting(first)),
        ("확인 문구에 예외 승인 완료 1건 표시", "예외 승인 완료 1건" in first["answer"]),
        ("'거절' 후 제출 기록 없음", "TRIP-004" not in load_submissions()),
        ("도구가 제출하지 않음 반환", any(o.startswith("제출하지 않음") for o in tool_outputs(second, "submit_settlement"))),
    ]


def scenario_blocked_by_issues(client):
    """최민준: 대전 출장(누락·초과 남음) 제출 요청 → 확인 없이 거절, 남은 조치 안내."""
    body = Session(client, "EMP-004").say("대전 출장(TRIP-002) 정산서 제출해줘").json()
    return [
        ("승인 대기로 멈추지 않음", not waiting(body)),
        ("제출 기록 없음", "TRIP-002" not in load_submissions()),
        ("남은 조치에 영수증 누락 EXP-007", "EXP-007" in body["answer"]),
        ("남은 조치에 식비 초과 13,000원", "13,000원" in body["answer"]),
    ]


def scenario_approval_status(client):
    """한지우: 울산 출장 결재 상태 조회 → APR-001 기한초과 승인."""
    body = Session(client, "EMP-006").say("울산 출장(TRIP-004) 예외 결재 상태 알려줘").json()
    return [
        ("get_approval_status 호출", bool(tool_outputs(body, "get_approval_status"))),
        ("답변에 APR-001", "APR-001" in body["answer"]),
    ]


def scenario_other_employee_cannot_approve(client):
    """한지우가 제출 요청해 승인 대기 중일 때 윤재원이 같은 대화로 '승인' → 403, 제출되지 않음."""
    s = Session(client, "EMP-006")
    first = s.say("울산 출장(TRIP-004) 정산서 제출해줘").json()
    hijack = s.say("승인", employee_id="EMP-008")
    still_waiting = waiting(first) and "TRIP-004" not in load_submissions()
    s.say("거절")  # 대기 상태 정리
    return [
        ("제출 요청에 승인 대기로 멈춤", waiting(first)),
        ("다른 직원의 승인은 403", hijack.status_code == 403),
        ("다른 직원 승인 후에도 제출 기록 없음", still_waiting),
    ]


SCENARIOS = [scenario_submit_and_approve, scenario_resubmit_blocked, scenario_reject,
             scenario_blocked_by_issues, scenario_approval_status, scenario_other_employee_cannot_approve]


def write_report(results: list[tuple[str, str, list]]):
    """시나리오 결과를 evaluation/hitl_report.md 로 저장한다."""
    total = sum(len(checks) for _, _, checks in results)
    passed = sum(ok for _, _, checks in results for _, ok in checks)
    lines = ["# HITL 시나리오 검증 리포트", "",
             f"- 실행: {time.strftime('%Y-%m-%d %H:%M')} / 기준일 {os.environ['BASE_DATE']} / 확인 항목 {passed}/{total} 통과",
             "- 판정은 승인 대기 여부, 제출 기록 파일, 도구 반환값, 숫자로 한다 (모델 문장 표현은 보지 않는다)", ""]
    for name, doc, checks in results:
        mark = "PASS" if all(ok for _, ok in checks) else "**FAIL**"
        lines += [f"## {mark} {name}", "", doc, "", "| 확인 항목 | 결과 |", "|---|---|"]
        lines += [f"| {label} | {'O' if ok else 'X'} |" for label, ok in checks]
        lines.append("")
    (ROOT / "evaluation" / "hitl_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n리포트 저장: evaluation/hitl_report.md  ({passed}/{total} 통과)")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    results = []
    with TestClient(app) as client:
        for scenario in SCENARIOS:  # 순서가 중요하다: 제출 → 재제출 차단 순
            checks = scenario(client)
            results.append((scenario.__name__, scenario.__doc__, checks))
            print(f"{'PASS' if all(ok for _, ok in checks) else 'FAIL'}  {scenario.__name__}")
            for label, ok in checks:
                print(f"    {'O' if ok else 'X'} {label}")
    write_report(results)
