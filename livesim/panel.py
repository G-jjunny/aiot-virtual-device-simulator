"""로컬 웹 패널 — 가상 하드웨어 실험대.

러너와는 **파일 채널로만** 통신한다 (control/state.json 읽기, cmd-*.json 쓰기).
패널이 러너에 직접 붙지 않으므로 둘은 서로의 생명주기를 모르고, 컨테이너/호스트
어디에서 띄우든 같은 디렉터리만 공유하면 동작한다.

표준 라이브러리 http.server만 쓴다. 이 도구를 위해 웹 프레임워크를 들이면
시뮬레이터를 돌리려고 의존성 트리를 관리하게 된다.

⚠ 인증이 없다. 기본 바인딩이 127.0.0.1인 이유이며, 공인망에 노출하면 안 된다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import textwrap
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

from livesim import __version__, control
from livesim.config import (
    DEVICE_TYPES,
    FACILITY_TYPES,
    ConfigError,
    Settings,
    load_inventory,
    parse_credential,
)
from livesim.profiles import DEVICE_FIELDS, PRESET_NAMES, SENSOR_PROFILES

LOG = logging.getLogger("livesim.panel")

DEFAULT_PORT = 8390
DEFAULT_HOST = "127.0.0.1"
MAX_BODY_BYTES = 64 * 1024


class PanelError(RuntimeError):
    """패널 조작이 거부되었을 때 (사용자 입력 오류)."""


# ---- 인벤토리 조작 -------------------------------------------------------


def public_inventory(devices_file: str | Path) -> list[dict[str, str]]:
    """secret을 뺀 인벤토리. 패널 응답에는 절대 평문이 나가지 않는다."""
    return [
        {
            "device_id": item.device_id,
            "site_id": item.site_id,
            "device_type": item.device_type,
            "facility_type": item.facility_type,
            "power": item.power,
        }
        for item in load_inventory(devices_file)
    ]


BACKUP_SUFFIX = ".bak"
KEEP_BACKUPS = 10

# 리스트 항목 줄의 들여쓰기 감지용. 주석(#)은 매치되지 않는다.
_ITEM_INDENT = re.compile(r"^(\s*)-\s", re.MULTILINE)


def _backup(path: Path) -> Path:
    """교체 직전 원본을 타임스탬프 사본으로 남긴다.

    이 파일의 secret은 백엔드에 해시로만 저장되어 있어, 잘못 덮어쓰면 어디서도
    복구할 수 없고 21대치 시크릿을 전부 재발급해야 한다. 사본 한 벌의 값이
    그 복구 비용보다 훨씬 싸다. (실제로 그 사고가 한 번 났다.)
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.{stamp}{BACKUP_SUFFIX}")
    backup.write_bytes(path.read_bytes())

    # 무한히 쌓이면 시크릿 사본이 디렉터리에 널린다. 최근 것만 남긴다.
    existing = sorted(path.parent.glob(f"{path.name}.*{BACKUP_SUFFIX}"))
    for stale in existing[:-KEEP_BACKUPS]:
        stale.unlink(missing_ok=True)
    return backup


