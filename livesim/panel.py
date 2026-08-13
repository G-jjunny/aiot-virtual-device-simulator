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
        }
        for item in load_inventory(devices_file)
    ]


BACKUP_SUFFIX = ".bak"
KEEP_BACKUPS = 10


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

    block = yaml.safe_dump(
        [{
            "device_id": credential.device_id,
            "secret": credential.secret,
            "site_id": credential.site_id,
            "device_type": credential.device_type,
            "facility_type": credential.facility_type,
        }],
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    original = path.read_text(encoding="utf-8")
    separator = "" if original.endswith("\n") else "\n"
    candidate = original + separator + textwrap.indent(block, "  ")

    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(candidate, encoding="utf-8")
    try:
        load_inventory(tmp)
    except ConfigError:
        tmp.unlink(missing_ok=True)
        raise
    _backup(path)
    os.replace(tmp, path)
    LOG.info("기기 주입: %s", credential.device_id)  # secret은 남기지 않는다
    return credential.device_id


def read_state(control_dir: str | Path) -> dict[str, Any]:
    try:
        state = control.read_state(control_dir)
    except control.ControlError as exc:
        return {"running": False, "reason": str(exc)}
    state["running"] = True
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
        control.write_command(
            self.settings.control_dir, command, device_id, minutes
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
</style></head><body>
<header>
  <h1>livesim 패널</h1>
  <div class="sum" id="sum">불러오는 중...</div>
</header>
<main>
  <section><div class="grid" id="grid"></div></section>
  <aside>
    <h2>새 가상 기기 주입</h2>
    <p>FE 관리자 화면에서 디바이스를 등록하고 발급받은 시크릿을 붙여넣으세요.
       devices.yaml에 추가된 뒤 러너가 자동으로 리로드합니다.</p>
    <label>device_id</label><input id="f_id" placeholder="AQ-GANGNAM-05">
    <label>secret (발급받은 값)</label><input id="f_sec" type="password">
    <label>site_id (UUID)</label><input id="f_site">
    <label>device_type</label><select id="f_dt">__DEVICE_TYPES__</select>
    <label>facility_type</label><select id="f_ft">__FACILITY_TYPES__</select>
    <button class="wide" id="inject">주입하고 리로드</button>
    <div id="msg"></div>
  </aside>
</main>
<script>
const $=s=>document.querySelector(s);
let inv=[];

async function api(path,opt){
  const r=await fetch(path,opt);
  let b={}; try{b=await r.json()}catch(e){}
  if(!r.ok) throw new Error(b.error||('HTTP '+r.status));
  return b;
}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

function cls(d){
  if(d.disabled) return 'bad';
  if(d.event==='power_off') return 'offp';
  if(d.event==='dropout'||!d.online) return 'drop';
  return d.connected?'on':'offp';
}
function badges(d){
  let h='';
  if(d.disabled) h+='<span class="badge r">비활성</span>';
  else if(d.event) h+='<span class="badge w">'+esc(d.event)+
    (d.event_manual?' 수동':'')+'</span>';
  else if(d.connected&&d.online) h+='<span class="badge g">정상</span>';
  if(d.pending>0) h+='<span class="badge">버퍼 '+d.pending+'</span>';
  if(d.event_ends_in!=null) h+='<span class="badge">'+Math.round(d.event_ends_in)+'초</span>';
  return h;
}

async function cmd(type,device_id,minutes){
  try{
    await api('/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({type,device_id,minutes})});
    say('명령 전달: '+type+' '+(device_id||''),false);
    setTimeout(refresh,600);
  }catch(e){say(e.message,true)}
}
function say(t,bad){const m=$('#msg');m.textContent=t;m.className=bad?'err':'good'}

function render(state,invList){
  const running=state.running!==false;
  const devs=running?(state.devices||[]):[];
  const conn=devs.filter(d=>d.connected).length;
  $('#sum').innerHTML = running
    ? '시나리오 <b>'+esc(state.scenario)+'</b> · tick <b>'+esc(state.tick)+
      '</b> · 접속 <b>'+conn+'/'+devs.length+'</b> · 갱신 '+esc(state.updated_at)
    : '<span class="err">러너가 실행 중이 아닙니다</span> · 인벤토리 '+invList.length+'대';

  const rows = running ? devs
    : invList.map(i=>({device_id:i.device_id,connected:false,online:false,
        pending:0,event:null,disabled:false}));
  if(!rows.length){
    $('#grid').innerHTML='<div class="empty">등록된 기기가 없습니다. 오른쪽에서 주입하세요.</div>';
    return;
  }
  $('#grid').innerHTML=rows.map(d=>{
    const info=inv.find(i=>i.device_id===d.device_id)||{};
    return '<div class="card '+cls(d)+'">'+
      '<div class="did">'+esc(d.device_id)+'</div>'+
      '<div class="meta">'+esc(info.device_type||'')+
        (info.facility_type?' · '+esc(info.facility_type):'')+'</div>'+
      '<div>'+badges(d)+'</div>'+
      '<div class="btns">'+
        '<button onclick="cmd(\\'on\\',\\''+esc(d.device_id)+'\\')">전원 on</button>'+
        '<button onclick="cmd(\\'off\\',\\''+esc(d.device_id)+'\\')">전원 off</button>'+
        '<button onclick="cmd(\\'dropout\\',\\''+esc(d.device_id)+'\\',5)">단절 5분</button>'+
        '<button onclick="cmd(\\'burst\\',\\''+esc(d.device_id)+'\\',10)">버스트 10분</button>'+
      '</div></div>';
  }).join('');
}

async function refresh(){
  try{
    const [s,i]=await Promise.all([api('/api/state'),api('/api/inventory')]);
    inv=i.devices||[];
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
        facility_type:$('#f_ft').value})});
    say('주입 완료: '+r.device_id+' — 러너 리로드 요청됨',false);
    $('#f_id').value='';$('#f_sec').value='';$('#f_site').value='';
    setTimeout(refresh,800);
  }catch(e){say(e.message,true)}
  finally{btn.disabled=false}
};

window.cmd=cmd;
refresh();
setInterval(refresh,2000);
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
)
