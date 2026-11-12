"""Telegram HTML widgets for the owner channel (Bot API 10.4).

A widget is one self-contained HTML document that Telegram renders inside the
message bubble, sandboxed with no network access. The bot never replaces the
HTML; it sends a compact JSON state (chat_data, at most 1024 bytes) and edits
that state when the picture changes. Taps on a task open its card in the chat;
the refresh button asks the bot for fresh state through a widget query.
"""

from __future__ import annotations

import json
import zlib
from typing import Any

MAX_DATA_BYTES = 1024
MAX_HTML_BYTES = 32768
MAX_COMPRESSED_BYTES = 4096
TASKS_PER_PROJECT = 5
TITLE_CHARS = 22

# Order matters: what needs the owner first, then what is stuck, then motion.
TASK_ORDER = ("o", "b", "a", "r", "q")

PANEL_HTML = """<style>
:root{color-scheme:light dark}
body{margin:0;font:13px/1.35 system-ui,sans-serif;color:var(--tg-theme-text-color,#111)}
svg{width:16px;height:16px;flex:none;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.w{padding:10px 12px 8px}
.h{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.h b{display:flex;align-items:center;gap:6px;font-size:15px}
.h b svg{width:18px;height:18px;color:var(--tg-theme-button-color,#3390ec)}
.m{color:var(--tg-theme-hint-color,#888);font-size:11px}
.tabs{display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap}
.tab{border:0;border-radius:12px;padding:3px 10px;font:inherit;font-size:12px;background:var(--tg-theme-secondary-bg-color,#eee);color:inherit}
.tab.on{background:var(--tg-theme-button-color,#3390ec);color:var(--tg-theme-button-text-color,#fff)}
.g{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:8px}
.c{border-radius:10px;padding:6px 2px 5px;text-align:center;background:var(--tg-theme-secondary-bg-color,#f1f1f1);font-size:11px;color:var(--tg-theme-hint-color,#888)}
.c i{display:flex;justify-content:center;align-items:center;gap:4px;font-style:normal;font-size:18px;font-weight:600;color:var(--tg-theme-text-color,#111)}
.c svg{width:14px;height:14px}
.bar{height:6px;border-radius:3px;overflow:hidden;display:flex;background:var(--tg-theme-secondary-bg-color,#eee);margin-bottom:6px}
.bar span{display:block;height:100%;transition:width .4s}
ul{list-style:none;margin:0;padding:0}
li{padding:6px 0;border-top:1px solid #8883;display:flex;gap:8px;align-items:center}
li em{font-style:normal;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
li .go{width:14px;height:14px;color:var(--tg-theme-hint-color,#888)}
.sa{color:#43a047}.sq{color:#f9a825}.sb{color:#e53935}.sr{color:#3390ec}.so{color:#8e24aa}
.f{display:flex;justify-content:space-between;align-items:center;margin-top:8px}
.k{display:flex;align-items:center;gap:5px;color:var(--tg-theme-destructive-text-color,#e53935);font-weight:600}
.btn{display:flex;align-items:center;gap:5px;border:0;border-radius:8px;padding:4px 12px;font:inherit;background:var(--tg-theme-button-color,#3390ec);color:var(--tg-theme-button-text-color,#fff)}
.btn svg{width:14px;height:14px}
.spin svg{animation:r 1s linear infinite}@keyframes r{to{transform:rotate(360deg)}}
.e{color:var(--tg-theme-hint-color,#888);padding:6px 0}
</style>
<div id="ic" style="display:none">
<svg id="i-a" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M10 8.5l5 3.5-5 3.5z"/></svg>
<svg id="i-q" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
<svg id="i-b" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M5.7 5.7l12.6 12.6"/></svg>
<svg id="i-r" viewBox="0 0 24 24"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>
<svg id="i-o" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/></svg>
<svg id="i-go" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>
</div>
<div class="w"><div class="h"><b><svg viewBox="0 0 24 24"><path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/></svg>Continuity</b><span class="m" id="ago"></span></div>
<div class="tabs" id="tabs"></div><div class="g" id="g"></div><div class="bar" id="bar"></div>
<ul id="l"></ul><div class="f"><span id="wait"></span><button class="btn" id="r"><svg viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.4-5.7M20 4v6h-6"/></svg>刷新</button></div></div>
<script>
const T=window.TelegramWidget||{onEvent(){},request(){return Promise.reject()},openTelegramLink(){}};
const C=[["a","进行中","#43a047"],["q","排队","#f9a825"],["b","阻塞","#e53935"],["r","待审","#3390ec"]];
let S=null,P=0;
const $=id=>document.getElementById(id);
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function ic(k,c){const n=$("i-"+k);if(!n)return el("span");const s=n.cloneNode(true);s.removeAttribute("id");if(c)s.setAttribute("class",c);return s}
function load(){try{S=JSON.parse(T.chatData)}catch(e){S=null}}
function ago(){if(!S)return;const s=Math.max(0,Date.now()/1e3-S.t);$("ago").textContent=s<60?"刚刚更新":s<3600?Math.floor(s/60)+" 分钟前":Math.floor(s/3600)+" 小时前"}
function draw(){
 if(!S||!S.p||!S.p.length){$("l").replaceChildren(el("li","e","暂无数据"));return}
 if(P>=S.p.length)P=0;
 const tabs=$("tabs");tabs.replaceChildren();
 if(S.p.length>1)S.p.forEach((p,i)=>{const b=el("button","tab"+(i==P?" on":""),p[0]);b.onclick=()=>{P=i;draw()};tabs.append(b)});
 const p=S.p[P],n={a:p[1],q:p[2],b:p[3],r:p[4]},tot=p[1]+p[2]+p[3]+p[4];
 $("g").replaceChildren(...C.map(([k,l])=>{const c=el("div","c"),i=el("i");i.append(ic(k,"s"+k),String(n[k]));c.append(i,l);return c}));
 $("bar").replaceChildren(...C.map(([k,,col])=>{const s=el("span");s.style.width=(tot?n[k]*100/tot:0)+"%";s.style.background=col;return s}));
 const l=$("l");l.replaceChildren();
 (p[5]||[]).forEach(([st,ti,key])=>{const li=el("li");li.append(ic(st,"s"+st),el("em",null,ti));
  if(S.u&&key){li.append(ic("go","go"));li.onclick=()=>T.openTelegramLink("https://t.me/"+S.u+"?start=c_"+key)}l.append(li)});
 if(!l.children.length)l.append(el("li","e","没有进行中的任务"));
 const w=$("wait");w.replaceChildren();
 if(S.w){const k=el("span","k");k.append(ic("o"),"等你处理 "+S.w+" 件");w.append(k)}else w.append(el("span","m","没有等你处理的事"));
 ago()}
$("r").onclick=()=>{const b=$("r");b.className="btn spin";T.request("refresh").then(a=>{if(a&&a.result){try{S=JSON.parse(a.result)}catch(e){}}draw()},()=>{}).finally(()=>{b.className="btn"})};
T.onEvent("chatDataChanged",()=>{load();draw()});
T.onEvent("activated",ago);
setInterval(ago,30000);load();draw();
</script>"""


