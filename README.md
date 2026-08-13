# live-simulator (`livesim`)

AIoT 환경자율제어 플랫폼 백엔드(`aiot-be`)를 향해 **실제 측정기처럼 24시간 데이터를 흘려보내는
독립 실행형 시뮬레이터**입니다. 프론트엔드 개발/데모, 적재 파이프라인 관찰, 대시보드의 "살아 있는
데이터" 확보를 목적으로 합니다.

DB에 붙지 않고, 관리자 계정도 쓰지 않습니다. **실제 디바이스가 가진 것만** 가지고 동작합니다 —
주입받은 `device_id`와 `secret`, 그리고 REST/MQTT 엔드포인트. 그래서 시뮬레이터가 성공하면
실제 디바이스도 성공한다고 볼 수 있습니다.

---

## 1. 전체 흐름

```
[FE 관리자 대시보드]                        [livesim]
  사이트 등록                                  │
  디바이스 등록  ──┐                           │
  시크릿 발급     │  device_id / secret       │
  (1회만 노출)    └────── 손으로 옮겨적기 ────▶ devices.yaml
                                              │
                                              │ ① POST /auth/device/token
                                              │    {device_id, secret} → JWT
                                              ▼
                            MQTT PUBLISH (username=device_id, password=JWT)
                              aiot/v1/{facility}/{site_id}/{type}/{device_id}/sensor
                                              │
                                              ▼
                            EMQX ──webhook──▶ aiot-api ──▶ TimescaleDB
```

`devices.yaml`에 값을 옮겨 적는 행위가 실물 디바이스의 **"공장/설치 시 설정 주입"**에 해당합니다.
디바이스는 자기 자격증명만 알 뿐, 관리자 계정도 다른 디바이스의 존재도 알지 못합니다.

디바이스 1대당 MQTT 커넥션 1개를 씁니다. EMQX ACL이 CONNECT 시점의 인증된 `device_id` 하나로
발행 권한을 스코프하기 때문에, 커넥션을 공유하면 다른 디바이스의 토픽 발행이 거부됩니다.

### 모듈

| 파일 | 역할 |
| --- | --- |
| `livesim/config.py` | 환경변수 `Settings`, `devices.yaml` 인벤토리, 시나리오 YAML |
| `livesim/api.py` | `POST /auth/device/token` 하나뿐 (secret → JWT 교환) |
| `livesim/profiles.py` | 센서 파형 (일주기 + 결정적 노이즈), IAQ 18종 + 생체 3종 |
| `livesim/payload.py` | 페이로드/토픽 빌더, 오버라이드 클램프 |
| `livesim/device.py` | `LiveDevice` — 발행, 오프라인 버퍼링, batch 재전송 |
| `livesim/scheduler.py` | 이벤트 상태 머신 (확률 이벤트 + 수동 조작) |
| `livesim/control.py` | 파일 기반 제어 채널 (`ctl` ↔ 러너) |
| `livesim/rehearse.py` | 보안 리허설 3케이스 |
| `livesim/runner.py` | 메인 루프 — 토큰 교환, 틱 발행, 재접속, 제어 명령 |
| `livesim/__main__.py` | CLI (`run` / `ctl` / `rehearse`) |

---

## 2. 시작하기

### 1단계 — FE에서 등록하고 시크릿 발급

관리자 대시보드에서 사이트를 만들고, 디바이스를 등록한 뒤, 디바이스 상세에서 시크릿을 발급받습니다.
**시크릿은 발급 직후 한 번만 노출**됩니다(백엔드에 해시로만 저장). 그 자리에서 옮겨 적으세요.
잃어버리면 재발급받아야 합니다.

### 2단계 — devices.yaml 작성

```bash
cp devices.example.yaml devices.yaml
```

```yaml
devices:
  - device_id: AQ-GANGNAM-01
    secret: "발급받은-시크릿"
    site_id: "550e8400-e29b-41d4-a716-446655440000"
    device_type: FIXED # FIXED | PORTABLE | WEARABLE
    facility_type: OFFICE # OFFICE|SCHOOL|DAYCARE|WELFARE|HOME|HOME_ELDERLY
```

