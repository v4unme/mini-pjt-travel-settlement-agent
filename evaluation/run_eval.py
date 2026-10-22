"""run_eval.py - test_queries.csv 로 API 를 실행·채점하고 라운드별 리포트를 만든다.

실행 (프로젝트 루트에서):
  python evaluation/run_eval.py --round 1            # 15건 전체
  python evaluation/run_eval.py --round 1 --ids 2,13 # 일부만 다시 실행 (기존 결과에 덮어쓴다)
  python evaluation/run_eval.py --round 1 --report   # 실행 없이 저장된 결과로 리포트만 다시 만든다
  python evaluation/run_eval.py --round 2 --regrade  # Agent 재실행 없이 저장된 답변을 현재 기준으로 다시 채점
"""
import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "evaluation"
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("BASE_DATE", "2026-10-21")  # 채점 기준일 고정 (CLAUDE.md 평가 규약)
os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="eval_state_")  # 제출 기록을 매번 빈 상태로 시작

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import BaseModel, Field, ValidationError  # noqa: E402

from llm import build_llm  # noqa: E402
from api import app  # noqa: E402
# 정확히 일치해야 하는 것은 숫자 값뿐이다 (금액·날짜·건수·박수·영업일·결제ID·결재ID).
# 문장 표현은 의미가 맞으면 통과이므로 Judge 가 판정한다.
NUMBER_TOKEN = re.compile(
    r"\d{4}-\d{4}-\d{4}-\d{4}|\d{4}-\d{2}-\d{2}|\d[\d,]*원|(?:EXP|TRIP|APR|EMP)-\d{3}"
    r"|\d+(?:건|박|영업일|일)(?!\s*(?:한도|단가))"  # "1일 한도", "1박 단가" 같은 이름은 값이 아니므로 제외
)
NON_TOOL_STEPS = {"llm", "input_guard"}
PASS_TARGET = 12  # SERVICE.md 5번: 15건 중 12건 이상


class Throttled(Exception):
    """모델 호출 한도에 걸려 평가를 멈춰야 할 때."""


# ---------------------------------------------------------------- 채점 기준

def split_items(value: str) -> list[str]:
    """세미콜론으로 구분된 항목을 나눈다."""
    return [v.strip() for v in (value or "").split(";") if v.strip()]


def number_tokens(text: str) -> list[str]:
    """trait 에서 정확히 일치해야 할 숫자 값을 뽑는다."""
    return NUMBER_TOKEN.findall(text)


# 근거(reason)를 결론(passed/fabricated)보다 먼저 쓰게 해서, 근거와 결론이 어긋나는 판정을 줄인다
class TraitJudgement(BaseModel):
    trait: str = Field(description="평가한 기대 특성 원문")
    reason: str = Field(description="판단 근거 한 문장")
    passed: bool = Field(description="근거에 따라, 답변이 이 특성을 만족하면 true")


class ForbiddenJudgement(BaseModel):
    item: str = Field(description="금지 항목 원문")
    reason: str = Field(description="판단 근거 한 문장")
    found: bool = Field(description="근거에 따라, 답변이 이 금지 항목의 의미를 실제로 말했으면 true")


class JudgeResult(BaseModel):
    traits: list[TraitJudgement] = Field(description="주어진 기대 특성 각각의 판정")
    forbidden: list[ForbiddenJudgement] = Field(description="주어진 금지 항목 각각의 판정")
    fabricated_reason: str = Field(description="규정 날조 여부 판단 근거 한 문장")
    fabricated: bool = Field(description="근거에 따라, 규정 조항·도구 결과에 없는 규정 내용을 사실처럼 말했으면 true")


