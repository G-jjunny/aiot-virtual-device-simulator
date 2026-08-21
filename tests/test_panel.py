"""패널 HTTP 핸들러 — 임시 포트에 실제로 띄워 requests로 호출한다."""

import json
import textwrap
import threading

import pytest
import requests
import yaml

from livesim import control
from livesim.config import InventoryError, Settings, load_inventory
from livesim.panel import PanelError, append_device, build_server

INVENTORY = """
devices:
  - device_id: AQ-01
    secret: super-secret-one
    site_id: 550e8400-e29b-41d4-a716-446655440000
    device_type: FIXED
    facility_type: OFFICE
  - device_id: WB-01
    secret: super-secret-two
    site_id: 550e8400-e29b-41d4-a716-446655440000
    device_type: WEARABLE
    facility_type: SCHOOL
"""


@pytest.fixture
def env(tmp_path):
    devices = tmp_path / "devices.yaml"
    devices.write_text(textwrap.dedent(INVENTORY), encoding="utf-8")
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    return Settings(
        api_base_url="http://api",
        mqtt_host="broker",
        mqtt_port=1883,
        devices_file=str(devices),
        control_dir=str(control_dir),
    )


@pytest.fixture
def panel(env):
    server = build_server(env, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, env
    server.shutdown()
    server.server_close()


# ---- 페이지 ------------------------------------------------------------


def test_index_serves_html(panel):
    base, _ = panel
    res = requests.get(base + "/", timeout=5)

    assert res.status_code == 200
    assert "text/html" in res.headers["Content-Type"]
    assert "livesim 패널" in res.text


def test_page_has_no_external_references(panel):
    """오프라인에서 동작해야 한다 — CDN/폰트를 물면 시연 중에 깨진다."""
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    for marker in ("http://", "https://", "//cdn", "fonts.googleapis"):
        assert marker not in body, marker


def test_page_corrects_for_clock_skew(panel):
    """서버가 적은 잔여값을 그대로 쓰면 브라우저 시계가 어긋날 때 틀어진다."""
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "clockOffset" in body
    assert "syncClock(s.server_now)" in body


def test_page_does_not_use_written_at_as_the_time_base(panel):
    """회귀 방지: written_at으로 시계를 맞추면 카운트다운이 멈추거나 부풀었다.

    폴링마다 다시 맞추면 serverNow()가 늘 written_at으로 되돌아가 얼어붙고,
    한 번만 맞추면 그 파일이 낡은 만큼(최대 한 틱=5분) 남은시간이 부풀었다.
    실제로 두 번 다 브라우저에서 확인된 버그다.
    """
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "syncClock(s.written_at)" not in body
    assert "Date.now()/1000-s.written_at" not in body


def test_state_stamps_a_fresh_server_clock(panel):
    """written_at은 마지막 틱 시점이라 최대 한 틱(기본 5분) 낡았다.

    그걸 시계 기준으로 쓰면 갓 연 패널이 남은시간을 그만큼 부풀려 보여준다.
    """
    base, env = panel
    control.write_state(env.control_dir, {"tick": 1, "written_at": 1000.0,
                                          "devices": []})

    body = requests.get(base + "/api/state", timeout=5).json()

    assert body["written_at"] == 1000.0          # 러너가 적은 값은 그대로 전달
    assert body["server_now"] > 1_700_000_000    # 응답 시점의 실제 시각
    assert body["server_now"] != body["written_at"]


def test_server_now_is_present_even_without_a_runner(panel):
    base, _ = panel
    body = requests.get(base + "/api/state", timeout=5).json()

    assert body["running"] is False
    assert "server_now" in body


def test_page_counts_down_every_second_separately_from_polling(panel):
    """데이터는 2초 폴링이지만 남은시간은 매초 다시 계산해야 흐른다."""
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "tickCountdowns();" in body
    assert "},1000);" in body
    assert "setInterval(refresh,2000)" in body


def test_page_uses_absolute_end_time_for_countdown(panel):
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "event_ends_at" in body
    assert "data-ends" in body


def test_page_explains_the_wait_after_zero(panel):
    """0이 되어도 스케줄러가 틱 경계에서 걷을 때까지 배지가 남는다."""
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "종료 대기(다음 틱)" in body
    assert "복구·재전송 대기(다음 틱)" in body  # dropout은 더 구체적으로


def test_page_has_type_filter_tabs(panel):
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    for label in ("전체", "고정형", "이동형", "웨어러블"):
        assert label in body, label
    assert "renderTabs" in body
    assert "selectType" in body


def test_tab_selection_survives_re_render(panel):
    """2초 폴링·1초 카운트다운이 카드를 다시 그린다.

    선택을 DOM에서 읽으면 갱신마다 '전체'로 튕기므로 JS 변수에 둬야 한다.
    """
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "let activeType" in body
    assert "localStorage" in body          # 새로고침 후에도 유지
    assert "livesim.panel.type" in body


def test_tabs_filter_by_device_type(panel):
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "matchesType" in body
    # 유형 필터와 사이트 필터가 함께 걸린다
    assert "matchesType(d)&&matchesSite(d)" in body
    # 구버전 상태(device_type 없음)는 '전체'에서만 보여야 한다.
    assert "activeType==='ALL'" in body


def test_unknown_route_is_404(panel):
    base, _ = panel
    assert requests.get(base + "/nope", timeout=5).status_code == 404


# ---- /api/state --------------------------------------------------------


def test_state_reports_not_running_without_runner(panel):
    base, _ = panel
    body = requests.get(base + "/api/state", timeout=5).json()

    assert body["running"] is False


def test_state_passes_through_runner_snapshot(panel):
    base, env = panel
    control.write_state(env.control_dir, {"tick": 7, "scenario": "daily-ops",
                                          "devices": [{"device_id": "AQ-01"}]})

    body = requests.get(base + "/api/state", timeout=5).json()

    assert body["running"] is True
    assert body["tick"] == 7
    assert body["devices"][0]["device_id"] == "AQ-01"


# ---- /api/inventory (조회) ---------------------------------------------


def test_inventory_lists_devices(panel):
    base, _ = panel
    body = requests.get(base + "/api/inventory", timeout=5).json()

    assert [d["device_id"] for d in body["devices"]] == ["AQ-01", "WB-01"]
    assert body["devices"][0]["facility_type"] == "OFFICE"


def test_inventory_never_exposes_secrets(panel):
    """패널 응답에 평문 시크릿이 나가면 화면 공유 한 번으로 유출된다."""
    base, _ = panel
    res = requests.get(base + "/api/inventory", timeout=5)

    assert "super-secret-one" not in res.text
    assert "secret" not in res.json()["devices"][0]


# ---- /api/cmd ----------------------------------------------------------


def test_cmd_writes_a_command_file(panel):
    base, env = panel
    res = requests.post(
        base + "/api/cmd",
        json={"type": "dropout", "device_id": "AQ-01", "minutes": 5},
        timeout=5,
    )

    assert res.status_code == 200
    commands = control.drain_commands(env.control_dir)
    assert commands == [control.Command("dropout", "AQ-01", 5.0)]


def test_cmd_accepts_reload_without_device_id(panel):
    base, env = panel
    res = requests.post(base + "/api/cmd", json={"type": "reload"}, timeout=5)

    assert res.status_code == 200
    assert control.drain_commands(env.control_dir)[0].command == "reload"


def test_cmd_forwards_burst_overrides(panel):
    base, env = panel
    res = requests.post(
        base + "/api/cmd",
        json={"type": "burst", "device_id": "AQ-01", "minutes": 60,
              "overrides": {"pm25": 150, "co2": 3000}},
        timeout=5,
    )

    assert res.status_code == 200
    command = control.drain_commands(env.control_dir)[0]
    assert command.minutes == 60.0
    assert command.overrides == {"pm25": 150.0, "co2": 3000.0}


def test_cmd_rejects_unknown_sensor_with_a_reason(panel):
    base, env = panel
    res = requests.post(
        base + "/api/cmd",
        json={"type": "burst", "device_id": "AQ-01", "overrides": {"nope": 1}},
        timeout=5,
    )

    assert res.status_code == 422
    assert "알 수 없는 센서" in res.json()["error"]
    assert control.drain_commands(env.control_dir) == []


def test_cmd_rejects_non_numeric_target(panel):
    base, _ = panel
    res = requests.post(
        base + "/api/cmd",
        json={"type": "burst", "device_id": "AQ-01", "overrides": {"pm25": "높게"}},
        timeout=5,
    )

    assert res.status_code == 422
    assert "숫자" in res.json()["error"]


def test_page_embeds_sensor_metadata(panel):
    """프로필은 파이썬이 진실 — JS에 값을 복사해 두면 범위 표시가 어긋난다."""
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "const META=" in body
    assert '"burst_defaults"' in body
    assert '"pm25"' in body and '"heart_rate"' in body   # 센서 범위
    assert '"WEARABLE"' in body                          # 유형별 필드


def test_page_has_inline_event_forms(panel):
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "dropoutForm" in body and "burstForm" in body
    assert "toggleForm" in body
    assert "지속(분)" in body
    assert "항목 추가" in body
    assert "pointsHint" in body        # 주기 대비 포인트 수 안내
    assert "let openForm" in body      # 폴링 재렌더에도 열린 폼이 유지되도록


def test_editing_card_is_excluded_from_rerender(panel):
    """2초 폴링이 열린 폼을 매번 파괴하면 편집 자체가 불가능해진다."""
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "function renderGrid" in body
    assert "editingId()" in body
    assert "data-did=" in body                          # 카드별 식별자
    assert "cardProtected(node,d.device_id)" in body    # 편집 중 카드 건너뛰기


def test_other_cards_keep_updating_while_editing(panel):
    """전체 정지 금지 — 보호된 단위 하나만 건너뛴다."""
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    # 서명이 바뀐 카드는 다시 그리고, 같으면 손대지 않는다
    assert "node.dataset.sig===signature" in body
    assert "node.outerHTML=cardHtml(d,interval,signature)" in body


def test_editing_card_shows_paused_notice(panel):
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "갱신 일시정지" in body


def test_countdown_skips_the_editing_card(panel):
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "dataset.did===keep" in body


# ---- 상태 3분류 표시 ----------------------------------------------------


def test_pending_connect_is_distinct_from_power_off(panel):
    """주입 직후 '접속 대기'가 전원 off처럼 보여 꺼진 줄 알았다는 제보."""
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "function isPendingConnect" in body
    assert "접속 대기(다음 틱)" in body
    assert ".card.wait" in body       # 전용 스타일 (하늘색 점선)
    assert ".badge.wait" in body


def test_three_status_classes_are_ordered(panel):
    """disabled → power_off → 접속 대기 순으로 판정해야 서로 겹치지 않는다."""
    body = requests.get(panel[0] + "/", timeout=5).text

    order = [
        body.index("if(d.disabled) return 'bad';"),
        body.index("if(d.event==='power_off') return 'offp';"),
        body.index("if(isPendingConnect(d)) return 'wait';"),
    ]
    assert order == sorted(order)


def test_pending_connect_requires_no_event_and_not_disabled(panel):
    """이벤트가 있거나 비활성이면 '대기'가 아니다."""
    body = requests.get(panel[0] + "/", timeout=5).text

    assert "!d.disabled && !d.connected && !d.event" in body


def test_inject_message_explains_the_wait(panel):
    body = requests.get(panel[0] + "/", timeout=5).text

    assert "다음 틱(최대 발행주기)에 접속합니다" in body


# ---- 포커스 가드 (리렌더 진입점 일반화) ---------------------------------


def test_focus_guard_exists_at_the_rerender_entry(panel):
    """컨트롤이 늘 때마다 플래그를 다는 대신 진입점에서 한 번에 막는다.

    같은 실패가 두 번 났다 — 편집 폼, 그리고 0.4.0의 프로파일 셀렉트·사이트 필터.
    """
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "function holdsFocus" in body
    assert "document.activeElement" in body
    assert "node.contains(active)" in body


def test_every_rerender_unit_is_marked(panel):
    """단위마다 data-unit이 붙어 있어야 새 컨트롤도 자동으로 보호된다."""
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    for unit in ('data-unit="tabs"', 'data-unit="sitebar"',
                 'data-unit="card"', 'data-unit="inject"'):
        assert unit in body, unit


def test_units_skip_when_unchanged_then_when_focused(panel):
    """서명이 같으면 무작업, 다르면 포커스 여부로 스킵 — paintUnit 한 곳에서."""
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "function paintUnit" in body
    assert "node.dataset.sig===signature" in body
    assert "if(!force && holdsFocus(node)) return false" in body


def test_toolbar_units_go_through_the_guard(panel):
    """탭·사이트 필터도 카드와 같은 경로로 보호된다."""
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "paintUnit($('#tabs')" in body
    assert "paintUnit($('#sitebar')" in body


def test_guard_ignores_buttons(panel):
    """버튼은 클릭 후에도 포커스를 유지한다.

    버튼까지 가드에 세면 [전원 on]을 한 번 누른 카드가 영원히 스킵되고
    '갱신 일시정지' 배지가 붙박이가 된다 — 실제로 그랬다.
    """
    body = requests.get(panel[0] + "/", timeout=5).text

    assert "function isEditingControl" in body
    assert "isEditingControl(active)" in body
    assert "'button','submit','reset','checkbox','radio'" in body


def test_action_buttons_release_focus(panel):
    """가드가 입력류만 보긴 하지만, 남은 포커스 링도 편집 중처럼 보인다."""
    body = requests.get(panel[0] + "/", timeout=5).text

    assert "function releaseButton" in body
    for caller in ("async function cmd", "async function bulkProfile",
                   "function toggleForm"):
        start = body.index(caller)
        assert "releaseButton()" in body[start:start + 400], caller


def test_pause_notice_is_debounced(panel):
    """셀렉트를 잠깐 스친 정도로 배지가 번쩍이면 안 된다."""
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "PAUSE_NOTICE_DELAY" in body
    assert "focusedSince" in body
    assert "updatePauseNotices" in body


def test_countdown_patches_text_not_nodes(panel):
    """자주 바뀌는 값은 노드를 다시 만들지 않는다 — 보호 필요 자체를 줄인다."""
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "el.textContent=fmtLeft(left)" in body
    # 카드 서명에서 카운트다운 '텍스트'는 빠져 있어야 매초 재생성되지 않는다
    assert "function cardSignature" in body


# ---- 환경 프로파일 -----------------------------------------------------


def test_cmd_forwards_profile_change(panel):
    base, env = panel
    res = requests.post(
        base + "/api/cmd",
        json={"type": "profile", "device_id": "AQ-01", "preset": "bad"},
        timeout=5,
    )

    assert res.status_code == 200
    command = control.drain_commands(env.control_dir)[0]
    assert command.command == "profile"
    assert command.preset == "bad"


def test_cmd_forwards_site_scoped_profile(panel):
    base, env = panel
    res = requests.post(
        base + "/api/cmd",
        json={"type": "profile", "site_id": "S-1", "preset": "very_bad"},
        timeout=5,
    )

    assert res.status_code == 200
    command = control.drain_commands(env.control_dir)[0]
    assert command.site_id == "S-1"
    assert command.preset == "very_bad"


def test_cmd_rejects_unknown_preset(panel):
    base, env = panel
    res = requests.post(
        base + "/api/cmd",
        json={"type": "profile", "device_id": "AQ-01", "preset": "awful"},
        timeout=5,
    )

    assert res.status_code == 422
    assert control.drain_commands(env.control_dir) == []


def test_page_exposes_presets_and_site_controls(panel):
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert '"presets"' in body and '"very_bad"' in body
    assert "renderSiteBar" in body
    assert "bulkProfile" in body
    assert "matchesSite" in body
    assert "setProfile" in body
    assert "envBadge" in body
    assert "이 사이트 전체 적용" in body


def test_cmd_rejects_unknown_type(panel):
    base, env = panel
    res = requests.post(
        base + "/api/cmd", json={"type": "rm-rf", "device_id": "AQ-01"}, timeout=5
    )

    assert res.status_code == 422
    assert "알 수 없는 명령" in res.json()["error"]
    assert control.drain_commands(env.control_dir) == []


def test_cmd_rejects_device_command_without_target(panel):
    base, _ = panel
    res = requests.post(base + "/api/cmd", json={"type": "off"}, timeout=5)

    assert res.status_code == 422


def test_cmd_rejects_non_numeric_minutes(panel):
    base, _ = panel
    res = requests.post(
        base + "/api/cmd",
        json={"type": "burst", "device_id": "AQ-01", "minutes": "곧"},
        timeout=5,
    )

    assert res.status_code == 422


def test_malformed_json_is_422_not_500(panel):
    base, _ = panel
    res = requests.post(
        base + "/api/cmd", data=b"{broken",
        headers={"Content-Type": "application/json"}, timeout=5,
    )

    assert res.status_code == 422


# ---- /api/inventory (주입) ---------------------------------------------


NEW_DEVICE = {
    "device_id": "AQ-99",
    "secret": "fresh-secret",
    "site_id": "550e8400-e29b-41d4-a716-446655440000",
    "device_type": "FIXED",
    "facility_type": "HOME",
}


def test_inject_appends_and_requests_reload(panel):
    base, env = panel
    res = requests.post(base + "/api/inventory", json=NEW_DEVICE, timeout=5)

    assert res.status_code == 201
    assert res.json()["device_id"] == "AQ-99"

    inventory = load_inventory(env.devices_file)
    assert [item.device_id for item in inventory] == ["AQ-01", "WB-01", "AQ-99"]
    assert inventory[2].secret == "fresh-secret"
    # 사람이 두 번 조작하지 않도록 리로드가 자동으로 들어가야 한다.
    assert control.drain_commands(env.control_dir)[0].command == "reload"


def test_inject_response_does_not_echo_the_secret(panel):
    base, _ = panel
    res = requests.post(base + "/api/inventory", json=NEW_DEVICE, timeout=5)

    assert "fresh-secret" not in res.text


def test_inject_records_power_off(panel):
    """FE에서 offline인 기기를 발행 없이 등재만 해두는 경로."""
    base, env = panel
    res = requests.post(
        base + "/api/inventory", json=dict(NEW_DEVICE, power="off"), timeout=5
    )

    assert res.status_code == 201
    injected = load_inventory(env.devices_file)[-1]
    assert injected.device_id == "AQ-99"
    assert injected.starts_powered_off is True


def test_inject_defaults_to_power_on(panel):
    base, env = panel
    requests.post(base + "/api/inventory", json=NEW_DEVICE, timeout=5)

    assert load_inventory(env.devices_file)[-1].power == "on"


def test_inventory_response_exposes_power(panel):
    base, _ = panel
    requests.post(
        base + "/api/inventory", json=dict(NEW_DEVICE, power="off"), timeout=5
    )

    devices = requests.get(base + "/api/inventory", timeout=5).json()["devices"]

    assert devices[-1]["power"] == "off"
    assert devices[0]["power"] == "on"
    assert "secret" not in devices[-1]


def test_page_has_power_off_checkbox(panel):
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "전원 off로 주입" in body
    assert 'id="f_off"' in body
    assert "checked?'off':'on'" in body


def test_summary_reports_powered_off_count(panel):
    """꺼둔 기기 때문에 접속 수가 모자라 보이는 것을 설명해야 한다."""
    base, _ = panel
    body = requests.get(base + "/", timeout=5).text

    assert "전원off" in body
    assert "d.event==='power_off'" in body


def test_inject_rejects_duplicate_device_id(panel):
    base, env = panel
    duplicate = dict(NEW_DEVICE, device_id="AQ-01")

    res = requests.post(base + "/api/inventory", json=duplicate, timeout=5)

    assert res.status_code == 422
    assert "이미 등록된" in res.json()["error"]
    assert len(load_inventory(env.devices_file)) == 2


def test_inject_rejects_invalid_device_type(panel):
    base, env = panel
    res = requests.post(
        base + "/api/inventory", json=dict(NEW_DEVICE, device_type="DRONE"), timeout=5
    )

    assert res.status_code == 422
    assert len(load_inventory(env.devices_file)) == 2


def test_inject_rejects_blank_secret(panel):
    base, env = panel
    res = requests.post(
        base + "/api/inventory", json=dict(NEW_DEVICE, secret="  "), timeout=5
    )

    assert res.status_code == 422
    assert len(load_inventory(env.devices_file)) == 2


def test_rejected_injection_leaves_no_temp_file(panel):
    base, env = panel
    requests.post(
        base + "/api/inventory", json=dict(NEW_DEVICE, device_type="DRONE"), timeout=5
    )

    from pathlib import Path

    leftovers = list(Path(env.devices_file).parent.glob(".*tmp"))
    assert leftovers == []


# ---- append_device 단위 ------------------------------------------------


def test_append_preserves_existing_comments(tmp_path):
    """손으로 관리하는 파일이라 주석이 날아가면 안 된다."""
    path = tmp_path / "devices.yaml"
    path.write_text(
        "# 강남 지점 기기들\ndevices:\n"
        "  - device_id: AQ-01\n    secret: s1\n"
        "    site_id: S-1\n    device_type: FIXED\n    facility_type: OFFICE\n",
        encoding="utf-8",
    )

    append_device(path, NEW_DEVICE)

    text = path.read_text(encoding="utf-8")
    assert "# 강남 지점 기기들" in text
    assert len(load_inventory(path)) == 2


def test_append_handles_file_without_trailing_newline(tmp_path):
    path = tmp_path / "devices.yaml"
    path.write_text(
        "devices:\n  - device_id: AQ-01\n    secret: s1\n"
        "    site_id: S-1\n    device_type: FIXED\n    facility_type: OFFICE",
        encoding="utf-8",
    )

    append_device(path, NEW_DEVICE)

    assert [item.device_id for item in load_inventory(path)] == ["AQ-01", "AQ-99"]


def test_append_matches_indentless_list(tmp_path):
    """safe_dump 산출물(0칸 리스트)에 붙일 때 들여쓰기를 파일에 맞춰야 한다.

    2026-08-13 운영 결함: 새 항목을 2칸으로 하드코딩해 provision 스크립트가
    만든 indentless 파일에서 YAML 문법 오류가 났다. 들여쓰기는 감지한다.
    """
    path = tmp_path / "devices.yaml"
    path.write_text(
        "devices:\n- device_id: AQ-01\n  secret: s1\n"
        "  site_id: S-1\n  device_type: FIXED\n  facility_type: OFFICE\n",
        encoding="utf-8",
    )

    append_device(path, NEW_DEVICE)

    assert [item.device_id for item in load_inventory(path)] == ["AQ-01", "AQ-99"]


def test_append_rejects_file_without_items(tmp_path):
    """빈 인벤토리는 로더가 먼저 거부한다 — 파일은 손대지 않은 채 남아야 한다.

    (항목이 하나라도 있어야 로더를 통과하므로, append 단계의 들여쓰기 감지는
    항상 기존 항목을 찾을 수 있다.)
    """
    path = tmp_path / "devices.yaml"
    path.write_text("devices: []\n", encoding="utf-8")

    with pytest.raises(InventoryError):
        append_device(path, NEW_DEVICE)

    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"devices": []}


