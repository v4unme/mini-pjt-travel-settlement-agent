# 프로젝트 규칙 · 출장비 정산 사전 검토 Agent

무엇을 왜 만드는지는 `SERVICE.md` 가 기준이다. 이 문서는 어떻게 만드는지만 적는다. 둘이 충돌하면 SERVICE.md 를 따르고 사용자에게 알린다.

## 기술 스택
- Python 3.11+, LangChain 1.x, LangGraph 1.x
- 모델: Amazon Bedrock `ChatBedrockConverse`, 모델 ID `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, 리전 `us-east-1` (값은 .env 에서 읽는다)
- 그래프: LangGraph `StateGraph` 로 바깥 흐름(가드레일 → Agent → 가드레일)을 직접 그리고, 그 안의 Agent 노드는 `langchain.agents` 의 `create_agent` 로 만든 그래프를 하위 그래프로 넣는다
  ```
  START → input_guard ─(정상)──────→ agent → output_guard → END
                      └(타인 조회·인젝션)→ refuse → END
  ```
  - `input_guard`: LCEL 구조화 출력으로 질문을 판정하고 조건부 분기(`add_conditional_edges`)로 경로를 고른다
  - `refuse`: SERVICE.md 4번의 거절 문구로 답하고 끝낸다 (Agent·도구를 부르지 않는다)
  - `agent`: create_agent (모델 ↔ 도구 반복)
  - `output_guard`: 카드번호 마스킹, 고정 안내 문구 부착
- 임베딩: `BedrockEmbeddings` (`amazon.titan-embed-text-v2:0`), 벡터 저장소: `langchain-chroma`
- 체크포인터: `SqliteSaver` (HITL 중단·재개와 대화 이어가기용)
- API: FastAPI + uvicorn
- 평가: 자체 채점 스크립트 + RAGAS

## 폴더 구조 (제출 규약. 바꾸지 않는다)
```
src/agent.py         메인 그래프 (StateGraph 바깥 흐름 + create_agent 하위 그래프, 시스템 프롬프트, 체크포인터)
src/tools.py         도메인 도구 7개 (@tool 정의만. 계산은 settlement.py 호출)
src/settlement.py    정산 점검 계산 (한도·누락·기한·영업일). LLM 호출 없는 순수 함수
src/llm.py           Bedrock 모델 생성 (그래프·도구·평가 공용)
src/drafting.py      예외 결재 사유서 초안 (LCEL 체인은 사유 문장만, 숫자는 코드가 넣음)
src/retriever.py     RAG 파이프라인 (규정 문서 청킹·임베딩·검색)
src/guards.py        입력·출력 가드레일 함수 (타인 조회·인젝션 판정 체인, 카드번호 마스킹). 그래프 노드 연결은 agent.py 에서 한다
src/tracer.py        트레이스 기록 콜백 (API 응답의 trace 와 파일 기록)
src/api.py           FastAPI 서버 (POST /query)
data/                더미 데이터 JSON 과 data/policy/ 규정 문서
evaluation/          test_queries.csv(8컬럼, 아래 평가 규약), run_eval.py, round1_report.md, round2_report.md,
                     hitl_scenarios.py·hitl_report.md (사람 승인 여러 턴 대화 검증)
state/               실행 중 생기는 기록: submissions.json(제출), approvals.json(이 Agent 로 요청한 예외 결재), drafts.json(승인 대기 중 사유서 문단). 커밋 제외. data/ 원본은 바꾸지 않는다
tests/               test_settlement.py (settlement.py 계산 검증. LLM 호출 없음)
```

## 주고받는 형식 (제출 규약)
- `POST /query`, body 는 `{"question": "..."}` 만 받는다.
- 응답은 `answer`, `contexts`, `trace` 세 키로 돌려준다.
  - `contexts`: search_policy 가 찾은 규정 조항 `[{"doc_id": "조항 제목", "text": "본문"}]`. 규정 검색이 없었으면 빈 리스트
  - `trace`: 실행 단계 `[{"step": "도구명 또는 llm", "input": "...", "output": "..."}]`
- 로그인 직원과 대화는 body 가 아니라 헤더로 받는다.
  - `X-Employee-Id`: 로그인 직원ID. 없으면 .env 의 `DEMO_EMPLOYEE_ID` 를 쓴다
  - `X-Thread-Id`: 대화 ID. 없으면 새로 만들고 응답 헤더로 돌려준다
- HITL(결재 요청·최종 제출) 흐름
  1. 도구가 `interrupt()` 로 멈추면 `answer` 에 확인할 내용(사유서 초안 또는 제출 요약)과 "승인 / 거절" 안내를 담는다
  2. 같은 `X-Thread-Id` 로 다음 질문이 오면 `Command(resume=...)` 로 재개한다. 승인 판정은 모델이 아니라 코드(`tools.is_approval`)가 한다: 답의 첫 단어가 승인·네·예·제출 계열이면 실행, 그 외(애매한 답 포함)는 실행하지 않는다
  3. 승인 대기 중인 대화에는 처음 요청한 직원만 답할 수 있다. 다른 `X-Employee-Id` 면 403
  4. 승인 대기 중이면 응답 `trace` 마지막에 `{"step": "interrupt", ...}` 단계가 붙는다
- 제출 기록은 `data/trips.json` 을 바꾸지 않고 `STATE_DIR`(기본 `state/`)의 submissions.json 에 남기고, 조회 시 원본 상태 위에 덮어씌운다. 평가·시나리오 스크립트는 매번 빈 임시 폴더를 쓴다

## 구현 규약
- 직원ID는 `create_agent(context_schema=...)` 의 런타임 컨텍스트로 넣고 도구는 `ToolRuntime` 으로 읽는다. 도구 인자에 employee_id 를 두지 않는다 (모델이 직원을 고를 수 없게)
- 금액·날짜 계산은 `src/settlement.py` 에서만 한다. 모델에게 계산을 시키지 않는다
- 사람 승인 도구는 재개 시 처음부터 다시 실행된다. interrupt 앞에서 모델을 부르면 결과를 상태 파일에 보관해 직원이 본 내용과 저장 내용이 같게 한다 (예: 사유서 문단)
- 기준일(오늘)은 .env 의 `BASE_DATE` 가 있으면 그 값, 없으면 실제 오늘 날짜를 쓴다
- 금액 표기는 `12,000원` 형식으로 통일한다 (평가의 숫자 정확 일치 기준)
- 결제ID 형식 `EXP-001`, 출장ID `TRIP-001`, 직원ID `EMP-001`, 결재ID `APR-001`
- 규정 문서는 `## ` 제목 단위로 청킹하고 제목을 `doc_id` 로 쓴다
- 입력 가드레일은 LCEL 체인(`프롬프트 | 모델.with_structured_output(Pydantic)`)으로 판정한다
- 출력 가드레일은 카드번호를 끝 4자리만 남기고 마스킹한다
- 답변 형식과 고정 안내 문구는 SERVICE.md 4번을 그대로 따른다