def html_size() -> tuple[int, int]:
    raw = PANEL_HTML.encode("utf-8")
    return len(raw), len(zlib.compress(raw, 9))


def encode(state: dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"))


def fit(state: dict[str, Any]) -> str:
    """Drop task rows, then shorten titles, until the state fits chat_data."""
    projects = state["p"]
    while len(encode(state).encode("utf-8")) > MAX_DATA_BYTES:
        longest = max(projects, key=lambda row: len(row[5]), default=None)
        if longest and longest[5]:
            longest[5].pop()
            continue
        for row in projects:
            row[0] = row[0][:12]
        if len(encode(state).encode("utf-8")) > MAX_DATA_BYTES:
            projects.pop()
        if not projects:
            break
    return encode(state)


def digest_view(data: str) -> str:
    """The state without its timestamp, to see whether the picture changed."""
    try:
        state = json.loads(data)
    except ValueError:
        return data
    state.pop("t", None)
    return encode(state)


__all__ = [
    "MAX_COMPRESSED_BYTES",
    "MAX_DATA_BYTES",
    "MAX_HTML_BYTES",
    "PANEL_HTML",
    "TASKS_PER_PROJECT",
    "TASK_ORDER",
    "TITLE_CHARS",
    "digest_view",
    "encode",
    "fit",
    "html_size",
]