JUDGE_PROMPT = """너는 출장비 정산 Agent 답변의 채점자다. 엄격하고 일관되게 판정한다.

[질문]
{question}

[Agent 답변]
{answer}

[Agent 가 검색한 규정 조항]
{contexts}

[Agent 가 호출한 도구의 결과 (앞부분만)]
{tool_outputs}

[판정 1] 아래 기대 특성 각각을 답변이 의미상 만족하는지 판정한다. 표현·어순·문구가 달라도 의미가 같으면 만족이다.
숫자(금액·날짜·건수·ID)의 정확한 일치는 별도로 자동 확인하므로, 여기서는 의미만 본다.
{traits}

[판정 2] 아래 금지 항목 각각에 대해, 답변이 그 의미를 실제로 말했는지 판정한다.
단어가 들어 있어도 의미가 다르면(예: "조치를 마치면 제출 가능합니다"는 지금 제출 가능하다는 판정이 아님) found=false 다.
{forbidden}

[판정 3] 답변이 '규정'에 대해 말한 내용 중, 위 규정 조항과 도구 결과 어디에도 없는 내용을 사실처럼 단정한 것이 있으면 fabricated=true 다.
도구 결과에 있는 한도·금액·날짜·결제ID·결재 상태는 근거가 있는 데이터이므로 fabricated 가 아니다.
규정에 없다고 안내하거나 담당자 확인을 권한 것은 fabricated 가 아니다."""


def judge(llm, question: str, answer: str, contexts: list[dict], trace: list[dict],
          traits: list[str], forbidden: list[str]) -> JudgeResult:
    """LLM-as-Judge 로 trait·금지 항목의 의미와 규정 날조 여부를 1차 판정한다."""
    tool_steps = [s for s in trace if s["step"] not in NON_TOOL_STEPS | {"search_policy"}]
    prompt = JUDGE_PROMPT.format(
        question=question, answer=answer,
        contexts="\n\n".join(f"[{c['doc_id']}]\n{c['text']}" for c in contexts) or "(없음)",
        tool_outputs="\n\n".join(f"[{s['step']}]\n{s['output']}" for s in tool_steps) or "(없음)",
        traits="\n".join(f"- {t}" for t in traits) or "(없음 - traits 는 빈 리스트로)",
        forbidden="\n".join(f"- {f}" for f in forbidden) or "(없음 - forbidden 은 빈 리스트로)",
    )
    for attempt in range(3):  # Judge 가 형식이 깨진 응답을 내면 다시 묻는다
        try:
            return llm.with_structured_output(JudgeResult).invoke(prompt)
        except ValidationError:
            if attempt == 2:
                raise
        except Exception as error:
            if "Throttling" in type(error).__name__ or "Too many tokens" in str(error):
                raise Throttled(str(error)) from error
            raise


def grade(case: dict, body: dict, judge_llm) -> dict:
    """문항 1건의 API 응답을 채점한다."""
    answer, contexts, trace = body["answer"], body["contexts"], body["trace"]
    called = [s["step"] for s in trace if s["step"] not in NON_TOOL_STEPS]
    traits = split_items(case["expected_traits"])
    numbered = [(t, number_tokens(t)) for t in traits if number_tokens(t)]
    forbidden = split_items(case["forbidden"])
    forbidden_numbers = [f for f in forbidden if number_tokens(f)]       # 숫자 값은 정확 일치로 금지
    forbidden_meanings = [f for f in forbidden if not number_tokens(f)]  # 문장은 의미로 금지 (Judge)

    auto = {
        "traits": [{"trait": t, "numbers": nums, "missing": [n for n in nums if n not in answer],
                    "passed": all(n in answer for n in nums)} for t, nums in numbered],
        "forbidden": [{"item": f, "found": all(n in answer for n in number_tokens(f))} for f in forbidden_numbers],
        "tools": [{"tool": t, "called": t in called} for t in split_items(case["expected_tools"])],
    }
    auto_passed = (all(t["passed"] for t in auto["traits"])
                   and not any(f["found"] for f in auto["forbidden"])
                   and all(t["called"] for t in auto["tools"]))

    verdict = judge(judge_llm, case["input"], answer, contexts, trace, traits, forbidden_meanings)
    judge_passed = (all(t.passed for t in verdict.traits) and not any(f.found for f in verdict.forbidden)
                    and not verdict.fabricated)

    return {
        "id": int(case["id"]), "category": case["category"], "employee": case["employee"],
        "input": case["input"], "answer": answer, "contexts": contexts, "tools_called": called,
        "trace": trace, "auto": auto, "auto_passed": auto_passed,
        "judge": verdict.model_dump(), "judge_passed": judge_passed,
        "passed": auto_passed and judge_passed,
    }


# ---------------------------------------------------------------- 실행