> `devices.yaml`은 시크릿 평문을 담으므로 `.gitignore`에 등록되어 있습니다. **절대 커밋하지 마세요.**
> 커밋용 예시는 `devices.example.yaml`입니다.

### 3단계 — 실행

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements-dev.txt
cp .env.example .env            # 값 채우기 (관리자 계정 불필요)

python -m livesim --dry-run scenarios/daily-ops.yaml   # 먼저 검증
python -m livesim scenarios/daily-ops.yaml             # 발행 시작
```

| 명령 | 설명 |
| --- | --- |
| `livesim run [시나리오]` | 발행 시작. `run`은 생략 가능(0.1 호환) |
| `livesim run --dry-run` | **접속 없이** 시나리오·인벤토리 검증 후 계획만 출력 |
| `livesim run --devices N` | 발행 디바이스 수 상한 (`0`=제한 없음) |
| `livesim run --interval S` | 발행 주기(초) |
| `livesim ctl ...` | 실행 중인 러너에 수동 명령 (§4) |
| `livesim rehearse` | 보안 리허설 (§5) |

### 환경변수

| 이름 | 기본값 | 설명 |
| --- | --- | --- |
| `API_BASE_URL` | `http://localhost:8080` | 백엔드 REST 주소 |
| `MQTT_HOST` | `localhost` | EMQX 호스트 |
| `MQTT_PORT` | `1883` | EMQX MQTT 포트 |
| `DEVICES_FILE` | `devices.yaml` | 디바이스 인벤토리 경로 |
| `CONTROL_DIR` | `control` | `ctl` 명령/상태 교환 디렉터리 |

**필수 항목이 없습니다.** 관리자 계정을 요구하지 않는 것이 0.2.0의 핵심입니다.

---

## 3. 시나리오 작성법

```yaml
name: daily-ops
description: 현실적 24시간 운영 패턴
interval_seconds: 300 # 발행 주기(초), 최소 1
max_devices: 0 # 0 = 제한 없음
exclude_devices: [] # devices.yaml에 있지만 이번 실행에서 뺄 device_id
events:
  - type: dropout # 통신 단절: 버퍼링 → 복구 시 batch 재전송
    per_device_per_day: 0.2
    duration_minutes: [5, 20]
  - type: silence # 발행 중단만 (버퍼링 없음 = 실제 유실)
    per_device_per_day: 0.05
    duration_minutes: [15, 30]
  - type: alert_burst # 오염 급증
    per_device_per_day: 0.1
    duration_minutes: [10, 30]
    overrides: { pm25: 120, pm10: 180, co2: 2200, tvoc: 900 }
```

- **발생 확률**: 틱마다 디바이스별로 `p = per_device_per_day × interval_seconds / 86400`.
  주기를 바꿔도 "하루 N회"라는 의미가 유지됩니다.
- **한 디바이스 = 한 이벤트**: 이미 이벤트 중인 디바이스는 새 이벤트를 받지 않습니다.
  같은 `type`을 두 번 정의하면 검증에서 거부됩니다.
- **`alert_burst`의 값**: 목표값에 매 틱 ±10% 노이즈를 얹고 **센서 프로필의 min/max로 클램프**한 뒤
  DB 컬럼 자릿수로 반올림합니다. 과장된 목표값을 줘도 업로드 검증(422)에 걸리지 않습니다.
- **`exclude_devices`**: 자격증명을 지우지 않고 일시적으로 재우는 용도입니다. 인벤토리에서 지우면
  나중에 secret을 다시 발급받아야 하므로, 잠깐 빼는 것은 여기서 합니다.

---

## 4. `livesim ctl` — 개별 디바이스 수동 조작

시나리오의 확률 이벤트가 "언젠가 저절로" 일어나는 것을 기다리지 않고, 특정 디바이스를 지금
조작합니다. 데모나 FE 개발 중 "이 디바이스를 지금 오프라인으로 만들어 주세요" 같은 요구에 씁니다.

