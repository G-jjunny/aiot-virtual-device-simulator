FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY livesim ./livesim
COPY scenarios ./scenarios

# devices.yaml은 이미지에 넣지 않는다 — 시크릿 평문이므로 레지스트리에 올라가면
# 이미지를 받을 수 있는 모두가 디바이스를 사칭할 수 있다. 볼륨으로만 주입한다.

# 비루트 실행. control/은 러너가 state.json을 쓰고 ctl이 명령 파일을 넣는 곳이라
# 미리 만들어 소유권을 넘긴다 (볼륨이 덮어써도 마운트 권한이 맞아야 한다).
RUN useradd --create-home --uid 10001 livesim \
    && mkdir -p /app/control \
    && chown -R livesim:livesim /app
USER livesim

ENTRYPOINT ["python", "-m", "livesim"]
CMD ["scenarios/daily-ops.yaml"]