def load_cases() -> list[dict]:
    """test_queries.csv (8컬럼) 를 읽는다."""
    with (EVAL_DIR / "test_queries.csv").open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def run(round_no: int, ids: set[int] | None):
    """문항을 실행·채점하고 결과를 문항마다 바로 저장한다 (한도에 걸려도 이어서 실행할 수 있게)."""
    results_path = EVAL_DIR / "results" / f"round{round_no}_results.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else {}
    cases = [c for c in load_cases() if ids is None or int(c["id"]) in ids]
    judge_llm = build_llm()

    with TestClient(app) as client:
        for case in cases:
            employee_id = case["employee"].split()[0]
            started = time.time()
            res = client.post("/query", json={"question": case["input"]},
                              headers={"X-Employee-Id": employee_id, "X-Thread-Id": str(uuid.uuid4())})
            if res.status_code == 503:
                print(f"#{case['id']} 모델 호출 한도 초과로 평가를 멈춥니다. 남은 문항은 --ids 로 이어서 실행하세요.")
                break
            res.raise_for_status()
            try:
                result = grade(case, res.json(), judge_llm)
            except Throttled:
                print(f"#{case['id']} 채점(Judge) 중 모델 호출 한도 초과로 멈춥니다.")
                break
            result["latency_s"] = round(time.time() - started, 1)
            results[str(result["id"])] = result
            results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"#{result['id']:>2} {result['category']:<9} {'PASS' if result['passed'] else 'FAIL'}"
                  f"  (자동 {'O' if result['auto_passed'] else 'X'} / Judge {'O' if result['judge_passed'] else 'X'})"
                  f"  {result['latency_s']}s")
    return results


def regrade(round_no: int, ids: set[int] | None):
    """Agent 를 다시 실행하지 않고, 저장된 답변을 현재 판정 기준으로 다시 채점한다."""
    results_path = EVAL_DIR / "results" / f"round{round_no}_results.json"
    results = json.loads(results_path.read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in load_cases()}
    judge_llm = build_llm()
    for key in sorted(results, key=int):
        if ids is not None and int(key) not in ids:
            continue
        old = results[key]
        try:
            new = grade(cases[key], old, judge_llm)
        except Throttled:
            print(f"#{key} 채점(Judge) 중 모델 호출 한도 초과로 멈춥니다.")
            break
        new["latency_s"] = old.get("latency_s")
        results[key] = new
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"#{new['id']:>2} {new['category']:<9} {'PASS' if old['passed'] else 'FAIL'} → {'PASS' if new['passed'] else 'FAIL'}")
    return results


# ---------------------------------------------------------------- 리포트

def _fail_reasons(r: dict) -> list[str]:
    """문항이 실패한 이유를 사람이 읽을 문장으로 모은다."""
    reasons = [f"숫자 불일치: `{t['trait']}` 에서 {', '.join(f'`{n}`' for n in t.get('missing', []))} 없음"
               for t in r["auto"]["traits"] if not t["passed"]]
    reasons += [f"금지 문구 포함: `{f['item']}`" for f in r["auto"]["forbidden"] if f["found"]]
    reasons += [f"도구 미호출: `{t['tool']}`" for t in r["auto"]["tools"] if not t["called"]]
    reasons += [f"Judge 미충족: {t['trait']} — {t['reason']}" for t in r["judge"]["traits"] if not t["passed"]]
    reasons += [f"Judge 금지 의미 포함: {f['item']} — {f['reason']}" for f in r["judge"].get("forbidden", []) if f["found"]]
    if r["judge"]["fabricated"]:
        reasons.append(f"규정 날조 의심: {r['judge']['fabricated_reason']}")
    return reasons