```bash
livesim ctl off AQ-GANGNAM-01              # 전원 off (발행 중단 + MQTT 해제, 버퍼링 없음)
livesim ctl on  AQ-GANGNAM-01              # 재기동 (재접속 + 발행 재개)
livesim ctl dropout AQ-GANGNAM-02 --minutes 15   # 통신 단절 (버퍼 → 복구 시 batch 재전송)
livesim ctl burst   AQ-GANGNAM-02 --minutes 30   # 오염 급증
livesim ctl status                          # 현재 상태 표로 출력
```

```
시나리오 daily-ops · tick 42 · 갱신 2026-08-13T10:45:22
DEVICE                 CONN   ONLINE   PEND  EVENT
--------------------------------------------------
AQ-GANGNAM-01          no     no          0  power_off (수동)
AQ-GANGNAM-02          yes    no          3  dropout (수동) 177초 남음
WB-GANGNAM-01          yes    yes         0  -
```

### off와 dropout의 차이

| | 발행 | 로컬 버퍼 | MQTT | 복구 시 |
| --- | --- | --- | --- | --- |
| `off` | 중단 | **없음** | 해제 | 이후 데이터만 발행 |
| `dropout` | 중단 | 쌓임 | 유지 | 버퍼를 batch로 일괄 재전송 |

전원이 꺼진 기기는 측정 자체를 하지 않으므로 나중에 보낼 것도 없습니다. 통신만 끊긴 기기는
측정을 계속하다가 복구되면 밀린 것을 몰아 보냅니다.

### 동작 방식

러너가 `CONTROL_DIR`을 1초마다 폴링해 명령 파일(`cmd-*.json`)을 읽고 적용한 뒤 지웁니다. 상태는
매 틱 `CONTROL_DIR/state.json`에 기록됩니다. TCP/HTTP 서버를 두지 않은 이유는, 필요한 것이
"같은 호스트(또는 같은 볼륨)에서 사람이 가끔 명령을 넣는" 것뿐이라 포트를 열면 인증·바인딩
주소·방화벽을 전부 따져야 하기 때문입니다.

**수동 조작이 확률 이벤트보다 우선합니다.** 같은 상태 머신을 공유하며, 수동 이벤트가 걸린
디바이스에는 확률 이벤트가 들어오지 않습니다. `off`는 기간이 없어 `on` 할 때까지 유지됩니다.

### Docker에서

컨테이너와 호스트가 `./control`을 공유하므로 둘 중 아무 쪽에서나 쓸 수 있습니다.

```bash
docker exec livesim python -m livesim ctl off AQ-GANGNAM-01
docker exec livesim python -m livesim ctl status
# 또는 호스트에서 (같은 디렉터리를 보므로 동일하게 동작)
livesim ctl status
```

---

## 5. `livesim rehearse` — 보안 리허설

**"들어갈 수 있는가"가 아니라 "들어갈 수 없어야 하는데 정말 막히는가"**를 확인합니다.
세 케이스 모두 **거부되어야 PASS**이고, 하나라도 통과되면 종료 코드 1입니다.

```bash
livesim rehearse
```

| 케이스 | 시도 | 기대 |
| --- | --- | --- |
| SEC-01 | 미등록 `device_id` + 임의 secret으로 토큰 교환 | 4xx 거부 |
| SEC-02 | 등록된 `device_id` + **틀린** secret으로 토큰 교환 | 4xx 거부 |
| SEC-03 | 위조 JWT(무작위 문자열)로 MQTT CONNECT | CONNACK 거부 |

실제 secret은 어떤 케이스에서도 전송하지 않습니다. 5xx나 네트워크 오류는 "막혔다"는 증거가
아니므로 PASS로 세지 않고 "확인 불가"로 실패 처리합니다.

---

## 6. Docker로 24시간 운영

```bash
cp .env.example .env                   # 값 채우기
cp devices.example.yaml devices.yaml   # 발급받은 자격증명 입력 (반드시 먼저!)
docker compose up -d --build
docker compose logs -f livesim
```