def test_append_explains_single_file_mount_failure(tmp_path, monkeypatch):
    """단일 파일 바인드 마운트에선 rename이 EBUSY로 막힌다 (2026-08-13 운영 결함).

    OSError 원문만 보면 원인을 알 수 없으므로, 디렉터리 마운트로 안내한다.
    원본은 그대로, 임시 파일은 정리되어야 한다.
    """
    path = tmp_path / "devices.yaml"
    original = (
        "devices:\n- device_id: AQ-01\n  secret: s1\n"
        "  site_id: S-1\n  device_type: FIXED\n  facility_type: OFFICE\n"
    )
    path.write_text(original, encoding="utf-8")

    def deny(src, dst):
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr("livesim.panel.os.replace", deny)

    with pytest.raises(PanelError, match="마운트"):
        append_device(path, NEW_DEVICE)

    assert path.read_text(encoding="utf-8") == original
    assert list(path.parent.glob(".*tmp")) == []


def test_append_quotes_values_safely(tmp_path):
    """YAML 특수문자가 든 시크릿이 파일 구조를 깨뜨리면 안 된다."""
    path = tmp_path / "devices.yaml"
    path.write_text(
        "devices:\n  - device_id: AQ-01\n    secret: s1\n"
        "    site_id: S-1\n    device_type: FIXED\n    facility_type: OFFICE\n",
        encoding="utf-8",
    )
    tricky = dict(NEW_DEVICE, secret="a: b #c\n- d")

    append_device(path, tricky)

    inventory = load_inventory(path)
    assert inventory[1].secret == "a: b #c\n- d"
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["devices"][1]


