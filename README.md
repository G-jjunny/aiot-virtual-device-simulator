# live-simulator (`livesim`)

AIoT 환경자율제어 플랫폼 백엔드(`aiot-be`)를 향해 **실제 측정기처럼 24시간 데이터를 흘려보내는
독립 실행형 시뮬레이터**입니다. 프론트엔드 개발/데모, 적재 파이프라인 관찰, 대시보드의 "살아 있는
데이터" 확보를 목적으로 합니다.

DB에 직접 붙지 않습니다. 백엔드가 실제 디바이스에게 제공하는 경로 — **REST API와 MQTT** — 만
사용하므로, 시뮬레이터가 성공하면 실제 디바이스도 성공한다고 볼 수 있습니다.

---

## 1. 아키텍처

```
livesim (디바이스 N대, 각각 별도 MQTT 커넥션)
   │
   │  ① 관리자 로그인 (REST)           POST /auth/login
   │  ② 디바이스 목록/사이트 목록 조회   GET  /admin/devices, /admin/sites
   │  ③ 디바이스별 MQTT JWT 프로비저닝   POST /admin/devices/{id}/secret
   │                                    POST /auth/device/token
   ▼
 MQTT PUBLISH (username=device_id, password=device JWT)
   │   aiot/v1/{facility}/{site_id}/{device_type}/{device_id}/sensor
   ▼
 EMQX  ──webhook──▶  aiot-api  ──▶  TimescaleDB
```

디바이스 1대당 MQTT 커넥션 1개를 씁니다. EMQX ACL이 CONNECT 시점의 인증된 `device_id` 하나로
발행 권한을 스코프하기 때문에, 커넥션을 공유하면 다른 디바이스의 토픽 발행이 거부됩니다.

### 모듈

| 파일 | 역할 |
| --- | --- |
| `livesim/config.py` | 환경변수 `Settings`, 시나리오 YAML 로더/검증 |
| `livesim/api.py` | `AdminApi` — 로그인, 디바이스/사이트 조회, 디바이스 JWT 프로비저닝 |
| `livesim/profiles.py` | 센서 파형 (일주기 + 결정적 노이즈), IAQ 18종 + 생체 3종 |
| `livesim/payload.py` | 페이로드/토픽 빌더, 오버라이드 클램프 |
| `livesim/device.py` | `LiveDevice` — 발행, 오프라인 버퍼링, batch 재전송 |
| `livesim/scheduler.py` | 24시간 이벤트 엔진 (dropout / silence / alert_burst) |
| `livesim/runner.py` | 메인 루프 — 탐색, 접속, 틱 발행, 재접속, 정상 종료 |
| `livesim/__main__.py` | CLI |

---

## 2. 설치 및 실행

### 로컬 (개발)

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS/Linux

pip install -r requirements-dev.txt
cp .env.example .env            # 값 채우기
pytest
```

`.env`를 셸에 로드한 뒤 실행합니다 (livesim은 `.env`를 직접 읽지 않습니다 — 컨테이너에서는
docker compose의 `env_file`이 주입합니다).

```bash
python -m livesim                                   # scenarios/steady.yaml
python -m livesim scenarios/daily-ops.yaml
python -m livesim scenarios/stress.yaml --devices 10 --interval 60
python -m livesim scenarios/daily-ops.yaml --dry-run
```

| 옵션 | 설명 |
| --- | --- |
| `scenario` | 시나리오 YAML 경로 (생략 시 `scenarios/steady.yaml`) |
| `--devices N` | 발행 디바이스 수 상한. `0`이면 제한 없음 |
| `--interval S` | 발행 주기(초) |
| `--dry-run` | **접속 없이** 시나리오를 검증하고 발행 계획만 출력 후 종료 |
| `--log-level` | 기본 `INFO` |

`--dry-run`은 환경변수 없이도 동작합니다. 시나리오 lint와 컨테이너 헬스체크 용도입니다.

### 환경변수

| 이름 | 기본값 | 설명 |
| --- | --- | --- |
| `API_BASE_URL` | `http://localhost:8080` | 백엔드 REST 주소 |
| `MQTT_HOST` | `localhost` | EMQX 호스트 |
| `MQTT_PORT` | `1883` | EMQX MQTT 포트 |
| `ADMIN_USERNAME` | (필수) | 디바이스 시크릿 발급 권한이 있는 관리자 계정 |
| `ADMIN_PASSWORD` | (필수) | 위 계정의 비밀번호 |