> **`devices.yaml`을 만들기 전에 `docker compose up`을 하지 마세요.** compose는 존재하지 않는
> 파일을 볼륨으로 마운트하면 같은 이름의 **디렉터리**를 만들어버립니다. 그 상태로는 기동에
> 실패하며(원인을 알려주는 메시지가 나옵니다), 그 디렉터리를 지우고 파일로 다시 만들어야 합니다.

- `devices.yaml`은 **이미지에 들어가지 않고 볼륨으로만 주입**됩니다(읽기 전용). 시크릿 평문이
  이미지에 박히면 레지스트리에서 이미지를 받을 수 있는 모두가 디바이스를 사칭할 수 있습니다.
- `./control`이 마운트되어 `docker exec ... ctl`과 호스트 `ctl`이 같은 채널을 봅니다.
- `restart: unless-stopped`로 호스트 재부팅·이상 종료 후 자동 기동합니다.
- `healthcheck`는 5분마다 `--dry-run`으로 프로세스와 설정 유효성만 확인합니다(발행 성공 여부는
  보지 않으므로 로그로 확인하세요).
- 컨테이너는 비루트(uid 10001)로 실행됩니다.

호스트에서 돌고 있는 백엔드/EMQX에 붙을 때는 `.env`에 `host.docker.internal`을 씁니다.

```
API_BASE_URL=http://host.docker.internal:8080
MQTT_HOST=host.docker.internal
```

### 로그 읽는 법

```
2026-08-13 10:45:30 INFO tick 6: 발행 21, 버퍼 1, 재전송 0, 침묵 0, 전원off 1, 미접속 0, 실패 0, 비활성 0
```

| 항목 | 의미 |
| --- | --- |
| 발행 | 브로커로 나간 단건 |
| 버퍼 | dropout 중이라 로컬에 쌓인 건수 |
| 재전송 | dropout 종료로 batch 재전송된 건수 |
| 침묵 | silence로 건너뛴 디바이스 수 |
| 전원off | `ctl off` 상태인 디바이스 수 |
| 미접속 | 접속 실패로 이번 틱을 건너뜀 (백오프 재시도 중) |
| 실패 | 발행 중 예외 (커넥션을 버리고 재교환) |
| 비활성 | **자격증명이 거부되어 제외됨** — `devices.yaml` 확인 후 재기동 필요 |

### 장애 복구 동작

- **자격증명 거부(4xx)**: 재시도해도 같으므로 그 디바이스만 접고 나머지는 계속 발행합니다.
  `devices.yaml`의 secret을 고치고 재기동해야 합니다.
- **네트워크·5xx 실패**: 일시 장애로 보고 지수 백오프(5초 → 최대 5분)로 재시도합니다.
- 재접속 시 **디바이스 JWT를 새로 교환**합니다. paho의 자동 재접속은 저장된 옛 password를
  그대로 재사용하므로, 토큰이 만료되면 영원히 붙지 못하기 때문입니다.
- 오프라인 버퍼는 재접속을 넘어 유지되며, 상한(288건 = 5분 주기 24시간)을 넘으면 가장 오래된
  측정값부터 버립니다.
- `SIGINT`/`SIGTERM`(= `docker stop`)에 모든 커넥션을 정리하고 종료합니다.

---

## 7. `captured_at`을 naive KST로 보내는 이유

```
"captured_at": "2026-08-13T14:30:00"       ← livesim이 보내는 형식
"captured_at": "2026-08-13T14:30:00+09:00" ← 보내면 안 되는 형식
```

백엔드의 `FlexibleLocalDateTimeDeserializer`는 오프셋을 **파싱한 뒤 버리고** 숫자만
`timestamptz`(UTC) 컬럼에 저장합니다. `+09:00`을 붙여 보내면 값이 9시간 밀려 저장되고, 보낸
`captured_at`으로는 그 행을 다시 찾을 수 없습니다. **백엔드 결함에 대한 우회이며, 백엔드가
고쳐지면 이 동작도 함께 바꿔야 합니다.**

---

## 8. ⚠ 백엔드 버전 커플링