def append_device(devices_file: str | Path, entry: dict[str, Any]) -> str:
    """새 기기를 devices.yaml에 원자적으로 덧붙인다.

    파일 전체를 다시 쓰지 않고 텍스트를 덧붙이는 이유: safe_dump로 재작성하면
    사용자가 적어둔 주석과 정렬이 전부 날아간다. 손으로 관리하는 파일이므로
    원본을 보존한다.

    교체 전에 임시 파일을 로더로 한 번 통과시키고, 원본은 타임스탬프 사본으로
    남긴다 — 검증에 실패하면 devices.yaml은 손대지 않은 상태로 남고, 성공해도
    직전 상태로 되돌릴 수 있다.
    """
    path = Path(devices_file)
    credential = parse_credential("입력", entry)

    existing = load_inventory(path)
    if any(item.device_id == credential.device_id for item in existing):
        raise PanelError(f"이미 등록된 device_id입니다: {credential.device_id}")

    entry_out: dict[str, Any] = {
        "device_id": credential.device_id,
        "secret": credential.secret,
        "site_id": credential.site_id,
        "device_type": credential.device_type,
        "facility_type": credential.facility_type,
    }
    if credential.starts_powered_off:
        # 기본값(on)은 적지 않는다 — 손으로 보는 파일에 기본값 줄이 늘어나면
        # 정작 꺼둔 기기가 눈에 띄지 않는다.
        entry_out["power"] = credential.power

    block = yaml.safe_dump(
        [entry_out],
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    original = path.read_text(encoding="utf-8")
    # 새 항목의 들여쓰기는 기존 항목에서 감지해 그대로 따른다. 파일 출처에 따라
    # 리스트 들여쓰기가 다르다 — safe_dump 산출물은 0칸(indentless), 예시 파일은
    # 2칸. 하드코딩하면 어느 한쪽에서 문법이 깨진다 (실제로 운영 파일에서 깨졌다).
    item_match = _ITEM_INDENT.search(original)
    if item_match is None:
        raise PanelError(
            "기존 항목의 들여쓰기를 감지할 수 없습니다 — devices.yaml에 항목이 "
            "하나도 없으면 첫 항목은 파일에 직접 추가한 뒤 리로드하세요."
        )
    separator = "" if original.endswith("\n") else "\n"
    candidate = original + separator + textwrap.indent(block, item_match.group(1))

    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(candidate, encoding="utf-8")
    try:
        load_inventory(tmp)
    except ConfigError:
        tmp.unlink(missing_ok=True)
        raise
    _backup(path)
    try:
        os.replace(tmp, path)
    except OSError as exc:
        # docker에서 devices.yaml을 "파일 하나"로 바인드 마운트하면 마운트
        # 지점 위로 rename할 수 없다 (EBUSY). 디렉터리 마운트가 정답이다.
        tmp.unlink(missing_ok=True)
        raise PanelError(
            "인벤토리 교체 실패 — devices.yaml이 단일 파일로 바인드 마운트되어 "
            "있으면 원자적 교체(rename)가 막힙니다. 디렉터리째 마운트하고 "
            "DEVICES_FILE로 경로를 지정하세요 (docker-compose.yml 참조). "
            f"원인: {exc}"
        ) from exc
    LOG.info("기기 주입: %s", credential.device_id)  # secret은 남기지 않는다
    return credential.device_id


def read_state(control_dir: str | Path) -> dict[str, Any]:
    try:
        state = control.read_state(control_dir)
    except control.ControlError as exc:
        return {"running": False, "reason": str(exc), "server_now": time.time()}
    state["running"] = True
    # 응답을 만드는 '지금'의 서버 시각. state.json의 written_at은 마지막 틱
    # 시점이라 최대 한 틱(기본 5분) 낡았고, 그걸로 시계를 맞추면 카운트다운이
    # 딱 그만큼 부풀어 오른다. 패널은 러너와 같은 호스트에서 도므로 이 값이
    # 러너의 현재 시각과 같다.
    state["server_now"] = time.time()
    return state


# ---- HTTP ----------------------------------------------------------------


class PanelHandler(BaseHTTPRequestHandler):
    server_version = f"livesim-panel/{__version__}"

    @property
    def settings(self) -> Settings:
        return self.server.settings  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    # -- 응답 헬퍼 --

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 로컬 전용 도구지만, 외부 페이지가 이 API를 부르게 둘 이유는 없다.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise PanelError("요청 본문이 비어 있습니다")
        if length > MAX_BODY_BYTES:
            raise PanelError("요청 본문이 너무 큽니다")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError as exc:
            raise PanelError(f"JSON을 해석할 수 없습니다: {exc}") from exc
        if not isinstance(body, dict):
            raise PanelError("요청 본문은 객체여야 합니다")
        return body

    # -- 라우팅 --

    def do_GET(self) -> None:  # noqa: N802 (http.server 규약)
        route = self.path.split("?", 1)[0]
        try:
            if route == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif route == "/api/state":
                self._json(200, read_state(self.settings.control_dir))
            elif route == "/api/inventory":
                self._json(
                    200, {"devices": public_inventory(self.settings.devices_file)}
                )
            else:
                self._json(404, {"error": "그런 경로가 없습니다"})
        except ConfigError as exc:
            self._json(422, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("GET %s 처리 실패", route)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]
        try:
            if route == "/api/cmd":
                self._json(200, self._handle_cmd(self._read_json()))
            elif route == "/api/inventory":
                self._json(201, self._handle_inject(self._read_json()))
            else:
                self._json(404, {"error": "그런 경로가 없습니다"})
        except (PanelError, ConfigError, control.ControlError) as exc:
            self._json(422, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("POST %s 처리 실패", route)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    # -- 핸들러 --

    def _handle_cmd(self, body: dict[str, Any]) -> dict[str, Any]:
        command = str(body.get("type") or "")
        if command not in control.COMMANDS:
            raise PanelError(
                f"알 수 없는 명령 '{command}' (사용 가능: {', '.join(control.COMMANDS)})"
            )
        device_id = str(body.get("device_id") or "")
        minutes = body.get("minutes")
        if minutes is not None:
            try:
                minutes = float(minutes)
            except (TypeError, ValueError) as exc:
                raise PanelError(f"minutes는 숫자여야 합니다: {minutes!r}") from exc
        # 알 수 없는 센서·비수치 값은 여기서 422로 돌려준다 (control.parse_overrides).
        control.write_command(
            self.settings.control_dir, command, device_id, minutes,
            body.get("overrides"),
            site_id=str(body.get("site_id") or ""),
            preset=str(body.get("preset") or ""),
        )
        return {"ok": True, "type": command, "device_id": device_id}

    def _handle_inject(self, body: dict[str, Any]) -> dict[str, Any]:
        device_id = append_device(self.settings.devices_file, body)
        # 주입 직후 러너가 새 기기를 집도록 리로드를 자동으로 넣는다.
        # 사람이 두 번 조작해야 하면 "넣었는데 왜 안 보이지"가 반복된다.
        control.write_command(self.settings.control_dir, control.RELOAD)
        return {"ok": True, "device_id": device_id, "reload": True}


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], settings: Settings) -> None:
        self.settings = settings
        super().__init__(address, PanelHandler)