> 관리자 계정은 `POST /admin/devices/{id}/secret` 권한이 필요합니다. 권한이 없으면 프로비저닝이
> 403으로 실패하며, livesim은 해당 디바이스를 백오프로 계속 재시도합니다.

---

## 3. 시나리오 작성법

```yaml
name: daily-ops                  # 필수
description: 현실적 24시간 운영 패턴
interval_seconds: 300            # 발행 주기(초), 최소 1. 기본 300
max_devices: 0                   # 0 = 제한 없음
exclude_devices:                 # 발행에서 뺄 device_id
  - AQ-SONGPA-02
  - AQ-SONGPA-03
events:
  - type: dropout                # MQTT 단절 모의: 로컬 버퍼링 → 복구 시 batch 재전송
    per_device_per_day: 0.2      # 디바이스 1대당 하루 기대 발생 횟수
    duration_minutes: [5, 20]    # [최소, 최대] 균등 랜덤

  - type: silence                # 발행 중단만 (버퍼링 없음 = 실제 유실)
    per_device_per_day: 0.05
    duration_minutes: [15, 30]

  - type: alert_burst            # 오염 급증
    per_device_per_day: 0.1
    duration_minutes: [10, 30]
    overrides: {pm25: 120, pm10: 180, co2: 2200, tvoc: 900}
```

### 이벤트 3종

| type | 동작 |
| --- | --- |
| `dropout` | 발행을 로컬 버퍼에 쌓아두고, 이벤트가 끝나면 `.../sensor/batch` 토픽으로 일괄 재전송합니다. 재전송 경로와 백엔드의 배치 적재를 검증합니다. |
| `silence` | 아무것도 하지 않습니다. 버퍼링도 없으므로 그 구간은 영구 유실됩니다. 데이터 결손 구간이 있는 대시보드를 만들 때 씁니다. |
| `alert_burst` | 지정한 센서 값을 목표값 부근으로 끌어올립니다. 경보/임계 UI를 확인할 때 씁니다. |

### 동작 규칙

- **발생 확률**: 틱마다 디바이스별로 `p = per_device_per_day × interval_seconds / 86400`
  확률로 이벤트가 시작됩니다. 주기를 바꿔도 "하루 N회"라는 의미가 유지됩니다.
- **한 디바이스 = 한 이벤트**: 이미 이벤트 중인 디바이스는 새 이벤트를 받지 않습니다.
  `events` 목록 순서가 우선순위이며, 같은 `type`을 두 번 정의하면 검증에서 거부됩니다.
- **지속 시간**: `duration_minutes` 범위에서 균등 랜덤으로 뽑아 종료 시각을 정하고, 틱마다 만료를
  확인합니다.
- **`alert_burst`의 값**: 목표값에 매 틱 ±10% 노이즈를 얹은 뒤 **센서 프로필의 min/max로
  클램프**하고 DB 컬럼 자릿수로 반올림합니다. 과장된 목표값을 줘도 업로드 DTO 검증(422)에 걸리지
  않습니다. `overrides`에 알 수 없는 센서 이름을 쓰면 로딩 단계에서 거부됩니다(오타 방지).
- **측정하지 않는 센서는 무시**: 웨어러블에 `pm25` 오버라이드를 걸어도 페이로드에 끼워 넣지
  않습니다.

---

## 4. 디바이스 선별 정책

livesim은 **백엔드에 이미 등록된 디바이스**에만 발행합니다. 디바이스나 사이트를 새로 만들지
않습니다.

