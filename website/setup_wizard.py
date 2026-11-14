# -*- coding: utf-8 -*-
"""首次运行「快速配置向导」——公开白板版的入门面板。

把已有的三块能力串成一条引导流：填模型 API → 扫码登录 QQ → 看后台状态。
重活全部复用现有端点（napcat/qr、napcat/status、health），本模块只补：
  · 向导页 /setup 与少量 setup 专用 API
  · 安全地把 API key 写进 config.env（文本/视觉等通道是进程启动时读 env，
    所以保存后需重启 bot 才生效——向导明确提示并提供受控重启）

安全与门控（与 AGENTS.md 一致）：
  · 所有端点都要求管理员会话（ceylan_admin），不引入新的认证体系。
  · 「完成」落一个 data/.setup_complete 标记；一旦完成，向导页重定向回面板、
    写配置/测试/重启等端点一律 409——已配置的生产部署里本模块完全惰性。
  · 只允许写白名单内的 LLM_* 通道键；绝不回显/记录 api_key。
  · 受控重启只重启 qqbot 服务，绝不触碰 NapCat 容器（QQ 登录态红线）。
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

from flask import jsonify, request, session, redirect, Response


# 向导暴露的模型通道（都是 bot 启动时从 env 读的 LLM_* 前缀）。
CHANNELS = {
    "text":    ("LLM_TEXT_",    "文本对话 · 必需"),
    "vision":  ("LLM_VISION_",  "视觉识别 · 可留空沿用文本"),
    "summary": ("LLM_SUMMARY_", "记忆摘要 · 可留空"),
    "deep":    ("LLM_DEEP_",    "深度长文 · 可留空"),
}
_FIELDS = ("base_url", "model", "api_key")


def _data_dir():
    return Path(os.getenv("DATA_DIR", "./data"))


def _marker():
    return _data_dir() / ".setup_complete"


def _setup_complete():
    return _marker().is_file()


def _config_path():
    return Path(__file__).resolve().parent.parent / "config.env"


def _update_env(updates):
    """就地更新 config.env 的指定键，保留注释与其它键；原子写回。绝不打印值。"""
    path = _config_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen = set()
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key = s.split("=", 1)[0].strip()
            if key in updates:
                out.append("%s=%s" % (key, updates[key]))
                seen.add(key)
                continue
        out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append("%s=%s" % (key, val))
    tmp = path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _read_env():
    path = _config_path()
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, _, v = s.partition("=")
                env[k.strip()] = v.strip()
    return env


def _channels_state():
    env = _read_env()
    out = {}
    for role, (prefix, label) in CHANNELS.items():
        out[role] = {
            "label": label,
            "base_url": env.get(prefix + "BASE_URL", ""),
            "model": env.get(prefix + "MODEL", ""),
            "has_key": bool(env.get(prefix + "API_KEY", "")),
        }
    return out


def _require_admin():
    if not session.get("ceylan_admin"):
        return jsonify({"error": "unauthorized", "need_login": True}), 401
    return None


def _require_incomplete():
    if _setup_complete():
        return jsonify({"error": "setup_already_complete",
                        "message": "初始化已完成，请在常规面板修改配置"}), 409
    return None


def install(app):
    @app.get("/setup")
    def setup_page():
        if not session.get("ceylan_admin"):
            return redirect("/admin/login")
        if _setup_complete():
            return redirect("/admin/dashboard")
        return Response(WIZARD_HTML, mimetype="text/html")

    @app.get("/admin/api/setup/state")
    def setup_state():
        err = _require_admin()
        if err:
            return err
        return jsonify({"ok": True, "complete": _setup_complete(),
                        "channels": _channels_state(),
                        "config_exists": _config_path().exists()})

    @app.post("/admin/api/setup/save-keys")
    def setup_save_keys():
        err = _require_admin() or _require_incomplete()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        channels = data.get("channels") or {}
        if not isinstance(channels, dict):
            return jsonify({"error": "channels 格式错误"}), 400
        updates = {}
        for role, vals in channels.items():
            if role not in CHANNELS:
                return jsonify({"error": "未知通道：%s" % role}), 400
            if not isinstance(vals, dict):
                return jsonify({"error": "通道 %s 格式错误" % role}), 400
            prefix = CHANNELS[role][0]
            for field in _FIELDS:
                if field not in vals:
                    continue
                v = vals[field]
                if not isinstance(v, str):
                    return jsonify({"error": "%s.%s 必须是字符串" % (role, field)}), 400
                v = v.strip()
                if field == "base_url" and v and not re.match(r"^https?://", v):
                    return jsonify({"error": "%s 的 base_url 需以 http(s):// 开头" % role}), 400
                env_key = prefix + {"base_url": "BASE_URL", "model": "MODEL",
                                    "api_key": "API_KEY"}[field]
                updates[env_key] = v
        if not updates:
            return jsonify({"error": "没有要保存的字段"}), 400
        try:
            _update_env(updates)
        except OSError as e:
            return jsonify({"error": "写入 config.env 失败：%s" % type(e).__name__}), 500
        return jsonify({"ok": True, "saved": len(updates), "restart_needed": True,
                        "message": "已保存到 config.env；文本/视觉通道需重启 bot 生效"})

    @app.post("/admin/api/setup/test-model")
    def setup_test_model():
        err = _require_admin() or _require_incomplete()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        base_url = str(data.get("base_url") or "").strip().rstrip("/")
        model = str(data.get("model") or "").strip()
        api_key = str(data.get("api_key") or "").strip()
        if not base_url or not re.match(r"^https?://", base_url):
            return jsonify({"ok": False, "error": "base_url 需以 http(s):// 开头"}), 400
        if not model:
            return jsonify({"ok": False, "error": "请填写模型 ID"}), 400
        import urllib.request
        import urllib.error
        url = base_url + "/chat/completions"
        payload = json.dumps({"model": model,
                              "messages": [{"role": "user", "content": "ping"}],
                              "max_tokens": 5}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + api_key})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read(4096).decode("utf-8", "replace")
            latency = int((time.time() - t0) * 1000)
            try:
                parsed = json.loads(body)
                ok = "choices" in parsed or "id" in parsed
                preview = ""
                if ok and parsed.get("choices"):
                    preview = str(parsed["choices"][0].get("message", {}).get("content", ""))[:40]
                return jsonify({"ok": bool(ok), "latency_ms": latency,
                                "reply_preview": preview,
                                "error": "" if ok else "响应不含 choices"})
            except ValueError:
                return jsonify({"ok": False, "latency_ms": latency,
                                "error": "响应不是合法 JSON"})
        except urllib.error.HTTPError as e:
            return jsonify({"ok": False, "error": "HTTP %d（检查 key/模型/地址）" % e.code})
        except Exception as e:
            return jsonify({"ok": False, "error": "连接失败：%s" % type(e).__name__})

    @app.post("/admin/api/setup/restart")
    def setup_restart():
        err = _require_admin() or _require_incomplete()
        if err:
            return err
        # 只重启 qqbot 服务；绝不触碰 NapCat 容器。
        try:
            active = subprocess.run(["systemctl", "is-active", "qqbot"],
                                    capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            active = ""
        if active == "active":
            try:
                subprocess.Popen(["systemctl", "restart", "qqbot"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                return jsonify({"ok": True, "method": "systemd",
                                "message": "已请求重启 qqbot，约几秒后刷新"})
            except Exception as e:
                return jsonify({"ok": False, "error": type(e).__name__})
        return jsonify({"ok": False, "manual": True,
                        "message": "未检测到 systemd qqbot 服务；请手动重启：python launcher.py"})

    @app.post("/admin/api/setup/complete")
    def setup_complete():
        err = _require_admin() or _require_incomplete()
        if err:
            return err
        try:
            _data_dir().mkdir(parents=True, exist_ok=True)
            _marker().write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        except OSError as e:
            return jsonify({"error": "无法写完成标记：%s" % type(e).__name__}), 500
        return jsonify({"ok": True, "message": "初始化完成"})


WIZARD_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>快速配置向导 · QingyiPersonal</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#2b3440;--tx:#e6edf3;--dim:#8b98a5;--ac:#4a9eff;--ok:#3fb950;--warn:#d29922;--err:#f85149}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.6 system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--dim);margin:0 0 24px}
.steps{display:flex;gap:8px;margin-bottom:24px}
.step{flex:1;text-align:center;padding:10px;border:1px solid var(--line);border-radius:10px;color:var(--dim);cursor:pointer;background:var(--card)}
.step.on{border-color:var(--ac);color:var(--tx)}
.step.done{border-color:var(--ok)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px}
.card h2{font-size:16px;margin:0 0 12px}
label{display:block;color:var(--dim);font-size:12px;margin:10px 0 4px}
input{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:#0d1117;color:var(--tx);font-size:13px}
.row{display:flex;gap:10px}.row>div{flex:1}
button{padding:9px 16px;border:none;border-radius:8px;background:var(--ac);color:#fff;cursor:pointer;font-size:13px;margin-top:14px}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--tx)}
button:disabled{opacity:.5;cursor:default}
.hint{color:var(--dim);font-size:12px;margin-top:8px}
.msg{margin-top:10px;font-size:13px}.msg.ok{color:var(--ok)}.msg.err{color:var(--err)}.msg.warn{color:var(--warn)}
.chan{border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px}
.chan .t{font-weight:600;margin-bottom:2px}.chan .d{color:var(--dim);font-size:12px}
#qr{max-width:240px;border-radius:10px;border:1px solid var(--line);display:none;margin-top:12px}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;border:1px solid var(--line)}
.badge.ok{color:var(--ok);border-color:var(--ok)}.badge.warn{color:var(--warn);border-color:var(--warn)}
pre{background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;font-size:12px}
a{color:var(--ac)}
</style></head><body><div class="wrap">
<h1>快速配置向导</h1>
<p class="sub">三步把机器人跑起来：填模型 API → 扫码登录 QQ → 查看后台。全程在本机，密钥只写进 config.env。</p>
<div class="steps">
  <div class="step on" id="st0" onclick="go(0)">① 模型 API</div>
  <div class="step" id="st1" onclick="go(1)">② QQ 登录</div>
  <div class="step" id="st2" onclick="go(2)">③ 后台状态</div>
</div>

<div id="p0">
  <div class="card"><h2>模型通道</h2>
    <p class="hint">至少填「文本对话」。OpenAI 兼容接口即可（base_url 通常以 /v1 结尾）。保存后需重启 bot 生效。</p>
    <div id="chans"></div>
    <button onclick="saveKeys()">保存到 config.env</button>
    <button class="ghost" onclick="restartBot()">保存并重启服务</button>
    <div class="msg" id="savemsg"></div>
  </div>
</div>

<div id="p1" style="display:none">
  <div class="card"><h2>QQ 账号登录</h2>
    <p class="hint">NapCat 容器承载 QQ 登录。未登录时会出现二维码，用 QQ 扫码。状态每 3 秒刷新。</p>
    <div>状态：<span class="badge" id="napstate">检测中…</span></div>
    <img id="qr" alt="登录二维码">
    <div class="msg" id="napmsg"></div>
    <button class="ghost" onclick="pollNap()">立即刷新</button>
  </div>
</div>

<div id="p2" style="display:none">
  <div class="card"><h2>后台状态</h2>
    <pre id="health">加载中…</pre>
    <button class="ghost" onclick="loadHealth()">刷新</button>
  </div>
  <div class="card"><h2>完成</h2>
    <p class="hint">确认 API 已保存、QQ 已登录后，点下方完成初始化。之后向导关闭，改用常规面板。</p>
    <button onclick="finish()">完成初始化，进入面板</button>
    <div class="msg" id="finmsg"></div>
  </div>
</div>

<p class="hint">返回 <a href="/admin/dashboard">常规面板</a> · 登录 <a href="/admin/login">/admin/login</a></p>
</div>
<script>
var ROLES=[["text","文本对话 · 必需"],["vision","视觉识别 · 可留空"],["summary","记忆摘要 · 可留空"],["deep","深度长文 · 可留空"]];
function $(id){return document.getElementById(id)}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})}
function go(n){for(var i=0;i<3;i++){$("p"+i).style.display=i===n?"":"none";$("st"+i).className="step"+(i===n?" on":"")}if(n===1)pollNap();if(n===2)loadHealth()}
function buildChans(st){var h="";ROLES.forEach(function(r){var c=(st&&st.channels&&st.channels[r[0]])||{};
 h+='<div class="chan"><div class="t">'+r[0]+'</div><div class="d">'+r[1]+'</div>'
 +'<label>Base URL</label><input id="'+r[0]+'_base_url" value="'+esc(c.base_url)+'" placeholder="https://api.example.com/v1">'
 +'<div class="row"><div><label>模型 ID</label><input id="'+r[0]+'_model" value="'+esc(c.model)+'" placeholder="model-name"></div>'
 +'<div><label>API Key'+(c.has_key?'（已配置，留空不改）':'')+'</label><input id="'+r[0]+'_api_key" type="password" placeholder="'+(c.has_key?'••••••••':'sk-…')+'"></div></div>'
 +'<button class="ghost" onclick="testChan(\''+r[0]+'\')">测试连接</button><span class="msg" id="'+r[0]+'_test"></span></div>'});
 $("chans").innerHTML=h}
function collect(role){var o={};["base_url","model","api_key"].forEach(function(f){var v=$(role+"_"+f).value.trim();if(v!=="")o[f]=v});return o}
function testChan(role){var m=$(role+"_test");m.className="msg";m.textContent="测试中…";
 var body=collect(role);if(!body.base_url||!body.model){m.className="msg warn";m.textContent="请先填 base_url 和模型";return}
 fetch("/admin/api/setup/test-model",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})
 .then(function(r){return r.json()}).then(function(d){if(d.ok){m.className="msg ok";m.textContent="✓ 连通 "+(d.latency_ms||"?")+"ms"+(d.reply_preview?" · "+d.reply_preview:"")}else{m.className="msg err";m.textContent="✗ "+(d.error||"失败")}})
 .catch(function(e){m.className="msg err";m.textContent="请求失败"})}
function saveKeys(){var ch={};ROLES.forEach(function(r){var o=collect(r[0]);if(Object.keys(o).length)ch[r[0]]=o});
 var m=$("savemsg");if(!Object.keys(ch).length){m.className="msg warn";m.textContent="没有可保存的字段";return}
 fetch("/admin/api/setup/save-keys",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({channels:ch})})
 .then(function(r){return r.json()}).then(function(d){if(d.ok){m.className="msg ok";m.textContent="✓ "+d.message;loadState()}else{m.className="msg err";m.textContent=d.error||d.message||"保存失败"}})
 .catch(function(){m.className="msg err";m.textContent="请求失败"})}
function restartBot(){saveKeys();var m=$("savemsg");
 fetch("/admin/api/setup/restart",{method:"POST"}).then(function(r){return r.json()}).then(function(d){
  if(d.ok){m.className="msg ok";m.textContent="✓ "+d.message}else{m.className="msg warn";m.textContent=d.message||"请手动重启"}}).catch(function(){})}
function pollNap(){fetch("/admin/api/bot/napcat/status").then(function(r){return r.json()}).then(function(d){
 var b=$("napstate");var online=d.online;var txt=online?"在线":(d.has_qr?"待扫码":(d.container==="running"?"容器运行中·未登录":"容器未运行"));
 b.textContent=txt;b.className="badge "+(online?"ok":"warn");
 var q=$("qr");if(d.has_qr&&!online){q.src="/admin/api/bot/napcat/qr?t="+Date.now();q.style.display="block"}else{q.style.display="none"}
 $("napmsg").textContent=online?"✓ QQ 已登录":(d.container!=="running"?"NapCat 容器未运行：先运行 python setup.py --napcat 或手动启动容器":"用 QQ 扫描二维码登录");
}).catch(function(){$("napstate").textContent="无法获取"})}
function loadHealth(){fetch("/admin/api/bot/health").then(function(r){return r.json()}).then(function(d){$("health").textContent=JSON.stringify(d,null,2)}).catch(function(){$("health").textContent="无法获取健康状态"})}
function finish(){var m=$("finmsg");fetch("/admin/api/setup/complete",{method:"POST"}).then(function(r){return r.json()}).then(function(d){
 if(d.ok){m.className="msg ok";m.textContent="✓ 完成，正在进入面板…";setTimeout(function(){location.href="/admin/dashboard"},900)}else{m.className="msg err";m.textContent=d.error||d.message||"失败"}}).catch(function(){m.className="msg err";m.textContent="请求失败"})}
function loadState(){fetch("/admin/api/setup/state").then(function(r){return r.json()}).then(function(d){if(d.complete){location.href="/admin/dashboard";return}buildChans(d)}).catch(function(){buildChans(null)})}
setInterval(function(){if($("p1").style.display!=="none")pollNap()},3000);
loadState();
</script></body></html>
"""
