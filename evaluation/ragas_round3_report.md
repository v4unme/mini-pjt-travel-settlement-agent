# RAGAS 평가 리포트 (Round 3)

- 대상: contexts 가 있는 9건 (규정 질문 3건, 점검 질문 6건). contexts 없는 6건 제외 (가드레일 거절, 출장 되묻기, 제출 요청 등 근거 조항 없이 끝나는 문항)
- 평가 모델: Claude Sonnet 4.5 / 임베딩: amazon.titan-embed-text-v2:0
- contexts: 근거 규정 조항 + 점검 문항은 check_settlement 결과 데이터 (Agent 가 실제로 받은 정보)
- answer_relevancy: 질문 역생성 프롬프트를 한국어로 변환해 사용
- 한계: 정답 문장(reference) 컬럼이 없어 expected_traits 를 이어 붙여 reference 로 썼다. context_recall·context_precision 은 이 reference 기준이다.

## 지표 평균

| 지표 | 전체 | 규정 질문 | 점검 질문 |
|---|---|---|---|
| context_recall | 0.83 | 0.83 | 0.83 |
| context_precision | 0.48 | 0.67 | 0.38 |
| faithfulness | 0.71 | 0.60 | 0.77 |
| answer_relevancy | 0.41 | 0.52 | 0.36 |

보정 전: contexts 에 규정 조항만 넣고 영어 기본 프롬프트로 잰 첫 측정값. 점검 답변의 금액·결제ID 같은 도구 데이터까지 근거 없는 주장으로 세어 faithfulness 가 낮게 나왔다. 측정 방식을 바꾼 이유와 함께 두 값을 모두 남긴다.

## 문항별

| # | 카테고리 | 유형 | context_recall | context_precision | faithfulness | answer_relevancy |
|---|---|---|---|---|---|---|
| 1 | positive | 점검 질문 | 0.60 | 0.33 | 0.56 | 0.36 |
| 2 | positive | 점검 질문 | 0.83 | 0.20 | 0.87 | 0.29 |
| 3 | positive | 점검 질문 | 1.00 | 0.25 | 0.94 | 0.41 |
| 4 | positive | 점검 질문 | 0.75 | 0.33 | 0.63 | 0.36 |
| 5 | positive | 규정 질문 | 1.00 | 1.00 | 0.67 | 0.88 |
| 6 | positive | 규정 질문 | 1.00 | 1.00 | 0.79 | 0.69 |
| 7 | negative | 규정 질문 | 0.50 | 0.00 | 0.33 | 0.00 |
| 11 | edge | 점검 질문 | 1.00 | 0.75 | 0.92 | 0.37 |
| 12 | edge | 점검 질문 | 0.80 | 0.42 | 0.71 | 0.36 |

참고: answer_relevancy 는 '규정에 없으니 담당자에게 확인하라' 같은 답을 회피성 답변(noncommittal)으로 보고 0점을 준다. #7(규정에 없는 항목)은 정책상 올바른 답이지만 이 지표에서는 낮게 나올 수 있다.