1. `GET /admin/devices`, `GET /admin/sites`를 페이지 끝까지 조회합니다.
2. `status`가 **`MAINTENANCE`인 디바이스만 제외**합니다.
3. 시나리오의 `exclude_devices`에 있는 디바이스를 제외합니다.
4. 사이트를 찾을 수 없는 디바이스(미배정, 삭제됨)를 제외하고 경고를 남깁니다.
5. `device_id` 오름차순 정렬 후 `max_devices`만큼 자릅니다.

> **`OFFLINE`을 제외하지 않는 이유**: 백엔드의 `DeviceHealthChecker`는 10분 이상 응답이 없는
> 디바이스를 자동으로 `OFFLINE`으로 내립니다. 시뮬레이터를 몇 시간 멈춰 두면 모든 디바이스가
> `OFFLINE`이 되므로, `ONLINE`만 고르는 방식은 재시작 시 **0대가 잡히는** 부트스트랩 결함이
> 있습니다. `OFFLINE` 디바이스에 발행하면 적재 트리거가 다시 `ONLINE`으로 되살리는 것이 의도된
> 복구 경로입니다.
>
> 반대로, 시드에서 **일부러 꺼 둔** 디바이스는 발행하면 되살아나 버립니다. 그런 디바이스는
> `exclude_devices`에 넣으세요 (`steady.yaml`, `daily-ops.yaml`에 `AQ-SONGPA-02/03`이 기본
> 기재되어 있습니다).

---

## 5. `captured_at`을 naive KST로 보내는 이유

livesim은 `captured_at`을 **오프셋 없는 현재 KST 벽시계 문자열**로 보냅니다.

```
"captured_at": "2026-08-13T14:30:00"      ← livesim이 보내는 형식
"captured_at": "2026-08-13T14:30:00+09:00" ← 보내면 안 되는 형식
```

백엔드의 `FlexibleLocalDateTimeDeserializer`는 오프셋을 **파싱한 뒤 버리고** 숫자만
`timestamptz`(UTC) 컬럼에 저장합니다. 따라서 `+09:00`을 붙여 보내면 값이 9시간 밀려 저장되고,
보낸 `captured_at`으로는 그 행을 다시 찾을 수 없습니다.

오프셋 없이 보내면 저장된 숫자가 KST 벽시계와 일치해, 실제 디바이스가 남기는 데이터 및 시드
히스토리와 같은 시간축에 놓입니다. **이것은 백엔드 결함에 대한 우회이며, 백엔드가 고쳐지면 이
동작도 함께 바꿔야 합니다.**

---

## 6. Docker로 24시간 운영

```bash
cp .env.example .env    # 값 채우기
docker compose up -d --build
docker compose logs -f livesim
```

- `restart: unless-stopped` — 호스트 재부팅이나 프로세스 이상 종료 후 자동 재기동합니다.
- `healthcheck` — 5분마다 `--dry-run`으로 프로세스와 시나리오 유효성을 확인합니다.
  (발행 성공 여부까지 보지는 않습니다. 발행 상태는 로그로 확인하세요.)
- 로그 로테이션 10MB × 5개가 설정되어 있습니다.
- 컨테이너는 비루트(uid 10001)로 실행됩니다.

호스트에서 돌고 있는 백엔드/EMQX에 붙을 때는 `.env`에 `host.docker.internal`을 씁니다
(compose에 `extra_hosts` 매핑이 되어 있습니다).

```
API_BASE_URL=http://host.docker.internal:8080
MQTT_HOST=host.docker.internal
MQTT_PORT=1883
```

시나리오를 바꾸려면 compose의 `command`와 `healthcheck`를 함께 수정하세요.

### 로그 읽는 법

틱마다 한 줄이 나옵니다.

```
2026-08-13 14:30:00,123 INFO tick 12: 발행 21, 버퍼 1, 재전송 0, 침묵 0, 미접속 0, 실패 0
```