def write_report(round_no: int, results: dict):
    """라운드 결과를 evaluation/round{N}_report.md 로 정리한다."""
    rows = [results[k] for k in sorted(results, key=int)]
    total, passed = len(rows), sum(r["passed"] for r in rows)
    money = [t for r in rows for t in r["auto"]["traits"] if "초과" in t["trait"] and re.search(r"\d원", t["trait"])]
    missing = [t for r in rows for t in r["auto"]["traits"] if t["trait"].startswith("영수증 누락 EXP-")]
    fabricated = [r for r in rows if r["judge"]["fabricated"]]

    def ok(flag):
        return "✅ 충족" if flag else "❌ 미충족"

    lines = [
        f"# Round {round_no} 평가 리포트",
        "",
        f"- 실행 문항: {total}건 / 기준일 {os.environ['BASE_DATE']} / 모델 {os.getenv('BEDROCK_MODEL_ID', 'Claude Sonnet 4.5 (기본값)')}",
        "- 채점 원칙: 숫자(금액·날짜·건수·ID·카드번호)는 정확 일치로 자동 채점, expected_tools 도 자동. 그 밖의 문장(trait·금지 항목)은 의미가 맞으면 통과이며 LLM-as-Judge 1차 판정 후 사람이 최종 확인",
        "",
        "## 성공 기준 (SERVICE.md 5번)",
        "",
        "| 기준 | 결과 | 판정 |",
        "|---|---|---|",
        f"| 15건 중 {PASS_TARGET}건 이상 통과 | {passed} / {total} | {ok(total == 15 and passed >= PASS_TARGET)} |",
        f"| 초과 금액 계산 오차 0원 | 초과 금액 {sum(t['passed'] for t in money)} / {len(money)} 일치 | {ok(all(t['passed'] for t in money))} |",
        f"| 영수증 누락 탐지율 100% | {sum(t['passed'] for t in missing)} / {len(missing)} 탐지 | {ok(all(t['passed'] for t in missing))} |",
        f"| 규정 날조 0건 | {len(fabricated)}건 | {ok(not fabricated)} |",
        "",
        "## 카테고리별 통과율",
        "",
        "| 카테고리 | 통과 | 전체 | 통과율 |",
        "|---|---|---|---|",
    ]
    for category in ("positive", "negative", "edge", "guardrail"):
        group = [r for r in rows if r["category"] == category]
        if group:
            n = sum(r["passed"] for r in group)
            lines.append(f"| {category} | {n} | {len(group)} | {n / len(group):.0%} |")
    lines.append(f"| **전체** | **{passed}** | **{total}** | **{passed / total:.0%}** |" if total else "")

    lines += ["", "## 문항별 결과", "", "| # | 카테고리 | 직원 | 질문 | 자동 | Judge | 결과 | 호출 도구 |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id']} | {r['category']} | {r['employee']} | {r['input']} | "
                     f"{'O' if r['auto_passed'] else 'X'} | {'O' if r['judge_passed'] else 'X'} | "
                     f"{'PASS' if r['passed'] else '**FAIL**'} | {', '.join(r['tools_called']) or '-'} |")

    failed = [r for r in rows if not r["passed"]]
    lines += ["", "## 실패 문항 상세", ""]
    if not failed:
        lines.append("실패 문항 없음")
    for r in failed:
        lines += [f"### #{r['id']} {r['category']} — {r['input']}", ""]
        lines += [f"- {reason}" for reason in _fail_reasons(r)]
        lines += ["", "<details><summary>답변 원문</summary>", "", "```", r["answer"], "```", "", "</details>", ""]

    lines += ["", "## 사람 확인", "", "Judge 판정은 1차 판정이다. 아래 칸에 최종 확인 결과를 적는다.", "",
              "| # | Judge 판정 | 사람 확인 (O/X) | 메모 |", "|---|---|---|---|"]
    lines += [f"| {r['id']} | {'O' if r['judge_passed'] else 'X'} |  |  |" for r in rows]

    path = EVAL_DIR / f"round{round_no}_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n리포트 저장: {path.relative_to(ROOT)}  ({passed}/{total} 통과)")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="인-아웃 세트 평가")
    parser.add_argument("--round", type=int, required=True, help="라운드 번호 (1, 2, ...)")
    parser.add_argument("--ids", help="실행할 문항 번호 (쉼표 구분). 없으면 전체")
    parser.add_argument("--report", action="store_true", help="실행 없이 저장된 결과로 리포트만 만든다")
    parser.add_argument("--regrade", action="store_true", help="Agent 재실행 없이 저장된 답변을 현재 기준으로 다시 채점한다")
    args = parser.parse_args()
    selected = {int(x) for x in args.ids.split(",")} if args.ids else None

    if args.report:
        saved = EVAL_DIR / "results" / f"round{args.round}_results.json"
        write_report(args.round, json.loads(saved.read_text(encoding="utf-8")))
    elif args.regrade:
        write_report(args.round, regrade(args.round, selected))
    else:
        write_report(args.round, run(args.round, selected))