def test_append_leaves_a_restorable_backup(tmp_path):
    """secret은 백엔드에 해시로만 남아, 잘못 덮어쓰면 어디서도 복구할 수 없다."""
    path = tmp_path / "devices.yaml"
    original = textwrap.dedent(INVENTORY)
    path.write_text(original, encoding="utf-8")

    append_device(path, NEW_DEVICE)

    backups = list(tmp_path.glob("devices.yaml.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


def test_backup_retains_the_original_secrets(tmp_path):
    path = tmp_path / "devices.yaml"
    path.write_text(textwrap.dedent(INVENTORY), encoding="utf-8")

    append_device(path, NEW_DEVICE)

    backup = next(tmp_path.glob("devices.yaml.*.bak"))
    restored = load_inventory(backup)
    assert [c.device_id for c in restored] == ["AQ-01", "WB-01"]
    assert restored[0].secret == "super-secret-one"


def test_backups_are_capped(tmp_path, monkeypatch):
    """시크릿 평문 사본이 디렉터리에 무한히 쌓이면 그 자체가 유출 표면이 된다."""
    from livesim import panel

    path = tmp_path / "devices.yaml"
    path.write_text(textwrap.dedent(INVENTORY), encoding="utf-8")

    # 같은 초에 여러 번 주입해도 사본이 구분되도록 타임스탬프를 흉내낸다.
    stamps = iter(f"20260813-0000{n:02d}" for n in range(30))
    monkeypatch.setattr(panel.time, "strftime", lambda fmt: next(stamps))

    for n in range(panel.KEEP_BACKUPS + 3):
        append_device(path, dict(NEW_DEVICE, device_id=f"AQ-{n:03d}"))

    backups = sorted(tmp_path.glob("devices.yaml.*.bak"))
    assert len(backups) == panel.KEEP_BACKUPS
    # 오래된 것부터 지워야 한다 — 최근 상태로 되돌리는 게 목적이다.
    assert backups[-1].name.endswith("20260813-000012.bak")


def test_rejected_injection_makes_no_backup(tmp_path):
    """검증 실패는 원본을 건드리지 않으므로 사본도 남길 이유가 없다."""
    path = tmp_path / "devices.yaml"
    path.write_text(textwrap.dedent(INVENTORY), encoding="utf-8")

    with pytest.raises(PanelError):
        append_device(path, dict(NEW_DEVICE, device_id="AQ-01"))

    assert list(tmp_path.glob("devices.yaml.*.bak")) == []


def test_append_rejects_duplicate(tmp_path):
    path = tmp_path / "devices.yaml"
    path.write_text(textwrap.dedent(INVENTORY), encoding="utf-8")

    with pytest.raises(PanelError, match="이미 등록된"):
        append_device(path, dict(NEW_DEVICE, device_id="WB-01"))


def test_injected_device_is_immediately_loadable(tmp_path):
    """주입 직후 러너가 리로드하므로 파일이 항상 유효해야 한다."""
    path = tmp_path / "devices.yaml"
    path.write_text(textwrap.dedent(INVENTORY), encoding="utf-8")

    append_device(path, NEW_DEVICE)

    parsed = json.loads(json.dumps([c.device_id for c in load_inventory(path)]))
    assert parsed == ["AQ-01", "WB-01", "AQ-99"]