def build_server(settings: Settings, host: str, port: int) -> PanelServer:
    return PanelServer((host, port), settings)


def serve(settings: Settings, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = build_server(settings, host, port)
    actual = server.server_address[1]
    print(f"livesim {__version__} 패널: http://{host}:{actual}")
    print(f"  인벤토리 {settings.devices_file} · 제어 {settings.control_dir}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("  ⚠ 인증이 없습니다 — 공인망에 노출하지 마세요.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _sensor_meta() -> str:
    """센서 범위·유형별 필드·기본 목표치를 JSON으로 내보낸다.

    프로필은 파이썬 쪽이 진실이므로 JS에 값을 복사해 두지 않는다 — 두 벌이 되면
    화면에 표시하는 범위와 실제 클램프가 조용히 어긋난다.
    """
    return json.dumps(
        {
            "sensors": {
                name: {
                    "min": profile.minimum,
                    "max": profile.maximum,
                    "decimals": profile.decimals,
                }
                for name, profile in SENSOR_PROFILES.items()
            },
            "fields": {key: list(value) for key, value in DEVICE_FIELDS.items()},
            "burst_defaults": control.DEFAULT_BURST_OVERRIDES,
            "presets": list(PRESET_NAMES),
        },
        ensure_ascii=False,
    )


PAGE = (
    """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>livesim 패널</title>
<style>
:root{--bg:#0f1117;--card:#181b23;--line:#272b36;--fg:#e6e8ee;--dim:#8b90a0;
--ok:#31c48d;--off:#5b6070;--warn:#f6a723;--bad:#f05252;--acc:#5b8def}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font-family:system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
header{padding:16px 20px;border-bottom:1px solid var(--line);
display:flex;flex-wrap:wrap;gap:16px;align-items:baseline}
h1{font-size:16px;margin:0;font-weight:650}
.sum{color:var(--dim);font-size:13px}
.sum b{color:var(--fg);font-weight:600}
main{padding:20px;display:grid;gap:20px;
grid-template-columns:minmax(0,1fr) minmax(280px,340px)}
@media(max-width:860px){main{grid-template-columns:1fr}}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-left-width:4px;
border-radius:8px;padding:12px}
.card.on{border-left-color:var(--ok)}
.card.offp{border-left-color:var(--off)}
.card.drop{border-left-color:var(--warn)}
.card.bad{border-left-color:var(--bad)}
.did{font-weight:650;font-size:14px;word-break:break-all}
.meta{color:var(--dim);font-size:12px;margin:4px 0 8px}
.badge{display:inline-block;font-size:11px;padding:2px 7px;border-radius:99px;
background:#232734;color:var(--dim);margin-right:4px}
.badge.w{background:#3a2c11;color:var(--warn)}
.badge.g{background:#12321f;color:var(--ok)}
.badge.r{background:#3a1a1a;color:var(--bad)}
.btns{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
button{background:#232734;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:5px 9px;font-size:12px;cursor:pointer;font-family:inherit}
button:hover{border-color:var(--acc)}
button:disabled{opacity:.45;cursor:not-allowed}
aside{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:14px;align-self:start}
aside h2{font-size:14px;margin:0 0 4px}
aside p{color:var(--dim);font-size:12px;margin:0 0 12px;line-height:1.5}
label{display:block;font-size:12px;color:var(--dim);margin:8px 0 3px}
input,select{width:100%;background:#0f1117;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:7px 9px;font-size:13px;font-family:inherit}
.wide{width:100%;margin-top:12px;padding:8px;background:var(--acc);
border-color:var(--acc);color:#fff;font-weight:600}
#msg{margin-top:10px;font-size:12px;line-height:1.5;white-space:pre-wrap}
.err{color:var(--bad)}.good{color:var(--ok)}
.empty{color:var(--dim);font-size:13px;padding:24px;text-align:center;
border:1px dashed var(--line);border-radius:8px}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px;
border-bottom:1px solid var(--line);padding-bottom:10px}
.tab{background:transparent;border:1px solid var(--line);color:var(--dim);
border-radius:99px;padding:5px 12px;font-size:12px}
.tab:hover{color:var(--fg)}
.tab.sel{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
.tab .n{opacity:.75;margin-left:4px}
.chk{display:flex;align-items:center;gap:7px;margin-top:12px;color:var(--fg)}
.chk input{width:auto}
.hint{color:var(--dim);font-size:11px;line-height:1.5;margin:6px 0 0}
.panelform{margin-top:10px;padding:10px;background:#12151d;border:1px solid var(--line);
border-radius:6px}
.panelform.hidden{display:none}
.row{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.row label{margin:0;flex:0 0 auto;color:var(--dim);font-size:11px}
.row input,.row select{padding:4px 6px;font-size:12px}
.row input.num{width:80px;flex:0 0 auto}
.row .rng{color:var(--dim);font-size:10px;white-space:nowrap}
.row .x{padding:2px 7px;font-size:11px;line-height:1.2}
.formhint{color:var(--dim);font-size:10px;line-height:1.5;margin:2px 0 8px}
.paused{color:var(--warn);font-size:10px;margin:0 0 8px}
.env{border:1px solid transparent}
.env.moderate{background:#3a3411;color:#e3c74a;border-color:#5c5220}
.env.bad{background:#3d2a11;color:var(--warn);border-color:#6b4718}
.env.very_bad{background:#3f1717;color:#ff7b7b;border-color:#6e2222}
.site{color:var(--dim);font-size:10px;margin-top:2px;font-family:ui-monospace,monospace}
.sitebar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:12px}
.sitebar .lbl{color:var(--dim);font-size:11px}
.sitebar select{width:auto;padding:4px 8px;font-size:12px}
.go{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
</style></head><body>
<header>
  <h1>livesim 패널</h1>
  <div class="sum" id="sum">불러오는 중...</div>
</header>
<main>
  <section>
    <div class="tabs" id="tabs"></div>
    <div class="sitebar" id="sitebar"></div>
    <div class="grid" id="grid"></div>
  </section>
  <aside>
    <h2>새 가상 기기 주입</h2>
    <p>FE 관리자 화면에서 디바이스를 등록하고 발급받은 시크릿을 붙여넣으세요.
       devices.yaml에 추가된 뒤 러너가 자동으로 리로드합니다.</p>
    <label>device_id</label><input id="f_id" placeholder="AQ-GANGNAM-05">
    <label>secret (발급받은 값)</label><input id="f_sec" type="password">
    <label>site_id (UUID)</label><input id="f_site">
    <label>device_type</label><select id="f_dt">__DEVICE_TYPES__</select>
    <label>facility_type</label><select id="f_ft">__FACILITY_TYPES__</select>
    <label class="chk"><input type="checkbox" id="f_off"> 전원 off로 주입</label>
    <p class="hint">FE에서 offline·정비중인 기기를 등재만 해둘 때 켜세요. 켜는 순간
      발행이 시작되고 백엔드가 그 기기를 online으로 되살립니다.</p>
    <button class="wide" id="inject">주입하고 리로드</button>
    <div id="msg"></div>
  </aside>
</main>
<script>
const $=s=>document.querySelector(s);
let inv=[], lastState=null;

async function api(path,opt){
  const r=await fetch(path,opt);
  let b={}; try{b=await r.json()}catch(e){}
  if(!r.ok) throw new Error(b.error||('HTTP '+r.status));
  return b;
}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

// 유형 필터. 선택은 JS 변수에 두고 DOM에서 읽지 않는다 — 2초 폴링과 1초
// 카운트다운이 카드를 다시 그리므로, DOM에 기대면 갱신마다 '전체'로 튕긴다.
const TYPE_TABS=[['ALL','전체'],['FIXED','고정형'],['PORTABLE','이동형'],
                 ['WEARABLE','웨어러블']];
const TYPE_KEY='livesim.panel.type';
let activeType='ALL';
try{ const saved=localStorage.getItem(TYPE_KEY);
     if(saved && TYPE_TABS.some(t=>t[0]===saved)) activeType=saved; }catch(e){}

function matchesType(d){
  if(activeType==='ALL') return true;
  // device_type이 없는 구버전 상태 항목은 '전체'에서만 보인다.
  return (d.device_type||'')===activeType;
}
function selectType(t){
  activeType=t;
  try{ localStorage.setItem(TYPE_KEY,t) }catch(e){}
  if(lastState) render(lastState,inv,true);
}
function renderTabs(devs){
  $('#tabs').innerHTML=TYPE_TABS.map(([key,label])=>{
    const n=key==='ALL' ? devs.length
      : devs.filter(d=>(d.device_type||'')===key).length;
    return '<button class="tab'+(activeType===key?' sel':'')+
      '" onclick="selectType(\\''+key+'\\')">'+label+
      '<span class="n">'+n+'</span></button>';
  }).join('');
}

// ---- 사이트 필터 · 환경 등급 -------------------------------------------
//
// 사이트 이름은 알 수 없다 (admin API를 부르지 않는 것이 이 도구의 원칙).
// site_id 앞 8자와 소속 대수로 라벨을 만들고, 기기 이름 접두어로 사람이 알아본다.
const SITE_ALL='ALL';
const SITE_KEY='livesim.panel.site';
let activeSite=SITE_ALL;
try{ const saved=localStorage.getItem(SITE_KEY); if(saved) activeSite=saved; }catch(e){}

function shortSite(id){ return (id||'').slice(0,8) }
function matchesSite(d){ return activeSite===SITE_ALL || (d.site_id||'')===activeSite }
function selectSite(id){
  activeSite=id;
  try{ localStorage.setItem(SITE_KEY,id) }catch(e){}
  if(lastState) render(lastState,inv,true);
}
function renderSiteBar(devs){
  const counts={};
  devs.forEach(d=>{ const s=d.site_id||''; if(s) counts[s]=(counts[s]||0)+1 });
  const ids=Object.keys(counts).sort();
  if(!ids.length){ $('#sitebar').innerHTML=''; return; }
  const options=[`<option value="${SITE_ALL}">전체 사이트 (${devs.length}대)</option>`]
    .concat(ids.map(id=>'<option value="'+esc(id)+'"'+
      (activeSite===id?' selected':'')+'>'+esc(shortSite(id))+'… ('+counts[id]+'대)</option>'));
  const presets=META.presets.map(p=>'<option>'+esc(p)+'</option>').join('');
  $('#sitebar').innerHTML=
    '<span class="lbl">사이트</span>'+
    '<select onchange="selectSite(this.value)">'+options.join('')+'</select>'+
    (activeSite===SITE_ALL ? '<span class="lbl">— 사이트를 고르면 일괄 변경할 수 있습니다</span>'
      : '<span class="lbl">환경 일괄</span>'+
        '<select id="bulkpreset">'+presets+'</select>'+
        '<button onclick="bulkProfile()">이 사이트 전체 적용 ('+
          (counts[activeSite]||0)+'대)</button>');
}
async function bulkProfile(){
  const preset=$('#bulkpreset')?.value;
  if(!preset || activeSite===SITE_ALL) return;
  try{
    await api('/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({type:'profile',site_id:activeSite,preset})});
    say('사이트 '+shortSite(activeSite)+'… 환경 등급 '+preset+' 적용',false);
    setTimeout(refresh,600);
  }catch(e){say(e.message,true)}
}
function setProfile(device_id,preset){
  cmd('profile',device_id,null,null,preset);
}
function envBadge(d){
  const p=d.profile;
  if(!p || p===META.presets[0]) return '';   // good은 무표시
  return '<span class="badge env '+esc(p)+'">'+esc(p)+'</span>';
}

function cls(d){
  if(d.disabled) return 'bad';
  if(d.event==='power_off') return 'offp';
  if(d.event==='dropout'||!d.online) return 'drop';
  return d.connected?'on':'offp';
}
// 서버와 브라우저의 시계 차이.
//
// 기준은 응답마다 새로 찍히는 server_now다. state.json의 written_at은 마지막
// 틱 시점이라 최대 한 틱(기본 5분) 낡았고, 그걸로 맞추면 카운트다운이 그만큼
// 부풀거나(첫 로드) 폴링마다 되돌아가 얼어붙는다.
let clockOffset=0;
function syncClock(serverEpoch){
  if(serverEpoch!=null) clockOffset=Date.now()/1000-serverEpoch;
}
function serverNow(){return Date.now()/1000-clockOffset}

function fmtLeft(sec){
  const m=Math.floor(sec/60), s=Math.floor(sec%60);
  return String(m).padStart(2,'0')+':'+String(s).padStart(2,'0')+' 남음';
}
// 스케줄러는 틱 경계에서만 이벤트를 걷는다. 0이 되어도 배지가 바로 사라지지
// 않는 것이 정상이므로, 그 사이를 '대기'로 설명해 준다.
function waitingText(event){
  return event==='dropout' ? '복구·재전송 대기(다음 틱)' : '종료 대기(다음 틱)';
}

function tickCountdowns(){
  const keep=editingId();
  document.querySelectorAll('[data-ends]').forEach(el=>{
    // 편집 중인 카드는 배지도 건드리지 않는다 (편집 우선 — 잠깐 멈춰도 된다).
    if(keep && el.closest('.card')?.dataset.did===keep) return;
    const left=parseFloat(el.dataset.ends)-serverNow();
    if(left>0){ el.textContent=fmtLeft(left); el.className='badge'; }
    else { el.textContent=el.dataset.waiting; el.className='badge w'; }
  });
}

function badges(d){
  let h='';
  if(d.disabled) h+='<span class="badge r">비활성</span>';
  else if(d.event) h+='<span class="badge w">'+esc(d.event)+
    (d.event_manual?' 수동':'')+'</span>';
  else if(d.connected&&d.online) h+='<span class="badge g">정상</span>';
  if(d.pending>0) h+='<span class="badge">버퍼 '+d.pending+'</span>';
  if(d.event_ends_at!=null)
    h+='<span class="badge" data-ends="'+d.event_ends_at+'" data-waiting="'+
       esc(waitingText(d.event))+'"></span>';
  else if(d.event_ends_in!=null)  // 구버전 러너의 state.json 대비
    h+='<span class="badge">'+Math.round(d.event_ends_in)+'초</span>';
  return h;
}

async function cmd(type,device_id,minutes,overrides,preset){
  try{
    await api('/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({type,device_id,minutes,overrides,preset})});
    say('명령 전달: '+type+' '+(device_id||''),false);
    openForm=null;
    setTimeout(refresh,600);
  }catch(e){say(e.message,true)}
}
function say(t,bad){const m=$('#msg');m.textContent=t;m.className=bad?'err':'good'}

// ---- 수동 이벤트 설정 폼 -------------------------------------------------
//
// 카드 안에서 접었다 펴는 방식. 어떤 카드의 어떤 폼이 열렸는지는 JS 변수에
// 둔다 — 2초 폴링이 카드를 다시 그리므로 DOM에 기대면 매번 닫힌다.
const META=__SENSOR_META__;
let openForm=null;          // "device_id|dropout" 또는 "device_id|burst"
let draft={};               // device_id -> {minutes, overrides:{name:value}}

function formKey(id,kind){return id+'|'+kind}
function toggleForm(id,kind){
  const key=formKey(id,kind);
  openForm = openForm===key ? null : key;
  if(!draft[id]) draft[id]={};
  if(lastState) render(lastState,inv,true);
}
function draftFor(id,kind,allowed){
  if(!draft[id]) draft[id]={};
  const d=draft[id];
  if(d.minutes==null) d.minutes = kind==='dropout'?5:10;
  if(kind==='burst' && !d.overrides){
    // 기본 목표치는 그대로 쓰되, 이 기기가 측정하지 않는 항목은 뺀다.
    // 웨어러블에 pm25를 걸면 페이로드에 실리지 않아 "실행했는데 아무 변화가
    // 없다"가 된다 — 이 기능이 고치려는 문제 그 자체다.
    const base={...META.burst_defaults};
    d.overrides = (allowed && allowed.length)
      ? Object.fromEntries(Object.entries(base).filter(([k])=>allowed.includes(k)))
      : base;
  }
  return d;
}
function setMinutes(id,kind,value){ draftFor(id,kind).minutes=value }
function setTarget(id,name,value){ draftFor(id,'burst').overrides[name]=value }
function dropTarget(id,name){
  delete draftFor(id,'burst').overrides[name];
  if(lastState) render(lastState,inv,true);
}
function addTarget(id,name){
  if(!name) return;
  const meta=META.sensors[name];
  draftFor(id,'burst').overrides[name]=meta?Math.round((meta.min+meta.max)/2*100)/100:0;
  if(lastState) render(lastState,inv,true);
}

// 발행 주기 대비 몇 포인트가 영향을 받는지 — 5분 단절이 결측 1개뿐이라
// "아무 일도 안 일어난 것처럼" 보이는 게 이 기능이 생긴 이유다.
function pointsHint(minutes,interval,kind){
  const n=Math.floor((Number(minutes)||0)*60/(interval||300));
  const unit = kind==='dropout' ? '결측' : '상승';
  return '발행 주기 '+(interval||300)+'초 기준 ≈ '+unit+' '+n+'포인트';
}

function numInput(handler,value,extra){
  return '<input class="num" type="number" value="'+value+'" '+(extra||'')+
         ' oninput="'+handler+'">';
}

function dropoutForm(d,interval){
  const cur=draftFor(d.device_id,'dropout');
  return '<div class="panelform">'+
    '<div class="paused">갱신 일시정지(편집 중) — 이 카드의 상태는 낡을 수 있습니다</div>'+
    '<div class="row"><label>지속(분)</label>'+
      numInput("setMinutes('"+esc(d.device_id)+"','dropout',this.value);"+
               "this.closest('.panelform').querySelector('.formhint').textContent="+
               "pointsHint(this.value,"+interval+",'dropout')",
               cur.minutes,'min="1" max="1440"')+
      '<button class="go" onclick="cmd(\\'dropout\\',\\''+esc(d.device_id)+
        '\\',Number(draftFor(\\''+esc(d.device_id)+'\\',\\'dropout\\').minutes))">실행</button>'+
    '</div>'+
    '<div class="formhint">'+pointsHint(cur.minutes,interval,'dropout')+'</div>'+
  '</div>';
}

function burstForm(d,interval){
  const id=d.device_id;
  const allowed=META.fields[d.device_type||'FIXED']||[];
  const cur=draftFor(id,'burst',allowed);
  // 이 기기가 실제로 발행하는 센서만 고를 수 있다. 웨어러블에 pm25를 걸어도
  // 페이로드에 실리지 않으므로 목록에서 빼는 편이 정직하다.
  const rows=Object.keys(cur.overrides).map(name=>{
    const meta=META.sensors[name]||{min:0,max:0};
    const unsupported = allowed.length && !allowed.includes(name);
    return '<div class="row">'+
      '<label style="flex:0 0 74px">'+esc(name)+'</label>'+
      numInput("setTarget('"+esc(id)+"','"+esc(name)+"',this.value)",cur.overrides[name],
               'step="any"')+
      '<span class="rng">'+meta.min+'~'+meta.max+
        (unsupported?' · 이 유형 미측정':'')+'</span>'+
      '<button class="x" onclick="dropTarget(\\''+esc(id)+'\\',\\''+esc(name)+
        '\\')">제거</button>'+
    '</div>';
  }).join('');
  const addable=allowed.filter(n=>!(n in cur.overrides));
  const adder=addable.length
    ? '<div class="row"><label>항목 추가</label><select onchange="addTarget(\\''+
      esc(id)+'\\',this.value);this.value=\\'\\'"><option value="">선택…</option>'+
      addable.map(n=>'<option>'+esc(n)+'</option>').join('')+'</select></div>'
    : '';
  return '<div class="panelform">'+
    '<div class="paused">갱신 일시정지(편집 중) — 이 카드의 상태는 낡을 수 있습니다</div>'+
    '<div class="row"><label>지속(분)</label>'+
      numInput("setMinutes('"+esc(id)+"','burst',this.value);"+
               "this.closest('.panelform').querySelector('.formhint').textContent="+
               "pointsHint(this.value,"+interval+",'burst')",
               cur.minutes,'min="1" max="1440"')+
      '<button class="go" onclick="cmd(\\'burst\\',\\''+esc(id)+
        '\\',Number(draftFor(\\''+esc(id)+'\\',\\'burst\\').minutes),'+
        'draftFor(\\''+esc(id)+'\\',\\'burst\\').overrides)">실행</button>'+
    '</div>'+
    '<div class="formhint">'+pointsHint(cur.minutes,interval,'burst')+'</div>'+
    (rows||'<div class="formhint">아래에서 항목을 추가하세요.</div>')+adder+
  '</div>';
}

function render(state,invList,force){
  const running=state.running!==false;
  const devs=running?(state.devices||[]):[];
  const conn=devs.filter(d=>d.connected).length;
  // 꺼둔 기기가 있으면 접속 수가 모자라 보인다 — 왜 모자란지 같이 적는다.
  const off=devs.filter(d=>d.event==='power_off').length;
  $('#sum').innerHTML = running
    ? '시나리오 <b>'+esc(state.scenario)+'</b> · tick <b>'+esc(state.tick)+
      '</b> · 접속 <b>'+conn+'/'+devs.length+'</b>'+
      (off?' · 전원off <b>'+off+'</b>':'')+' · 갱신 '+esc(state.updated_at)
    : '<span class="err">러너가 실행 중이 아닙니다</span> · 인벤토리 '+invList.length+'대';

  // 러너가 없으면 인벤토리로 대신 그린다. 인벤토리에도 device_type이 있어
  // 탭 필터는 두 경우 모두 동작한다.
  const all = running ? devs
    : invList.map(i=>({device_id:i.device_id,device_type:i.device_type,
        connected:false,online:false,pending:0,event:null,disabled:false}));
  renderTabs(all);
  renderSiteBar(all);
  const rows = all.filter(d=>matchesType(d)&&matchesSite(d));
  if(!all.length){
    $('#grid').innerHTML='<div class="empty">등록된 기기가 없습니다. 오른쪽에서 주입하세요.</div>';
    return;
  }
  if(!rows.length){
    $('#grid').innerHTML='<div class="empty">이 유형의 기기가 없습니다.</div>';
    return;
  }
  const interval=state.interval_seconds||300;
  renderGrid(rows,interval,force);
  tickCountdowns();   // 새로 그린 배지에 즉시 숫자를 채운다
}

function cardHtml(d,interval){
  const info=inv.find(i=>i.device_id===d.device_id)||{};
  const id=esc(d.device_id);
  const dropout=openForm===formKey(d.device_id,'dropout');
  const burst=openForm===formKey(d.device_id,'burst');
  const site=d.site_id||info.site_id||'';
  const presetOptions=META.presets.map(p=>
    '<option'+(p===(d.profile||META.presets[0])?' selected':'')+'>'+esc(p)+'</option>'
  ).join('');
  return '<div class="card '+cls(d)+'" data-did="'+id+'">'+
    '<div class="did">'+id+'</div>'+
    '<div class="meta">'+esc(d.device_type||info.device_type||'')+
      (info.facility_type?' · '+esc(info.facility_type):'')+'</div>'+
    (site?'<div class="site">site '+esc(shortSite(site))+'…</div>':'')+
    '<div>'+badges(d)+envBadge(d)+'</div>'+
    '<div class="btns">'+
      '<button onclick="cmd(\\'on\\',\\''+id+'\\')">전원 on</button>'+
      '<button onclick="cmd(\\'off\\',\\''+id+'\\')">전원 off</button>'+
      '<button onclick="toggleForm(\\''+id+'\\',\\'dropout\\')">단절…</button>'+
      '<button onclick="toggleForm(\\''+id+'\\',\\'burst\\')">버스트…</button>'+
      '<select title="환경 등급" onchange="setProfile(\\''+id+'\\',this.value)">'+
        presetOptions+'</select>'+
    '</div>'+
    (dropout?dropoutForm(d,interval):'')+
    (burst?burstForm(d,interval):'')+
  '</div>';
}

// 편집 중인 카드는 재렌더에서 제외한다.
//
// 2초 폴링이 그리드를 통째로 다시 그리면, 열려 있는 폼의 입력값과 포커스가
// 매번 날아가 편집 자체가 불가능하다. 그 카드의 DOM 노드만 손대지 않고
// 나머지 카드는 평소대로 갱신한다 — 전체를 멈추면 다른 기기 상태가 낡는다.
function editingId(){ return openForm ? openForm.split('|')[0] : null }

function renderGrid(rows,interval,force){
  const grid=$('#grid');
  // force: 사용자가 직접 일으킨 렌더(폼 열기/닫기, 항목 추가·제거, 탭 전환).
  // 이때는 편집 중인 카드도 다시 그려야 한다 — 안 그리면 폼을 열라는 조작이
  // 바로 그 '편집 중 보호'에 걸려 아무 일도 일어나지 않는다.
  const keep=force?null:editingId();
  const wanted=rows.map(d=>d.device_id).join('\\u0000');
  const current=[...grid.children].map(n=>n.dataset.did||'').join('\\u0000');

  if(wanted!==current){
    // 구성(대수·순서·탭 필터)이 바뀌면 통째로 다시 그린다. 편집 중이었다면
    // 폼은 닫히지만 입력값은 draft에 남아 다시 열면 그대로다.
    grid.innerHTML=rows.map(d=>cardHtml(d,interval)).join('');
    return;
  }
  rows.forEach((d,index)=>{
    if(d.device_id===keep) return;              // 편집 중 — 건드리지 않는다
    const node=grid.children[index];
    const html=cardHtml(d,interval);
    if(node.outerHTML!==html) node.outerHTML=html;
  });
}

async function refresh(){
  try{
    const [s,i]=await Promise.all([api('/api/state'),api('/api/inventory')]);
    // 응답마다 찍힌 서버 시각으로 시계를 다시 맞춘다 (written_at은 낡았다).
    syncClock(s.server_now);
    inv=i.devices||[];
    lastState=s;   // 탭을 바꿀 때 폴링을 기다리지 않고 바로 다시 그리기 위해
    render(s,inv);
  }catch(e){ $('#sum').innerHTML='<span class="err">'+esc(e.message)+'</span>'; }
}

$('#inject').onclick=async()=>{
  const btn=$('#inject'); btn.disabled=true;
  try{
    const r=await api('/api/inventory',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({device_id:$('#f_id').value,secret:$('#f_sec').value,
        site_id:$('#f_site').value,device_type:$('#f_dt').value,
        facility_type:$('#f_ft').value,
        power:$('#f_off').checked?'off':'on'})});
    say('주입 완료: '+r.device_id+' — 러너 리로드 요청됨',false);
    $('#f_id').value='';$('#f_sec').value='';$('#f_site').value='';
    setTimeout(refresh,800);
  }catch(e){say(e.message,true)}
  finally{btn.disabled=false}
};

// 카드·탭·폼의 인라인 onclick/oninput에서 부른다
Object.assign(window,{cmd,selectType,toggleForm,setMinutes,setTarget,
                      dropTarget,addTarget,draftFor,pointsHint,
                      selectSite,bulkProfile,setProfile});
refresh();
setInterval(refresh,2000);        // 데이터 폴링
setInterval(tickCountdowns,1000); // 남은시간만 매초 다시 계산 (재렌더 없음)
</script></body></html>
"""
    .replace(
        "__DEVICE_TYPES__",
        "".join(f"<option>{name}</option>" for name in DEVICE_TYPES),
    )
    .replace(
        "__FACILITY_TYPES__",
        "".join(f"<option>{name}</option>" for name in FACILITY_TYPES),
    )
    .replace("__SENSOR_META__", _sensor_meta())
)
