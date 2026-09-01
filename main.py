#!/usr/bin/env python3
"""Usage Tray — Kimi & GLM coding plan quota monitor.

System tray icon.  Hovering over the icon shows a graphical popup card
(tkinter, frameless, always-on-top) with progress bars and reset times.
Right-click menu: Open Panel / Refresh Now / Exit.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import math
import os
import threading
import time
import queue
import tkinter as tk
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pystray
import requests
from PIL import Image
from pystray._util import win32 as pwin

# ------------------------------------------------------------------ config

VERSION = "1.0.0"
APP_DIR = Path(__file__).resolve().parent
ICON_PATH = APP_DIR / "assets" / "icon.ico"
LOG_PATH = APP_DIR / "logs" / "usage-tray.log"
PORT = 26000
POLL_INTERVAL = 300  # seconds

KIMI_API = "https://api.kimi.com/coding/v1/usages"
GLM_API = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"

LOG_PATH.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("usage-tray")

# ------------------------------------------------------------------ win32

_user32 = ctypes.windll.user32
_shell32 = ctypes.windll.shell32
_dwmapi = ctypes.windll.dwmapi


def _set_dpi_aware() -> bool:
    """Make the process DPI-aware so GetCursorPos and Shell_NotifyIconGetRect
    use the same (physical) coordinate space on scaled displays."""
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return True
    except Exception:
        try:
            _user32.SetProcessDPIAware()
            return True
        except Exception:
            return False


DPI_AWARE = _set_dpi_aware()
try:
    UI_SCALE = max(1.0, _user32.GetDpiForSystem() / 96.0)
except Exception:
    UI_SCALE = 1.0
log.info("DPI aware=%s scale=%.2f", DPI_AWARE, UI_SCALE)


def _s(px: float) -> int:
    """Scale a pixel dimension for high-DPI displays."""
    return int(round(px * UI_SCALE))

class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("guidItem", ctypes.c_byte * 16),
    ]


def get_tray_icon_rect(icon: pystray.Icon) -> tuple[int, int, int, int] | None:
    """Return (left, top, right, bottom) of the tray icon, or None."""
    if not icon._hwnd:
        return None
    nii = NOTIFYICONIDENTIFIER()
    nii.cbSize = ctypes.sizeof(NOTIFYICONIDENTIFIER)
    nii.hWnd = icon._hwnd
    nii.uID = 0  # pystray passes hID which ctypes silently drops; effective uID is 0
    rect = wt.RECT()
    hr = _shell32.Shell_NotifyIconGetRect(ctypes.byref(nii), ctypes.byref(rect))
    if hr != 0:
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def get_cursor_pos() -> tuple[int, int] | None:
    pt = wt.POINT()
    if _user32.GetCursorPos(ctypes.byref(pt)):
        return pt.x, pt.y
    return None


def set_dark_titlebar(hwnd: int, dark: bool = True) -> None:
    DWMWA_USE_IMMERSIVE_DARK_MODE = 20
    val = ctypes.c_int(1 if dark else 0)
    _dwmapi.DwmSetWindowAttribute(
        wt.HWND(hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE,
        ctypes.byref(val), ctypes.sizeof(val))


def make_clickthrough(hwnd: int) -> None:
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080
    ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)


def set_round_corners(hwnd: int) -> None:
    """Win11 DWM rounded window corners (DWMWCP_ROUND)."""
    DWMWA_WINDOW_CORNER_PREFERENCE = 33
    DWMWCP_ROUND = 2
    val = ctypes.c_int(DWMWCP_ROUND)
    _dwmapi.DwmSetWindowAttribute(
        wt.HWND(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
        ctypes.byref(val), ctypes.sizeof(val))


def rect_hoverable(rect: tuple[int, int, int, int]) -> bool:
    """True when the tray icon rect is in a hoverable state.

    When the overflow flyout is closed, Shell_NotifyIconGetRect reports the
    chevron button's rect (full taskbar height) — hovering that must NOT show
    the card.  When the flyout is open, the rect moves above the taskbar to
    the icon's real position.  A pinned taskbar icon is shorter than the
    taskbar band, so it stays hoverable.
    """
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    for wnd_name in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        tb = _user32.FindWindowW(wnd_name, None)
        if not tb:
            continue
        r = wt.RECT()
        _user32.GetWindowRect(tb, ctypes.byref(r))
        tb_h = r.bottom - r.top
        if r.left <= cx <= r.right and r.top <= cy <= r.bottom:
            # rect inside this taskbar: hoverable unless it IS the collapsed
            # chevron (its height equals the full taskbar height)
            return (rect[3] - rect[1]) < tb_h * 0.9
    # not on any taskbar → icon is in the open flyout → hoverable
    return True


# ------------------------------------------------------------------ model


@dataclass
class Window:
    label: str
    limit: float
    used: float
    remaining: float
    pct: float          # usage percent 0-100
    reset_epoch: float  # unix timestamp of reset or 0

    @property
    def reset_human(self) -> str:
        if self.reset_epoch <= 0:
            return ""
        if self.reset_epoch <= time.time():
            return "已重置"
        dt = datetime.fromtimestamp(self.reset_epoch)
        return f"{dt.month}月{dt.day}日 {dt.hour:02d}:{dt.minute:02d} 重置"


@dataclass
class Provider:
    name: str
    ok: bool = False
    error: str = ""
    windows: list[Window] = field(default_factory=list)
    level: str = ""
    fetched_at: float = 0.0


class UsageStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Provider] = {"kimi": Provider("Kimi"), "glm": Provider("GLM")}

    def snapshot(self) -> dict[str, Provider]:
        with self._lock:
            return {k: Provider(**vars(v)) for k, v in self._data.items()}

    def set(self, key: str, provider: Provider) -> None:
        with self._lock:
            self._data[key] = provider

    def fetch_all(self) -> None:
        threading.Thread(target=self._fetch_kimi, daemon=True).start()
        threading.Thread(target=self._fetch_glm, daemon=True).start()

    def _fetch_kimi(self) -> None:
        key = os.environ.get("KIMI_API_KEY", "")
        if not key:
            self.set("kimi", Provider("Kimi", error="未设置 KIMI_API_KEY"))
            return
        try:
            r = requests.get(KIMI_API, headers={"Authorization": f"Bearer {key}"},
                             timeout=15)
            r.raise_for_status()
            d = r.json()
            usage = d.get("usage") or {}
            limits = d.get("limits") or []
            wins: list[Window] = []
            # subscription-period quota
            top = (_num(usage.get("limit")), _num(usage.get("used")),
                   _num(usage.get("remaining")))
            if top[0] > 0:
                rt = usage.get("resetTime", "")
                rts = datetime.fromisoformat(rt.replace("Z", "+00:00")).timestamp() if rt else 0
                wins.append(Window("周额度", top[0], top[1], top[2],
                                   round(top[1] / top[0] * 100, 1), rts))
            # 5-hour windows
            for lim in limits:
                det = lim.get("detail") or {}
                w0 = lim.get("window") or {}
                dur = w0.get("duration", 0)
                unit = w0.get("timeUnit", "")
                if unit == "TIME_UNIT_MINUTE":
                    label = "5小时额度" if dur == 300 else (f"{int(dur)}分钟窗口" if dur < 60 else f"{int(dur // 60)}小时窗口")
                elif unit == "TIME_UNIT_HOUR":
                    label = f"{int(dur)}小时窗口"
                else:
                    label = f"{dur} {unit}"
                l, u, rem = _num(det.get("limit")), _num(det.get("used")), _num(det.get("remaining"))
                if l <= 0:
                    continue
                rt = det.get("resetTime", "")
                rts = datetime.fromisoformat(rt.replace("Z", "+00:00")).timestamp() if rt else 0
                wins.append(Window(label, l, u, rem, round(u / l * 100, 1), rts))
            wins.sort(key=lambda w: 0 if "5小时" in w.label else 1)
            level = (d.get("user", {}).get("membership", {}) or {}).get("level", "")
            level = level.replace("LEVEL_", "").title() if level else ""
            self.set("kimi", Provider("Kimi", True, windows=wins, level=level,
                                      fetched_at=time.time()))
            log.info("Kimi fetched: %s", [(w.label, w.used, w.limit) for w in wins])
        except Exception as exc:  # noqa: BLE001
            log.exception("Kimi fetch failed: %s", exc)
            self.set("kimi", Provider("Kimi", error=str(exc)))

    def _fetch_glm(self) -> None:
        key = os.environ.get("ZHIPU_API_KEY", "")
        if not key:
            self.set("glm", Provider("GLM", error="未设置 ZHIPU_API_KEY"))
            return
        try:
            r = requests.get(GLM_API, headers={"Authorization": key, "Accept": "application/json"},
                             timeout=15)
            r.raise_for_status()
            d = r.json()
            data = d.get("data") or {}
            wins: list[Window] = []
            for lim in data.get("limits") or []:
                unit = lim.get("unit", 0)
                if unit == 3:
                    label = "5小时额度"
                elif unit == 6:
                    label = "周额度"
                else:
                    label = f"unit={unit}"
                total = lim.get("usage", 0)
                used = lim.get("currentValue", 0)
                rem = lim.get("remaining", 0)
                pct = lim.get("percentage", 0)
                reset_ms = lim.get("nextResetTime", 0)
                rts = reset_ms / 1000.0 if reset_ms else 0.0
                wins.append(Window(label, float(total), float(used), float(rem),
                                   float(pct), rts))
            wins.sort(key=lambda w: 0 if "5小时" in w.label else 1)
            level = str(data.get("level", "")).title()
            self.set("glm", Provider("GLM", True, windows=wins, level=level,
                                     fetched_at=time.time()))
            log.info("GLM fetched: %s", [(w.label, w.used, w.limit) for w in wins])
        except Exception as exc:  # noqa: BLE001
            log.exception("GLM fetch failed: %s", exc)
            self.set("glm", Provider("GLM", error=str(exc)))


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


STORE = UsageStore()
ICON_REF: list = [None]

# ------------------------------------------------------------------ popup

POPUP_W = _s(360)


class PopupCard:
    """Frameless always-on-top tkinter window that acts as a hover card.

    Thread-safe: cross-thread callers only push events into a queue that is
    drained by the tkinter thread via ``after()``.  All widget access happens
    inside the tk thread.
    """

    def __init__(self):
        self._root: tk.Tk | None = None
        self._visible = False
        self._thread: threading.Thread | None = None
        self._closing = False
        self._events: "queue.Queue[tuple[str, Any]]" = queue.Queue()

    def start(self):
        self._thread = threading.Thread(target=self._tk_main, daemon=True)
        self._thread.start()

    def stop(self):
        self._closing = True

    # ---- runs in its own thread

    def _tk_main(self):
        try:
            self._root = tk.Tk()
            self._root.withdraw()
            self._root.overrideredirect(True)
            self._root.attributes("-topmost", True)
            self._root.attributes("-alpha", 0.0)
            self._root.configure(bg="#3b4a6e")  # border color; content sits on top
            self._root.tk.call("tk", "scaling", UI_SCALE)
            self._root.protocol("WM_DELETE_WINDOW", lambda: None)
            # init win32 style
            self._root.update_idletasks()
            hwnd = int(self._root.frame(), 16) if isinstance(self._root.frame(), str) else 0
            set_dark_titlebar(hwnd)
            set_round_corners(hwnd)
            make_clickthrough(hwnd) if False else None  # keep interactive for now
            self._build_ui()
            self._root.after(50, self._drain_events)
            self._root.mainloop()
        except Exception:
            log.exception("popup thread crashed")

    def _drain_events(self):
        """Consume cross-thread events inside the tk thread."""
        try:
            while True:
                kind, arg = self._events.get_nowait()
                if kind == "show":
                    self._do_show(*arg)
                elif kind == "hide":
                    self._do_hide()
        except queue.Empty:
            pass
        if self._root and not self._closing:
            self._root.after(50, self._drain_events)

    def _do_show(self, x: int, y: int):
        if not self._root or self._closing:
            return
        self._update_content(STORE.snapshot())
        w, h = POPUP_W, self._root.winfo_reqheight()
        px = max(0, x + 16 - w)
        py = max(0, y - h - 8)
        self._root.geometry(f"{w}x{h}+{px}+{py}")
        self._root.deiconify()
        self._root.attributes("-alpha", 1.0)
        self._visible = True

    def _do_hide(self):
        if not self._root:
            return
        self._root.withdraw()
        self._visible = False

    def _build_ui(self):
        c = self._root
        BG, CARD, CARD2 = "#0b1020", "#151c2f", "#1a2340"
        FG, MUTED, BORDER = "#e5e7eb", "#8b95a8", "#2a3550"
        CYAN, VIOLET = "#22d3ee", "#a78bfa"
        GREEN, YELLOW, RED = "#34d399", "#fbbf24", "#f87171"

        f = tk.Frame(c, bg=BG, padx=0, pady=0)
        f.pack(fill=tk.BOTH, expand=True, padx=_s(1), pady=_s(1))

        self._title = tk.Label(f, text="Coding Plan 额度", bg=BG, fg=FG,
                               font=("Microsoft YaHei UI", 16, "bold"))
        self._title.pack(pady=(_s(14), _s(4)), fill=tk.X)

        self._rows: dict[str, list[tk.Widget]] = {"kimi": [], "glm": []}
        for key, name, accent in (("kimi", "Kimi", CYAN), ("glm", "GLM", VIOLET)):
            head = tk.Frame(f, bg=CARD, padx=_s(14), pady=_s(10))
            head.pack(fill=tk.X, padx=_s(12), pady=(_s(8), 0))
            lbl = tk.Label(head, text=name, bg=CARD, fg=accent,
                           font=("Microsoft YaHei UI", 15, "bold"))
            lbl.pack(side=tk.LEFT)
            lvl = tk.Label(head, text="", bg=CARD, fg=MUTED,
                           font=("Microsoft YaHei UI", 12))
            lvl.pack(side=tk.RIGHT)
            body = tk.Frame(f, bg=CARD, padx=_s(14), pady=_s(10))
            body.pack(fill=tk.X, padx=_s(12), pady=(0, _s(2)))
            self._rows[key] = [head, body, lvl]

        self._foot = tk.Label(f, text="", bg=BG, fg=MUTED,
                              font=("Microsoft YaHei UI", 10))
        self._foot.pack(pady=(_s(4), _s(12)), fill=tk.X)

    def _update_content(self, snap: dict[str, Provider]):
        if not self._root:
            return
        try:
            for key in ("kimi", "glm"):
                p = snap[key]
                head, body, lvl_lbl = self._rows[key]
                for w in body.winfo_children():
                    w.destroy()
                lvl_lbl.config(text=p.level or "")
                if not p.ok:
                    tk.Label(body, text=p.error or "加载中…", bg="#151c2f", fg="#f87171",
                             font=("Microsoft YaHei UI", 13)).pack(anchor=tk.W)
                    continue
                for win in p.windows:
                    row = tk.Frame(body, bg="#151c2f")
                    row.pack(fill=tk.X, pady=_s(3))
                    top = tk.Frame(row, bg="#151c2f")
                    top.pack(fill=tk.X)
                    tk.Label(top, text=win.label, bg="#151c2f", fg="#8b95a8",
                             font=("Microsoft YaHei UI", 12)).pack(side=tk.LEFT)
                    val = tk.Label(top,
                                   text=f"{win.used:,.0f}/{win.limit:,.0f}  剩 {win.remaining:,.0f}",
                                   bg="#151c2f", fg="#e5e7eb",
                                   font=("Microsoft YaHei UI", 12, "bold"))
                    val.pack(side=tk.RIGHT)
                    # progress bar
                    bar_bg = tk.Frame(row, bg="#1a2340", height=_s(6))
                    bar_bg.pack(fill=tk.X, pady=(_s(3), _s(1)))
                    pct = min(win.pct, 100)
                    color = "#f87171" if pct >= 90 else "#fbbf24" if pct >= 70 else "#34d399"
                    bar = tk.Frame(bar_bg, bg=color, height=_s(6), width=int(_s(300) * pct / 100))
                    bar.pack(side=tk.LEFT, anchor=tk.W)
                    meta = tk.Label(row, text=f"已用 {win.pct:.0f}% · {win.reset_human}",
                                    bg="#151c2f", fg="#8b95a8",
                                    font=("Microsoft YaHei UI", 11))
                    meta.pack(anchor=tk.W)
            latest = max((p.fetched_at for p in snap.values()), default=0)
            if latest:
                ts = datetime.fromtimestamp(latest).strftime("%H:%M:%S")
                self._foot.config(text=f"更新于 {ts} · 每 5 分钟自动刷新")
        except Exception:
            log.exception("popup update failed")

    # ---- called from poll thread

    def show_at(self, x: int, y: int):
        self._events.put(("show", (x, y)))

    def hide(self):
        self._events.put(("hide", ()))


POPUP = PopupCard()

# ------------------------------------------------------------------ web UI

HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Coding Plan 额度</title>
<style>
:root{--bg:#0b1020;--card:#151c2f;--card2:#1a2340;--text:#e5e7eb;--muted:#8b95a8;
      --cyan:#22d3ee;--violet:#a78bfa;--green:#34d399;--yellow:#fbbf24;--red:#f87171;
      --border:#2a3550;--radius:16px}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:"Segoe UI","Microsoft YaHei",sans-serif;
     min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:32px 16px}
h1{font-size:1.5rem;font-weight:600;margin-bottom:4px}
.sub{color:var(--muted);font-size:.85rem;margin-bottom:28px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px;
      width:100%;max-width:860px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
      padding:24px;display:flex;flex-direction:column;gap:16px;transition:border .2s}
.card:hover{border-color:#3b4a6e}
.head{display:flex;align-items:center;gap:10px}
.dot{width:10px;height:10px;border-radius:50%}
.dot.ok{background:var(--green)}.dot.err{background:var(--red)}.dot.pend{background:var(--muted)}
.name{font-size:1.1rem;font-weight:600}
.lvl{margin-left:auto;font-size:.72rem;color:var(--muted);background:var(--card2);
     border:1px solid var(--border);padding:2px 10px;border-radius:20px}
.win{display:flex;flex-direction:column;gap:6px}
.win-row{display:flex;justify-content:space-between;align-items:baseline}
.win-label{font-size:.85rem;color:var(--muted)}
.win-val{font-size:1rem;font-weight:600;font-variant-numeric:tabular-nums}
.win-val small{font-size:.75rem;color:var(--muted);font-weight:400}
.bar{height:8px;border-radius:4px;background:var(--card2);overflow:hidden}
.bar>div{height:100%;border-radius:4px;transition:width .6s cubic-bezier(.4,0,.2,1)}
.meta{font-size:.72rem;color:var(--muted)}
.err{color:var(--red);font-size:.85rem;padding:12px 0}
.refresh{margin-top:8px;font-size:.75rem;color:var(--muted);text-align:center}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--muted);
      border-top-color:transparent;border-radius:50%;animation:sp 1s linear infinite;
      vertical-align:-2px;margin-right:4px}
@keyframes sp{to{transform:rotate(360deg)}}
button{margin-top:16px;background:var(--card2);color:var(--text);border:1px solid var(--border);
       padding:8px 20px;border-radius:8px;cursor:pointer;font-size:.85rem}
button:hover{border-color:#4a5a80;background:#202b4a}
</style>
</head>
<body>
<h1>Coding Plan 额度</h1>
<div class="sub">Kimi · GLM — 每 5 分钟自动刷新 · 本地端口 __PORT__</div>
<div class="grid" id="grid"></div>
<button onclick="fetchData()">手动刷新</button>
<div class="refresh" id="refresh"></div>
<script>
const PCT_COLOR = p => p>=90?'var(--red)':p>=70?'var(--yellow)':'var(--green)';
function bar(p,color){return `<div class="bar"><div style="width:${Math.min(p,100)}%;background:${color}"></div></div>`}
function card(p){
  const dot = p.ok?'ok':'err';
  let body='';
  if(p.error){ body=`<div class="err">${p.error}</div>`; }
  else if(!p.windows.length){ body=`<div class="err" style="color:var(--muted)">加载中…</div>`; }
  else{
    body = p.windows.map(w=>{
      const color=PCT_COLOR(w.pct);
      return `<div class="win">
        <div class="win-row"><span class="win-label">${w.label}</span>
          <span class="win-val">${w.used.toLocaleString()} <small>/ ${w.limit.toLocaleString()}（剩 ${w.remaining.toLocaleString()}）</small></span></div>
        ${bar(w.pct,color)}
        <div class="meta">已用 ${w.pct}% · ${w.resetHuman||''}</div>
      </div>`;
    }).join('');
  }
  return `<div class="card">
    <div class="head"><span class="dot ${dot}"></span><span class="name">${p.name}</span>
      ${p.level?`<span class="lvl">${p.level}</span>`:''}</div>
    ${body}
  </div>`;
}
async function fetchData(){
  document.getElementById('refresh').innerHTML='<span class="spin"></span>刷新中…';
  const r=await fetch('/api/usage'); const d=await r.json();
  document.getElementById('grid').innerHTML=d.providers.map(card).join('');
  document.getElementById('refresh').textContent='数据更新于 '+new Date(d.fetchedAt*1000).toLocaleTimeString();
}
fetchData(); setInterval(fetchData, 60000);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            body = HTML.replace("__PORT__", str(PORT)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/usage":
            snap = STORE.snapshot()
            items = []
            latest = 0.0
            for key in ("kimi", "glm"):
                p = snap[key]
                latest = max(latest, p.fetched_at)
                items.append({
                    "name": p.name, "ok": p.ok, "error": p.error, "level": p.level,
                    "windows": [{
                        "label": w.label, "limit": w.limit, "used": w.used,
                        "remaining": w.remaining, "pct": w.pct,
                        "resetHuman": w.reset_human,
                    } for w in p.windows],
                })
            body = json.dumps({"fetchedAt": latest, "providers": items},
                              ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("HTTP panel at http://127.0.0.1:%s", PORT)


# ------------------------------------------------------------------ hover loop

HOVER_PAD = 12  # extra px around icon rect that counts as hover


def hover_watch(icon: pystray.Icon):
    """Poll cursor position; show/hide popup when hovering the tray icon."""
    was_hover = False
    while True:
        try:
            pos = get_cursor_pos()
            rect = get_tray_icon_rect(icon)
            hover = False
            if pos and rect and rect_hoverable(rect):
                l, t, r, b = rect
                hover = (l - HOVER_PAD <= pos[0] <= r + HOVER_PAD and
                         t - HOVER_PAD <= pos[1] <= b + HOVER_PAD)
            if hover and not was_hover:
                log.info("hover show rect=%s cursor=%s", rect, pos)
                POPUP.show_at(rect[0], rect[1])
            elif not hover and was_hover:
                log.info("hover hide")
                POPUP.hide()
            was_hover = hover
        except Exception:
            pass
        time.sleep(0.15)


def poll_loop():
    while True:
        STORE.fetch_all()
        time.sleep(4)
        time.sleep(POLL_INTERVAL - 4)


def _hover_text(snap: dict[str, Provider]) -> str:
    lines = ["Coding Plan 额度"]
    for key in ("kimi", "glm"):
        p = snap[key]
        if not p.ok:
            lines.append(f"{p.name}: {p.error or '加载中…'}")
            continue
        parts = [f"{w.label} 剩{w.remaining:,.0f}/{w.limit:,.0f}（{100 - w.pct:.0f}%）"
                 for w in p.windows]
        lines.append(f"{p.name}: " + " · ".join(parts))
    return "\n".join(lines)


# ------------------------------------------------------------------ main


def main():
    icon = pystray.Icon(
        "usage-tray",
        icon=Image.open(ICON_PATH),
        title="",  # empty tooltip: suppress the native text box so the card is unobstructed
        menu=pystray.Menu(
            pystray.MenuItem("打开面板", lambda: webbrowser.open(f"http://127.0.0.1:{PORT}"), default=True),
            pystray.MenuItem("立即刷新", lambda: STORE.fetch_all()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: (POPUP.stop(), icon.stop())),
        ),
    )
    ICON_REF[0] = icon
    start_server()
    POPUP.start()
    # wait for tk thread to be ready
    deadline = time.time() + 5
    while POPUP._root is None and time.time() < deadline:
        time.sleep(0.1)
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=hover_watch, args=(icon,), daemon=True).start()
    log.info("tray running (pid %s)", os.getpid())
    icon.run()


if __name__ == "__main__":
    main()











