"""ragas_eval.py - 저장된 평가 결과(질문·답변·contexts)로 RAGAS 4개 지표를 계산한다. Agent 는 다시 실행하지 않는다.

실행 (프로젝트 루트에서): python evaluation/ragas_eval.py --round 2
- 대상: contexts(근거 조항)가 있는 문항만
- contexts: Agent 가 실제로 받은 정보 = 근거 규정 조항 + (점검 문항이면) check_settlement 결과 데이터.
  점검 결과는 LLM 없이 데이터에서 똑같이 재계산한다 (trace 는 500자에서 잘려 있어 쓰지 않는다)
- reference: 정답 문장 컬럼이 없어 test_queries.csv 의 expected_traits 를 문장으로 이어 대신 쓴다 (한계로 리포트에 기록)
- answer_relevancy: 답변에서 질문을 역생성하는 프롬프트를 한국어로 변환해 쓴다 (기본 영어 프롬프트는 한국어 질문과 비교가 왜곡됨)
- 평가 모델: Claude Sonnet 4.5 (llm.py), 임베딩: Titan v2
"""
import argparse
import asyncio
import csv
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "evaluation"
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("BASE_DATE", "2026-10-21")  # 평가와 같은 기준일로 점검 결과를 재계산
os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="ragas_state_")  # 평가 실행과 같은 빈 상태

# ragas 0.4.3 은 최신 langchain-community 에서 삭제된 Vertex AI 모듈을 import 한다.
# 이 프로젝트는 Vertex AI 를 쓰지 않으므로, import 가 실패하지 않게 빈 모듈만 채워 넣는다.
for module_name, class_name in [("langchain_community.chat_models.vertexai", "ChatVertexAI"),
                                ("langchain_community.llms.vertexai", "VertexAI")]:
    if module_name not in sys.modules:
        stub = types.ModuleType(module_name)
        setattr(stub, class_name, type(class_name, (), {}))
        sys.modules[module_name] = stub

from langchain_aws import BedrockEmbeddings  # noqa: E402
from ragas import EvaluationDataset, evaluate  # noqa: E402
from ragas.embeddings import LangchainEmbeddingsWrapper  # noqa: E402
from ragas.llms import LangchainLLMWrapper  # noqa: E402
from ragas.metrics import (  # noqa: E402
    Faithfulness, LLMContextPrecisionWithReference, LLMContextRecall, ResponseRelevancy,
)
from ragas.run_config import RunConfig  # noqa: E402

import tools  # noqa: E402
from llm import build_llm  # noqa: E402

METRICS = ["context_recall", "llm_context_precision_with_reference", "faithfulness", "answer_relevancy"]
LABELS = {"context_recall": "context_recall", "llm_context_precision_with_reference": "context_precision",
          "faithfulness": "faithfulness", "answer_relevancy": "answer_relevancy"}


def settlement_data(result: dict) -> list[str]:
    """점검 문항이면 Agent 가 받은 check_settlement 결과를 데이터에서 다시 계산해 context 문자열로 만든다."""
    employee_id = result["employee"].split()[0]
    trip_ids = []
    for step in result["trace"]:
        if step["step"] == "check_settlement":
            trip_ids += [t for t in re.findall(r"TRIP-\d{3}", step["input"]) if t not in trip_ids]
    contexts = []
    for trip_id in trip_ids:
        trip, error = tools._find_trip(trip_id, employee_id)
        check, error = (None, error) if error else tools._run_check(trip, employee_id)
        body = error or json.dumps(check, ensure_ascii=False)
        contexts.append(f"[점검 결과 데이터 check_settlement {trip_id}]\n{body}")
    return contexts


def build_samples(round_no: int) -> list[dict]:
    """저장된 결과에서 contexts 가 있는 문항을 RAGAS 입력으로 바꾼다."""
    results = json.loads((EVAL_DIR / "results" / f"round{round_no}_results.json").read_text(encoding="utf-8"))
    with (EVAL_DIR / "test_queries.csv").open(encoding="utf-8-sig", newline="") as f:
        traits = {row["id"]: row["expected_traits"] for row in csv.DictReader(f)}
    samples = []
    for key in sorted(results, key=int):
        r = results[key]
        if not r["contexts"]:
            continue
        data = settlement_data(r)
        samples.append({
            "id": r["id"], "category": r["category"], "kind": "점검 질문" if data else "규정 질문",
            "user_input": r["input"], "response": r["answer"],
            "retrieved_contexts": [c["text"] for c in r["contexts"]] + data,
            "reference": ". ".join(t.strip() for t in traits[key].split(";") if t.strip()),
        })
    return samples


def _mean(rows: list[dict], metric: str) -> float:
    """NaN·None 을 빼고 평균을 낸다."""
    values = [r[metric] for r in rows if r.get(metric) is not None and r[metric] == r[metric]]
    return sum(values) / len(values) if values else float("nan")


