FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY livesim ./livesim
COPY scenarios ./scenarios

# 비루트 실행. 쓰기 권한이 필요한 경로가 없으므로 소유권만 맞춘다.
RUN useradd --create-home --uid 10001 livesim && chown -R livesim:livesim /app
USER livesim

ENTRYPOINT ["python", "-m", "livesim"]
CMD ["scenarios/daily-ops.yaml"]
