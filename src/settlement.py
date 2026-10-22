"""settlement.py - 출장비 정산 사전 점검 계산 (LLM 호출 없는 순수 함수)"""
from collections import defaultdict
from datetime import date, timedelta

# 직급별 한도 (원). 숙박은 1박, 식비·교통은 1일 기준
LIMITS = {
    "평직원": {"식비": 70000, "숙박": 150000, "교통": 100000},
    "임원": {"식비": 100000, "숙박": 200000, "교통": 100000},
}
DEADLINE_BUSINESS_DAYS = 7

STATUS_APPROVED = "초과지만 예외 승인 완료, 정산 가능"
STATUS_PENDING = "결재 대기"
STATUS_NEEDED = "예외 결재 필요"


def won(amount: int) -> str:
    """금액을 '12,000원' 형식 문자열로 바꾼다."""
    return f"{amount:,}원"


def is_business_day(d: date, holidays: set[date]) -> bool:
    """토·일·공휴일이 아니면 영업일이다."""
    return d.weekday() < 5 and d not in holidays


def settlement_deadline(return_date: date, holidays: set[date]) -> date:
    """복귀일 다음 영업일부터 7영업일째 되는 날(정산 마감일)을 구한다."""
    d, count = return_date, 0
    while count < DEADLINE_BUSINESS_DAYS:
        d += timedelta(days=1)
        if is_business_day(d, holidays):
            count += 1
    return d


def business_days_between(start: date, end: date, holidays: set[date]) -> int:
    """start 다음 날부터 end 까지(end 포함)의 영업일 수를 센다."""
    d, count = start, 0
    while d < end:
        d += timedelta(days=1)
        if is_business_day(d, holidays):
            count += 1
    return count


def _approval_status(approvals: list[dict], kind: str, expense_ids: list[str] | None = None) -> dict:
    """해당 유형(한도초과/기한초과)의 예외 결재 상태로 판정 문구와 관련 결재ID를 고른다.

    한도초과는 대상 결제ID가 겹치는 결재만 본다. 같은 대상에 결재가 여러 건이면 승인 > 요청됨 > 반려 순으로 본다.
    """
    related = [
        a for a in approvals
        if a["type"] == kind and (expense_ids is None or set(a["expense_ids"]) & set(expense_ids))
    ]
    for status, label in (("승인", STATUS_APPROVED), ("요청됨", STATUS_PENDING)):
        ids = [a["approval_id"] for a in related if a["status"] == status]
        if ids:
            return {"status": label, "approval_ids": ids}
    return {"status": STATUS_NEEDED, "approval_ids": [a["approval_id"] for a in related]}


def check_trip(trip: dict, expenses: list[dict], grade: str, approvals: list[dict],
               holidays: set[date], base_date: date) -> dict:
    """출장 1건의 정산 대상 결제를 규정과 대조해 판정 결과를 돌려준다.

    trip: trips.json 한 건, expenses: 그 출장의 결제 목록, grade: 평직원/임원,
    approvals: 그 출장의 예외 결재 목록, base_date: 점검 기준일
    """
    limits = LIMITS[grade]
    selected = [e for e in expenses if e["selected"]]
    excluded = [e["expense_id"] for e in expenses if not e["selected"]]

    # 1) 영수증 누락
    missing = [e["expense_id"] for e in selected if not e["receipt_attached"]]

    # 2) 한도 초과 - 식비·교통은 같은 날짜 합계, 숙박은 결제 1건의 1박 단가
    over_limits = []
    daily = defaultdict(list)
    for e in selected:
        if e["category"] in ("식비", "교통"):
            daily[(e["date"], e["category"])].append(e)
        elif e["category"] == "숙박":
            # 1박 단가 > 1박 한도 와 같은 판정을 나눗셈 없이 한다 (원 단위 오차 0원)
            nights = e["nights"] or 1
            allowed = limits["숙박"] * nights
            if e["amount"] > allowed:
                over = e["amount"] - allowed
                ids = [e["expense_id"]]
                over_limits.append({
                    "date": e["date"], "category": "숙박", "expense_ids": ids,
                    "total": won(e["amount"]), "nights": nights, "per_night": won(e["amount"] // nights),
                    "limit": f"1박 {won(limits['숙박'])}", "over_amount": won(over), "over_value": over,
                    **_approval_status(approvals, "한도초과", ids),
                })
    for (day, category), items in sorted(daily.items()):
        total = sum(e["amount"] for e in items)
        if total > limits[category]:
            ids = [e["expense_id"] for e in items]
            over_limits.append({
                "date": day, "category": category, "expense_ids": ids,
                "total": won(total), "limit": f"1일 {won(limits[category])}",
                "over_amount": won(total - limits[category]), "over_value": total - limits[category],
                **_approval_status(approvals, "한도초과", ids),
            })
    over_limits.sort(key=lambda o: (o["date"], o["category"]))

    # 3) 정산 기한 (출장 건 단위)
    return_date = date.fromisoformat(trip["return_date"])
    deadline = settlement_deadline(return_date, holidays)
    passed = base_date > deadline
    deadline_info = {
        "deadline": deadline.isoformat(),
        "passed": passed,
        "remaining_business_days": 0 if passed else business_days_between(base_date, deadline, holidays),
        "elapsed_business_days": business_days_between(deadline, base_date, holidays) if passed else 0,
        **(_approval_status(approvals, "기한초과") if passed else {"status": "기한 내", "approval_ids": []}),
    }

    # 4) 조치 필요 건수와 제출 가능 여부
    actions = len(missing)
    actions += sum(1 for o in over_limits if o["status"] != STATUS_APPROVED)
    actions += 1 if passed and deadline_info["status"] != STATUS_APPROVED else 0
    already_submitted = trip["status"] == "제출완료"

    return {
        "trip_id": trip["trip_id"],
        "destination": trip["destination"],
        "period": f"{trip['start_date']} ~ {trip['return_date']}",
        "grade": grade,
        "limits": {k: won(v) for k, v in limits.items()},
        "base_date": base_date.isoformat(),
        "settlement_status": trip["status"],
        "items": [
            {k: e[k] for k in ("expense_id", "date", "category", "merchant", "nights", "receipt_attached")}
            | {"amount": won(e["amount"])}
            for e in selected
        ],
        "excluded_not_selected": excluded,
        "missing_receipts": missing,
        "over_limits": over_limits,
        "deadline": deadline_info,
        "action_needed_count": actions,
        "submittable": actions == 0 and not already_submitted,
    }