def write_report(round_no: int, samples: list[dict], rows: list[dict], skipped: int):
    """지표 평균(전체·유형별·보정 전 비교)과 문항별 점수를 evaluation/ragas_round{N}_report.md 로 저장한다."""
    by_kind = {kind: [r for s, r in zip(samples, rows) if s["kind"] == kind] for kind in ("규정 질문", "점검 질문")}
    before_path = EVAL_DIR / "results" / f"ragas_round{round_no}_uncorrected.json"
    before = json.loads(before_path.read_text(encoding="utf-8")) if before_path.exists() else []

    lines = [
        f"# RAGAS 평가 리포트 (Round {round_no})", "",
        f"- 대상: contexts 가 있는 {len(samples)}건 (규정 질문 {len(by_kind['규정 질문'])}건, 점검 질문 {len(by_kind['점검 질문'])}건). "
        f"contexts 없는 {skipped}건 제외 (가드레일 거절, 출장 되묻기, 제출 요청 등 근거 조항 없이 끝나는 문항)",
        f"- 평가 모델: {os.getenv('BEDROCK_MODEL_ID', 'Claude Sonnet 4.5')} / 임베딩: amazon.titan-embed-text-v2:0",
        "- contexts: 근거 규정 조항 + 점검 문항은 check_settlement 결과 데이터 (Agent 가 실제로 받은 정보)",
        "- answer_relevancy: 질문 역생성 프롬프트를 한국어로 변환해 사용",
        "- 한계: 정답 문장(reference) 컬럼이 없어 expected_traits 를 이어 붙여 reference 로 썼다. "
        "context_recall·context_precision 은 이 reference 기준이다.",
        "", "## 지표 평균", "",
        "| 지표 | 전체 | 규정 질문 | 점검 질문 |" + (" 보정 전 (전체) |" if before else ""),
        "|---|---|---|---|" + ("---|" if before else ""),
    ]
    for m in METRICS:
        row = f"| {LABELS[m]} | {_mean(rows, m):.2f} | {_mean(by_kind['규정 질문'], m):.2f} | {_mean(by_kind['점검 질문'], m):.2f} |"
        lines.append(row + (f" {_mean(before, m):.2f} |" if before else ""))
    lines += [
        "", "보정 전: contexts 에 규정 조항만 넣고 영어 기본 프롬프트로 잰 첫 측정값. 점검 답변의 금액·결제ID 같은 도구 데이터까지 "
        "근거 없는 주장으로 세어 faithfulness 가 낮게 나왔다. 측정 방식을 바꾼 이유와 함께 두 값을 모두 남긴다.",
        "", "## 문항별", "", "| # | 카테고리 | 유형 | " + " | ".join(LABELS[m] for m in METRICS) + " |",
        "|---|---|---|" + "---|" * len(METRICS),
    ]
    for sample, row in zip(samples, rows):
        cells = [f"{row[m]:.2f}" if row.get(m) is not None and row[m] == row[m] else "-" for m in METRICS]
        lines.append(f"| {sample['id']} | {sample['category']} | {sample['kind']} | " + " | ".join(cells) + " |")
    lines += ["", "참고: answer_relevancy 는 '규정에 없으니 담당자에게 확인하라' 같은 답을 회피성 답변(noncommittal)으로 보고 0점을 준다. "
              "#7(규정에 없는 항목)은 정책상 올바른 답이지만 이 지표에서는 낮게 나올 수 있다."]
    (EVAL_DIR / f"ragas_round{round_no}_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:18]))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="RAGAS 평가")
    parser.add_argument("--round", type=int, default=2)
    args = parser.parse_args()

    samples = build_samples(args.round)
    total = len(json.loads((EVAL_DIR / "results" / f"round{args.round}_results.json").read_text(encoding="utf-8")))
    dataset = EvaluationDataset.from_list(
        [{k: s[k] for k in ("user_input", "response", "retrieved_contexts", "reference")} for s in samples])
    llm = LangchainLLMWrapper(build_llm())
    embeddings = LangchainEmbeddingsWrapper(BedrockEmbeddings(
        model_id="amazon.titan-embed-text-v2:0", region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1")))
    relevancy = ResponseRelevancy()
    relevancy.set_prompts(**asyncio.run(relevancy.adapt_prompts(language="korean", llm=llm)))  # 질문 역생성을 한국어로
    result = evaluate(
        dataset,
        metrics=[LLMContextRecall(), LLMContextPrecisionWithReference(), Faithfulness(), relevancy],
        llm=llm, embeddings=embeddings,
        run_config=RunConfig(max_workers=2, timeout=300),  # 호출 한도를 고려해 동시 실행 수를 낮춘다
    )
    rows = result.to_pandas().to_dict(orient="records")
    (EVAL_DIR / "results" / f"ragas_round{args.round}.json").write_text(
        json.dumps([{**{"id": s["id"]}, **{m: r.get(m) for m in METRICS}} for s, r in zip(samples, rows)],
                   ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    write_report(args.round, samples, rows, total - len(samples))
