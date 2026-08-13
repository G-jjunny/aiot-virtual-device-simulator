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
