#!/usr/bin/env bash
# run.sh - 실행 스크립트 (프로젝트 루트에서 실행)
#   ./run.sh server        API 서버 + 시연 화면 (http://localhost:8000/)
#   ./run.sh test          단위 테스트 (LLM 호출 없음)
#   ./run.sh eval [N]      인-아웃 세트 15건 평가 → evaluation/roundN_report.md (기본 N=3)
#   ./run.sh hitl          사람 승인 흐름 시나리오 → evaluation/hitl_report.md
#   ./run.sh ragas [N]     저장된 roundN 결과로 RAGAS 측정 → evaluation/ragas_round{N}_report.md (기본 N=3)
#   ./run.sh reset         시연 상태(제출·결재·첨부 기록) 초기화
set -euo pipefail
cd "$(dirname "$0")"
export BASE_DATE="${BASE_DATE:-2026-10-21}"

case "${1:-server}" in
  server) exec python -m uvicorn src.api:app --host 0.0.0.0 --port "${PORT:-8000}" ;;
  test)   exec python -m pytest tests -q ;;
  eval)   exec python evaluation/run_eval.py --round "${2:-3}" ;;
  hitl)   exec python evaluation/hitl_scenarios.py ;;
  ragas)  exec python evaluation/ragas_eval.py --round "${2:-3}" ;;
  reset)  rm -rf state && echo "시연 상태를 초기화했습니다 (state/ 삭제)." ;;
  *)      sed -n '2,9p' "$0"; exit 1 ;;
esac
