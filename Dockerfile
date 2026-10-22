# 출장비 정산 사전 검토 Agent - 클린 환경 재현용 이미지
#   빌드: docker build -t travel-settlement-agent .
#   실행: docker run --rm -p 8000:8000 --env-file .env travel-settlement-agent
#         → http://localhost:8000/ (시연 화면), POST /query (API)
#   평가: docker run --rm --env-file .env travel-settlement-agent ./run.sh test
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BASE_DATE=2026-10-21

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
 && pip install ragas==0.4.3 --no-deps

COPY src/ src/
COPY data/ data/
COPY web/ web/
COPY evaluation/ evaluation/
COPY tests/ tests/
COPY run.sh .
RUN chmod +x run.sh

EXPOSE 8000
CMD ["./run.sh", "server"]