| 항목 | 의미 |
| --- | --- |
| 발행 | 브로커로 나간 단건 |
| 버퍼 | dropout 중이라 로컬에 쌓인 건수 |
| 재전송 | dropout 종료로 batch 재전송된 건수 |
| 침묵 | silence로 건너뛴 디바이스 수 |
| 미접속 | 접속 실패로 이번 틱을 건너뛴 디바이스 수 (백오프 재시도 중) |
| 실패 | 발행 중 예외가 난 디바이스 수 (커넥션을 버리고 재프로비저닝) |

### 장애 복구 동작

- 접속/프로비저닝 실패는 **해당 디바이스만** 건너뛰고, 나머지는 정상 발행합니다.
- 재시도는 지수 백오프(5초 → 최대 5분)로 밀립니다.
- 재접속 시 **디바이스 JWT를 새로 발급**받습니다. paho의 자동 재접속은 저장된 옛 password를
  그대로 재사용하므로, 토큰이 만료되면 영원히 붙지 못하기 때문입니다.
- 관리자 토큰(1시간 만료)은 401을 받으면 자동으로 재로그인 후 요청을 재시도합니다.
- 오프라인 버퍼는 재접속을 넘어 유지되며, 상한(288건 = 5분 주기 24시간)을 넘으면 가장 오래된
  측정값부터 버립니다.
- `SIGINT`/`SIGTERM`(= `docker stop`)을 받으면 모든 커넥션을 정리하고 종료합니다.

---

## 7. ⚠ 백엔드 버전 커플링

**이 저장소는 `aiot-be` 0.1.1 기준으로 검증되었습니다.** 아래 항목은 백엔드와 계약으로 묶여
있으며, 백엔드가 바뀌면 여기도 함께 고쳐야 합니다. 계약이 깨지면 대개 "발행은 성공하는데 데이터가
안 쌓이는" 조용한 실패로 나타나므로, 백엔드 스키마 변경 시 반드시 이 목록을 점검하세요.

| 계약 | 위치 | 깨졌을 때 증상 |
| --- | --- | --- |
| 토픽 6-세그먼트 포맷 `aiot/v1/{facility}/{site}/{type}/{id}/sensor` | `payload.build_topic` | EMQX 룰이 매칭되지 않아 적재 안 됨 (발행은 성공) |
| IAQ 18종 + 생체 3종 필드명과 값 범위 | `profiles.SENSOR_PROFILES` | 업로드 DTO 검증 422 또는 컬럼 누락 |
| 디바이스 타입별 필드 구성 (FIXED/PORTABLE/WEARABLE) | `profiles.DEVICE_FIELDS` | 미탑재 센서 값이 들어가거나 필수 값 누락 |
| 페이로드 snake_case 키 (`device_id`, `captured_at` …) | `payload.build_payload` | 역직렬화 실패 |
| `captured_at` naive KST (§5) | `runner.kst_now` | 적재 시각 9시간 밀림 |
| 프로비저닝 2단계 API와 응답 키 (`secret`, `access_token`) | `api.AdminApi` | MQTT 인증 실패로 전 디바이스 미접속 |
| 목록 API의 `ReadableList` envelope (`records`/`totalPages`) | `api.AdminApi._list_all` | 디바이스 탐색 실패 (`envelope가 아닙니다` 오류) |
| 배치 토픽 페이로드 `{"readings": [...]}` | `device.LiveDevice.go_online` | dropout 재전송분만 유실 |

`SENSOR_PROFILES`의 `minimum`/`maximum`은 백엔드 업로드 DTO의 `@DecimalMin`/`@DecimalMax` 안에,
`decimals`는 DB 컬럼의 `NUMERIC(x,y)`에 맞춰져 있습니다. 둘 중 하나라도 바뀌면 프로필을 함께
조정해야 합니다.

---

## 8. 테스트

```bash
pytest              # 전체
pytest -q tests/test_scheduler.py
```

모든 테스트는 네트워크 없이 동작합니다. HTTP는 `requests-mock`으로, MQTT는 페이크 publisher로
대체하며, 이벤트 스케줄러는 난수 생성기를 주입해 결정적으로 검증합니다.