**이 저장소는 `aiot-be` 0.1.1 기준으로 검증되었습니다.** 계약이 깨지면 대개 "발행은 성공하는데
데이터가 안 쌓이는" 조용한 실패로 나타나므로, 백엔드 스키마 변경 시 반드시 점검하세요.

| 계약 | 위치 | 깨졌을 때 증상 |
| --- | --- | --- |
| 토픽 6-세그먼트 포맷 | `payload.build_topic` | EMQX 룰 미매칭 → 적재 안 됨(발행은 성공) |
| IAQ 18종 + 생체 3종 필드명·범위 | `profiles.SENSOR_PROFILES` | 업로드 검증 422 또는 컬럼 누락 |
| 디바이스 타입별 필드 구성 | `profiles.DEVICE_FIELDS` | 미탑재 센서가 실리거나 필수 값 누락 |
| 페이로드 snake_case 키 | `payload.build_payload` | 역직렬화 실패 |
| `captured_at` naive KST (§7) | `runner.kst_now` | 적재 시각 9시간 밀림 |
| `POST /auth/device/token` → `access_token` | `api.exchange_device_token` | 전 디바이스 미접속 |
| 배치 토픽 `{"readings": [...]}` | `device.LiveDevice.go_online` | dropout 재전송분만 유실 |

`SENSOR_PROFILES`의 `minimum`/`maximum`은 업로드 DTO의 `@DecimalMin`/`@DecimalMax` 안에,
`decimals`는 DB 컬럼의 `NUMERIC(x,y)`에 맞춰져 있습니다.

---

## 9. 0.1 → 0.2 변경점

**관리자 계정 의존을 완전히 제거했습니다.** 0.1은 admin으로 로그인해 디바이스 목록을 조회하고
시크릿을 발급받았지만, **실제 디바이스는 관리자 계정을 알지 못합니다.** 등록과 발급은 사람이 FE
대시보드에서 하는 일이고, 디바이스는 주입받은 자격증명으로 토큰만 교환합니다. 0.2는 그 경계를
그대로 따릅니다 — 시뮬레이터가 실제 디바이스보다 더 큰 권한을 갖고 있으면, 시뮬레이터가 통과해도
실제 디바이스가 통과한다는 보장이 없습니다.

| 항목 | 0.1 | 0.2 |
| --- | --- | --- |
| 자격증명 | `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `devices.yaml` (device_id + secret) |
| 디바이스 탐색 | `GET /admin/devices`, `/admin/sites` | 인벤토리 파일 |
| 프로비저닝 | admin 로그인 → 시크릿 발급 → 토큰 교환 | 토큰 교환만 |
| 대상 선별 | `status != MAINTENANCE` 자동 | 인벤토리에 적은 것만 |
| 수동 조작 | 없음 | `livesim ctl` |
| 보안 점검 | 없음 | `livesim rehearse` |

**끊긴 하위 호환**

- `ADMIN_USERNAME` / `ADMIN_PASSWORD` 환경변수는 더 이상 읽지 않습니다(있어도 무시).
- `devices.yaml`이 없으면 시작하지 않습니다. 0.1은 백엔드에서 목록을 받아왔지만 이제 파일이
  진실의 원천입니다.
- `livesim.api.AdminApi`와 `DeviceRecord`가 사라졌습니다. `DeviceRecord`의 자리는
  `config.DeviceCredential`이 대신합니다.
- 시나리오의 `exclude_devices` 의미가 "시드에서 꺼둔 디바이스 보호"에서 "인벤토리 항목 일시
  비활성"으로 바뀌었습니다. 기본 시나리오의 예시 값은 비웠습니다.

**유지되는 것**: `python -m livesim [시나리오] [--dry-run]` 실행 형태, 페이로드·토픽 포맷,
파형 생성, 오프라인 버퍼링과 batch 재전송, 재접속 내구성.

---

## 10. 테스트

```bash
pytest
```

모든 테스트는 네트워크 없이 동작합니다. HTTP는 `requests-mock`, MQTT는 페이크 publisher,
제어 채널은 임시 디렉터리, 이벤트 스케줄러는 주입한 난수 생성기로 검증합니다.