## 평가 규약
- `evaluation/test_queries.csv` 스키마 (8컬럼, 이 순서를 바꾸지 않는다)
  `id, category, input, expected_traits, forbidden, expected_tools, note, employee`
  - 앞 7컬럼은 공지 §4-4 스키마 그대로다. 이 서비스는 로그인 직원에 따라 결과가 달라지므로(본인 출장만, 직급별 한도) 8번째에 `employee` 를 추가했다
  - `employee` 는 필수. 값은 `EMP-004 최민준` 형식(직원ID + 공백 + 이름)이고 첫 토큰이 직원ID다
  - 새 컬럼은 반드시 맨 뒤에만 추가한다 (§4-4 의 앞 7컬럼 위치를 유지하기 위해)
- run_eval.py 는 `employee` 의 직원ID를 `X-Employee-Id` 로 넣어 문항을 실행한다
- 문항마다 새 `X-Thread-Id` 로 실행한다 (이전 문항의 대화가 섞이지 않게)
- 채점 기준일은 `BASE_DATE=2026-10-21` 로 고정한다
- expected_traits 채점
  - 원칙: **숫자는 엄격하게, 나머지는 의미만 맞으면 통과**
  - 숫자(정확 일치, 자동): trait 안의 금액(`13,000원`)·날짜(`2026-10-19`)·건수·박수·영업일(`2건`·`2박`·`7영업일`)·ID(`EXP-007`·`APR-001`)·카드번호. "1일 한도", "1박 단가" 같은 이름의 일부는 숫자 값이 아니므로 제외
  - 의미(LLM-as-Judge 1차 판정 → 사람 최종 확인): trait 의 문장 표현. 문구·어순이 달라도 의미가 같으면 통과
- forbidden 도 같은 원칙: 숫자가 든 항목은 그 숫자가 답변에 나오면 fail, 문장 항목은 그 의미를 실제로 말했을 때만 fail (단어만 겹치면 통과)
- expected_tools 가 있으면 해당 도구 호출 여부를 본다
- 문자열을 그대로 요구하는 고정 문구는 코드가 출력하는 것(거절 문구, 점검 안내 문구)뿐이다. 모델이 쓰는 안내 문구에 정확한 문장을 강제하지 않는다
- 더미 데이터는 각 문항 note 에 적힌 금액·날짜·ID 와 정확히 일치하게 만든다

## 코드 규칙
- 파일 하나에 한 가지 역할만 둔다
- 함수와 도구에는 한국어 docstring 을 쓴다. 도구 docstring 은 모델이 읽는 설명이므로 언제 쓰는지와 입력을 적는다
- 비밀 값(AWS 키 등)은 .env 에서 읽고 코드에 적지 않는다. .env 는 커밋하지 않는다
- 경로는 프로젝트 루트 기준으로 계산한다 (`Path(__file__).resolve().parent.parent`)

## 실행
- 단독 실행: `python src/agent.py` (시험 질문 1개 실행)
- 서버: `uvicorn src.api:app --reload`
- 계산 테스트: `pytest tests` (LLM 없이 즉시 실행. settlement.py 를 고치면 반드시 먼저 돌린다)
- 평가: `python evaluation/run_eval.py --round N` → 결과를 evaluation/ 에 저장 (`--ids`, `--regrade`, `--report` 옵션)
- 승인 흐름 검증: `python evaluation/hitl_scenarios.py` → evaluation/hitl_report.md (시나리오 이름 일부를 인자로 주면 그것만 실행)
- RAGAS: `python evaluation/ragas_eval.py --round 2` → evaluation/ragas_report.md (저장된 답변 재사용, Agent 재실행 없음)

## 작업 방식
- 계획을 먼저 보여 주고 승인받은 뒤 코드를 만든다
- 개발 순서는 SERVICE.md 3번의 1차 → 2차 → 3차를 따른다. 앞 단계가 돌기 전에 다음 단계 도구를 만들지 않는다
- 만든 뒤에는 직접 실행해 결과를 보여 준다
- 평가 루프에서는 한 번에 한 가지만 고친다
- 답변은 한국어로 한다

## 하지 말 것
- 요청하지 않은 파일을 새로 만들지 않는다
- 기존 파일을 통째로 다시 쓰지 않는다. 바뀐 부분만 고친다
- SERVICE.md 의 범위 밖 기능(OCR, 타인 조회, 해외 출장, 출장 준비)을 만들지 않는다
- 사용자가 요청하기 전에는 git commit·push 하지 않는다
