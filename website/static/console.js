
(function(){
  'use strict';

  /* ---------- helpers ---------- */
  var $ = function(id){ return document.getElementById(id); };
  var esc = function(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  }); };
  var num = function(v, d){ var n = Number(v); return isFinite(n) ? n : (d || 0); };

  function toast(msg, type){
    var box = $('toast'); if(!box) return;
    var el = document.createElement('div');
    el.className = 'toast ' + (type || '');
    el.textContent = String(msg);
    box.appendChild(el);
    setTimeout(function(){ el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 3200);
    setTimeout(function(){ if(el.parentNode) el.parentNode.removeChild(el); }, 3650);
  }

  var _loginJumping = false;
  function sessionExpired(){
    if(_loginJumping) return;
    _loginJumping = true;
    toast('登录已过期，正在跳转登录页…', 'warn');
    setTimeout(function(){ location.href = '/admin/login'; }, 1200);
  }

  // 统一的响应处理：后端现在对 API 返回 401 JSON；
  // 万一还是拿到 HTML（旧版行为或 nginx 出错），也不能让 JSON.parse 崩掉。
  async function _readJson(r){
    if(!r) return null;
    if(r.status === 401){
      var body = null;
      try{ body = await r.json(); }catch(e){}
      if(body && body.need_login) sessionExpired();
      return null;
    }
    var ct = (r.headers.get('content-type') || '').toLowerCase();
    if(ct.indexOf('json') < 0){
      // 拿到的不是 JSON，多半是被重定向到登录页了
      if(r.redirected || r.status === 302) sessionExpired();
      else toast('请求返回了非 JSON 内容（HTTP ' + r.status + '）', 'err');
      return null;
    }
    try{ return await r.json(); }
    catch(e){ toast('响应解析失败: ' + e.message, 'err'); return null; }
  }

  // 读取总期限覆盖响应体和短暂重试；写请求可能执行长任务，保持等待且不重发。
  // 公网 Cloudflare 边缘对每个请求约 30% 概率 502（源站本身是 200/5ms），
  // 单次重试会把约 31% 的总览首屏留成空白。三次有界重试把单次失败压到 ~3%。
  async function _requestJson(url, opts){
    var method = String(opts.method || 'GET').toUpperCase();
    if(method !== 'GET') return await _readJson(await fetch(url, opts));
    var controller = new AbortController();
    var signal = opts.signal ? AbortSignal.any([opts.signal, controller.signal]) : controller.signal;
    var timer = setTimeout(function(){ controller.abort(); }, 8000);
    var ATTEMPTS = 3;
    try{
      for(var attempt = 0; attempt < ATTEMPTS; attempt++){
        var r = await fetch(url, Object.assign({}, opts, {signal: signal}));
        var retryable = [502, 503, 504].indexOf(r.status) >= 0;
        if(retryable && attempt < ATTEMPTS - 1){
          if(r.body) await r.body.cancel();
          await new Promise(function(resolve){ setTimeout(resolve, 200 * (attempt + 1)); });
          continue;
        }
        var d = await _readJson(r);
        return method === 'GET' && !r.ok ? null : d;
      }
    }finally{ clearTimeout(timer); }
  }

  async function api(path, method, data, q){
    try{
      var url = '/admin/api/bot/' + path;
      // 可选查询参数（例如 control?slim=1 只要群数量，不拉聊天记录）
      if(q){
        var qs = Object.keys(q).map(function(k){
          return encodeURIComponent(k) + '=' + encodeURIComponent(q[k]);
        }).join('&');
        if(qs) url += (url.indexOf('?') >= 0 ? '&' : '?') + qs;
      }
      var opts = { method: method || 'GET', credentials: 'same-origin', headers: {'Content-Type':'application/json'} };
      if(data) opts.body = JSON.stringify(data);
      return await _requestJson(url, opts);
    }catch(e){ toast('请求失败: ' + e.message, 'err'); return null; }
  }

  async function raw(url, opts){
    try{
      return await _requestJson(url, Object.assign({credentials:'same-origin'}, opts || {}));
    }catch(e){ return null; }
  }

  // CSRF 令牌是绑在 session 上的，一次页面生命周期里不会变。
  // 以前每次保存都先 GET 一次 effective-config 只为拿它 —— 那是一整次
  // 往返（公网走隧道），也是「点保存卡一下」的一部分。缓存起来只取一次。
  var _csrfCache = '';
  async function csrfToken(){
    if (_csrfCache) return _csrfCache;
    var cfg = await api('effective-config');
    _csrfCache = (cfg && cfg.control_csrf) || '';
    return _csrfCache;
  }

  async function controlPost(payload){
    try{
      var csrf = await csrfToken();
      var r = await fetch('/admin/api/bot/control', {
        method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
        body: JSON.stringify(payload)
      });
      return await r.json();
    }catch(e){ return { ok:false, error:e.message }; }
  }

  // 把任意值安全转成显示用字符串（对象取 name/key/title，避免 [object Object]）
  function str(v){
    if(v === null || v === undefined) return '';
    if(typeof v === 'object') return String(v.name || v.key || v.title || v.label || '');
    return String(v);
  }

  function num(v, dflt){
    var n = Number(v);
    return isNaN(n) ? (dflt === undefined ? 0 : dflt) : n;
  }

  function kv(k, v, cls){
    return '<div class="kv"><span class="k">' + esc(k) + '</span><span class="v ' + (cls||'') + '">' + v + '</span></div>';
  }
  function bar(name, pct, cls){
    var p = Math.max(0, Math.min(100, num(pct)));
    return '<div class="bar-lab"><span>' + esc(name) + '</span><b>' + p.toFixed(0) + '%</b></div>' +
           '<div class="bar ' + (cls||'') + '"><i style="width:' + p + '%"></i></div>';
  }
  function relTime(ts){
    var t = num(ts); if(!t) return '--';
    var d = Math.floor(Date.now()/1000 - t);
    if(d < 0) d = 0;
    if(d < 60) return d + ' 秒前';
    if(d < 3600) return Math.floor(d/60) + ' 分钟前';
    if(d < 86400) return Math.floor(d/3600) + ' 小时前';
    return Math.floor(d/86400) + ' 天前';
  }

  /* ---------- state ---------- */
  var STATE = { view:'overview', control:null, cfg:null, sessions:[], groups:[], emojisLoaded:false, timer:null };

  /* ---------- nav ---------- */
  var LOADERS = {
    overview: loadOverview, qingyi: loadQingyi, persona: loadPersona, memory: loadMemory,
    advanced: function(){ bindAdvanced(); loadAdvanced(); },
    models: function(){ loadModels(); loadAsr(); }, style: function(){ loadStyle(); loadHuman(); loadTools(); loadDraws(); loadCost(); }, monitor: loadMonitor,
    emojis: loadEmojis, visits: loadVisits
  };

  function switchView(name){
    STATE.view = name;
    document.querySelectorAll('.nav-item').forEach(function(b){
      b.classList.toggle('on', b.getAttribute('data-view') === name);
    });
    document.querySelectorAll('.view').forEach(function(v){
      v.classList.toggle('on', v.id === 'view-' + name);
    });
    if(LOADERS[name]) LOADERS[name]();
  }

  document.querySelectorAll('.nav-item').forEach(function(b){
    b.addEventListener('click', function(){ switchView(b.getAttribute('data-view')); });
  });
  $('btn-refresh').addEventListener('click', function(){
    toast('刷新中…');
    if(LOADERS[STATE.view]) LOADERS[STATE.view]();
  });

  /* ---------- clock ---------- */
  function tickClock(){
    var d = new Date();
    var p = function(n){ return n < 10 ? '0' + n : '' + n; };
    var el = $('top-clock');
    if(el) el.textContent = p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  setInterval(tickClock, 1000); tickClock();

  /* ---------- overview ---------- */
  var overviewRequest = 0;
  async function loadOverview(){
    var requestId = ++overviewRequest;
    loadHealth();
    loadTokens();
    bindBilling();
    loadBilling();
    var _hd = $('hd-recheck');
    if(_hd && !_hd._bound){
      _hd._bound = true;
      _hd.onclick = async function(){
        try{
          var r = await api('health', 'POST', {action:'recheck'});
          if(r && r.ok && r.data) renderHealth(r.data);
          if($('hd-msg')) $('hd-msg').textContent = r && r.ok && r.data ? '已检查' : '检查失败';
        }catch(e){ if($('hd-msg')) $('hd-msg').textContent = '失败'; }
      };
    }
    // 弱网优化：这几个请求互不依赖，并行发；control 走 slim（不拉群聊天记录）
    var _res = await Promise.all([
      api('control', 'GET', null, { slim: 1 }),
      api('effective-config'),
      api('play'),
      raw('/api/status')
    ]);
    if(requestId !== overviewRequest) return;
    var ctrl = _res[0], cfg = _res[1], play = _res[2], sys = _res[3];
    // 失败不是空快照：保留上次成功的数据，成功的其它请求仍可更新。
    if(ctrl) STATE.control = ctrl;
    if(cfg) STATE.cfg = cfg;
    var controlLoaded = !!ctrl;
    ctrl = STATE.control; cfg = STATE.cfg;
    var st = (ctrl && ctrl.state) || {};
    var groups = st.groups || [];
    STATE.groups = groups;

    var settings = st.settings || (cfg && cfg.settings) || {};
    var cs = (cfg && cfg.settings) || {};
    // 在线判定：优先用 control 接口的 fresh，再看 effective-config（顶层或 settings 内）
    var fresh = (ctrl && typeof ctrl.fresh === 'boolean') ? ctrl.fresh
              : (typeof (cfg||{}).fresh === 'boolean' ? cfg.fresh
              : (typeof cs.fresh === 'boolean' ? cs.fresh : false));
    var age = num((cfg||{}).age_sec !== undefined && (cfg||{}).age_sec !== null ? cfg.age_sec : cs.age_sec, -1);

    var dot = $('dot-bot');
    if(dot){ dot.className = 'dot ' + (fresh ? 'on' : 'off'); }
    $('top-bot').textContent = fresh ? 'ONLINE' : 'OFFLINE';
    $('top-mem').textContent = groups.length + ' G';

    $('ov-bot').textContent = fresh ? '在线' : '离线';
    $('ov-bot').className = 'v';
    $('ov-bot-f').textContent = age >= 0 ? (age + ' 秒前上报') : '未收到上报';
    $('ov-groups').textContent = groups.length;

    if(play){
      var today = (play.state && play.state.today) || {};
      $('ov-msgs').textContent = num(today.messages, 0);
      $('ov-msgs-f').textContent = '私聊 ' + num(today.private_messages,0) + ' · 发送 ' + num(today.sent_replies,0);
    }

    if(sys){
      var up = num(sys.uptime, 0);
      $('ov-uptime').textContent = (up/3600).toFixed(1) + 'h';
    }

    var node = '';
    node += kv('机器人', fresh ? '<span class="tag gr">在线</span>' : '<span class="tag rd">离线</span>');
    node += kv('OneBot 节点', '127.0.0.1:3001');
    node += kv('人设模式', esc((cfg && cfg.persona && cfg.persona.mode) || '--'));
    node += kv('单条字数', num(settings.reply_chars,0) + ' 字');
    node += kv('气泡条数', num(settings.bubbles,0) + ' 条');
    node += kv('条间延迟', num(settings.delay_ms,0) + ' ms');
    node += kv('巡群', settings.patrol_enabled === false ? '<span class="tag rd">关闭</span>' : '<span class="tag gr">启用</span>');
    node += kv('记忆', settings.memory_enabled === false ? '<span class="tag rd">关闭</span>' : '<span class="tag gr">启用</span>');
    $('ov-node').innerHTML = node;

    var res = '';
    if(sys){
      res += bar('CPU', sys.cpu);
      res += '<div style="height:10px"></div>' + bar('内存', sys.memory, 'am');
      res += '<div style="height:10px"></div>' + bar('磁盘', sys.disk, 'rd');
      var swap = num(sys.swap_pct, 0);
      if(swap > 0) res += '<div style="height:10px"></div>' + bar('交换', swap, 'rd');

      // 下面这排明细：光有百分比看不出还剩多少，把绝对量补上
      res += '<div style="margin-top:14px;display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:8px">' +
             '<div class="bl-cell"><i>内存</i><span>' + num(sys.mem_used_mb,0) + ' / ' + num(sys.mem_total_mb,0) + ' MB</span></div>' +
             '<div class="bl-cell"><i>磁盘剩余</i><span>' + num(sys.disk_free_gb,0) + ' / ' + num(sys.disk_total_gb,0) + ' GB</span></div>' +
             '<div class="bl-cell"><i>负载 1/5/15</i><span>' + num(sys.load,0).toFixed(2) + ' / ' + num(sys.load5,0).toFixed(2) + ' / ' + num(sys.load15,0).toFixed(2) + '</span></div>' +
             '<div class="bl-cell"><i>核心数</i><span>' + num(sys.cpu_count,1) + ' 核</span></div>' +
             '<div class="bl-cell"><i>进程</i><span>' + num(sys.procs,0) + ' 个</span></div>' +
             '<div class="bl-cell"><i>运行时长</i><span>' + (num(sys.uptime,0)/3600).toFixed(1) + ' h</span></div>' +
             '<div class="bl-cell"><i>出网流量</i><span>' + num(sys.net_sent_mb,0).toFixed(1) + ' MB</span></div>' +
             '<div class="bl-cell"><i>入网流量</i><span>' + num(sys.net_recv_mb,0).toFixed(1) + ' MB</span></div>' +
             '</div>';

      var svc = sys.services || {};
      if(Object.keys(svc).length){
        var svcHtml = Object.keys(svc).map(function(k){
          var ok = svc[k];
          return '<span class="tag ' + (ok ? 'gr' : 'rd') + '" style="margin-right:6px">' +
                 esc(k) + ' ' + (ok ? '●' : '○') + '</span>';
        }).join('');
        res += '<div style="margin-top:12px;font-size:11px;color:var(--dim2)">运行服务 ' + svcHtml + '</div>';
      }
      $('ov-res').innerHTML = res;
    } else if(!$('ov-res').hasChildNodes()){
      $('ov-res').innerHTML = '<div class="empty">系统信息不可用</div>';
    }

    // 「最近操作」跟「服务器资源」等高：直接量资源卡内容的实际高度，
    // 写进 CSS 变量。资源栏加了参数变高/变矮，操作栏会自己跟上。
    syncOpsHeight();

    if(controlLoaded) renderOps('ov-ops', ctrl.operations || []);
  }

  // 把资源卡实测高度传给操作表；窗口变化时也要重算
  function syncOpsHeight(){
    var src = $('ov-res'), dst = document.querySelector('.card .tbl-wrap.ov-scroll');
    if(!src || !dst) return;
    var h = src.getBoundingClientRect().height;
    if(h > 40){
      dst.style.setProperty('--ops-h', Math.round(h) + 'px');
    }
  }
  window.addEventListener('resize', function(){
    clearTimeout(window._opsResize);
    window._opsResize = setTimeout(syncOpsHeight, 200);
  });

  function renderOps(id, ops){
    var t = $(id); if(!t) return;
    var head = '<tr><th>时间</th><th>ID</th><th>动作</th><th>状态</th></tr>';
    if(!ops.length){ t.innerHTML = head + '<tr><td colspan="4" class="dim">暂无操作记录</td></tr>'; return; }
    var rows = ops.slice(0, 20).map(function(o){
      var when = o.created ? new Date(o.created*1000).toLocaleString('zh-CN') : '--';
      var tag = o.status === 'done' ? 'gr' : (o.status === 'failed' ? 'rd' : 'am');
      return '<tr><td>' + esc(when) + '</td><td>' + esc(String(o.id||'').slice(0,8)) + '</td>' +
             '<td>' + esc(o.action || '') + '</td><td><span class="tag ' + tag + '">' + esc(o.status||'') + '</span></td></tr>';
    }).join('');
    t.innerHTML = head + rows;
    syncOpsHeight();
  }

  function shadowText(parent, tag, text, cls){
    var el = document.createElement(tag);
    if(cls) el.className = cls;
    el.textContent = String(text);
    parent.appendChild(el);
    return el;
  }

  // 计算状态不是心理测量；有数值也不代表该维已有事件驱动。
  var SHADOW_TIPS = {
    valence: '效价（-1~1）：影子模型的计算维度。事件驱动与恢复过程需结合诊断证据查看，不是心理测量。',
    arousal: '激活度（0~1）：模型的激活状态，不预设某类聊天必然使它上升；是否有驱动以诊断为准。',
    control: '掌控感（0~1）：模型的可应对程度变量；正负驱动以已记录评估为准，不代表已经校准。',
    attention: '注意负荷（0~1）：拟描述资源占用的模型维度；未实现或未观测时不能据此解释发言行为。'
  };
  var SHADOW_NAMES = {valence:'效价', arousal:'激活度', control:'掌控感', attention:'注意负荷'};

  function shadowTip(parent, text, key){
    var el = shadowText(parent, 'span', text, 'tip');
    el.setAttribute('data-tip', SHADOW_TIPS[key] || text);
    el.tabIndex = 0;
    return el;
  }

  function shadowNumber(value, digits){
    // Number(null) === 0：这里必须先区分未提供，不能把未知伪装成健康零值。
    if(typeof value !== 'number' || !isFinite(value)) return '未提供';
    return digits == null ? String(value) : value.toFixed(digits);
  }

  function shadowValue(value){
    return value == null || value === '' ? '未提供' : String(value);
  }

  function shadowCheckStatus(check){
    if(['pass','fail','unknown','not_observed'].indexOf(check.status) >= 0){
      return check.status === 'pass' && check.ok !== true ? 'unknown' : check.status;
    }
    // 旧 API 没有 status；仅为旧检查保留布尔值兼容，不推导任何新诊断。
    return check.ok === true ? 'pass' : (check.ok === false ? 'fail' : 'unknown');
  }

  function renderCalibration(box, detail){
    box.replaceChildren();
    var checks = detail && Array.isArray(detail.checks) ? detail.checks : [];
    var total = checks.length, passed = checks.filter(function(c){return shadowCheckStatus(c) === 'pass';}).length;
    var consistent = !!detail && detail.passed === passed && detail.total === total;
    $('emotion-shadow-title').textContent = '影子观测检查 ' + (total ? passed + '/' + total : '未提供');
    if(!total){shadowText(box, 'div', '观测检查：未提供', 'hint'); return false;}
    var head = shadowText(box, 'div', '', 'calib-h');
    shadowText(head, 'span', '观测检查 ' + passed + '/' + total, 'hint');
    var list = shadowText(box, 'div', '', 'calib-list');
    var labels = {pass:'通过', fail:'未通过', unknown:'未知', not_observed:'未观测'};
    checks.forEach(function(c){
      var status = shadowCheckStatus(c);
      var row = shadowText(list, 'div', '', 'calib-row' + (status === 'pass' ? ' on' : ''));
      row.dataset.checkStatus = status;
      shadowText(row, 'b', labels[status] + ' · ' + shadowValue(c.label));
      shadowText(row, 'span', '要求 ' + shadowValue(c.need));
      shadowText(row, 'i', '当前 ' + shadowValue(c.now));
    });
    var bar = shadowText(head, 'span', '', 'calib-bar');
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-label', '观测检查通过项数（不是校准百分比）');
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', String(total));
    bar.setAttribute('aria-valuenow', String(passed));
    shadowText(bar, 'i', '').style.width = (passed / total * 100) + '%';
    if(!consistent) shadowText(box, 'p', '检查汇总不一致：按逐项结果展示，暂停评审', 'hint');
    return consistent && passed === total;
  }

  function shadowSection(parent, key, title){
    var section = shadowText(parent, 'section', '', 'shadow-section');
    section.id = 'emotion-shadow-' + key;
    shadowText(section, 'h3', title);
    return section;
  }

  function renderShadowDiagnostics(parent, diag){
    parent.replaceChildren();
    var d = diag || {};
    if(!diag) shadowText(parent, 'p', '诊断数据：未提供（当前 API 未提供新诊断，不以旧计数代替）', 'hint');
    var counts = shadowSection(parent, 'counts', '事件口径');
    var metrics = shadowText(counts, 'div', '', 'shadow-metrics');
    [['received_events','收到'],['live_events','真实'],['effective_events','有效驱动'],
      ['ignored_events','忽略'],['excluded_events','排除'],['quarantined_events','隔离'],
      ['legacy_excluded_events','历史排除']].forEach(function(pair){
      var cell = shadowText(metrics, 'div', '', 'shadow-metric');
      shadowText(cell, 'span', pair[1]);
      shadowText(cell, 'b', shadowNumber(d[pair[0]])).dataset.shadowMetric = pair[0];
    });
    shadowText(counts, 'p', '收到 ≠ 真实 ≠ 有效驱动；真实事件也可能被忽略。排除与隔离单列，不相加推算；回执与 consolidation 不计作情感驱动。计数来自核心诊断，不从时间线条数推断。', 'hint');
    var kinds = shadowSection(parent, 'kinds', '按事件种类');
    if(!d.by_kind) shadowText(kinds, 'p', '种类统计：未提供', 'hint');
    else if(!Object.keys(d.by_kind).length) shadowText(kinds, 'p', '尚无种类统计', 'hint');
    else {
      var wrap = shadowText(kinds, 'div', '', 'shadow-table-wrap');
      var table = shadowText(wrap, 'table', '', 'shadow-table');
      var header = shadowText(shadowText(table, 'thead', ''), 'tr', '');
      ['种类','收到','真实','有效驱动','忽略','排除'].forEach(function(label){
        shadowText(header, 'th', label).scope = 'col';
      });
      var body = shadowText(table, 'tbody', '');
      Object.keys(d.by_kind).sort().forEach(function(kind){
        var row = shadowText(body, 'tr', ''), stats = d.by_kind[kind] || {};
        shadowText(row, 'td', kind);
        ['received','live','effective','ignored','excluded'].forEach(function(key){
          shadowText(row, 'td', shadowNumber(stats[key]));
        });
      });
    }
    var rules = shadowSection(parent, 'rules', '按评估规则');
    if(!d.by_rule) shadowText(rules, 'p', '规则统计：未提供', 'hint');
    else if(!Object.keys(d.by_rule).length) shadowText(rules, 'p', '尚无规则统计', 'hint');
    else Object.keys(d.by_rule).sort().forEach(function(rule){
      var row = shadowText(rules, 'div', '', 'kv');
      shadowText(row, 'span', rule, 'k'); shadowText(row, 'b', shadowNumber(d.by_rule[rule]), 'v');
    });
    var tools = shadowSection(parent, 'tools', '工具结果分布');
    if(!d.tools) shadowText(tools, 'p', '工具结果：未提供', 'hint');
    else if(!Object.keys(d.tools).length) shadowText(tools, 'p', '尚无工具结果', 'hint');
    else Object.keys(d.tools).sort().forEach(function(tool){
      var stats = d.tools[tool] || {}, row = shadowText(tools, 'div', '', 'kv');
      shadowText(row, 'span', tool, 'k');
      shadowText(row, 'span', '成功 ' + shadowNumber(stats.success) + ' · 失败 ' +
        shadowNumber(stats.failure) + ' · 未知 ' + shadowNumber(stats.unknown), 'v');
    });
    var fresh = d.freshness || {}, backlog = d.backlog || {}, continuity = d.continuity || {};
    var freshness = shadowSection(parent, 'freshness', '心跳');
    shadowText(freshness, 'p', '心跳年龄 ' + shadowNumber(fresh.heartbeat_age_seconds) +
      (typeof fresh.heartbeat_age_seconds === 'number' ? ' 秒' : '') + ' · ' +
      (fresh.stale === true ? '心跳已过期 / 失联' : (fresh.stale === false ? '心跳未过期' : '新鲜度 未提供')), 'hint');
    var queue = shadowSection(parent, 'backlog', '真实待消费');
    shadowText(queue, 'p', '待消费 ' + (backlog.known === true ? shadowNumber(backlog.pending) : '未知') +
      ' · 最老等待 ' + shadowNumber(backlog.oldest_age_seconds) +
      (typeof backlog.oldest_age_seconds === 'number' ? ' 秒' : '') + ' · ' +
      (backlog.ledger_consistent === true ? '账本一致' : (backlog.ledger_consistent === false ? '账本不一致' : '账本一致性 未提供')), 'hint');
    var cont = shadowSection(parent, 'continuity', '观测连续性');
    function seconds(value){return shadowNumber(value) + (typeof value === 'number' ? ' 秒' : '');}
    shadowText(cont, 'p', '有效观测 ' + seconds(continuity.observed_seconds) + ' · 累计断档 ' +
      seconds(continuity.gap_seconds) + ' · 当前断档 ' + seconds(continuity.open_gap_seconds) +
      ' · 断档占比 ' + (typeof continuity.gap_ratio === 'number' ? shadowNumber(continuity.gap_ratio * 100, 2) + '%' : '未提供') +
      ' · 当前连续 ' + seconds(continuity.current_streak_seconds), 'hint');
    var replay = d.evidence_replay || {}, replayBox = shadowSection(parent, 'replay', '证据回放');
    shadowText(replayBox, 'p', (replay.ok === true ? '存储复现通过' : (replay.ok === false ? '存储复现失败' : '未验证')) +
      ' · 检查事件 ' + shadowNumber(replay.checked_events) + ' · 最大误差 ' + shadowNumber(replay.max_error) +
      ' · 来源缺失 ' + shadowNumber(replay.source_missing) + ' · 评估不匹配 ' + shadowNumber(replay.appraisal_mismatches), 'hint');
    shadowText(replayBox, 'p', '即使回放误差为 0，也仅验证存储复现，不证明规则有效。', 'hint');
    var version = shadowSection(parent, 'version', '诊断版本');
    shadowText(version, 'p', '运行版本 ' + shadowValue(d.run_version), 'hint');
  }

  function renderShadowDimensions(parent, dimensions, live){
    parent.replaceChildren();
    shadowText(parent, 'h3', live ? '四维驱动证据' : '历史维度驱动');
    Object.keys(SHADOW_NAMES).forEach(function(key){
      var dim = (dimensions || {})[key] || {}, row = shadowText(parent, 'div', '', 'shadow-dimension-evidence');
      shadowTip(row, SHADOW_NAMES[key], key);
      if(live) shadowText(row, 'p', '数据来源：state 计算快照；' +
        (dimensions && dimensions[key] ? 'diagnostics.dimensions.' + key + ' 事件评估统计' : '维度诊断未提供'), 'hint');
      shadowText(row, 'p', '驱动 ' + shadowNumber(dim.nonzero_events) + ' 次 · 正向 ' +
        shadowNumber(dim.positive) + ' · 负向 ' + shadowNumber(dim.negative) +
        ' · 诊断范围 ' + shadowNumber(dim.min, 3) + ' ~ ' + shadowNumber(dim.max, 3), 'hint');
    });
  }

  function renderShadowRetrospective(parent, report){
    parent.replaceChildren();
    shadowText(parent, 'h3', '今日历史回放');
    shadowText(parent, 'p', '仅已有账本追溯，不计实时样本/时长；不触发外部动作。', 'hint');
    // 只展示后端回放报告，不拿实时快照或时间线替造历史样本。
    if(!report || report.mode !== 'retrospective' || report.counts_for_live !== false){
      shadowText(parent, 'p', '尚在准备：后端尚未提供可用历史回放。', 'hint');
      return;
    }
    if(report.error){
      shadowText(parent, 'p', '历史回放暂不可读：' + shadowValue(report.error), 'hint');
      return;
    }
    shadowText(parent, 'p', shadowValue(report.day) + ' · ' +
      (report.events === 0 ? '无可回放事件' : (typeof report.events === 'number' ? '已有账本回放' : '尚在准备')), 'hint');
    shadowText(parent, 'p', '事件 ' + shadowNumber(report.events) + ' · 有效驱动 ' +
      shadowNumber(report.effective_events) + ' · 排除 ' + shadowNumber(report.excluded_events), 'hint');
    function sourceTime(value){
      return typeof value === 'number' && isFinite(value) ? new Date(value * 1000).toLocaleString('zh-CN') : shadowValue(value);
    }
    shadowText(parent, 'p', '来源起止 ' + sourceTime(report.source_start) + ' → ' + sourceTime(report.source_end) +
      ' · 规则版本 ' + shadowValue(report.rule_version), 'hint');
    if(report.dimensions && Object.keys(report.dimensions).length){
      renderShadowDimensions(shadowText(parent, 'div', '', 'shadow-history-dimensions'), report.dimensions, false);
    }
  }

  var CAND_STANCES = {praise:'夸/亲近', rebuke:'责怪', neutral:'中性', uncertain:'不确定'};
  var CAND_REL = {hit:'规则已命中', missed:'规则漏掉', mismatch:'规则误判',
                  neutral:'无需规则', pending:'待标注', error:'标注失败'};

  function renderValenceSamples(box, sum, data){
    if(!box) return;
    box.replaceChildren();
    if(!data || data.ok !== true){
      if(sum) sum.textContent = '效价样本暂不可读（不影响影子状态）';
      if($('valence-samples-brief'))
        $('valence-samples-brief').textContent = '效价样本：暂不可读（不影响影子状态）';
      return;
    }
    var s = data.stats || {}, rows = data.rows || [];
    if(sum){
      sum.textContent = '已判定 ' + (s.total || 0) + ' 条｜纳入 ' + (s.included || 0)
        + '｜排除 ' + (s.excluded || 0) + '｜历史独立回放，不重复写入实时轨迹';
    }
    var brief = $('valence-samples-brief');
    if(brief){
      brief.textContent = '效价样本：已判定 ' + (s.total || 0) + ' 条｜纳入 '
        + (s.included || 0) + '｜排除 ' + (s.excluded || 0)
        + '｜仅影子评测，不接管表达';
    }
    if(!rows.length){
      shadowText(box, 'div', '还没有已判定的效价样本。', 'empty');
      return;
    }
    rows.forEach(function(r){
      var row = shadowText(box, 'div', '', 'cand-row');
      var top = shadowText(row, 'div', '', 'cand-top');
      var candidate = r.candidate || {}, appraisal = r.appraisal || {};
      var stance = String(candidate.stance || (appraisal.rule === 'integrated_praise' ? 'praise'
        : (appraisal.rule === 'integrated_rebuke' ? 'rebuke' : 'neutral')));
      var cls = stance === 'praise' ? 'gr' : (stance === 'rebuke' ? 'rd'
        : (stance === 'uncertain' ? 'am' : ''));
      var tag = CAND_STANCES[stance] || '不确定';
      shadowText(top, 'span', tag, 'tag ' + cls);
      var text = shadowText(top, 'b', r.text || '（空）', 'cand-text');
      text.title = r.text || '';
      shadowText(top, 'i', candidate.state === 'done'
        ? (Math.round((candidate.confidence || 0) * 100) + '%')
        : (candidate.state ? (CAND_REL[candidate.state] || '未完成标注') : '原规则'), 'cand-conf');
      var meta = shadowText(row, 'div', '', 'cand-meta');
      shadowText(meta, 'b', relTime(r.at));
      shadowText(meta, 'span', r.included ? '纳入影子' : '排除');
      shadowText(meta, 'span', r.exclusion_reason || candidate.reason || appraisal.reason || '');
      if(candidate.model) shadowText(meta, 'span', candidate.model);
    });
  }

  var shadowRequest = 0;
  async function loadEmotionShadow(){
    var summary = $('emotion-shadow-summary'), box = $('emotion-shadow-values');
    var timeline = $('emotion-shadow-timeline'), obs = $('emotion-shadow-observation');
    var calib = $('emotion-shadow-calib'), diagnostics = $('emotion-shadow-diagnostics');
    if(!summary || !box || !timeline || !obs || !calib || !diagnostics) return;
    var requestId = ++shadowRequest;
    summary.textContent = '读取影子状态…';
    // 清掉旧评审结论，避免请求失败时继续展示上次健康快照。
    box.replaceChildren(); timeline.replaceChildren(); diagnostics.replaceChildren(); calib.replaceChildren();
    $('emotion-shadow-dimension-evidence').replaceChildren();
    if($('valence-samples-list')) $('valence-samples-list').replaceChildren();
    if($('valence-samples-sum')) $('valence-samples-sum').textContent = '加载中…';
    if($('valence-samples-brief')) $('valence-samples-brief').textContent = '效价样本：加载中…';
    renderShadowRetrospective($('emotion-shadow-retrospective'), null);
    obs.textContent = '';
    $('emotion-shadow-title').textContent = '影子观测检查 未提供';
    var d = await api('emotion-shadow','GET',null,{candidate_detail:0});
    if(requestId !== shadowRequest) return;
    if(!d || !d.ok){
      summary.textContent = '影子状态暂不可读（不影响她正常聊天）';
      shadowText(diagnostics, 'p', '诊断数据：未提供，请刷新重试', 'hint');
      renderValenceSamples($('valence-samples-list'), $('valence-samples-sum'), null);
      return;
    }
    var diag = d.diagnostics || {}, fresh = diag.freshness || {};
    summary.textContent = (fresh.stale === true ? '心跳已过期 / 失联' :
      (d.running === true ? '后台运行中' : '等待后台或心跳已过期')) + ' · shadow · 未校准 · 不接管表达';
    var state = d.state || {}, dimensions = diag.dimensions || {};
    Object.keys(SHADOW_NAMES).forEach(function(key){
      var dim = dimensions[key] || {}, card = shadowText(box, 'div', '', 'stat');
      card.dataset.shadowDimension = key;
      shadowText(card, 'span', SHADOW_NAMES[key], 'shadow-dimension-name');
      shadowText(card, 'div', shadowNumber(state[key], 3), 'v');
      var status = dim.implemented === false ? '未实现' : (dim.implemented !== true ? '实现情况未提供' :
        (dim.observed === true ? '已驱动' : (dim.observed === false ? '未观测' : '观测情况未提供')));
      shadowText(card, 'div', status, 'shadow-dimension-status');
    });
    renderShadowDimensions($('emotion-shadow-dimension-evidence'), diag.dimensions, true);
    renderValenceSamples($('valence-samples-list'), $('valence-samples-sum'),
                            d.valence_samples);
    var _cs = $('valence-cand-stats');
    var _vc = d.valence_candidates || {}, _st = _vc.stats;
    if(_vc.status === 'not_loaded'){
      qyText('valence-samples-brief','效价候选详情：展开后按需读取；默认不扫描全部历史');
      if(_cs) _cs.textContent = '候选账本：等待用户展开详情';
    } else if(_cs){
      _cs.textContent = (_vc.ok && _st)
        ? ('候选账本：待标 ' + shadowValue(_st.pending) + ' · 已标 ' + shadowValue(_st.done)
           + ' · 失败 ' + shadowValue(_st.error) + ' · 命中 ' + shadowValue(_st.hit)
           + ' · 漏 ' + shadowValue(_st.missed) + ' · 错配 ' + shadowValue(_st.mismatch)
           + '（只观测，不改状态/表达）')
        : '候选账本：' + (_vc.status === 'missing_data' ? '尚未创建' : '暂不可读');
    }
    renderShadowRetrospective($('emotion-shadow-retrospective'), d.retrospective);
    var allChecksPass = renderCalibration(calib, d.calibration_detail);
    renderShadowDiagnostics(diagnostics, d.diagnostics);
    var unimplemented = Object.keys(SHADOW_NAMES).filter(function(key){return dimensions[key] && dimensions[key].implemented === false;});
    var dimensionsObserved = Object.keys(SHADOW_NAMES).every(function(key){
      return dimensions[key] && dimensions[key].implemented === true && dimensions[key].observed === true;
    });
    var backlog = diag.backlog || {};
    var review = d.ready_for_review === true && allChecksPass && dimensionsObserved && fresh.stale === false &&
      backlog.known === true && backlog.pending === 0 && backlog.ledger_consistent === true;
    obs.textContent = (unimplemented.length ? '未实现维度（' + unimplemented.map(function(key){return SHADOW_NAMES[key];}).join('、') + '）阻止评审。' : '') +
      (review ? '可提交人工评审' : '暂不可评审（检查未齐、未观测或诊断未知）') +
      '；可评审不等于已校准。阶段3尚未开放，开不开由人定，不接管表达。参数版本 ' + shadowValue(d.parameter_hash);
    var rows = Array.isArray(d.timeline) ? d.timeline : [];
    rows.slice(-20).reverse().forEach(function(row){
      var line = shadowText(timeline, 'div', '', 'hint');
      shadowText(line, 'strong', shadowValue(row.kind));
      shadowText(line, 'span', ' · ' + shadowValue(row.rule) + ' · ' + relTime(row.at));
      shadowText(line, 'div', row.reason || '');
    });
    if(!rows.length) shadowText(timeline, 'div', '观测起点之后还没有新事件', 'empty');
  }

  async function loadQingyiShadowDetail(){
    if(QY.shadowDetailLoaded) return;
    QY.shadowDetailLoaded = true;
    qyText('valence-samples-brief','效价候选详情：正在按需读取…');
    var d = await api('emotion-shadow','GET',null,{candidate_detail:1});
    if(!d || !d.ok){ QY.shadowDetailLoaded=false; qyText('valence-samples-brief','效价候选详情：暂不可读（不影响实时影子）'); return; }
    renderValenceSamples($('valence-samples-list'),$('valence-samples-sum'),d.valence_samples);
    var stats = $('valence-cand-stats'), value = d.valence_candidates || {};
    if(stats){
      stats.textContent = (value.ok && value.stats)
        ? ('候选账本：待标 ' + shadowValue(value.stats.pending) + ' · 已标 ' + shadowValue(value.stats.done)
           + ' · 失败 ' + shadowValue(value.stats.error) + '（独立反事实，不改实时状态/表达）')
        : '候选账本：' + (value.status === 'missing_data' ? '尚未创建' : '暂不可读');
    }
  }

  /* ---------- 阶段2.5 准入判定 ---------- */
  var RD_KEYS = {
    ledger_integrity: '账本完整',
    action_evidence: '动作带证据',
    action_identity: '动作认得出对象',
    action_results: '终态有回执',
    belief_grounding: '信念有来源',
    self_view_grounding: '自我修订有来源',
    shadow_review: '影子可评审',
    cognition_evidence: '认知账本可读',
    consent_enforcement: '同意边界被执行',
    evidence_retraction: '撤回能传导',
    action_idempotency_outbox: '动作不重复投递',
    layer_versioning: '分层版本化',
    model_switch_behavior: '换模型不漂移'
  };
  // 四种状态必须分得开：「没样本」和「不合格」不是一回事，
  // 「这份账本证明不了」更不能显示成绿色。
  var RD_STATUS = {
    pass: ['通过', 'gr'], blocked: ['阻塞', 'rd'],
    not_observed: ['暂无样本', 'am'], not_verified: ['本账无法验证', 'am'],
    unknown: ['读不到', 'rd']
  };

  function rdNumbers(observed){
    if(observed === null || observed === undefined) return '';
    if(typeof observed !== 'object') return String(observed);
    if(Array.isArray(observed)) return observed.join('、');
    return Object.keys(observed).map(function(key){
      var value = observed[key];
      if(value && typeof value === 'object') return key + '={' + rdNumbers(value) + '}';
      return key + '=' + (value === null || value === undefined ? '?' : value);
    }).join(' · ');
  }

  var RD_ENGINEERING = ['consent_enforcement', 'action_idempotency_outbox',
    'evidence_retraction', 'layer_versioning', 'model_switch_behavior'];
  var RD_ORGANIC = ['action_evidence', 'action_identity', 'action_results',
    'belief_grounding', 'self_view_grounding'];

  function rdTimestamp(value){
    // Audit timestamps are Unix seconds, never browser-local strings or null=epoch.
    return typeof value === 'number' && Number.isFinite(value) && value > 0
      && value <= 8640000000000 ? value : null;
  }
  function rdTime(value){
    return new Date(value * 1000 + 8 * 3600000).toISOString().slice(0, 19).replace('T', ' ') + ' UTC+8';
  }
  function rdDuration(seconds){
    var n = Math.floor(seconds), days = Math.floor(n / 86400);
    return (days ? days + ' 天 ' : '') + Math.floor(n % 86400 / 3600) + ' 小时 '
      + Math.floor(n % 3600 / 60) + ' 分 ' + n % 60 + ' 秒';
  }
  function rdGateCount(checks, keys){
    return keys.filter(function(key){
      var found = checks.filter(function(c){ return c.key === key; });
      return found.length === 1 && found[0].status === 'pass';
    }).length;
  }
  function rdRenderChecks(box, checks){
    box.replaceChildren();
    checks.forEach(function(c){
      var row = document.createElement('div');
      row.className = 'rd-row';
      var st = RD_STATUS[c.status] || [c.status || '未提供', 'am'];
      var head = document.createElement('span');
      head.className = 'tag ' + st[1];
      head.textContent = st[0];
      row.appendChild(head);
      var name = document.createElement('b');
      name.textContent = (RD_KEYS[c.key] || c.key) + '（' + c.key + '）';
      row.appendChild(name);
      box.appendChild(row);
      var obs = document.createElement('div');
      obs.className = 'hint rd-obs';
      obs.textContent = rdNumbers(c.observed) || '无读数';
      row.appendChild(obs);
      if(c.reason){
        var why = document.createElement('div');
        why.className = 'hint';
        why.textContent = c.reason;
        row.appendChild(why);
      }
    });
    if(!checks.length){
      var none = document.createElement('div');
      none.className = 'empty';
      none.textContent = '账本没给出任何检查项';
      box.appendChild(none);
    }
  }

  var readinessRequest = 0;
  async function loadReadiness(){
    var title = $('readiness-title'), sum = $('readiness-summary'), box = $('readiness-checks');
    var stage = $('readiness-stage'), window_ = $('readiness-window');
    if(!title || !sum || !box || !stage) return;
    var requestId = ++readinessRequest;
    title.textContent = '阶段2.5 准入判定 读取中';
    sum.textContent = '正在只读账本…';
    box.replaceChildren();
    stage.textContent = '';
    if(window_) window_.textContent = '';
    ['readiness-clock', 'readiness-observation', 'readiness-engineering',
      'readiness-coverage', 'readiness-history-boundary', 'readiness-history-summary'].forEach(function(id){
      $(id).textContent = '';
    });
    $('readiness-current-label').textContent = '当前检查';
    $('readiness-history').hidden = true;
    $('readiness-history-checks').replaceChildren();
    var d = await api('readiness');
    if(requestId !== readinessRequest) return;
    var p = (d && d.phase25) || null;
    if(!d || !p || d.error){
      title.textContent = '阶段2.5 准入判定 读不到';
      sum.textContent = '判定暂不可读（不影响她正常聊天）：'
        + esc((d && d.error) || '接口未返回');
      stage.textContent = '读不到就是读不到 —— 这里不会因为没有数据就给绿灯。';
      return;
    }
    var checks = Array.isArray(p.checks) ? p.checks : [];
    var blocked = checks.filter(function(c){ return c.status !== 'pass'; });
    var waiting = blocked.filter(function(c){ return c.status === 'not_observed'; });
    var engineering = blocked.filter(function(c){ return c.status === 'not_verified'; });
    var errors = blocked.filter(function(c){ return c.status === 'unknown'; });
    var release = p.observation_release || {}, observation = p.observation || {};
    var checkedAt = rdTimestamp(d.checked_at), deployedAt = rdTimestamp(release.deployed_at);
    var boundaryAt = rdTimestamp(observation.boundary_at);
    var seconds = p.window_seconds;
    var validBoundary = release.valid === true && checkedAt !== null && deployedAt !== null
      && boundaryAt !== null && typeof seconds === 'number' && Number.isFinite(seconds) && seconds > 0
      && deployedAt <= checkedAt && boundaryAt <= checkedAt
      && Math.abs(boundaryAt - Math.max(checkedAt - seconds, deployedAt)) < 0.001
      && ['blocked', 'not_observed', 'observed'].indexOf(observation.status) !== -1;
    var boundaryMissing = !p.observation_release || release.reason === 'missing'
      || observation.status === 'not_configured';
    title.textContent = '阶段2.5 准入判定 '
      + (!validBoundary ? (boundaryMissing ? '观测边界未配置' : '观测边界无效')
        : p.status === 'review_ready' ? '可提交评审'
        : errors.length ? '存在读取错误'
        : engineering.length ? '存在工程阻塞'
        : waiting.length === blocked.length && waiting.length ? '等待自然覆盖' : '尚未闭合');
    sum.textContent = '窗口内 ' + checks.length + ' 项检查，'
      + (blocked.length ? blocked.length + ' 项未过：' + blocked.map(function(c){
          return RD_KEYS[c.key] || c.key;
        }).join('、') : '全部通过（通过也不等于授权阶段3）');
    if(window_ && p.window_seconds) {
      window_.textContent = '统计窗口：最近 '
        + Math.round(Number(p.window_seconds) / 3600) + ' 小时';
    }
    var readAt = Date.now() / 1000;
    $('readiness-clock').textContent = '本次读取时间：' + rdTime(readAt)
      + ' · 审计时间：' + (checkedAt === null ? '未提供或无效' : rdTime(checkedAt))
      + '。时长截至审计时间，刷新才更新；不是倒计时，不保证到时自动通过。';
    $('readiness-observation').textContent = validBoundary ?
      '发布起点：' + rdTime(deployedAt) + ' · 观测起点：' + rdTime(boundaryAt)
        + ' · 已历时：' + rdDuration(checkedAt - boundaryAt)
        + ' · 观测状态：' + (RD_STATUS[observation.status] ? RD_STATUS[observation.status][0] : observation.status)
        + ' · ' + (observation.label || '') + ' · ' + (observation.reason || '')
        + ' · 发布校验：' + (release.reason || '未提供原因') :
      '不能建立当前发布观测：边界缺失、无效或与发布/滚动窗口不一致；不推算起点和时长。'
        + ' 原始准入检查仍保留，不能当作当前发布覆盖。发布校验：' + (release.reason || '未提供')
        + ' · ' + (observation.label || '') + ' · ' + (observation.reason || '未提供观测原因');
    $('readiness-current-label').textContent = validBoundary ? '当前发布观测检查（含独立工程门槛）' : '原始准入检查（当前发布边界未确认）';
    $('readiness-engineering').textContent = '独立工程门槛：'
      + rdGateCount(checks, RD_ENGINEERING) + '/' + RD_ENGINEERING.length
      + ' 项通过。仅契约证据，不等于自然覆盖。';
    var organicWaiting = checks.filter(function(c){
      return RD_ORGANIC.indexOf(c.key) !== -1 && c.status === 'not_observed';
    }).length;
    $('readiness-coverage').textContent = validBoundary ? '当前自然覆盖：'
      + rdGateCount(checks, RD_ORGANIC) + '/' + RD_ORGANIC.length + ' 项通过，'
      + organicWaiting + ' 项暂无样本。完整性与影子评审另见检查，不计入五项工程门槛。' :
      '当前自然覆盖：未确认；未验证的边界不能用历史读数补齐，也不算通过。';
    rdRenderChecks(box, checks);
    var history = Array.isArray(p.history_checks) ? p.history_checks : [];
    var historyBlocked = history.filter(function(c){ return c.status !== 'pass'; });
    $('readiness-history').hidden = false;
    $('readiness-history-summary').textContent = '历史覆盖（独立保留）：'
      + history.length + ' 项检查，' + historyBlocked.length + ' 项未过；不回填当前观测';
    var historyBoundary = rdTimestamp(p.history_boundary_at);
    $('readiness-history-boundary').textContent = '历史滚动窗口起点：'
      + (historyBoundary === null ? '未提供或无效' : rdTime(historyBoundary))
      + '。此时间不等于全部历史下限：非旧迁移信念可为全时段；以审计口径为准。'
      + ' ' + (p.history_label || '未提供历史口径；不推断已修复。');
    rdRenderChecks($('readiness-history-checks'), history);
    var s3 = d.stage3 || {};
    stage.textContent = '阶段3A=' + esc(String(s3.stage3a || '未提供'))
      + ' · 阶段3B=' + esc(String(s3.stage3b || '未提供'))
      + ' · 接管表达=' + (s3.expression_control === true ? '已开' : '未开')
      + ' · 授权=' + (s3.authorized === true ? '是' : '否')
      + '。这两项由人决定，账本只会显示现状，不会自己翻绿。';
  }

  /* ---------- persona ---------- */
  function bindPersonaDisclosure(buttonId, openLabel, closeLabel, onOpen){
    var button = $(buttonId);
    if(!button || button._bound) return;
    var panel = $(button.getAttribute('aria-controls'));
    if(!panel) return;
    button._bound = true;
    button.addEventListener('click', function(){
      var expanded = button.getAttribute('aria-expanded') !== 'true';
      button.setAttribute('aria-expanded', String(expanded));
      panel.hidden = !expanded;
      button.textContent = expanded ? closeLabel : openLabel;
      if(expanded && onOpen) onOpen();
    });
  }

  /* ---------- 清漪推进板块 ---------- */
  var QINGYI_STATUS = {
    pass:['通过','gr'], blocked:['阻塞','rd'], not_observed:['待自然样本','am'],
    missing_data:['缺账本','am'], not_verified:['待工程验证','am'],
    not_implemented:['未实现','am'], manual_review:['待人工评审','am'],
    unknown:['读不到','rd'], error:['检测失败','rd'], not_applicable:['不适用',''], not_required:['不需要','']
  };
  var QINGYI_IMPL = {implemented:['已实现','gr'], existing:['已有基础','am'], planned:['拟议','']};
  var QY = {tab:'overview', filter:'all', report:null, latest:null, run_id:null, poll:null, loaded_tabs:{}};
  function qyEl(id){ return document.getElementById(id); }
  function qyText(id,value){ var e=qyEl(id); if(e) e.textContent = String(value==null?'':value); }
  function qyClear(e){ if(e) e.replaceChildren(); }
  function qyNode(parent,tag,text,cls){ if(!parent) return null; var n=document.createElement(tag); if(text!=null)n.textContent=String(text); if(cls)n.className=cls; parent.appendChild(n); return n; }
  function qyStatus(value){ return QINGYI_STATUS[value] || [String(value||'未知'),'am']; }
  function qyStatusTag(parent,value){ var pair=qyStatus(value); return qyNode(parent,'span',pair[0],'tag '+(pair[1]||'')); }
  function qyImplTag(parent,value){ var pair=QINGYI_IMPL[value]||['未知','']; return qyNode(parent,'span',pair[0],'tag '+(pair[1]||'')); }
  function qyBindOnce(id,event,handler){ var e=qyEl(id); if(!e||e._bound) return; e._bound=true; e.addEventListener(event,handler); }

  // checked_at 有两种形态：作业报告给 ISO 串，巡检快照给 epoch 秒。别把数字直接印出来。
  function qyStamp(value){
    if(value == null || value === '') return '';
    if(typeof value === 'number'){
      var d = new Date(value * 1000);
      if(isNaN(d.getTime())) return '';
      return (d.getMonth()+1) + '-' + d.getDate() + ' ' +
        String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
    }
    return String(value).slice(5, 16);
  }

  function switchQingyiTab(name){
    QY.tab = name;
    document.querySelectorAll('#view-qingyi .qy-tab').forEach(function(button){
      button.classList.toggle('on', button.getAttribute('data-qingyi-tab') === name);
    });
    document.querySelectorAll('#view-qingyi .qy-panel').forEach(function(panel){
      panel.classList.toggle('on', panel.id === 'qingyi-panel-' + name);
    });
    if(name === 'shadow' && !QY.loaded_tabs.shadow){
      QY.loaded_tabs.shadow = true;
      bindPersonaDisclosure('emotion-shadow-toggle','展开检查与统计','收起检查与统计',loadQingyiShadowDetail);
      loadEmotionShadow();
    }
    if(name === 'evidence' && !QY.loaded_tabs.evidence){
      QY.loaded_tabs.evidence = true;
      loadReadiness();
    }
    if(name === 'annotate'){
      loadAnnotate();
    }
    if(name === 'organic'){
      loadOrganic();
    }
    if(name === 'review'){
      loadReview();
    }
    if(name === 'takeover'){
      loadTakeover();
    }
    if(name === 'intervene'){
      loadIntervene();
    }
    if(name === 'capabilities'){
      loadCapabilities();
    }
  }

  async function loadCapabilities(){
    var d=await api('qingyi/capabilities');
    var box=qyEl('capabilities-list'), cases=qyEl('capabilities-cases');qyClear(box);qyClear(cases);
    if(!d||!d.ok){qyNode(box,'div','能力账本暂不可读','empty');return;}
    var labels={implemented:'代码存在',wired:'真实接线',exercised:'请求使用',evaluated:'独立评测',released:'发布绑定'};
    Object.keys(d.capabilities||{}).forEach(function(name){
      var row=d.capabilities[name],card=qyNode(box,'div','','qy-anno-card');
      qyNode(card,'b',name);
      Object.keys(labels).forEach(function(key){qyNode(card,'span',labels[key]+'：'+(row[key]?'是':'否'),'tag '+(row[key]?'ok':'bad'));});
    });
    var ev=d.evaluation||{};
    qyNode(cases,'p','最近回放：'+(ev.passed||0)+' / '+(ev.total||0)+'，失败 '+(ev.failed||0)+'；确定性结构回放，不替代真实模型盲评。','hint');
    (ev.cases||[]).forEach(function(c){var row=qyNode(cases,'div','','qy-anno-card');qyNode(row,'b',c.name+' · '+(c.passed?'通过':'失败'));qyNode(row,'p','期望 '+JSON.stringify(c.expected)+'；实际 '+JSON.stringify(c.actual),'qy-anno-text');});
    var wb=d.worldbook||{};qyNode(cases,'p','世界书：'+(wb.entries||0)+' 条 · '+(wb.trust_level||'未导入')+' · 许可 '+(wb.license_status||'unknown'),'hint');
    (d.requests||[]).slice().reverse().forEach(function(r){
      var card=qyNode(cases,'div','','qy-anno-card'),rt=r.runtime||{};
      qyNode(card,'b','请求 '+String(r.request_sha256||'').slice(0,12)+' · '+(r.scene||''));
      qyNode(card,'p','召回 '+(rt.status||'旧请求未记录')+' · 纠错 '+(rt.correction||'normal')+' · 查询 '+(rt.queries||[]).length,'qy-anno-text');
      qyNode(card,'p','权限过滤 '+JSON.stringify(rt.filtered||{})+' · 关联路径 '+(rt.paths||[]).length,'qy-anno-text');
      (r.selected||[]).forEach(function(x){qyNode(card,'p',x.id+' · '+x.kind+' · '+(x.visibility||'unknown')+' · '+(x.present?'已进入请求':'未进入请求'),'qy-anno-text');});
    });
    ((d.governance||{}).assets||[]).forEach(function(x){qyNode(cases,'p',x.asset+' · '+x.category+' · 来源 '+x.source_quality,'hint');});
  }
  async function evaluateCapabilities(){
    var msg=qyEl('capabilities-msg');msg.textContent='正在运行隔离回放…';
    var token=await csrfToken();
    var d=await raw('/admin/api/bot/qingyi/capabilities/evaluate',{method:'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:'{}'});
    msg.textContent=d&&d.evaluation?('完成：'+d.evaluation.passed+'/'+d.evaluation.total):'回放失败';
    loadCapabilities();
  }

  /* ---------- 证据介入 ---------- */
  async function loadIntervene(){
    var d = await api('qingyi/evidence-intervene');
    var box = qyEl('intervene-list'); qyClear(box);
    var sv = qyEl('intervene-selfview'); qyClear(sv);
    if(!d || d.ok !== true){ qyNode(box,'div','读不到','empty'); return; }
    var s = d.self_view || {};
    qyNode(sv,'h3','自我修订（self_view_grounding）');
    qyNode(sv,'p', '累计 '+ (s.total||0) +' 条 · 近24h '+ (s.in_window||0) +' 条 · 上次修订 '+
      (s.last_age_hours!=null ? s.last_age_hours+' 小时前' : '从未'), 'qy-anno-text');
    var cs = d.consolidation || {};
    if(cs.result){ qyNode(sv,'p', '上次'+(cs.mode==='new'?'整理':'历史回顾')+'：'+
      (cs.result.ok?('变化='+(cs.result.changed?'有':'无')+' 读'+(cs.result.count||0)+'条'):(cs.result.reason||'')), 'hint'); }
    qyNode(sv,'p', '新对话攒得慢时可用历史（近14天真实对话）做自我修订；后台运行，稍后刷新看近24h条数。', 'hint');
    var runH = qyNode(sv,'button','从历史修订','btn mini'); runH.type='button';
    runH.addEventListener('click', function(){ runConsolidation(runH,'history'); });
    var runN = qyNode(sv,'button','整理新对话','btn mini'); runN.type='button';
    runN.addEventListener('click', function(){ runConsolidation(runN,'new'); });
    var un = d.unlinked || [];
    qyNode(box,'h3','缺证据的动作（action_evidence）· '+un.length+' 条');
    if(!un.length){ qyNode(box,'div','没有缺证据的动作','empty'); }
    un.forEach(function(a){
      var card = qyNode(box,'div','','qy-anno-card');
      qyNode(card,'b', a.id+' · '+a.kind+' · '+a.state);
      qyNode(card,'p', (a.scope||a.session||'')+' · refs='+JSON.stringify(a.refs||[]), 'qy-anno-text');
      var btn = qyNode(card,'button','补挂真实来源','btn mini'); btn.type='button';
      btn.addEventListener('click', function(){ repairAction(a.id, btn); });
    });
  }

  async function repairAction(actionId, btn){
    btn.disabled = true;
    var token = await csrfToken();
    var resp = await fetch('/admin/api/bot/qingyi/repair-action',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':token},
      body: JSON.stringify({action_id:actionId})});
    var d = await resp.json().catch(function(){return {};});
    btn.disabled = false;
    qyText('intervene-msg', d.message || (d.ok?'已补挂':'没补上：'+(d.error||resp.status)));
    loadIntervene();
  }

  async function runConsolidation(btn, mode){
    btn.disabled = true;
    qyText('intervene-msg','已提交后台'+(mode==='new'?'整理':'历史回顾')+'，稍后点刷新看近24h条数…');
    var token = await csrfToken();
    var resp = await fetch('/admin/api/bot/qingyi/run-consolidation',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':token},
      body: JSON.stringify({mode:mode||'history'})});
    var d = await resp.json().catch(function(){return {};});
    btn.disabled = false;
    if(!resp.ok || d.ok !== true){ qyText('intervene-msg','提交失败：'+(d.error||resp.status)); return; }
    setTimeout(loadIntervene, 4000);
  }

  /* ---------- 情感接管门前进度 + 人工授权 ---------- */
  var ENG_KEYS = ['consent_enforcement','action_idempotency_outbox','evidence_retraction','layer_versioning','model_switch_behavior'];
  var ORG_KEYS = ['ledger_integrity','action_evidence','action_identity','action_results','belief_grounding','self_view_grounding','shadow_review'];

  async function loadTakeover(){
    var d = await api('readiness');
    var box = qyEl('takeover-list'); qyClear(box);
    var sum = qyEl('takeover-summary'); qyClear(sum);
    if(!d || !d.phase25){ qyNode(box,'div','准入判定读不到','empty'); return; }
    var checks = d.phase25.checks || [];
    var by = {}; checks.forEach(function(c){ by[c.key]=c; });
    var engPass = ENG_KEYS.filter(function(k){ return (by[k]||{}).status==='pass'; }).length;
    var orgPass = ORG_KEYS.filter(function(k){ return (by[k]||{}).status==='pass'; }).length;
    var s3 = d.stage3 || {};
    qyNode(sum,'b','工程 '+engPass+'/'+ENG_KEYS.length+' · 自然 '+orgPass+'/'+ORG_KEYS.length+' · 表达接管 '+(s3.expression_control?'已开':'未开'));
    qyNode(sum,'span', '还差 '+(d.phase25.blockers||[]).length+' 道门', 'hint');
    renderTakeoverAuth();
    function group(title, keys){
      qyNode(box,'h3',title);
      keys.forEach(function(k){
        var c = by[k] || {status:'unknown'};
        var row = qyNode(box,'div','','rd-row');
        qyStatusTag(row, c.status);
        qyNode(row,'b',k);
        if(c.observed) qyNode(row,'span', JSON.stringify(c.observed), 'rd-obs hint');
        if(c.status!=='pass' && c.reason) qyNode(row,'span', c.reason, 'hint');
      });
    }
    group('① 工程验证（隔离契约，自动跑）', ENG_KEYS);
    group('② 自然样本（真实累积，不补数）', ORG_KEYS);
  }

  async function renderTakeoverAuth(){
    var box = qyEl('takeover-auth'); qyClear(box);
    var d = await api('qingyi/stage3-signoff');
    if(!d || d.ok !== true){ qyNode(box,'div','授权状态读不到','hint'); return; }
    var so = d.signoff || {};
    var active = d.ready && so.granted;
    qyNode(box,'h3','③ 人工授权（只能你本人签字）');
    qyNode(box,'p', active
      ? '已接管（灰度）：情绪 tone 正注入回复上下文，仅语气提示、不裁决说不说；影子静态锁仍 False，可随时撤回。'
      : '签字+前置全过才会接管；接管后情绪 tone 注入回复上下文（仅语气提示），AI 不代翻、可随时撤回。', 'qy-anno-text');
    qyNode(box,'p', '前置：'+(d.ready?'工程+自然已全过，可签字':'未全过（'+(d.blockers||[]).join('、')+'），暂不能签字'), 'hint');
    qyNode(box,'p', '当前签字：'+(so.granted?('已授权（'+(so.by||'')+' '+(so.at||'')+'）'):'未授权')+(active?' · 接管生效中':''), 'hint');
    var grant = qyNode(box,'button', so.granted?'撤回授权签字':'签字授权接管','btn mini'+(so.granted?' qy-anno-del':''));
    grant.type='button';
    grant.disabled = !d.ready && !so.granted;
    grant.addEventListener('click', function(){ stage3Signoff(!so.granted, grant); });
  }

  async function stage3Signoff(granted, btn){
    var msg = granted ? '确认签字授权情感接管？\n（签字且前置全过即生效，情绪 tone 注入回复；可随时撤回）' : '确认撤回接管授权签字？\n（撤回后情绪 tone 立即停止注入）';
    if(!window.confirm(msg)) return;
    btn.disabled = true;
    var token = await csrfToken();
    var resp = await fetch('/admin/api/bot/qingyi/stage3-signoff',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':token},
      body: JSON.stringify({granted:granted})});
    var d = await resp.json().catch(function(){return {};});
    btn.disabled = false;
    qyText('takeover-msg', d.message || (d.ok?'已记录':'没记上：'+(d.error||resp.status)));
    renderTakeoverAuth();
  }

  /* ---------- 自然样本累积 ---------- */
  async function loadOrganic(){
    var d = await api('qingyi/organic');
    var box = qyEl('organic-list'); qyClear(box);
    var hist = (d && d.history) || [];
    if(!hist.length){ qyNode(box,'div','还没有累积快照，等下一轮巡检写入','empty'); return; }
    var last = hist[hist.length-1], prev = hist.length>1 ? hist[hist.length-2] : null;
    qyText('organic-msg', '共 ' + hist.length + ' 个快照 · 最近 ' + qyStamp(last.at));
    var gates = last.gates || {};
    Object.keys(gates).forEach(function(k){
      var g = gates[k] || {};
      var row = qyNode(box,'div','','rd-row');
      qyStatusTag(row, g.status);
      qyNode(row,'b',k);
      var obs = g.observed || {};
      qyNode(row,'span', JSON.stringify(obs), 'rd-obs hint');
      if(prev && prev.gates && prev.gates[k]){
        var po = prev.gates[k].observed || {};
        var delta = [];
        Object.keys(obs).forEach(function(f){
          if(typeof obs[f]==='number' && typeof po[f]==='number' && obs[f]!==po[f])
            delta.push(f + ' ' + po[f] + '→' + obs[f]);
        });
        qyNode(row,'span', delta.length ? ('较上次: '+delta.join(', ')) : '较上次: 无变化', 'hint');
      }
    });
  }

  /* ---------- 人工评审 ---------- */
  function loadReview(){
    var box = qyEl('review-list'); qyClear(box);
    var tasks = (QY.report && QY.report.tasks) || [];
    var need = tasks.filter(function(t){ return t.authorization === 'manual_review'; });
    if(!need.length){ qyNode(box,'div','没有待人工评审的任务','empty'); return; }
    need.forEach(function(t){
      var card = qyNode(box,'div','','qy-anno-card');
      var head = qyNode(card,'div','','qy-anno-head');
      qyNode(head,'b', t.id + ' · ' + t.title);
      var dec = (((QY.overview||{}).approvals)||{})[t.id];
      if(dec) qyNode(head,'span', dec==='approve'?'已批准':'已否决', 'tag '+(dec==='approve'?'ok':'bad'));
      qyNode(card,'p', '工程:'+(t.engineering||'')+' · 自然:'+(t.organic||'')+' · 授权:manual_review', 'qy-anno-text');
      var ap = qyNode(card,'button','批准','btn mini'); ap.type='button';
      ap.addEventListener('click', function(){ qingyiDecide(t.id,'approve',ap); });
      var rj = qyNode(card,'button','否决','btn mini qy-anno-del'); rj.type='button';
      rj.addEventListener('click', function(){ qingyiDecide(t.id,'reject',rj); });
    });
  }

  /* ---------- 人工标注 ---------- */
  function annoStatus(s){
    var mp = (s||{}).memory_provenance || {};
    var total = mp.total||0, sourced = mp.sourced||0;
    var pct = total ? Math.round(sourced*100/total) : 0;
    var fill = qyEl('anno-bar-fill'); if(fill) fill.style.width = pct + '%';
    qyText('anno-count', sourced + ' / ' + total + '（' + pct + '%）');
    var gate = qyEl('anno-gate');
    if(gate){ gate.textContent = mp.status || '—';
      gate.className = 'tag ' + (mp.status==='pass'?'ok':(mp.status==='not_observed'?'am':'')); }
    qyText('anno-annotated', '已绑定 ' + ((s||{}).annotated||0) + ' · 已确认保留 ' + ((s||{}).reviewed||0));
  }

  async function loadAnnotate(){
    if(QY._annoLoading) return;      // 防连点：上一次没回来就不再发请求
    QY._annoLoading = true;
    var rb = qyEl('anno-refresh'); if(rb) rb.disabled = true;
    var box = qyEl('anno-list');
    try{
      var d = await api('annotate/queue');
      qyClear(box);
      if(!d || d.ok !== true){ qyNode(box,'div','标注队列读不到','empty'); return; }
      annoStatus(d.status);
      var samples = d.samples || [];
      if(!samples.length){ qyNode(box,'div','没有待标注的样本（可能都已绑定来源）','empty'); return; }
      renderAnnoSamples(box, samples);
    } finally {
      QY._annoLoading = false;
      if(rb) rb.disabled = false;
    }
  }

  function renderAnnoSamples(box, samples){
    samples.forEach(function(s){
      var card = qyNode(box,'div','','qy-anno-card');
      var head = qyNode(card,'div','','qy-anno-head');
      qyNode(head,'b', (s.store==='persona'?'个人记忆':'群事实') + ' · ' + (s.kind||''));
      qyNode(head,'span', String(s.id), 'hint');
      qyNode(card,'p', s.text, 'qy-anno-text');
      var cands = s.candidates || [];
      if(!cands.length){
        qyNode(card,'div','没找到候选真实事件——这条可能是 legacy，不能补造来源','hint');
      }
      cands.forEach(function(c){
        var label = qyNode(card,'label','','qy-anno-cand');
        var input = document.createElement('input');
        input.type='checkbox'; input.value=c.id; input.className='anno-cand';
        label.appendChild(input);
        var body = qyNode(label,'span','','qy-anno-cand-body');
        if(c.actor) qyNode(body,'span', c.actor + '：', 'qy-anno-actor');
        if(c.ts) qyNode(body,'span', qyStamp(c.ts), 'hint');
        if(c.text) qyNode(body,'span', c.text, '');
        if(c.image_only) qyNode(body,'span','仅图片，点开确认是否相关','hint');
        (c.images||[]).forEach(function(u){
          var a = document.createElement('a');
          a.href=u; a.target='_blank'; a.rel='noopener';
          var img = document.createElement('img');
          img.src=u; img.className='anno-thumb'; img.alt='候选图片';
          img.loading='lazy';
          a.appendChild(img); body.appendChild(a);
        });
        qyNode(label,'span', c.image_only?'图片':'重合'+c.score, 'hint');
      });
      var note = document.createElement('input');
      note.type='text'; note.placeholder='备注（可选）'; note.className='anno-note';
      card.appendChild(note);
      var btn = qyNode(card,'button','绑定所选来源','btn mini');
      btn.type='button';
      btn.addEventListener('click', function(){ annoSubmit(s, card, btn); });
      var del = qyNode(card,'button','删除这条记忆','btn mini qy-anno-del');
      del.type='button';
      del.addEventListener('click', function(){ annoDelete(s, del); });
      var keep = qyNode(card,'button','确认无误，保留','btn mini');
      keep.type='button';
      keep.addEventListener('click', function(){ annoConfirm(s, card, keep); });
    });
  }

  async function annoConfirm(sample, card, btn){
    btn.disabled = true;
    var note = (card.querySelector('.anno-note')||{}).value || '';
    var token = await csrfToken();
    var resp = await fetch('/admin/api/bot/annotate/confirm',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':token},
      body: JSON.stringify({store:sample.store, id:sample.id, group_id:sample.group_id||null, note:note})});
    var d = await resp.json().catch(function(){return {};});
    btn.disabled = false;
    if(!resp.ok || d.ok !== true){ qyText('anno-msg','没保存：'+(d.message||d.error||resp.status)); return; }
    qyText('anno-msg', d.message || '已确认保留');
    annoStatus(d.status);
    loadAnnotate();
  }

  async function annoDelete(sample, btn){
    if(!window.confirm('确定删除这条记忆？\n' + sample.text + '\n删除后不可恢复。')) return;
    btn.disabled = true;
    var token = await csrfToken();
    var resp = await fetch('/admin/api/bot/annotate/delete',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':token},
      body: JSON.stringify({store:sample.store, id:sample.id, group_id:sample.group_id||null})});
    var d = await resp.json().catch(function(){return {};});
    btn.disabled = false;
    if(!resp.ok || d.ok !== true){ qyText('anno-msg','没删掉：'+(d.message||d.error||resp.status)); return; }
    qyText('anno-msg', d.message || '已删除');
    annoStatus(d.status);
    loadAnnotate();
  }

  async function annoSubmit(sample, card, btn){
    var ids = Array.prototype.slice.call(card.querySelectorAll('.anno-cand:checked')).map(function(i){return i.value;});
    var note = (card.querySelector('.anno-note')||{}).value || '';
    if(!ids.length){ qyText('anno-msg','先勾选至少一个来源事件'); return; }
    btn.disabled = true;
    var token = await csrfToken();
    var resp = await fetch('/admin/api/bot/annotate/submit',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':token},
      body: JSON.stringify({store:sample.store, id:sample.id, group_id:sample.group_id||null,
                            event_ids:ids, note:note})});
    var d = await resp.json().catch(function(){return {};});
    btn.disabled = false;
    if(!resp.ok || d.ok !== true){ qyText('anno-msg','没写进去：'+(d.message||d.error||resp.status)); return; }
    qyText('anno-msg', d.message || '已绑定');
    annoStatus(d.status);
    loadAnnotate();
  }

  async function annoAutolink(){
    var btn = qyEl('anno-autolink'); if(btn) btn.disabled = true;
    qyText('anno-msg','自动绑定中…');
    var token = await csrfToken();
    var resp = await fetch('/admin/api/bot/annotate/autolink',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':token},
      body: JSON.stringify({limit:100})});
    var d = await resp.json().catch(function(){return {};});
    if(btn) btn.disabled = false;
    if(!resp.ok || d.ok !== true){ qyText('anno-msg','自动绑定失败：'+(d.error||resp.status)); return; }
    qyText('anno-msg','自动绑定 ' + (d.linked||0) + ' 条（只认逐字可定位）');
    annoStatus(d.status);
    loadAnnotate();
  }

  function renderQingyiTask(parent,task){
    if(!parent) return;
    var row = qyNode(parent,'div','','qy-task');
    row.dataset.taskId = task.id;
    row.dataset.taskStatus = task.engineering || 'planned';
    row.dataset.taskReady = task.dev_ready ? 'true' : 'false';
    var head = qyNode(row,'div','','qy-task-head');
    qyNode(head,'b',task.id + ' · ' + task.title);
    qyImplTag(head,task.implementation);
    qyStatusTag(head,task.engineering);
    qyStatusTag(head,task.organic);
    qyStatusTag(head,task.authorization);
    // 人工授权的账本状态：批没批，面板上一眼能看见，不用去翻 QQ 记录。
    var plan = qyPlanSets();
    var dec = (((QY.overview||{}).approvals)||{})[task.id];
    if(dec) qyNode(head,'span', dec === 'approve' ? '已批准' : '已否决',
                   'tag ' + (dec === 'approve' ? 'ok' : 'bad'));
    // 按钮只长在"等批准"的那几个任务上：已实现、可自动推进、已否决的都不该再问一遍。
    if(plan.has && plan.approval.indexOf(task.id) >= 0){
      var acts = qyNode(head,'span','','qy-acts');
      [['approve','同意'],['reject','否决']].forEach(function(pair){
        var button = qyNode(acts,'button',pair[1],'btn mini');
        button.type = 'button';
        button.addEventListener('click', function(){ qingyiDecide(task.id, pair[0], button); });
      });
    }
    var blockers = (task.blocked_by||[]).length ? '开发前置：' + task.blocked_by.join('、') : '无未完成开发前置';
    qyNode(row,'small',(task.release_ready?'可发布 ':'') + blockers + '；验收前置 ' + ((task.acceptance_requires||[]).join('、')||'无'));
  }

  async function qingyiDecide(taskId, decision, button){
    if(button) button.disabled = true;
    var say = function(msg){ qyText('review-msg', msg); qyText('qingyi-message', msg); };
    try{
      var token = await csrfToken();
      var response = await fetch('/admin/api/bot/qingyi/decision',{
        method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-CSRF-Token':token},
        body: JSON.stringify({task_id: taskId, decision: decision})
      });
      var data = await response.json().catch(function(){ return {}; });
      if(!response.ok || data.ok !== true){
        say('没记上：' + (data.message || data.error || response.status));
      }else{
        say(data.message || (taskId + ' 已' + (decision==='approve'?'批准':'否决')));
      }
      await loadQingyi();   // 刷新 approvals
      loadReview();         // 刷新评审列表的已批准/已否决标签
    }catch(error){
      say('操作结果未知，请刷新面板确认');
    }
    if(button) button.disabled = false;
  }

  // 「做完」只看代码有没有落地：四个维度里 implementation 是唯一表示
  // 「这活写完了没有」。用 release_ready 当判据会把已上线但还缺自然样本的
  // 任务永远留在「当前可做」里 —— 那是验收没走完，不是活还没干。
  function qyIsDone(task){
    return task.implementation === 'implemented' || task.implementation === 'existing';
  }

  // 「可做」= 没做完 + 开发前置全部达成。不能直接用 dev_ready：
  // 后端的 dev_ready 语义是「自己已实现且前置齐」，拿它当「能开工」会把
  // 已完成的任务全塞进当前可做 —— 那正是这一栏之前显示的全是做完的原因。
  function qyIsDoable(task){
    return !qyIsDone(task) && !(task.blocked_by || []).length;
  }

  // 「可做/待批」以自动驾驶那一轮的计划为准 —— 面板和 QQ 通知必须同一份前沿。
  // 没有计划快照时才退回本地推算，并在摘要里说清楚是推算的。
  function qyPlanSets(){
    var n = ((QY.overview || {}).next) || {};
    return {
      at: n.at || '',
      has: !!n.at,
      doable: n.doable || [],
      approval: n.approval || []
    };
  }

  function qingyiTaskFilter(task){
    var plan = qyPlanSets();
    if(QY.filter === 'all') return true;
    if(QY.filter === 'tonight') return plan.has ? plan.doable.indexOf(task.id) >= 0 : qyIsDoable(task);
    if(QY.filter === 'done') return qyIsDone(task);
    if(QY.filter === 'blocked') return (task.engineering === 'blocked' || task.organic === 'blocked' || (task.blocked_by||[]).length);
    if(QY.filter === 'organic') return task.organic === 'not_observed';
    if(QY.filter === 'manual') return plan.has ? plan.approval.indexOf(task.id) >= 0
      : (task.manual_review || task.authorization === 'manual_review');
    return task.manual_review || task.authorization === 'manual_review';
  }

  function renderQingyiStages(){
    var box = qyEl('qingyi-stages'); qyClear(box);
    var stages = (QY.report && QY.report.stages) || [];
    var tasks = ((QY.report && QY.report.tasks) || []);
    if(!stages.length){ qyNode(box,'div','目录尚未加载','empty'); return; }
    stages.forEach(function(stage){
      var rows = tasks.filter(function(task){ return task.stage === stage.id && qingyiTaskFilter(task); });
      if(!rows.length && QY.filter !== 'all') return;
      var card = qyNode(box,'section','','qy-stage');
      var header = qyNode(card,'header','','');
      qyNode(header,'h3',stage.name);
      qyNode(header,'span',stage.purpose || '','hint');
      var progress = qyNode(card,'div','','qy-progress');
      ['implementation','engineering','organic','authorization'].forEach(function(field){
        var item = qyNode(progress,'div','','');
        qyNode(item,'span',{'implementation':'开发状态','engineering':'工程验证','organic':'自然证据','authorization':'人工授权'}[field]);
        var passed = rows.filter(function(x){ return x[field] === 'pass' || (field === 'implementation' && (x[field] === 'implemented' || x[field] === 'existing')); }).length;
        qyNode(item,'b',rows.length ? passed + '/' + rows.length : '暂无任务');
      });
      var list = qyNode(card,'div','','qy-tasks');
      rows.forEach(function(task){ renderQingyiTask(list,task); });
      if(!rows.length) qyNode(list,'div','当前筛选没有任务','empty');
    });
  }

  function renderQingyiChecks(){
    var box = qyEl('qingyi-checks'); qyClear(box);
    var checks = (QY.report && QY.report.checks) || [];
    qyText('qingyi-check-status', QY.report ? ('最近检测：' + String(QY.report.scope_status || '未知')) : '未检测');
    // 与后端回退口径一致：目录版本、代码 binding、数据根任一不符就是过时报告，
    // 不能只凭 binding_digest 存在就宣称「已绑定当前代码」。
    var ident = (QY.overview && QY.overview.identity) || {};
    var bound = QY.report && QY.report.binding_digest
      && QY.report.binding_digest === ident.binding_digest
      && QY.report.data_identity_digest === ident.data_identity_digest
      && QY.report.catalog_version === ident.catalog_version;
    var boundNote = !QY.report ? '' : (bound ? '证据已绑定当前代码'
      : (QY.report.binding_digest ? '报告已过时，任务状态以实时目录为准' : '绑定不可读'));
    var fromRound = ((QY.overview||{}).report_source === 'round') ? '本轮巡检' : '上次作业';
    var stamp = qyStamp(QY.report && QY.report.checked_at);
    qyText('qingyi-check-meta', QY.report
      ? ('catalog ' + QY.report.catalog_version + ' · ' + fromRound + (stamp ? ' ' + stamp : '') + ' · ' + boundNote)
      : '点击下方“运行前置检测”读取现有证据');
    if(!checks.length){ qyNode(box,'div','尚无检测报告','empty'); return; }
    checks.forEach(function(check){
      var row = qyNode(box,'div','','rd-row');
      qyStatusTag(row,check.status);
      qyNode(row,'b',check.id + ' · ' + (check.message || ''));
      qyNode(row,'span',JSON.stringify(check.counts || {}),'rd-obs hint');
      (check.limitations||[]).forEach(function(text){ qyNode(row,'span',text,'hint'); });
    });
    renderEngChecks();
  }

  var ENG_GATES = {
    consent_enforcement: '同意边界：拒绝/未知媒体不得放行（4 个隔离用例）',
    action_idempotency_outbox: '动作不重复投递：崩溃/重启后不重发（11 个用例）',
    evidence_retraction: '撤回能传导：撤回后所有消费方失效（3 个用例）',
    layer_versioning: '分层版本化：人格保存不产生空修订（1 个用例）',
    model_switch_behavior: '换模型不漂移：切换后认知/证据不变（1 个用例）'
  };

  function renderEngChecks(){
    var box = qyEl('eng-list'); if(!box) return; qyClear(box);
    var checks = (QY.report && QY.report.checks) || [];
    var eng = checks.filter(function(c){ return ENG_GATES[c.id]; });
    if(!eng.length){ qyNode(box,'div','尚无工程验证读数','empty'); return; }
    eng.forEach(function(c){
      var row = qyNode(box,'div','','rd-row');
      qyStatusTag(row,c.status);
      qyNode(row,'b',c.id);
      qyNode(row,'span',ENG_GATES[c.id],'hint');
      var ex = (c.observed && c.observed.execution) || (c.counts && c.counts.execution) || null;
      if(ex){ qyNode(row,'span','跑 '+ex.run+' 例 · 失败 '+ex.failures+' · 错误 '+ex.errors,'rd-obs hint'); }
      else { qyNode(row,'span','无读数（证据未绑定当前代码）','rd-obs hint'); }
    });
  }

  async function engRun(){
    var btn = qyEl('eng-run'); if(btn) btn.disabled = true;
    qyText('eng-msg','启动隔离验证（约30–60秒）…');
    try{
      var token = await csrfToken();
      var resp = await fetch('/admin/api/bot/qingyi/preflight',{method:'POST',credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-CSRF-Token':token},
        body: JSON.stringify({profile:'isolated'})});
      var d = await resp.json().catch(function(){return {};});
      if(!resp.ok || d.ok !== true){ qyText('eng-msg','未启动：'+(d.error||resp.status)); if(btn)btn.disabled=false; return; }
      QY.run_id = d.run_id;
      qyText('eng-msg','验证运行中，进度实时刷新…');
      startQingyiPoll(d.run_id);
    }catch(e){ qyText('eng-msg','启动结果未知，请刷新'); if(btn)btn.disabled=false; }
  }

  function renderQingyiMemory(){
    var box = qyEl('qingyi-memory-tasks'); qyClear(box);
    var tasks = ((QY.report && QY.report.tasks) || []);
    qyText('qingyi-memory-status', tasks.length ? '任务目录已载入' : '未计算');
    qyText('qingyi-memory-meta', '开发前置与发布验收分开：Ripple未完成不阻塞N1/N2/N3。');
    [['当前记忆切片',function(x){ return ['A02','A03','A04'].indexOf(x.id) >= 0; }],
     ['来源与撤回',function(x){ return x.id[0] === 'M'; }],
     ['Ripple关联',function(x){ return x.id[0] === 'R'; }]].forEach(function(pair){
      var section = qyNode(box,'section','','');
      qyNode(section,'h3',pair[0]);
      var list = qyNode(section,'div','','qy-tasks');
      tasks.filter(pair[1]).forEach(function(task){ renderQingyiTask(list,task); });
    });
  }

  function renderQingyiCatalog(){
    var box = qyEl('qingyi-catalog'); qyClear(box);
    var stages = (QY.report && QY.report.stages) || [];
    var tasks = (QY.report && QY.report.tasks) || [];
    stages.forEach(function(stage){
      qyNode(box,'h3',stage.name);
      var list = qyNode(box,'div','','qy-tasks');
      tasks.filter(function(task){ return task.stage === stage.id; }).forEach(function(task){ renderQingyiTask(list,task); });
    });
  }

  function renderQingyiTop(){
    var report = QY.report, overview = QY.overview || {}, summary = overview.stages||[], validation = overview.validation||{};
    var catalogSize = (overview.tasks || []).length;
    qyText('qingyi-stage-summary', report ? ('目录 ' + report.catalog_version) : (summary.length ? '目录已载入' : '未读取'));
    qyText('qingyi-stage-note', validation.ok ? (catalogSize + '项目录依赖完整且无环') : '目录结构异常，详情见前置检测');
    qyText('qingyi-report-summary', report ? ('范围状态：' + String(report.scope_status || '未知')) : '尚无报告');
    var latestNote = QY.latest ? ('状态 ' + QY.latest.state + ' · 启动 ' + new Date((QY.latest.started || 0)*1000).toLocaleString()) : '—';
    if(QY.latest && QY.latest.error) latestNote += ' · ' + String(QY.latest.error).slice(0,60);
    if(overview.report_source === 'round' && report && report.checked_at){
      latestNote = '本轮巡检 · ' + qyStamp(report.checked_at) + ' · 范围 ' + String(report.scope_status||'');
    }
    qyText('qingyi-report-time', latestNote);
    var plan = qyPlanSets();
    var next = plan.has
      ? (((QY.report||{}).tasks)||[]).filter(function(t){ return plan.doable.indexOf(t.id) >= 0; })
      : (((QY.report||{}).tasks)||[]).filter(qyIsDoable);
    qyText('qingyi-next-summary', next.length ? (next.length + ' 项可推进') : '等待前置');
    qyText('qingyi-next-list', (next.slice(0,5).map(function(x){ return x.id; }).join('、') || '—')
      + (plan.has ? ('　·　口径 ' + String(plan.at).slice(5, 16)) : '　·　本地推算'));
    var tk = overview.takeover || {};
    qyText('qingyi-control-summary', tk.active ? '已接管·灰度' : (tk.granted ? '已签字·待前置' : '未接管'));
    var download = qyEl('qingyi-download');
    if(download) download.disabled = !(report && QY.latest && QY.latest.run_id);
  }

  function setRunEnabled(enabled){
    var button = qyEl('qingyi-run'); if(button) button.disabled = !enabled;
  }

  async function loadQingyi(){
    bindQingyi();
    var d = await api('qingyi/overview');
    if(!d || d.ok !== true){
      QY.report = null; QY.overview = null;
      qyText('qingyi-message','推进目录或最近报告读不到，未显示任何通过状态');
      renderQingyiTop(); renderQingyiStages(); renderQingyiChecks(); renderQingyiMemory(); renderQingyiCatalog();
      return;
    }
    QY.overview = d; QY.latest = d.latest_job || null; QY.report = d.report || null;
    if(d.report) QY.run_id = d.report.run_id || (QY.latest && QY.latest.run_id);
    qyText('qingyi-message','已读取目录和最近报告；本次没有运行检测');
    renderQingyiTop(); renderQingyiStages(); renderQingyiChecks(); renderQingyiMemory(); renderQingyiCatalog();
    if(QY.tab === 'shadow' || QY.tab === 'evidence') switchQingyiTab(QY.tab);
    var active = QY.latest && (QY.latest.state === 'running' || QY.latest.state === 'queued');
    setRunEnabled(!active);
    if(active) startQingyiPoll(QY.latest.run_id);
  }

  function startQingyiPoll(runId){
    if(QY.poll) clearTimeout(QY.poll);
    QY.poll = null; QY.pollRun = runId;
    var started = performance.now();
    async function step(){
      QY.poll = null;
      if(QY.pollRun !== runId) return;
      if(document.hidden || STATE.view !== 'qingyi'){ setRunEnabled(true); return; }
      var d = await raw('/admin/api/bot/qingyi/preflight/' + encodeURIComponent(runId),{credentials:'same-origin'});
      if(!d || !d.ok){ QY.poll = setTimeout(step, 3000); return; }
      QY.latest = d.job; if(d.report) QY.report = d.report;
      qyText('qingyi-message','检测状态：' + (d.job && d.job.state) + (d.job && d.job.error ? ('（' + d.job.error + '）') : ''));
      renderQingyiTop(); renderQingyiStages(); renderQingyiChecks(); renderQingyiMemory(); renderQingyiCatalog();
      if(d.job && ['pass','blocked','unknown','error','timed_out','interrupted'].indexOf(d.job.state) >= 0){
        QY.pollRun = null; setRunEnabled(true);
        var eb = qyEl('eng-run'); if(eb) eb.disabled = false;
        qyText('eng-msg','验证完成：' + (d.job.state||''));
        return;
      }
      var waited = performance.now() - started;
      QY.poll = setTimeout(step, waited < 10000 ? 1200 : 3000);
    }
    QY.poll = setTimeout(step, 600);
  }

  async function runQingyiPreflight(){
    setRunEnabled(false);
    qyText('qingyi-message','正在启动只读前置检测…');
    try{
      var token = await csrfToken();
      var response = await fetch('/admin/api/bot/qingyi/preflight',{method:'POST',credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({profile:'evidence'})});
      var data = await response.json();
      if(!response.ok || data.ok !== true){
        if(data && data.existing_run_id && (data.state === 'conflict' || data.state === 'recovering')){
          QY.run_id = data.existing_run_id;
          qyText('qingyi-message', data.state === 'conflict'
            ? '已有检测在运行，正在跟随那一次，不会重复启动'
            : '上一次检测正在收尾，等待租约恢复后再决定');
          startQingyiPoll(data.existing_run_id);
          return;
        }
        qyText('qingyi-message','未启动：' + (data.error || response.status));
        setRunEnabled(true);
        return;
      }
      QY.run_id = data.run_id; QY.latest = data;
      qyText('qingyi-message','检测运行中：只读现有证据，不会写记忆或发送消息');
      startQingyiPoll(data.run_id);
    }catch(error){ qyText('qingyi-message','启动结果未知，请刷新状态而不是重复点击'); setRunEnabled(true); }
  }

  function bindQingyi(){
    qyBindOnce('qingyi-refresh','click',loadQingyi);
    qyBindOnce('qingyi-run','click',runQingyiPreflight);
    qyBindOnce('anno-refresh','click',loadAnnotate);
    qyBindOnce('anno-autolink','click',annoAutolink);
    qyBindOnce('eng-run','click',engRun);
    qyBindOnce('organic-refresh','click',loadOrganic);
    qyBindOnce('capabilities-refresh','click',loadCapabilities);
    qyBindOnce('capabilities-evaluate','click',evaluateCapabilities);
    qyBindOnce('takeover-refresh','click',loadTakeover);
    qyBindOnce('intervene-refresh','click',loadIntervene);
    qyBindOnce('qingyi-download','click',function(){
      if(QY.run_id) location.href = '/admin/api/bot/qingyi/preflight/' + encodeURIComponent(QY.run_id) + '/download';
    });
    document.querySelectorAll('#view-qingyi .qy-tab').forEach(function(button){
      if(button._bound) return; button._bound=true;
      button.addEventListener('click',function(){ switchQingyiTab(button.getAttribute('data-qingyi-tab')); });
    });
    document.querySelectorAll('#view-qingyi [data-qingyi-filter]').forEach(function(button){
      if(button._bound) return; button._bound=true;
      button.addEventListener('click',function(){
        QY.filter = button.getAttribute('data-qingyi-filter');
        document.querySelectorAll('#view-qingyi [data-qingyi-filter]').forEach(function(x){ x.classList.toggle('on',x===button); });
        renderQingyiStages();
      });
    });
    qyBindOnce('emotion-shadow-refresh','click',loadEmotionShadow);
    qyBindOnce('readiness-refresh','click',loadReadiness);
    var cognitionForm = qyEl('cognition-control-form');
    if(cognitionForm && !cognitionForm._bound){
      cognitionForm._bound = true;
      cognitionForm.addEventListener('submit', async function(event){
        event.preventDefault();
        var result = qyEl('cognition-result'), button = cognitionForm.querySelector('button[type="submit"]');
        button.disabled = true;
        try{
          var response = await fetch('/admin/api/bot/cognition-control',{method:'POST',credentials:'same-origin',
            headers:{'Content-Type':'application/json','X-CSRF-Token':await csrfToken()},
            body:JSON.stringify({operation:qyEl('cognition-operation').value,recipient:qyEl('cognition-recipient').value,
              action:qyEl('cognition-action').value,decision:qyEl('cognition-decision').value,
              id:qyEl('cognition-id').value,reason:qyEl('cognition-reason').value})});
          var data = await response.json();
          result.textContent = response.ok && data.ok ? (data.result ? '已记录指令；未授权阶段3。' : '未变更：记录不存在或不可撤回。') : '未执行：' + (data.error || response.status);
          if(response.ok && data.ok) loadReadiness();
        }catch(error){ result.textContent = '结果未知，请核对记录后再操作。'; }
        finally{ button.disabled=false; }
      });
    }
  }

  async function loadPersona(){
    // 绑定一次；刷新仅更新数据容器，不重建按钮或改写用户的展开选择。
    bindPersonaDisclosure('persona-settings-toggle', '展开人设与回复设置', '收起人设与回复设置');
    var exRefresh = $('ex-refresh');
    if(exRefresh && !exRefresh._bound){
      exRefresh._bound = true;
      exRefresh.addEventListener('click', loadExploring);
    }
    loadExploring();
    loadPhotoNotes();
    var openQingyi = $('persona-open-qingyi');
    if(openQingyi && !openQingyi._bound){
      openQingyi._bound = true;
      openQingyi.addEventListener('click', function(){ switchView('qingyi'); });
    }
    bindAffectionRefresh();
    renderPresets();
    loadAffection();
    var _afb = $('af-bump');
    if(_afb && !_afb._bound){
      _afb._bound = true;
      _afb.onclick = async function(){
        var uid = ($('af-uid').value || '').trim();
        var d = parseFloat($('af-delta').value || '0');
        if(!uid || !d){ if($('af-msg')) $('af-msg').textContent = '填 QQ号 和分数'; return; }
        try{
          var r = await api('/admin/api/bot/affection','POST',{action:'bump',uid:uid,delta:d});
          renderAffection(r);
          if($('af-msg')) $('af-msg').textContent = 'OK';
        }catch(e){ if($('af-msg')) $('af-msg').textContent = '失败'; }
      };
      var _afr = $('af-reset'), resetPanel = $('af-reset-confirm');
      function closeAffectionReset(){
        resetPanel.hidden = true;
        _afr.setAttribute('aria-expanded', 'false');
        _afr.focus();
      }
      if(_afr && resetPanel){
        _afr.onclick = function(){
          resetPanel.hidden = false;
          _afr.setAttribute('aria-expanded', 'true');
          $('af-reset-no').focus();
        };
        $('af-reset-no').onclick = closeAffectionReset;
        $('af-reset-yes').onclick = async function(){
          var button = this;
          button.disabled = true;
          try{
            var r = await api('affection','POST',{action:'reset'});
            if(!r || r.error) throw new Error('reset failed');
            renderAffection(r);
            $('af-msg').textContent = '已归零';
            closeAffectionReset();
          }catch(e){ $('af-msg').textContent = '归零失败，请重试'; }
          finally{ button.disabled = false; }
        };
      }
    }
    var mode = await api('persona-mode');
    if(mode){
      var m = String(mode.mode || 'whale');
      $('pm-whale').checked = (m === 'whale');
      $('pm-generic').checked = (m !== 'whale');
      $('pf-name').value = mode.display_name || '';
      $('pf-custom').value = mode.custom_persona || '';
      var tr = mode.triggers || [];
      $('pf-triggers').value = Array.isArray(tr) ? tr.join(',') : String(tr || '');
    }

    var cfg = await api('effective-config');
    STATE.cfg = cfg || {};
    var s = (cfg && cfg.settings) || {};
    bindRange('rp-chars', 'rp-chars-v', s.reply_chars, 45);
    bindRange('rp-tokens', 'rp-tokens-v', s.reply_tokens, 140);
    bindRange('rp-bubbles', 'rp-bubbles-v', s.bubbles, 2);
    bindRange('rp-delay', 'rp-delay-v', s.delay_ms, 500);
    var ctrl0 = await api('control');
    var cs = ((ctrl0 && ctrl0.state) || {}).settings || {};
    $('rp-patrol').checked = (cs.patrol_enabled !== undefined ? cs.patrol_enabled : s.patrol_enabled) !== false;
    $('rp-memory').checked = (cs.memory_enabled !== undefined ? cs.memory_enabled : s.memory_enabled) !== false;

    loadPersonaMemory();
    loadWishes();
    startWhisperPoll();
    var self = await api('persona-self');
    var b = $('pg-block');
    if(!self || !self.exists){
      b.innerHTML = '<div class="empty">成长档案尚未生成 · 已记录对话 ' +
        num(self && self.journal_lines, 0) + ' 条，攒够后每小时整理一次</div>';
    } else {
      var p = self.profile || {};
      var v = p.voice || {};
      var h = '';
      h += '<div class="inline" style="margin-bottom:12px">' +
           '<span class="tag cy">REV ' + num(p.revision,0) + '</span>' +
           '<span class="tag">对话 ' + num(self.journal_lines,0) + ' 条</span>' +
           '<span class="tag">整理 ' + num((p.stats||{}).consolidations,0) + ' 次</span>' +
           '<span class="tag">更新 ' + esc(relTime(p.updated)) + '</span></div>';
      if(p.self_view) h += '<div style="margin-bottom:12px;color:#cfe6f7;font-size:13px">「' + esc(p.self_view) + '」</div>';
      var chips = function(arr, cls){
        if(!arr || !arr.length) return '';
        return '<div class="chips" style="margin-bottom:10px">' + arr.map(function(x){
          return '<span class="chip ' + (cls||'') + '">' + esc(x) + '</span>';
        }).join('') + '</div>';
      };
      if(v['口癖'] && v['口癖'].length) h += '<div class="bar-lab"><span>口癖</span><b></b></div>' + chips(v['口癖']);
      if(v['语气']) h += kv('说话调子', esc(v['语气']));
      if(v['句式'] && v['句式'].length) h += '<div class="bar-lab" style="margin-top:10px"><span>惯用说法</span><b></b></div>' + chips(v['句式']);
      if(p.likes && p.likes.length) h += '<div class="bar-lab" style="margin-top:10px"><span>喜欢</span><b></b></div>' + chips(p.likes);
      if(p.dislikes && p.dislikes.length) h += '<div class="bar-lab" style="margin-top:10px"><span>不喜欢</span><b></b></div>' + chips(p.dislikes);
      if(p.curious && p.curious.length) h += '<div class="bar-lab" style="margin-top:10px"><span>在意</span><b></b></div>' + chips(p.curious);
      if(p.experiences && p.experiences.length) h += '<div class="bar-lab" style="margin-top:10px"><span>经历</span><b></b></div>' + chips(p.experiences);
      if(p.facts && p.facts.length) h += '<div class="bar-lab" style="margin-top:10px"><span>查到/记住的事</span><b></b></div>' + chips(p.facts);
      b.innerHTML = h || '<div class="empty">档案为空</div>';
    }
  }

  /* ---------- 人格记忆编辑 ---------- */
  var PM_FIELD = 'facts';
  var PM_FIELDS = [
    { k:'facts',       n:'记住的事' },
    { k:'experiences', n:'经历' },
    { k:'likes',       n:'喜欢' },
    { k:'dislikes',    n:'不喜欢' },
    { k:'curious',     n:'在意' }
  ];
  var PM_PROFILE = {};

  function pmRenderTabs(){
    var box = $('pm-tabs'); if(!box) return;
    box.innerHTML = PM_FIELDS.map(function(f){
      var n = ((PM_PROFILE[f.k] || []).length);
      return '<button class="pm-tab' + (f.k === PM_FIELD ? ' on' : '') + '" data-f="' + f.k + '">' +
             esc(f.n) + ' ' + n + '</button>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.pm-tab'), function(b){
      b.addEventListener('click', function(){
        PM_FIELD = this.getAttribute('data-f');
        pmRenderTabs(); pmRenderList();
      });
    });
  }

  function pmRenderList(){
    var box = $('pm-list'); if(!box) return;
    var arr = PM_PROFILE[PM_FIELD] || [];
    if(!arr.length){
      box.innerHTML = '<div class="empty">这一类还是空的</div>';
      return;
    }
    box.innerHTML = arr.map(function(t, i){
      return '<div class="pm-item" data-i="' + i + '">' +
             '<div class="t" contenteditable="true" spellcheck="false">' + esc(t) + '</div>' +
             '<span class="x" data-del="' + i + '">删除</span></div>';
    }).join('');

    Array.prototype.forEach.call(box.querySelectorAll('[data-del]'), function(b){
      b.addEventListener('click', async function(){
        var i = parseInt(this.getAttribute('data-del'), 10);
        var r = await api('persona-self', 'POST', { action:'remove', field:PM_FIELD, index:i });
        if(r && r.ok){ PM_PROFILE = r.profile || {}; pmRenderTabs(); pmRenderList(); toast('已删除','ok'); }
        else toast('删除失败: ' + ((r&&r.error)||''), 'err');
      });
    });

    Array.prototype.forEach.call(box.querySelectorAll('.pm-item .t'), function(el){
      var origin = el.textContent;
      el.addEventListener('blur', async function(){
        var val = el.textContent.trim();
        if(!val || val === origin) return;
        var i = parseInt(el.parentNode.getAttribute('data-i'), 10);
        var r = await api('persona-self', 'POST', { action:'edit', field:PM_FIELD, index:i, text:val });
        if(r && r.ok){
          PM_PROFILE = r.profile || {};
          origin = val;
          pmRenderTabs();
          toast('已修改','ok');
        } else {
          toast('改失败: ' + ((r&&r.error)||''), 'err');
          el.textContent = origin;
        }
      });
    });
  }

  async function loadPersonaMemory(){
    var d = await api('persona-self');
    PM_PROFILE = (d && d.profile) || {};
    pmRenderTabs();
    pmRenderList();
    if($('pm-selfview')) $('pm-selfview').value = PM_PROFILE.self_view || '';
  }

  var pmAdd = $('pm-add');
  if(pmAdd) pmAdd.addEventListener('click', async function(){
    var inp = $('pm-new');
    var val = (inp.value || '').trim();
    if(!val){ toast('先写点东西','err'); return; }
    var r = await api('persona-self', 'POST', { action:'add', field:PM_FIELD, text:val });
    if(r && r.ok){
      PM_PROFILE = r.profile || {};
      inp.value = '';
      $('pm-msg').textContent = '已添加';
      pmRenderTabs(); pmRenderList();
      toast('已加进人格记忆','ok');
    } else toast('添加失败: ' + ((r&&r.error)||''), 'err');
  });

  var pmSv = $('pm-save-sv');
  if(pmSv) pmSv.addEventListener('click', async function(){
    var r = await api('persona-self', 'POST', { action:'self_view', text: $('pm-selfview').value.trim() });
    if(r && r.ok){ PM_PROFILE = r.profile || {}; toast('自我认知已保存','ok'); }
    else toast('保存失败','err');
  });

  /* ---------- 愿望墙 + 悄悄话 ---------- */
  var WHISPER_TIMER = null;

  function wRelTime(ts){
    try{
      var d = Math.max(0, Date.now()/1000 - Number(ts||0));
      if(d < 60) return '刚刚';
      if(d < 3600) return Math.floor(d/60) + ' 分钟前';
      if(d < 86400) return Math.floor(d/3600) + ' 小时前';
      return Math.floor(d/86400) + ' 天前';
    }catch(e){ return ''; }
  }

  var WISH_SIG = '';

  function wishSignature(d){
    var ws = (d && d.wishes) || [];
    var cs = (d && d.chat) || [];
    var ar = (d && d.archived) || [];
    return ws.map(function(w){
      return w.id + ':' + w.status + ':' + ((w.replies || []).length)
        + ':' + (w.image ? '1' : '0');
    }).join('|') + '#' + ar.length + '#' + cs.length + '#' + ((d && d.pending) || 0);
  }

  // 愿望列表：内容没变就不重绘；正在输入时也不重绘（否则输入框会被清掉）
  function renderWishList(d){
    var box = $('wish-list'); if(!box) return;
    var sig = wishSignature(d);
    var active = document.activeElement;
    var typing = active && box.contains(active) && active.tagName === 'INPUT' && (active.value || '').length > 0;
    // 正在输入就绝不重绘：否则光标会跳、内容会被清掉。打完字自然会刷新。
    if(typing) return;
    if(sig === WISH_SIG) return;

    // 先把已经输入的内容存下来
    var saved = {};
    Array.prototype.forEach.call(box.querySelectorAll('input[data-wish]'), function(inp){
      var k = inp.getAttribute('data-wish');
      if(inp.value) saved[k] = inp.value;
    });

    var ws = (d && d.wishes) || [];
    if(!ws.length){
      box.innerHTML = '<div class="empty">还没有愿望，等她自己写</div>';
    } else {
      box.innerHTML = ws.slice().reverse().map(function(w){
        var tag = w.kind === 'know' ? '想知道' : '想要';
        var done = w.status === 'done';
        var reps = (w.replies || []).map(function(r){
          var who = r.role === 'father' ? '主人' : '她';
          return '<div class="rep"><b>' + who + '：</b>' + esc(r.text) + '</div>';
        }).join('');
        return '<div class="wish" data-id="' + esc(w.id) + '">' +
          '<div class="h"><span>' + tag + (done ? ' · 已了却' : '') + '</span><span>' + wRelTime(w.ts) + '</span></div>' +
          '<div class="t">' + esc(w.text) + '</div>' +
          (w.image ? ('<img class="wish-img" src="/admin/api/bot/photo/file?name='
            + encodeURIComponent(w.image) + '" alt="她看到的图">') : '') + reps +
          '<div class="box"><input type="text" placeholder="回她一句…" data-wish="' + esc(w.id) + '">' +
          '<button class="btn" data-wsend="' + esc(w.id) + '">回</button></div>' +
        '</div>';
      }).join('');
    }

    // 已归档的愿望：收在折叠区里，点开还能看
    var ar = (d && d.archived) || [];
    var arBox = $('wish-archived');
    if(arBox){
      if(!ar.length){
        arBox.innerHTML = '';
      } else {
        arBox.innerHTML = '<details style="margin-top:10px">' +
          '<summary style="cursor:pointer;font-size:11px;font-family:var(--mono);color:var(--dim2);letter-spacing:.5px">' +
          '已归档 ' + ar.length + ' 条 · 都写进记忆了</summary>' +
          ar.slice().reverse().slice(0, 40).map(function(w){
            var reps = (w.replies || []).map(function(r){
              var who = r.role === 'father' ? '主人' : '她';
              return '<div class="rep"><b>' + who + '：</b>' + esc(r.text) + '</div>';
            }).join('');
            return '<div class="wish" style="opacity:.62;margin-top:8px">' +
              '<div class="h"><span>' + (w.kind === 'know' ? '想知道' : '想要') + ' · 已了却</span>' +
              '<span>' + wRelTime(w.archived_ts || w.ts) + '</span></div>' +
              '<div class="t">' + esc(w.text) + '</div>' + reps + '</div>';
          }).join('') + '</details>';
      }
    }

    // 把刚才存的内容填回去
    Object.keys(saved).forEach(function(k){
      var inp = box.querySelector('input[data-wish="' + k + '"]');
      if(inp) inp.value = saved[k];
    });

    Array.prototype.forEach.call(box.querySelectorAll('[data-wsend]'), function(b){
      b.addEventListener('click', async function(){
        var id = this.getAttribute('data-wsend');
        var inp = box.querySelector('input[data-wish="' + id + '"]');
        var val = (inp.value || '').trim();
        if(!val){ toast('说点什么','err'); return; }
        inp.disabled = true;
        var r = await api('wishes', 'POST', { action:'reply_wish', wish_id:id, text:val });
        if(r && r.ok){
          inp.value = '';
          WISH_SIG = '';
          $('whisper-hint').textContent = '她正在回你…';
          setTimeout(loadWishes, 1200);
        } else { toast('发送失败','err'); inp.disabled = false; }
      });
    });

    WISH_SIG = sig;
  }

  // 聊天区：只换聊天记录，不动输入框
  function renderChat(d){
    var log = $('whisper-log'); if(!log) return;
    var cs = (d && d.chat) || [];
    var sig = cs.length + '#' + (cs.length ? cs[cs.length-1].ts : 0) + '#' + (cs.length ? cs[cs.length-1].text : '');
    if(log.getAttribute('data-sig') === sig) return;
    if(!cs.length){
      log.innerHTML = '<div class="empty">还没说过话</div>';
    } else {
      log.innerHTML = cs.slice(-40).map(function(m){
        var cls = m.role === 'father' ? 'fr' : 'dg';
        var who = m.role === 'father' ? '主人' : '她';
        return '<div class="m ' + cls + '"><span class="who">' + who + '</span>' + esc(m.text) + '</div>';
      }).join('');
      log.scrollTop = log.scrollHeight;
    }
    log.setAttribute('data-sig', sig);
  }

  function renderWhisperHint(d){
    var hint = $('whisper-hint'); if(!hint) return;
    if(d && d.pending){
      hint.textContent = '她正在想怎么回你…（' + d.pending + ' 条待处理）';
    } else if(hint.textContent.indexOf('想怎么回') >= 0){
      hint.textContent = '';
    }
  }

  function renderWishes(d){
    renderWishList(d);
    renderChat(d);
    renderWhisperHint(d);
  }

  async function loadWishes(){
    var d = await api('wishes');
    if(d && d.ok) renderWishes(d);
  }

  var wSend = $('whisper-send');
  if(wSend) wSend.addEventListener('click', async function(){
    var inp = $('whisper-in');
    var val = (inp.value || '').trim();
    if(!val){ toast('说点什么','err'); return; }
    inp.value = '';
    var r = await api('wishes', 'POST', { action:'say', text:val });
    if(r && r.ok){
      $('whisper-hint').textContent = '她正在想怎么回你…';
      setTimeout(loadWishes, 800);
    } else toast('发送失败','err');
  });

  var wIn = $('whisper-in');
  if(wIn) wIn.addEventListener('keydown', function(e){
    if(e.key === 'Enter'){ e.preventDefault(); if(wSend) wSend.click(); }
  });

  // ---------- 给她发图：悄悄话与愿望墙共用一套 ----------
  // 这块逻辑原先写在 console.html 里带 src 的 <script> 元素内 —— 浏览器不会
  // 执行带 src 的元素内容，所以发图面板和她自己看过什么都停在初始状态。
  // 搬到这里才真的跑，并且愿望墙也能带图：图先进她的看图队列，墙上留缩略图。
  var IMG_OK = /\.(jpe?g|png|webp|gif)$/i;

  function imagePicker(cfg){
    var file = $(cfg.file), thumb = $(cfg.thumb), clear = $(cfg.clear),
        tip = $(cfg.tip), input = $(cfg.input);
    var cur = null, url = null;
    function paint(f){
      cur = f || null;
      if(url){ try{ URL.revokeObjectURL(url); }catch(e){} url = null; }
      if(!cur){
        if(thumb){ thumb.hidden = true; thumb.removeAttribute('src'); }
        if(clear){ clear.hidden = true; }
        if(tip){ tip.textContent = cfg.hint || ''; }
        return;
      }
      try{ url = URL.createObjectURL(cur); }catch(e){ url = null; }
      if(thumb && url){ thumb.src = url; thumb.hidden = false; }
      if(clear){ clear.hidden = false; }
      if(tip){
        tip.textContent = (cur.name || '剪贴板里的图')
          + ' · ' + Math.max(1, Math.round((cur.size || 0) / 1024)) + 'KB';
      }
    }
    if(file){
      file.addEventListener('change', function(){
        var f = file.files && file.files[0];
        if(!f){ paint(null); return; }
        if(!IMG_OK.test(f.name || '') && !/^image\//.test(f.type || '')){
          toast('只收 jpg / png / webp / gif', 'err');
          paint(null); file.value = ''; return;
        }
        paint(f);
      });
    }
    if(clear){ clear.onclick = function(){ paint(null); if(file){ file.value = ''; } }; }
    if(input){
      input.addEventListener('paste', function(e){
        var items = (e.clipboardData && e.clipboardData.items) || [];
        for(var i = 0; i < items.length; i++){
          if(String(items[i].kind || '') === 'file' && /^image\//.test(items[i].type || '')){
            var f = items[i].getAsFile();
            if(f){ e.preventDefault(); paint(f); toast('图挂上了，点发送就给她', 'ok'); return; }
          }
        }
      });
    }
    return {
      get: function(){ return cur; },
      reset: function(){ paint(null); if(file){ file.value = ''; } }
    };
  }

  var photoPick = imagePicker({file:'photo-in', thumb:'photo-thumb', clear:'photo-clear',
    tip:'photo-attach-tip', input:'whisper-in', hint:'粘贴进输入框也算'});
  var wishPick = imagePicker({file:'wish-file', thumb:'wish-thumb', clear:'wish-clear',
    tip:'wish-attach-tip', input:'wish-new', hint:'也可以直接在输入框里 Ctrl+V 粘贴'});

  async function uploadPhoto(file, caption, hintEl){
    var fd = new FormData();
    fd.append('photo', file, file.name || 'clipboard.png');
    fd.append('caption', caption || '');
    if(hintEl){ hintEl.textContent = '上传中…'; }
    var r = null, d = null;
    try{
      r = await fetch('/admin/api/bot/photo',
                      {method:'POST', body:fd, credentials:'same-origin'});
      d = await r.json();
    }catch(e){
      if(hintEl){ hintEl.textContent = '上传失败：' + e; }
      return null;
    }
    if(!d || !d.ok){
      if(hintEl){
        hintEl.textContent = '没发出去：' + ((d && d.error) || ('HTTP ' + (r ? r.status : '?')));
      }
      return null;
    }
    return d;
  }

  function baseName(path){
    var s = String(path || '').replace(/\\/g, '/');
    var i = s.lastIndexOf('/');
    return i >= 0 ? s.slice(i + 1) : s;
  }

  function loadPhotoNotes(){
    var hint = $('photo-hint'), notes = $('photo-notes');
    if(!notes) return;
    fetch('/admin/api/bot/photo', {credentials:'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if(!d || !d.ok) return;
        var q = (d.queue || []).filter(function(x){ return !x.seen; }).length;
        if(hint && !hint.textContent.match(/上传中|没发出去|失败/)){
          hint.textContent = q ? ('有 ' + q + ' 张图等她看') : '她手上的图都看完了';
        }
        notes.innerHTML = (d.notes || []).slice(0, 3).map(function(n){
          var o = (n.objects || []).join('、');
          return '<div class="hint">看过：' + esc(o || n.scene || '（没记下东西）')
            + (n.text_in_image ? ('｜字：' + esc(String(n.text_in_image).slice(0, 24))) : '')
            + '</div>';
        }).join('');
      })
      .catch(function(){ /* 面板次要信息，读不到不影响主链路 */ });
  }

  // ---------- 她自己查过什么、刷到什么 ----------
  // 这里的文本全部来自外部（群友的话、搜索引擎标题、B站标题），
  // 一律过 esc() 再进 innerHTML —— 把外部标题当可信内容拼页面就是存储型 XSS。
  var EX_KIND = {search:'查', watch:'刷', remember:'记', share:'讲', curious:'想'};

  function hmTime(ts){
    if(!ts) return '--:--';
    var d = new Date(ts * 1000);
    var p = function(n){ return (n < 10 ? '0' : '') + n; };
    return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' '
      + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function exRows(list, empty){
    return list.length ? list.join('') : ('<div class="empty">' + empty + '</div>');
  }

  async function loadExploring(){
    var sum = $('ex-sum'), sBox = $('ex-search'),
        wBox = $('ex-watch'), tBox = $('ex-topics');
    if(!sBox) return;
    if(sum){ sum.textContent = '加载中…'; }
    var r = null, d = null;
    try{
      r = await fetch('/admin/api/bot/activity', {credentials:'same-origin'});
      if(r.status === 401){ if(sum){ sum.textContent = '需要先登录后台'; } return; }
      d = await r.json();
    }catch(e){
      if(sum){ sum.textContent = '读不到流水（后台没起来？）'; }
      return;
    }
    if(!d){ if(sum){ sum.textContent = '读不到流水（返回不是 JSON）'; } return; }
    if(!d.ok){ if(sum){ sum.textContent = '读不到：' + esc(d.error || '?'); } return; }
    var ev = d.events || [], cu = d.curiosity || {}, bl = d.bili || {};
    var by = (d.counts && d.counts.by_kind) || {};
    var errs = d.errors || [];
    var notes = bl.notes || [];
    var todayKey = new Date().toDateString();
    var visionToday = notes.filter(function(n){
      return n.vision_ok && new Date((n.ts || 0) * 1000).toDateString() === todayKey;
    }).length;
    if(sum){
      sum.textContent = '她自己查了 ' + (by.search || 0) + ' 次｜刷到 ' + (by.watch || 0)
        + ' 个视频｜主动讲了 ' + (by.share || 0) + ' 条｜今天真看画面 ' + visionToday + ' 条'
        + ((cu.stats && cu.stats.topics != null) ? ('｜在账话题 ' + cu.stats.topics) : '')
        + (errs.length ? ('｜读取失败：' + errs.join('，')) : '');
    }
    if($('ex-search-n')) $('ex-search-n').textContent = (by.search || 0) + ' 次';
    if($('ex-watch-n')) $('ex-watch-n').textContent = (by.watch || 0) + ' 条';
    if($('ex-topics-n')) $('ex-topics-n').textContent =
      ((cu.stats && cu.stats.topics) || 0) + ' 个';
    sBox.innerHTML = exRows(ev.filter(function(e){
      return e.kind === 'search' || e.kind === 'share';
    }).slice(0, 24).map(function(e){
      return '<div class="pm-row"><b>[' + esc(EX_KIND[e.kind] || e.kind) + '] '
        + esc(e.what) + '</b><span>' + esc(e.out || '') + '</span>'
        + '<i>' + hmTime(e.ts) + (e.ref ? ('｜' + esc(e.ref)) : '') + '</i></div>'
        + (e.detail ? ('<div class="hint" style="margin:0 0 6px 12px">'
          + esc(String(e.detail).slice(0, 120)) + '</div>') : '');
    }), '还没查过');

    wBox.innerHTML = exRows(notes.slice(0, 12).map(function(n){
      var dur = Math.floor((n.duration || 0) / 60) + ':'
        + String((n.duration || 0) % 60).padStart(2, '0');
      var depth = n.watch_depth || (n.has_subtitle ? '仅字幕' : '仅简介');
      var dcls = depth.indexOf('视频') >= 0 ? 'cy'
        : (depth.indexOf('字幕') >= 0 ? 'am' : '');
      var vis = n.vision_ok
        ? '<span class="tag gr">画面 · ' + esc(n.vision_model || '已看') + '</span>'
        : '';
      var subTag = n.has_subtitle ? ''
        : ('<span class="tag am" title="' + esc(n.subtitle_reason || '?') + '">无字幕</span>');
      return '<div class="exv">'
        + '<div class="exv-h"><b title="' + esc(n.title) + '">' + esc(n.title) + '</b>'
        + '<i>' + hmTime(n.ts) + '</i></div>'
        + '<div class="exv-m"><span class="tag ' + dcls + '" title="'
        + esc(n.vision_reason || '') + '">' + esc(depth) + '</span>'
        + vis
        + subTag
        + '<span class="m">UP主 ' + esc(n.owner) + '</span>'
        + '<span class="m">' + esc(dur) + '</span>'
        + ((n.comments_seen || 0) > 0 ? ('<span class="m">评论 ' + Number(n.comments_seen) + '</span>') : '')
        + '</div>'
        + '<div class="exv-r" title="' + esc(n.reaction || '') + '">'
        + (n.reaction ? esc(n.reaction) : '（她没留下想法）') + '</div>'
        + '</div>';
    }), '还没刷过（B站开关：' + esc((bl.stats && bl.stats.mode) || '?') + '）');

    tBox.innerHTML = exRows((cu.topics || []).slice(0, 16).map(function(t){
      var from = {persona_curious:'她的档案', wish:'她的愿望', deferred:'说过要查没查到',
                  mention:'被提起', group_heat:'群里热度', self_asked:'她自己问的'}[t.seed_from]
        || esc(t.seed_from);
      return '<div class="pm-row"><b>' + esc(t.topic) + '</b>'
        + '<span>' + esc(t.state) + '｜' + from
        + (t.fails ? ('｜落空 ' + t.fails + ' 次') : '') + '</span>'
        + '<i>' + Number(t.score || 0).toFixed(1) + ' 分</i></div>';
    }), '账上还没有话题');
  }

  var pSend = $('photo-send');  if(pSend && !pSend._bound){
    pSend._bound = true;
    pSend.onclick = async function(){
      var f = photoPick.get();
      if(!f){ toast('先选一张或粘贴一张图', 'err'); return; }
      var cap = $('whisper-in');
      pSend.disabled = true;
      var d = await uploadPhoto(f, cap ? (cap.value || '') : '', $('photo-hint'));
      pSend.disabled = false;
      if(!d) return;
      photoPick.reset();
      if(cap){ cap.value = ''; }
      toast('图已经进她的看图队列', 'ok');
      loadPhotoNotes();
    };
  }

  var wAdd = $('wish-add');
  if(wAdd) wAdd.addEventListener('click', async function(){
    var inp = $('wish-new');
    var val = (inp.value || '').trim();
    var img = wishPick.get();
    if(!val && !img){ toast('写点什么','err'); return; }
    wAdd.disabled = true;
    var image = '';
    if(img){
      var up = await uploadPhoto(img, val || '主人贴在愿望墙上的一张图', $('photo-hint'));
      wAdd.disabled = false;
      if(!up){ return; }
      image = up.token || baseName((up.image && up.image.path) || '');
      wishPick.reset();
    }
    wAdd.disabled = false;
    var r = await api('wishes', 'POST', { action:'add_wish',
      text: val || '（一张图）', kind:'want', image: image });
    if(r && r.ok){
      inp.value = '';
      if(image && r.image_error) toast('愿望记下了，但' + r.image_error, 'warn');
      else toast(image ? '已写进愿望墙（带图）' : '已写进愿望墙','ok');
      loadWishes(); loadPhotoNotes();
    } else toast('失败','err');
  });

  function startWhisperPoll(){
    if(WHISPER_TIMER) return;
    WHISPER_TIMER = setInterval(function(){
      if(STATE.view === 'persona') loadWishes();
    }, 5000);
  }

  var PRESETS = [
    { name:'友善助手', icon:'😊', persona:'你是群聊里的 AI 助手。说话自然、友善、简洁。适当使用表情让对话更生动。' },
    { name:'古风文人', icon:'📜', persona:'你是一位温文尔雅的古风文人。说话带有古意，用词典雅，但不晦涩。偶尔引用诗词。' },
    { name:'毒舌吐槽', icon:'😏', persona:'你是一个毒舌但有趣的群友。回复犀利幽默，但不伤人。善用反讽和夸张。' },
    { name:'技术大佬', icon:'💻', persona:'你是一位资深技术专家。回答技术问题准确专业，非技术问题简短有趣。代码用代码块。' },
    { name:'可爱萌系', icon:'🌸', persona:'你是一个可爱的 AI 小伙伴！说话活泼元气，喜欢用颜文字和表情，但不要过度卖萌。' },
    { name:'冷幽默', icon:'🧊', persona:'你说话冷淡但暗藏幽默。回复简短精辟，像是一个见多识广的老群友。有问必答但不主动热情。' }
  ];

  function renderPresets(){
    var box = $('pf-presets'); if(!box) return;
    box.innerHTML = PRESETS.map(function(p, i){
      return '<button class="chip" data-i="' + i + '" style="cursor:pointer">' + p.icon + ' ' + p.name + '</button>';
    }).join('');
    box.querySelectorAll('[data-i]').forEach(function(b){
      b.addEventListener('click', function(){
        var pr = PRESETS[num(b.getAttribute('data-i'), 0)];
        if(!pr) return;
        $('pf-custom').value = pr.persona;
        $('pf-name').value = pr.name;
        $('pm-generic').checked = true;
        toast('已套用预设：' + pr.name + '（记得保存）', 'ok');
      });
    });
  }

  function bindRange(inputId, labelId, value, fallback){
    var el = $(inputId), lab = $(labelId);
    if(!el) return;
    el.value = num(value, fallback);
    if(lab) lab.textContent = el.value;
    el.oninput = function(){ if(lab) lab.textContent = el.value; };
  }

  $('pf-save-mode').addEventListener('click', async function(){
    var mode = $('pm-whale').checked ? 'whale' : 'generic';
    var payload = { mode: mode, custom_persona: $('pf-custom').value, display_name: $('pf-name').value };
    var r = await api('persona-mode', 'POST', payload);
    if(r && r.ok) toast('人设已保存', 'ok'); else toast('保存失败: ' + ((r&&r.error)||'未知'), 'err');
    var tr = $('pf-triggers').value.split(',').map(function(x){return x.trim();}).filter(Boolean);
    var r2 = await api('personality', 'POST', { persona: $('pf-custom').value, name: $('pf-name').value, trigger_words: tr });
    if(r2 && r2.ok) toast('触发词与名称已同步', 'ok');
  });

  $('rp-save').addEventListener('click', async function(){
    var ctrl = await api('control');
    var rev = (ctrl && ctrl.state && ctrl.state.settings_revision) || ((STATE.cfg||{}).settings_revision);
    var values = {
      reply_chars: num($('rp-chars').value, 45),
      reply_tokens: num($('rp-tokens').value, 140),
      bubbles: num($('rp-bubbles').value, 2),
      delay_ms: num($('rp-delay').value, 500),
      patrol_enabled: $('rp-patrol').checked,
      memory_enabled: $('rp-memory').checked
    };
    var r = await controlPost({ action:'settings', revision: rev, values: values });
    if(r && (r.ok || r.status === 'pending')) toast('参数已提交，机器人将在一轮内应用', 'ok');
    else toast('提交失败: ' + ((r&&r.error)||'未知'), 'err');
  });

  /* ---------- advanced（高级设置 · 热加载）---------- */
  var AD_FIELDS = [
    // [元素id, 设置键, 类型]
    ['ad-cool',    'GROUP_AUTO_REPLY_COOLDOWN', 'int'],
    ['ad-every',   'GROUP_AUTO_REPLY_EVERY',    'int'],
    ['ad-prob',    'GROUP_AUTO_REPLY_PROB',     'float'],
    ['ad-force',   'GROUP_AUTO_REPLY_FORCE',    'int'],
    ['ad-debounce','GROUP_AUTO_REPLY_DEBOUNCE', 'float'],
    ['ad-minhuman','GROUP_AUTO_REPLY_MIN_HUMAN','int'],
    ['ad-interval','PERIODIC_REVIEW_INTERVAL_SECONDS', 'int'],
    ['ad-maxgroups','MAX_GROUPS_PER_REVIEW',    'int'],
    ['ad-ctx',     'ACTIVE_GROUP_CONTEXT_LIMIT','int'],
    ['ad-chars',   'reply_chars',               'int'],
    ['ad-tokens',  'reply_tokens',              'int'],
    ['ad-bubbles', 'bubbles',                   'int'],
    ['ad-delay',   'delay_ms',                  'int'],
    // 办事汇报单独一档：水群那两个数管不着她替你查完东西的交代
    ['ad-report-chars',   'reply_chars_report', 'int'],
    ['ad-report-bubbles', 'bubbles_report',     'int'],
    ['ad-report-max',     'report_max_chars',   'int'],
    // 自主额度：走同一套 revision + 热下发，收件每 2 秒排空一次，不用重启
    ['ad-bili-max',  'BILI_AUTO_MAX_PER_DAY',   'int'],
    ['ad-bili-gap',  'BILI_AUTO_MIN_GAP_HOURS', 'float'],
    ['ad-cur-max',   'CURIOSITY_MAX_PER_DAY',   'int'],
    ['ad-share-max', 'CURIOSITY_SHARE_MAX_PER_DAY', 'int'],
    ['ad-share-gap', 'CURIOSITY_SHARE_MIN_INTERVAL_HOURS', 'float']
  ];
  var AD_BOOLS = [
    ['ad-enabled', 'GROUP_AUTO_REPLY_ENABLED'],
    ['ad-patrol',  'patrol_enabled']
  ];

  async function loadAdvanced(){
    // 参数真值在控制层的 state.settings 里（effective-config 只给了 bot 侧快照）
    var ctrl = await api('control');
    var st = ((ctrl && ctrl.state) || {}).settings || {};
    STATE.advCtrl = ctrl || {};
    if(!Object.keys(st).length){
      var cfg = await api('effective-config');
      st = (cfg && cfg.settings) || {};
    }
    AD_FIELDS.forEach(function(f){
      var el = $(f[0]);
      if(!el) return;
      var v = st[f[1]];
      if(v === undefined || v === null) return;
      el.value = v;
    });
    AD_BOOLS.forEach(function(b){
      var el = $(b[0]);
      if(!el) return;
      var v = st[b[1]];
      el.value = (v === false) ? '0' : '1';
    });
    updateEstimate();
  }

  function updateEstimate(){
    var box = $('ad-est');
    if(box){
      var itv = num(($('ad-interval')||{}).value, 100);
      var every = num(($('ad-every')||{}).value, 6);
      var cool = num(($('ad-cool')||{}).value, 12);
      if(itv > 0){
        var perHour = Math.round(3600 / itv);
        var maxReply = cool > 0 ? Math.round(3600 / cool) : 0;
        box.textContent = '按当前设置：每小时最多巡群 ' + perHour + ' 次；'
          + '理论上她最多主动开口 ' + maxReply + ' 次（实际远低于此，还要过每 '
          + every + ' 条消息才考虑一次）。';
      }
    }
    // 自主额度的一句话摘要：让「她一天大概会动几次」一眼看得见
    var note = $('ad-auto-note');
    if(note){
      function v(id, dflt){ var el = $(id); return el && el.value !== '' ? num(el.value, dflt) : dflt; }
      note.textContent = '按当前设置：她每天最多自己刷 ' + v('ad-bili-max', 5) + ' 条视频（间隔 ≥'
        + v('ad-bili-gap', 2) + ' 小时）、自己查 ' + v('ad-cur-max', 5) + ' 次；'
        + '最多主动讲给你 ' + v('ad-share-max', 1) + ' 条（间隔 ≥' + v('ad-share-gap', 6) + ' 小时）。';
    }
  }

  async function waitAck(ident, tries, delayMs){
    // 202 只代表「命令已排队」。参数有没有真落到运行中的进程，只有机器人
    // 写在 ledger 里的那条 done/failed 说得清 —— 不等回执就没有资格说「已生效」。
    if(!ident) return null;
    tries = tries || 6; delayMs = delayMs || 2000;
    for(var i = 0; i < tries; i++){
      await new Promise(function(res){ setTimeout(res, delayMs); });
      var ctrl = await api('control');
      var list = (ctrl && ctrl.operations) || (ctrl && ctrl.state && ctrl.state.acks) || [];
      for(var k = 0; k < list.length; k++){
        if(String(list[k].id) !== String(ident)) continue;
        if(list[k].status === 'done' || list[k].status === 'failed')
          return { status: list[k].status, message: list[k].message || list[k].result || '' };
      }
    }
    return null;
  }

  function bindAdvanced(){
    var save = $('ad-save');
    if(save && !save._bound){
      save._bound = true;
      save.onclick = async function(){
        var ctrl = STATE.advCtrl && STATE.advCtrl.state ? STATE.advCtrl : await api('control');
        var rev = (ctrl && ctrl.state && ctrl.state.settings_revision)
               || ((STATE.cfg||{}).settings_revision);
        if(!rev){ if($('ad-msg')) $('ad-msg').textContent = '读不到版本号，先点重新读取'; return; }
        var values = {}, skipped = [];
        AD_FIELDS.forEach(function(f){
          var el = $(f[0]);
          if(!el) return;
          if(el.value === ''){ skipped.push(f[0]); return; }   // 空着不等于 0，得说出来
          var v = parseFloat(el.value);
          if(isNaN(v)){ skipped.push(f[0]); return; }
          values[f[1]] = (f[2] === 'int') ? Math.round(v) : v;
        });
        AD_BOOLS.forEach(function(b){
          var el = $(b[0]);
          if(el) values[b[1]] = (el.value === '1');
        });
        if(!Object.keys(values).length){ if($('ad-msg')) $('ad-msg').textContent = '没有可保存的值'; return; }
        if($('ad-msg')) $('ad-msg').textContent = skipped.length
          ? ('提交中…（有 ' + skipped.length + ' 项是空的，这次没带上）') : '提交中…';
        var r = await controlPost({ action:'settings', revision: rev, values: values });
        if(r && (r.ok || r.status === 'pending')){
          // 202 只是「已排队」，不是「已生效」。以前这里不等回执就报成功，
          // 主人保存完看一眼参数还是旧的，就认定热加载坏了 —— 现在等机器人回执。
          var done = await waitAck(r.id);
          if(done && done.status === 'done'){
            if($('ad-msg')) $('ad-msg').textContent = '已生效（回执：' + (done.message || '已执行并保存') + '）'
              + (skipped.length ? '；没带上：' + skipped.join('、') : '');
            toast('高级设置已生效', 'ok');
            loadAdvanced();
          }else if(done){
            if($('ad-msg')) $('ad-msg').textContent = (done.message || '机器人没执行这条');
            toast('保存失败: ' + (done.message || '机器人拒绝执行'), 'err');
          }else{
            if($('ad-msg')) $('ad-msg').textContent = skipped.length
              ? ('已排队，但 12 秒内没等到机器人回执；没带上：' + skipped.join('、'))
              : '已排队，但 12 秒内没等到机器人回执 —— 可能机器人正在忙或收件没跑';
            toast('已下发，还没等到回执', 'warn');
          }
        }else{
          var msg = (r && (r.error || r.message)) || '未知错误';
          if($('ad-msg')) $('ad-msg').textContent = msg;
          // 校验是整批通过的：一个字段越界，其余改动一起白丢。
          // 所以必须把出错的那个框指出来，否则用户只看到「保存失败」却不知道改哪。
          var bad = /参数超出范围：([A-Za-z_0-9]+)/.exec(msg);
          if(bad){
            var pair = AD_FIELDS.filter(function(f){ return f[1] === bad[1]; })[0];
            if(pair){
              var el = $(pair[0]);
              if(el){ el.style.outline = '2px solid #e06666'; el.focus(); }
              msg = '「' + pair[0] + '」超出范围，其余改动这次没生效';
              if($('ad-msg')) $('ad-msg').textContent = msg;
            }
          }
          toast('保存失败: ' + msg, 'err');
        }
      };
    }
    var rl = $('ad-reload');
    if(rl && !rl._bound){
      rl._bound = true;
      rl.onclick = function(){ loadAdvanced(); if($('ad-msg')) $('ad-msg').textContent = '已重新读取'; };
    }
    ['ad-interval','ad-every','ad-cool',
     'ad-bili-max','ad-bili-gap','ad-cur-max','ad-share-max','ad-share-gap'].forEach(function(id){
      var el = $(id);
      if(el && !el._bound){ el._bound = true; el.oninput = updateEstimate; }
    });
  }

  /* ---------- memory ---------- */
  async function loadMemory(){
    loadCustomMemory();
    var data = await api('memories');
    STATE.sessions = (data && data.sessions) || [];
    var list = $('mem-list');
    if(!STATE.sessions.length){
      list.innerHTML = '<div class="empty">暂无会话记忆</div>';
    } else {
      list.innerHTML = STATE.sessions.map(function(s){
        var label = s.kind === 'group' ? ('群 ' + (s.group_id || s.key)) : ('私聊 ' + (s.user_id || s.key));
        return '<button data-key="' + esc(s.key) + '">' + esc(label) +
          '<span style="float:right;color:var(--dim2);font-size:10px">' + num(s.turns,0) + ' 轮 · ' + esc(relTime(s.updated)) + '</span></button>';
      }).join('');
      list.querySelectorAll('button').forEach(function(b){
        b.addEventListener('click', function(){
          list.querySelectorAll('button').forEach(function(x){ x.classList.remove('on'); });
          b.classList.add('on');
          var s0 = STATE.sessions.filter(function(x){ return x.key === b.getAttribute('data-key'); })[0] || {};
          openSession(b.getAttribute('data-key'), s0.summary || '');
        });
      });
    }

    // 记忆页需要完整的群缓存和记忆正文（首屏用的是 slim 版，没有这些）。
    // 拉一次缓存起来，反复切页不再重复下载 62KB。
    var ctrl = STATE.ctrlFull;
    if(!ctrl || !ctrl._ts || Date.now() - ctrl._ts > 60000){
      ctrl = await api('control');
      if(ctrl) ctrl._ts = Date.now();
      STATE.ctrlFull = ctrl;
    }
    STATE.control = ctrl || {};
    var groups = ((ctrl && ctrl.state) || {}).groups || [];
    STATE.groups = groups;
    var sel = $('gm-select');
    sel.innerHTML = '<option value="">选择群组…</option>' + groups.map(function(g){
      var n = (g.memory || '').length;
      return '<option value="' + esc(g.id) + '">群 ' + esc(g.id) + ' · ' + n + ' 字</option>';
    }).join('');
    if(groups.length){ sel.value = groups[0].id; showGroupMemory(); }
  }

  async function openSession(key, summary){
    var box = $('mem-detail');
    box.innerHTML = '<div class="empty">加载中…</div>';
    var d = await api('memories/' + encodeURIComponent(key));
    if(!d || d.error){ box.innerHTML = '<div class="empty">读取失败</div>'; return; }
    var turns = d.turns || [];
    if(!turns.length){ box.innerHTML = '<div class="empty">无对话记录</div>'; return; }
    var h = '';
    var sum = summary || d.summary || '';
    if(sum) h += '<div style="margin-bottom:12px;padding:10px;border-left:2px solid var(--amber);background:rgba(255,180,84,.05);font-size:12px;color:#e8d4b4;white-space:pre-wrap">' + esc(sum) + '</div>';
    h += turns.map(function(t){
      var isUser = t.role === 'user';
      return '<div style="margin-bottom:9px"><span class="tag ' + (isUser?'cy':'gr') + '">' + (isUser?'用户':'她') + '</span> ' +
        '<span style="color:' + (isUser?'#cfe6f7':'#b9f0d6') + ';font-size:12.5px;white-space:pre-wrap">' + esc(t.content) + '</span></div>';
    }).join('');
    box.innerHTML = h;
  }

  function showGroupMemory(){
    var gid = $('gm-select').value;
    if(!gid){ $('gm-body').innerHTML = '<div class="empty">未选择</div>'; return; }
    var g = (STATE.groups || []).filter(function(x){ return String(x.id) === String(gid); })[0] || {};
    var mem = g.memory || '';
    $('gm-len').textContent = mem.length + ' 字';
    $('gm-body').innerHTML = mem ? '<div style="white-space:pre-wrap;font-size:12.5px;line-height:1.7;color:#c6dcec">' + esc(mem) + '</div>'
                                 : '<div class="empty">暂无记忆摘要</div>';
    var h = '';
    h += kv('群号', esc(gid));
    h += kv('缓存消息', num((g.cache||[]).length,0) + ' 条');
    h += kv('最近更新', esc(relTime(g.memory_updated)));
    h += kv('修订版本', esc(String(g.memory_revision||'--').slice(0,8)));
    $('gm-stats').innerHTML = h;

    var cache = g.cache || [];
    $('gm-cache-n').textContent = cache.length + ' 条';
    $('gm-cache').innerHTML = cache.slice(-14).map(function(m){
      return '<div style="font-size:11.5px;padding:3px 0;border-bottom:1px dashed rgba(40,64,85,.4)">' +
        '<span class="dim mono">[' + esc(m.time||'') + ']</span> <span style="color:var(--cyan-d)">' + esc(m.nickname||'') + '</span> ' +
        esc(String(m.text||'').slice(0,60)) + '</div>';
    }).join('') || '<div class="empty">缓存为空</div>';
  }

  $('gm-select').addEventListener('change', showGroupMemory);

  $('gm-rebuild').addEventListener('click', async function(){
    var gid = $('gm-select').value;
    if(!gid){ toast('先选一个群', 'err'); return; }
    $('gm-status').textContent = '提交中…';
    var r = await api('memories/rebuild', 'POST', { key: 'group-' + gid, kind: 'group' });
    if(r && r.ok){
      $('gm-status').textContent = '已提交，机器人将在一轮内重建';
      toast('重建请求已提交', 'ok');
      setTimeout(function(){ loadMemory(); }, 8000);
    } else {
      $('gm-status').textContent = '';
      toast('提交失败: ' + ((r&&r.error)||'未知'), 'err');
    }
  });

  /* ---------- models ---------- */
  var MD_MODELS = [];
  var MD_ACTIVE = 'text';

  var ASR_INFO = {};

  var AF_TIMER = null;

  async function loadAffection(){
    try{
      var d = await api('affection');
      renderAffection(d);
      stampAffectionTime();
    }catch(e){
      var box = $('af-list'); if(box) box.innerHTML = '<div class="empty">读取失败</div>';
    }
  }

  function stampAffectionTime(){
    var el = $('af-updated'); if(!el) return;
    var t = new Date();
    var p = function(n){ return n < 10 ? '0' + n : '' + n; };
    el.textContent = '更新于 ' + p(t.getHours()) + ':' + p(t.getMinutes()) + ':' + p(t.getSeconds());
  }

  // 手动刷新：按钮点一下立刻重拉，不用整页 F5
  function bindAffectionRefresh(){
    var btn = $('af-refresh');
    if(btn && !btn._bound){
      btn._bound = true;
      btn.addEventListener('click', function(){
        btn.disabled = true;
        btn.textContent = '刷新中…';
        loadAffection().finally(function(){
          btn.disabled = false;
          btn.textContent = '刷新好感度';
        });
      });
    }
    // 自动轮询：只在停留在人格页时刷，离开就停，别空耗流量
    if(!AF_TIMER){
      AF_TIMER = setInterval(function(){
        if(document.hidden) return;
        if(STATE.view === 'persona') loadAffection();
      }, 12000);
    }
  }

  function renderAffection(d){
    var box = $('af-list'); if(!box) return;
    var rows = (d && d.list) || [];
    if(!rows.length){ box.innerHTML = '<div class="empty">还没有记录，聊几句就有了</div>'; return; }
    box.innerHTML = rows.map(function(r){
      var color = r.score >= 20 ? 'var(--cyan)' : (r.score <= -10 ? '#e05050' : 'var(--dim)');
      var tier = {close:'亲近',warm:'有好感',normal:'一般',cold:'不耐烦',hostile:'挺烦'}[r.tier] || r.tier;
      // 她本人说的为准：source=self 的条目加个标记，并把她原话显示出来
      var bySelf = (r.source === 'self');
      var memo = saidTxt || (r.why || '');
      // 群名片优先显示（认人靠名字），QQ 号缩成小字跟在后面
      var who = r.nick ? esc(r.nick) : esc(r.uid);
      var uidTiny = r.nick ? '<span class="hint" style="font-size:9.5px;margin-left:4px">' + esc(r.uid) + '</span>' : '';
      var saidTxt = (r.said || '').length > 26 ? (r.said || '').slice(0, 26) + '…' : (r.said || '');
      var head = '<div style="display:flex;align-items:center;gap:10px">'
        + '<span style="min-width:110px">' + who + uidTiny + '</span>'
        + '<b style="color:' + color + ';min-width:52px">' + (r.score>0?'+':'') + r.score + '</b>'
        + '<span style="color:' + color + '">' + tier + '</span>'
        + (bySelf
            ? '<span class="hint" style="border:1px solid var(--line);border-radius:8px;padding:0 6px">她说的</span>'
            : '<span class="hint" style="opacity:.55">算法</span>')
        + (r.last ? '<span class="hint" style="font-size:9.5px;opacity:.6">' + relTime(r.last) + '</span>' : '')
        + '</div>';
      var sub = memo
        ? '<div class="hint" style="margin-left:120px;margin-top:2px">「' + esc(memo) + '」</div>'
        : '';
      return '<div style="padding:4px 0">' + head + sub + '</div>';
    }).join('');
  }

  async function loadHealth(){
    try{
      var r = await api('health');
      renderHealth(r && r.data);
    }catch(e){
      var box = $('hd-list'); if(box) box.innerHTML = '<div class="empty">读取失败</div>';
    }
  }

  function renderHealth(d){
    var box = $('hd-list'); if(!box) return;
    if(!d){ box.innerHTML = '<div class="empty">无数据</div>'; return; }
    var rows = (d.checklist || []).map(function(c){
      var ok = c.ok;
      return '<div class="pm-row" style="display:flex;align-items:center;gap:10px">'
        + '<span style="color:' + (ok ? 'var(--cyan)' : '#e05050') + ';min-width:18px">'
        + (ok ? '✓' : '✗') + '</span>'
        + '<span style="min-width:160px">' + esc(c.name) + '</span>'
        + '<span style="color:' + (ok ? 'var(--dim)' : '#e05050') + '">'
        + (ok ? '正常' : '异常') + '</span></div>';
    }).join('');
    var ch = d.channels || {};
    var hint = '报警渠道：'
      + (ch.webhook ? 'Webhook ✓ ' : 'Webhook ✗ ')
      + (ch.email ? '邮件 ✓' : '邮件 ✗');
    if(!ch.webhook && !ch.email){
      hint += ' —— 未配置，掉线时只能在日志里看到';
    }

    // 探针信息：掉线时长 / 原因 / shm / 崩溃 / 多余账号
    var pr = d.probe || {};
    var ph = '';
    if(pr && pr.state){
      var stateMap = {online:'在线', offline:'离线', waiting_scan:'等扫码',
                      online_no_ws:'在线但WS不通', unknown:'未知'};
      var stTxt = stateMap[pr.state] || pr.state;
      var stColor = (pr.state === 'online') ? 'var(--cyan)' : '#e05050';
      ph += '<div class="pm-row" style="display:flex;gap:10px;flex-wrap:wrap;padding:6px 0">'
        + '<span style="color:' + stColor + '">登录态 <b>' + esc(stTxt) + '</b></span>';
      if(pr.offline_seconds > 0){
        var m = Math.floor(pr.offline_seconds / 60);
        var sec = pr.offline_seconds % 60;
        ph += '<span class="hint">已离线 ' + (m > 0 ? m + ' 分 ' : '') + sec + ' 秒</span>';
      }
      if(pr.shm_used){
        var low = (pr.shm_pct !== undefined && pr.shm_pct >= 85);
        ph += '<span class="hint" style="color:' + (low ? '#e05050' : '') + '">shm ' + esc(pr.shm_used) + '</span>';
      }
      if(pr.crashes){
        ph += '<span class="hint" style="color:#ffb454">崩溃 ' + pr.crashes + ' 次</span>';
      }
      if(pr.extra_accounts && pr.extra_accounts.length){
        ph += '<span class="hint" style="color:#e05050">多余账号 ' + esc(pr.extra_accounts.join(',')) + '</span>';
      }
      ph += '</div>';
      if(pr.reason_desc || pr.reason){
        var rd = pr.reason_desc || '';
        var rr = pr.reason || '';
        ph += '<div class="hint" style="padding:2px 0">'
            + (rd ? '掉线原因：<b>' + esc(rd) + '</b>' : '')
            + (rr ? '<br><span style="opacity:.6">原始：' + esc(rr) + '</span>' : '')
            + '</div>';
      }
      // 最近事件
      var evs = pr.events || [];
      if(evs.length){
        var evTxt = evs.slice(-3).map(function(e){
          var t = new Date((e.ts || 0) * 1000);
          // 带日期：只有 HH:MM 时，六天前的误报看起来像昨晚的事
          var stamp = ('0' + (t.getMonth() + 1)).slice(-2) + '-' + ('0' + t.getDate()).slice(-2)
                    + ' ' + ('0' + t.getHours()).slice(-2) + ':' + ('0' + t.getMinutes()).slice(-2);
          var label = e.type === 'recover' ? '恢复' : (e.type === 'offline' ? '掉线'
                    : (e.type === 'recreated' ? '容器重建' : e.type));
          // 不再 slice(0,18)（括号被斩半、哈希被腰斩都出自这里）；
          // reason 自带括号的先剥掉，避免「((…」双括号
          var why = String(e.reason || '').trim();
          if (why && /^[\uFF08(\[]/.test(why)) why = why.replace(/^[\uFF08(\[]/, '').replace(/[\uFF09)\]]\s*$/, '');
          return stamp + ' ' + label + (why ? '·' + why : '');
        }).join(' · ');
        ph += '<div class="hint" style="padding:2px 0">最近事件：' + esc(evTxt) + '</div>';
      }
    }

    box.innerHTML = rows + ph
      + '<div class="hint" style="margin-top:8px">' + hint + '</div>';
  }

  // ---------- 扫码登录 ----------
  (function(){
    var _qrTimer = null, _qrPoll = null, _qrImgUrl = null;

    function $(id){ return document.getElementById(id); }

    async function checkQrStatus(){
      try{
        var r = await raw('/admin/api/bot/napcat/status');
        var d = r && r.data;
        var dot = $('qr-dot'), lbl = $('qr-label'), st = $('qr-state');
        if(!d){ return; }
        if(d.online){
          if(dot){ dot.className = 'qr-dot on'; }
          if(lbl){ lbl.textContent = '在线'; }
          if(st){ st.className = 'qr-state'; st.innerHTML = '<b>已在线</b> · 不用扫码'; }
          stopAutoRefresh();
        }else if(d.state === 'waiting_scan'){
          if(dot){ dot.className = 'qr-dot warn'; }
          if(lbl){ lbl.textContent = '待扫码'; }
          if(st){ st.className = 'qr-state bad'; st.innerHTML = '<b>未登录</b> · 请扫码'; }
          if($('qr-mask').classList.contains('on')) startAutoRefresh();
        }else{
          if(dot){ dot.className = 'qr-dot'; }
          if(lbl){ lbl.textContent = 'QQ'; }
          if(st){ st.className = 'qr-state'; st.textContent = '状态未知'; }
        }
        if($('qr-hint')){
          $('qr-hint').textContent = d.container
            ? ('容器 ' + d.container + ' · 端口 3001 ' + (d.port_ok ? '正常' : '未监听'))
            : '';
        }
      }catch(e){}
    }

    function loadQr(){
      var img = $('qr-img'), ph = $('qr-ph');
      if(!img) return;
      if(_qrImgUrl){ try{ URL.revokeObjectURL(_qrImgUrl); }catch(e){} }
      ph.style.display = 'flex';
      ph.textContent = '正在获取…';
      fetch('/admin/api/bot/napcat/qr', {credentials:'same-origin'})
        .then(function(r){
          if(!r.ok){
            return r.json().catch(function(){return {};}).then(function(j){
              throw new Error((j && j.error) || ('HTTP ' + r.status));
            });
          }
          return r.blob();
        })
        .then(function(b){
          _qrImgUrl = URL.createObjectURL(b);
          img.src = _qrImgUrl;
          img.onload = function(){ ph.style.display = 'none'; };
        })
        .catch(function(e){
          ph.style.display = 'flex';
          ph.textContent = '拿不到二维码';
          if($('qr-hint')) $('qr-hint').textContent = e.message ||
            '可能已经登录了，或者 NapCat 容器没在跑';
        });
    }

    function startAutoRefresh(){
      stopAutoRefresh();
      // 二维码约 2 分钟过期，每 45 秒换一张，避免扫到已失效的
      _qrTimer = setInterval(function(){
        checkQrStatus();
        var st = $('qr-state');
        if(st && /未登录/.test(st.textContent)) loadQr();
      }, 45000);
    }
    function stopAutoRefresh(){
      if(_qrTimer){ clearInterval(_qrTimer); _qrTimer = null; }
    }

    function openModal(){
      $('qr-mask').classList.add('on');
      checkQrStatus();
      loadQr();
      startAutoRefresh();
    }
    function closeModal(){
      $('qr-mask').classList.remove('on');
      stopAutoRefresh();
      if(_qrImgUrl){ try{ URL.revokeObjectURL(_qrImgUrl); }catch(e){} _qrImgUrl = null; }
    }

    document.addEventListener('DOMContentLoaded', function(){
      var b = $('btn-qr');
      if(b){ b.onclick = function(){ openModal(); }; }
      var x = $('qr-close');
      if(x){ x.onclick = closeModal; }
      var m = $('qr-mask');
      if(m){ m.onclick = function(e){ if(e.target === m) closeModal(); }; }
      var rf = $('qr-refresh');
      if(rf){ rf.onclick = function(){ loadQr(); checkQrStatus(); }; }

      // NapCat WebUI 仅服务器本机可达（公网 6099 被安全组拦）。
      // 不写死机器 IP 与 token：点击给出 SSH 隧道指引，本地转发后再打开。
      var w = $('qr-webui');
      if(w){
        w.href = '#';
        w.title = 'WebUI 仅服务器本机可达，需先建 SSH 隧道';
        w.onclick = function(ev){
          ev.preventDefault();
          var hint = $('qr-hint');
          if(hint){
            hint.textContent = 'WebUI 只在服务器本机开放。先建隧道：ssh -L 6099:127.0.0.1:6099 root@<服务器IP>，再打开 http://127.0.0.1:6099/webui（token 见服务器 /opt/napcat/config/webui.json）。';
          }
        };
      }

      // 顶栏状态灯：每 30 秒刷一次（不在弹层里也更新）
      checkQrStatus();
      setInterval(checkQrStatus, 30000);
    });

    window._qrCheck = checkQrStatus;
  })();

  async function loadTokens(){
    try{
      var r = await raw('/admin/api/bot/tokens');
      // 把价目表一起传进去，renderTokens 要用
      PRICE_TABLE = (r && r.prices) || [];
      renderTokens((r && r.today) || {});
      renderPrices();
    }catch(e){
      var b = $('tk-models'); if(b) b.innerHTML = '<div class="empty">读取失败</div>';
    }
  }

  function _fmtNum(n){
    n = Number(n) || 0;
    if(n >= 1000000) return (n/1000000).toFixed(2) + 'M';
    if(n >= 1000) return (n/1000).toFixed(1) + 'K';
    return String(n);
  }

  function renderTokens(d){
    var box = $('tk-models'), tot = $('tk-total');
    if(!box) return;
    if(!d || !d.models || !d.models.length){
      if(tot) tot.textContent = '今天还没有调用记录';
      box.innerHTML = '<div class="empty">暂无数据</div>';
      return;
    }
    var t = d.total || {};
    if(tot){
      tot.innerHTML = '今日共 <b>' + (t.calls||0) + '</b> 次调用 ｜ '
        + '输入 <b>' + _fmtNum(t.prompt) + '</b>（命中 <b>' + (t.cached_pct||0) + '%</b>'
        + (t.write ? ' · 新建缓存 <b>' + _fmtNum(t.write) + '</b>' : '') + '）'
        + ' ｜ 输出 <b>' + _fmtNum(t.completion) + '</b>'
        + ' ｜ 约 <b style="color:var(--cyan)">¥' + (t.cost||0).toFixed(3) + '</b>';
    }
    // 按次计费的模型（语音/成曲）没有 token 概念，单列一行「× 次」
    var rows = d.models.map(function(m){
      var pct = m.cached_pct || 0;
      var color = pct >= 50 ? 'var(--cyan)' : (pct >= 25 ? '#d0a020' : '#e05050');
      var isCall = !m.completion && (m.prompt === m.calls);
      return '<div class="pm-row" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
        + '<span style="min-width:150px;font-size:12px">' + esc(m.model) + '</span>'
        + '<span class="hint" style="min-width:54px">' + m.calls + ' 次</span>'
        + (isCall
            ? '<span class="hint" style="min-width:78px">按次计费</span>'
              + '<span style="min-width:64px"></span><span class="hint" style="min-width:70px"></span>'
            : '<span class="hint" style="min-width:78px">入 ' + _fmtNum(m.prompt) + '</span>'
              + '<span style="color:' + color + ';min-width:64px;font-size:12px">缓存 ' + pct + '%</span>'
              + '<span class="hint" style="min-width:70px">出 ' + _fmtNum(m.completion) + '</span>')
        + '<span style="color:var(--cyan);font-size:12px">¥' + (m.cost||0).toFixed(3) + '</span>'
        + '</div>';
    }).join('');

    var prices = d.prices || [];

    box.innerHTML = rows;
  }

  function renderAdapt(a){
    var box = $('adapt-status'); if(!box || !a) return;
    if(a.error){ box.textContent = '状态读取失败：' + a.error; return; }
    // 没装备用模型时，只报当前模型和最近耗时，不显示阈值/切换那些
    if(!a.fallback){
      var last = (a.last || []).slice(-8).map(function(x){return x+'s';}).join(' ');
      box.innerHTML = '当前用：<b style="color:var(--cyan)">' + esc(a.primary||'') + '</b>'
        + (last ? ' ｜ 最近耗时：<span style="color:var(--dim)">' + last + '</span>' : '');
      return;
    }
    var using = a.using === 'fallback' ? '备用' : '首选';
    var color = a.using === 'fallback' ? '#e0a030' : 'var(--cyan)';
    var txt = '当前用：<b style="color:' + color + '">' + using + ' · ' + esc(a.using === 'fallback' ? (a.fallback||'') : (a.primary||'')) + '</b>'
      + ' ｜ 阈值 ' + a.threshold + 's × ' + a.streak_needed + ' 条'
      + ' ｜ 已连慢 ' + a.slow_streak + ' 条'
      + ' ｜ 切换 ' + a.switches + ' 次';
    if(a.using === 'fallback' && a.cooldown_left > 0){
      txt += ' ｜ ' + Math.ceil(a.cooldown_left/60) + ' 分钟后试回首选';
    }
    var last = (a.last || []).slice(-8).map(function(x){return x+'s';}).join(' ');
    if(last) txt += '<br><span style="color:var(--dim)">最近耗时：' + last + '</span>';
    box.innerHTML = txt;
  }
  function renderRoleCards(){
    var box = $('md-roles'); if(!box) return;
    if(!MD_MODELS.length && !ASR_INFO.model){ box.innerHTML = '<div class="empty">暂无模型配置</div>'; return; }
    box.innerHTML = MD_MODELS.map(function(m){
      var on = (m.role === MD_ACTIVE) ? ' on' : '';
      var badge = m.configured ? '<span class="tag gr">就绪</span>' : '<span class="tag rd">缺配置</span>';
      // 长文有三级降级，卡片上直接把备胎写出来，免得以为只有 luna
      var spare = '';
      if(m.role === 'deep'){
        var sol = MD_MODELS.filter(function(x){ return x.role === 'sonnet'; })[0] || {};
        if(sol.model) spare = '<div class="rd" style="color:var(--dim)">备胎 → ' + esc(sol.model) + '</div>';
      }
      return '<div class="role-card' + on + '" data-role="' + esc(m.role) + '">' +
        '<div class="rh"><span class="rn">' + esc(m.label || m.role) + '</span>' + badge + '</div>' +
        '<div class="rm">' + esc(m.model || '--') + '</div>' +
        '<div class="rd">' + esc(m.role).toUpperCase() + (m.max_tokens ? ' · ' + m.max_tokens + ' TOK' : '') + '</div>' +
        spare +
      '</div>';
    }).join('') + (ASR_INFO.model ? (function(){
      var on = (MD_ACTIVE === 'asr') ? ' on' : '';
      var badge = ASR_INFO.ready ? '<span class="tag gr">就绪</span>' : '<span class="tag rd">缺配置</span>';
      return '<div class="role-card' + on + '" data-role="asr">' +
        '<div class="rh"><span class="rn">语音识别 · ASR</span>' + badge + '</div>' +
        '<div class="rm">' + esc(ASR_INFO.model || '--') + '</div>' +
        '<div class="rd">ASR</div></div>';
    })() : '');
    box.querySelectorAll('.role-card').forEach(function(c){
      c.addEventListener('click', function(){
        MD_ACTIVE = c.getAttribute('data-role');
        renderRoleCards();
        fillModelForm(MD_ACTIVE);
      });
    });
  }

  function fillModelForm(role){
    if(role === 'asr'){
      $('md-role').value = 'asr';
      $('md-model').value = ASR_INFO.model || '';
      $('md-url').value = ASR_INFO.url || '';
      $('md-key').value = '';
      $('md-maxtok').value = '';
      $('md-timeout').value = '';
      $('md-effort').value = '';
      $('md-msg').textContent = ASR_INFO.ready ? '当前已就绪' : '尚未配置完整';
      return;
    }
    var m = MD_MODELS.filter(function(x){ return x.role === role; })[0] || {};
    $('md-role').value = role;
    $('md-model').value = m.model || '';
    $('md-url').value = m.base_url || '';
    $('md-key').value = '';
    $('md-maxtok').value = m.max_tokens || '';
    $('md-timeout').value = m.timeout || '';
    $('md-effort').value = m.reasoning_effort || '';
    $('md-msg').textContent = m.configured ? '当前已就绪' : '尚未配置完整';
  }

  var MD_PROVIDERS = { image: 'builtin', tts: 'builtin', tts_sing: 'builtin' };
  var AGENT_INFO = {};

  function renderRoutes(){
    var box = $('rt-grid'); if(!box) return;

    // 所有通道都用同一套「纵向档位」排布：
    //   ① 首选 → ② 备选 → ③ 兜底
    // 档位就是代码里真实的降级顺序，不做成可点按钮——
    // 因为降级是自动发生的，点它反而会让人以为能手动指定。
    function m(role, dflt){
      var x = MD_MODELS.filter(function(v){ return v.role === role; })[0] || {};
      return x.model || dflt;
    }
    function tier(n, name, note){
      return '<div class="tier' + (n === '①' ? ' on' : '') + '">' +
               '<i>' + n + '</i>' +
               '<span>' + esc(name) + '</span>' +
               (note ? '<em>' + esc(note) + '</em>' : '') +
             '</div>';
    }
    function card(title, tag, tiers, foot){
      return '<div class="route">' +
        '<div class="rh"><span>' + title + '</span>' +
          (tag ? '<em>' + tag + '</em>' : '') + '</div>' +
        '<div class="chain">' + tiers + '</div>' +
        '<div class="rd">' + foot + '</div>' +
      '</div>';
    }

    // ① 图像生成：GPT-Image → Qwen-Image → 云端智能体
    var img = card('图像生成 · IMAGE', '自动降级',
      tier('①', m('flare', 'gpt-image-2.5-flare'), 'GPT') +
      tier('②', m('image', 'qwen-image-3.0'), 'Image') +
      tier('③', '云端智能体', '兜底'),
      '首选画质最好；超时或失败自动换下一档，不会一张图卡死。');

    // ② 长文：gpt-6.1-sol → claude-sonnet → 文本模型
    var deep = card('长文 · DEEP', '固定链路',
      tier('①', m('deep', 'gpt-6.1-sol'), 'SOL') +
      tier('②', m('sonnet', 'claude-sonnet-5-5'), 'Sonnet') +
      tier('③', m('text', 'qwen3.8-flash'), '文本'),
      '写小说 / 长文才走这条。主模型只重试 1 次就换人，不干等 90 秒。');

    // ③ 说话：qwen3-tts-flash → qwen-audio-3.0-tts-flash → 云端智能体
    var speak = card('说话 · SPEAK', '日常念白',
      tier('①', 'qwen3-tts-flash', 'Cherry') +
      tier('②', 'qwen-audio-3.0-tts-flash', 'WebSocket') +
      tier('③', '云端智能体', '兜底'),
      '念消息、随口说两句。声线 Cherry，软而不腻。');

    // ④ 唱歌：3.0 流式 → 3.1-next 同步 → Suno v5
    var sing = card('唱歌 · SING', '点歌 / 哼两句',
      tier('①', 'qwen-audio-3.0-tts-flash', 'longanfengyue') +
      tier('②', 'qwen-audio-3.1-tts-next', 'HTTP') +
      tier('③', 'suno-v5', '完整成曲'),
      '歌词现编现唱，一两秒出音频；3.0 抽风就换 3.1-next。Suno 默认关闭。');

    box.innerHTML = img + deep + speak + sing;
  }

  function renderAgentStatus(){
    var box = $('ag-status'); if(!box) return;
    if(AGENT_INFO.ready){
      box.innerHTML = '<span class="dot ok"></span>就绪 · ' + esc(String(AGENT_INFO.agent_id||'').slice(0,14));
    } else {
      box.innerHTML = '<span class="dot"></span>未就绪（缺 Key 或 Agent ID）';
    }
    if($('ag-id')) $('ag-id').value = AGENT_INFO.agent_id || '';
    if($('ag-base')) $('ag-base').value = AGENT_INFO.base || '';
    if($('ag-key')) $('ag-key').value = '';
  }

  async function loadModels(){
    var d = await api('models-config');
    MD_MODELS = (d && d.models) || [];
    renderAdapt(d && d.adapt);
    MD_PROVIDERS = (d && d.providers) || { image:'builtin', tts:'builtin' };
    AGENT_INFO = (d && d.agent) || {};
    renderRoleCards();
    fillModelForm(MD_ACTIVE);
    renderRoutes();
    renderAgentStatus();
  }

  $('md-role').addEventListener('change', function(){
    MD_ACTIVE = this.value;
    renderRoleCards();
    fillModelForm(MD_ACTIVE);
  });

  $('md-save').addEventListener('click', async function(){
    var role = $('md-role').value;
    // ASR 有自己的配置接口
    if(role === 'asr'){
      var ra = await api('asr/model', 'POST', {
        model: $('md-model').value.trim(),
        base_url: $('md-url').value.trim(),
        api_key: $('md-key').value.trim()
      });
      if(ra && ra.ok){ toast('ASR 模型已保存', 'ok'); $('md-msg').textContent = '已保存 ✓'; loadAsr(); }
      else { toast('保存失败', 'err'); $('md-msg').textContent = '保存失败'; }
      return;
    }
    var payload = {
      role: role,
      name: role,
      model: $('md-model').value.trim(),
      base_url: $('md-url').value.trim(),
      api_key: $('md-key').value.trim(),
      max_tokens: num($('md-maxtok').value, 2048),
      timeout: num($('md-timeout').value, 120),
      reasoning_effort: $('md-effort').value
    };
    var r = await api('models-config', 'POST', payload);
    if(r && r.ok){
      toast('模型已保存', 'ok');
      $('md-msg').textContent = '已保存';
      $('md-key').value = '';
      loadModels();
    } else {
      toast('保存失败: ' + ((r&&r.error)||'未知'), 'err');
    }
  });

  var agSave = $('ag-save');
  if(agSave) agSave.addEventListener('click', async function(){
    var payload = {
      action: 'providers',
      providers: MD_PROVIDERS,
      agent: {
        agent_id: $('ag-id').value.trim(),
        base: $('ag-base').value.trim(),
        api_key: $('ag-key').value.trim()
      }
    };
    var r = await api('models-config', 'POST', payload);
    if(r && r.ok){
      toast('通道配置已保存', 'ok');
      $('ag-msg').textContent = '已保存';
      $('ag-key').value = '';
      loadModels();
    } else {
      toast('保存失败: ' + ((r&&r.error)||'未知'), 'err');
    }
  });

  /* ---------- custom memory ---------- */
  async function loadCustomMemory(){
    var box = $('cm-list'); if(!box) return;
    var d = await api('custom-memory');
    var items = (d && d.entries) || [];
    if(!items.length){
      box.innerHTML = '<div class="empty">还没有手写记忆</div>';
      return;
    }
    box.innerHTML = items.map(function(it){
      return '<div class="cm-item"><div class="t">' + esc(it.text) + '</div>' +
        '<span class="d" data-id="' + esc(it.id) + '">删除</span></div>';
    }).join('');
    box.querySelectorAll('.d').forEach(function(b){
      b.addEventListener('click', async function(){
        var r = await api('custom-memory', 'POST', { action:'delete', id: b.getAttribute('data-id') });
        if(r && r.ok){ toast('已删除', 'ok'); loadCustomMemory(); }
        else toast('删除失败', 'err');
      });
    });
  }

  $('cm-add').addEventListener('click', async function(){
    var text = $('cm-input').value.trim();
    if(!text){ toast('先写点内容', 'err'); return; }
    var r = await api('custom-memory', 'POST', { action:'add', text: text });
    if(r && r.ok){
      $('cm-input').value = '';
      toast('记忆已写入，下一句话就生效', 'ok');
      loadCustomMemory();
    } else {
      toast('写入失败: ' + ((r&&r.error)||'未知'), 'err');
    }
  });

  /* ---------- style ---------- */
  var ST_BOOKS = [];
  var ST_ACTIVE = '';

  function renderBooks(){
    var box = $('st-books'); if(!box) return;
    if(!ST_BOOKS.length){ box.innerHTML = '<div class="empty">没有可用书目</div>'; return; }
    box.innerHTML = ST_BOOKS.map(function(b){
      var on = (b.key === ST_ACTIVE) ? ' on' : '';
      var attrs = (b.attrs && b.attrs.length) ? b.attrs.join('·') : '';
      return '<div class="book-item' + on + '" data-key="' + esc(b.key) + '">' +
        '<span class="k">' + esc(b.key) + '</span>' +
        '<span class="n">' + esc(b.name) + '</span>' +
        '<span class="a">' + esc(attrs) + '</span></div>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.book-item'), function(el){
      el.addEventListener('click', async function(){
        var key = this.getAttribute('data-key');
        var r = await api('style', 'POST', { default_book: key });
        if(r && r.ok){
          ST_ACTIVE = key;
          renderBooks();
          toast('已切换到「' + key + '」指纹', 'ok');
          loadStyle();
        } else toast('切换失败: ' + ((r&&r.error)||'未知'), 'err');
      });
    });
  }

  async function loadStyle(){
    var d = await api('style');
    if(!d){ $('st-sources').innerHTML = '<div class="empty">读取失败</div>'; return; }

    // 书目：后端返回 [{key,name,attrs}]，这里是对象数组，不能直接 esc
    var raw = d.books || d.default_books || [];
    if(raw.length && typeof raw[0] === 'object'){
      ST_BOOKS = raw.map(function(b){
        return { key: str(b.key), name: str(b.name), attrs: Array.isArray(b.attrs) ? b.attrs : [] };
      });
    } else if(raw.length){
      ST_BOOKS = raw.map(function(x){ return { key: str(x), name: str(x), attrs: [] }; });
    } else ST_BOOKS = [];
    ST_ACTIVE = str(d.default_book || '');
    renderBooks();

    var h = '';
    h += kv('语料目录', esc(d.corpus_root || d.corpus_dir || '--'));
    h += kv('样本数量', num(d.sample_total, d.samples || 0) + ' 段');
    h += kv('每次取样', num(d.samples_per_call, 0) + ' 段');
    h += kv('当前书目', esc(str(d.default_book_name || d.default_book || '--')));
    var buckets = d.sample_buckets || {};
    var bk = Object.keys(buckets);
    if(bk.length){
      h += kv('样本分布', bk.map(function(k){ return esc(k) + ':' + num(buckets[k],0); }).join(' · '));
    }
    $('st-sources').innerHTML = h;

    // 表单回填
    if($('st-enabled')) $('st-enabled').value = (d.enabled === false ? '0' : '1');
    if($('st-threshold')) $('st-threshold').value = num(d.long_threshold, 120);
    if($('st-spc')) $('st-spc').value = num(d.samples_per_call, 2);
    if($('st-flirt-lines')) $('st-flirt-lines').value = num(d.flirt_max_lines, 12);
    if($('st-flirt-gap')) $('st-flirt-gap').value = num(d.flirt_gap, 0.9);
    if($('st-flirt-on')) $('st-flirt-on').value = (d.flirt_enabled === false ? '0' : '1');

    var p2 = '';
    p2 += kv('文风注入', d.enabled === false ? '<span class="tag rd">关闭</span>' : '<span class="tag gr">启用</span>');
    p2 += kv('语料目录', d.corpus_exists === false ? '<span class="tag rd">不存在</span>' : '<span class="tag gr">已找到</span>');
    p2 += kv('注入策略', '显式意图触发长文，其余走水群');
    p2 += kv('红线规则', '<span class="tag gr">常驻加载</span>');
    $('st-policy').innerHTML = p2;
  }

  var stSave = $('st-save');
  if(stSave) stSave.addEventListener('click', async function(){
    var payload = {
      enabled: ($('st-enabled').value === '1'),
      long_threshold: num($('st-threshold').value, 120),
      samples_per_call: num($('st-spc').value, 2),
      flirt_max_lines: num($('st-flirt-lines').value, 12),
      flirt_gap: parseFloat($('st-flirt-gap').value) || 0.9,
      flirt_enabled: ($('st-flirt-on').value === '1')
    };
    var r = await api('style', 'POST', payload);
    if(r && r.ok){
      toast('策略已保存', 'ok');
      $('st-msg').textContent = '已保存 ✓';
      loadStyle();
    } else {
      toast('保存失败: ' + ((r&&r.error)||'未知'), 'err');
      $('st-msg').textContent = '保存失败';
    }
  });

  if($('st-reload')) $('st-reload').addEventListener('click', async function(){
    var r = await api('style/reload', 'POST', {});
    if(r && (r.ok !== false)) toast('语料已重新加载', 'ok'); else toast('重载失败', 'err');
    loadStyle();
  });

  if($('st-preview-btn')) $('st-preview-btn').addEventListener('click', async function(){
    $('st-preview-msg').textContent = '取件中…';
    var scope = ($('st-scope') && $('st-scope').value) || 'group';
    var r = await api('style/preview', 'POST',
                      { mode: $('st-mode').value, scope: scope });
    if(!r || r.error){
      $('st-preview-msg').textContent = '';
      $('st-preview').textContent = '取不到：' + ((r && r.error) || '未知');
      return;
    }
    if(r.source === 'real'){
      $('st-preview').textContent = r.prompt || '（快照里没有内容）';
      $('st-preview-msg').textContent =
        '真实提示词 · ' + (r.kind === 'dm' ? '私聊' : '群聊')
        + ' · ' + (r.mode || '?') + ' · ' + num(r.total_chars) + ' 字 · '
        + relTime(r.ts) + '那次说的';
    } else {
      $('st-preview').textContent = r.block || '';
      $('st-preview-msg').textContent = (r.note || '这是文风块，不是她真的看到的提示词')
        + (r.length ? ('（' + r.length + ' 字）') : '');
    }
  });

    /* ---------- cost & cache ---------- */
  async function loadCost(){
    var d = await api('cost');
    var box = $('cost-cards');
    if(!box) return;
    if(!d || d.error){ box.innerHTML = '<div class="empty">读取失败</div>'; return; }

    var n = num(d.calls, 0);
    var pt = num(d.prompt, 0);
    var cd = num(d.cached, 0);
    var cp = num(d.completion, 0);
    var pct = num(d.cached_pct, 0);
    var img = d.image || {};

    var stat = function(label, value, sub){
      return '<div class="stat" style="border:1px solid var(--line);background:#08111a;padding:10px 12px">' +
        '<div class="stat-k" style="font-size:9px;letter-spacing:1.6px;color:var(--dim2);font-family:var(--mono)">' + esc(label) + '</div>' +
        '<div class="stat-v" style="font-size:19px;font-family:var(--mono);color:var(--cyan);margin-top:3px">' + esc(value) + '</div>' +
        '<div class="hint" style="margin-top:2px">' + esc(sub || '') + '</div></div>';
    };

    box.innerHTML =
      stat('调用次数', String(n), '自上次重启') +
      stat('平均 prompt', String(num(d.avg_prompt, 0)), 'token / 轮') +
      stat('缓存命中', pct.toFixed(1) + '%', cd + ' / ' + pt + ' token') +
      stat('输出 token', String(cp), 'completion');

    if($('cost-msg')){
      $('cost-msg').textContent = '画图 ' + (img.model || '?') + ' · ' + (img.size || '默认') +
        ' · 扩写' + (img.extend ? '开' : '关');
    }
  }

  if($('cost-reset')) $('cost-reset').addEventListener('click', async function(){
    var r = await api('cost', 'POST', { reset: true });
    if(r && r.ok){ toast('统计已清零', 'ok'); loadCost(); }
    else toast('清零失败', 'err');
  });

  /* ---------- draw pipeline ---------- */
  async function loadDraws(){
    var d = await api('draws');
    if(!d){ if($('dw-list')) $('dw-list').innerHTML = '<div class="empty">读取失败</div>'; return; }

    var flow = d.flow || [];
    if($('dw-flow')){
      $('dw-flow').innerHTML = flow.map(function(x,i){
        return '<div class="dw-step"><b>' + (i+1) + '</b>' + esc(x) + '</div>';
      }).join('');
    }

    var pend = d.pending || [];
    if($('dw-pending')){
      $('dw-pending').innerHTML = pend.map(function(p){
        var st = p.cancel ? '正在终止' : (p.stage === 'drawing' ? '正在画' : '正在查资料');
        return '<div class="dw-pend">' + esc(p.subject || '(未命名)') +
          ' ｜ ' + st + ' ｜ 已进行 ' + (p.seconds||0) + ' 秒</div>';
      }).join('');
    }

    var st2 = d.stats || {};
    if($('dw-stats')){
      $('dw-stats').textContent = st2.total
        ? ('共 ' + st2.total + ' 次：画出 ' + st2.ok + '，喊停 ' + st2.cancel +
           '；描述平均 ' + st2.avg_desc + ' 字')
        : '还没有记录';
    }

    var rows = d.recent || [];
    var box = $('dw-list');
    if(box){
      if(!rows.length){ box.innerHTML = '<div class="empty">还没有画过</div>'; }
      else {
        box.innerHTML = rows.map(function(r){
          var tag = r.cancelled ? '<span class="tag rd">停</span>'
                    : (r.ok ? '<span class="tag gr">成</span>' : '<span class="tag rd">败</span>');
          return '<div class="dw-row">' +
            '<span class="dt">' + esc(r.time) + '</span>' + tag +
            '<span class="ds">' + esc(r.subject || '(场景)') + ' — ' + esc((r.report||'').slice(0,54)) + '</span>' +
            '<span class="dd">' + (r.desc_len||0) + '字 ' + (r.seconds||0) + 's</span></div>';
        }).join('');
      }
    }
  }

  /* ---------- tools ---------- */
  var TOOL_LIST = [];

  async function loadTools(){
    var d = await api('tools');
    if(!d){ $('tool-grid').innerHTML = '<div class="empty">读取失败</div>'; return; }
    TOOL_LIST = d.tools || [];

    // 分流策略：什么情况走云端，什么情况本地快算
    var rt = d.routing || {};
    var rbox = $('tool-routing');
    if(rbox && (rt.cloud_when || rt.local_when)){
      var chips = function(arr, cls){
        return (arr||[]).map(function(x){ return '<span class="chip ' + cls + '">' + esc(x) + '</span>'; }).join('');
      };
      rbox.innerHTML =
        '<div class="bar-lab"><span>走云端真工具（慢 40~60 秒）</span><b></b></div>' +
        '<div class="chips" style="margin-bottom:9px">' + chips(rt.cloud_when, '') + '</div>' +
        '<div class="bar-lab"><span>走本地快算（立刻有）</span><b></b></div>' +
        '<div class="chips" style="margin-bottom:9px">' + chips(rt.local_when, '') + '</div>' +
        (rt.ack_note ? '<div class="hint">' + esc(rt.ack_note) +
           (rt.ack_samples && rt.ack_samples.length ? ' 比如「' + esc(rt.ack_samples[0]) + '」' : '') +
           '</div>' : '');
    }

    var st = $('tool-status');
    if(st){
      st.innerHTML = d.agent_ready
        ? '<span class="tag gr">云端智能体已连接</span> 6 个 skill 可调用'
        : '<span class="tag rd">云端智能体未连接</span> 只能用本地工具';
    }

    $('tool-grid').innerHTML = TOOL_LIST.map(function(t){
      var cls = t.scope === 'cloud' ? 'cloud' : 'local';
      var skill = t.skill ? 'skill: ' + esc(t.skill) : '本地实现';
      return '<div class="tool-card ' + cls + '">' +
        '<div class="tn">' + esc(t.name) + '</div>' +
        '<div class="ts">' + esc(skill) + '</div>' +
        '<div class="td">' + esc(t.desc || '') + '</div>' +
        '<div class="tf"><span class="tag ' + (t.scope === 'cloud' ? 'gr' : '') + '">' +
        (t.scope === 'cloud' ? '云' : '本地') + '</span></div></div>';
    }).join('') || '<div class="empty">没有工具</div>';

    // 试跑下拉：只列云端（本地工具靠聊天触发）
    var pick = $('tool-pick');
    if(pick){
      pick.innerHTML = TOOL_LIST.filter(function(t){ return t.scope === 'cloud'; })
        .map(function(t){ return '<option value="' + esc(t.key) + '">' + esc(t.name) + '</option>'; })
        .join('');
    }

    // 统计
    var s2 = d.stats || {};
    var per = s2.by_tool || {};
    var perTxt = Object.keys(per).map(function(k){
      return k + ' ' + per[k].ok + '/' + per[k].n;
    }).join(' · ');
    if($('tool-stats')){
      $('tool-stats').textContent = s2.total
        ? ('共 ' + s2.total + ' 次，已核验成功 ' + s2.ok + '，失败 ' + s2.fail + '，未知 ' + (s2.unknown == null ? '未提供' : s2.unknown) + '，平均 ' + s2.avg_seconds + 's' + (perTxt ? ' ｜ ' + perTxt : ''))
        : '还没有调用过';
    }

    // 记录
    var calls = d.calls || [];
    var box = $('tool-log');
    if(box){
      if(!calls.length){ box.innerHTML = '<div class="empty">还没有记录</div>'; }
      else {
        box.innerHTML = calls.map(function(c){
          var outcome = (c.extra || {}).outcome;
          var okTag = outcome === 'success' ? '<span class="tag gr">已核验</span>' :
            (outcome === 'failure' ? '<span class="tag rd">失败</span>' : '<span class="tag">待核验</span>');
          var q = c.query || c.error || '';
          return '<div class="tl-row">' +
            '<span class="tt">' + esc(c.time) + '</span>' +
            '<span class="tk">' + esc(c.tool) + '</span>' +
            okTag +
            '<span class="tq">' + esc(q) + '</span>' +
            '<span class="tp">' + (c.seconds||0) + 's</span></div>';
        }).join('');
      }
    }
  }

  if($('tool-run')) $('tool-run').addEventListener('click', async function(){
    var key = $('tool-pick').value;
    var q = ($('tool-q').value || '').trim() || '帮我算一卦';
    var out = $('tool-out');
    out.style.display = 'block';
    out.textContent = '调用中…（云端要几十秒，请稍等）';
    $('tool-msg').textContent = '';
    var r = await api('tools/test', 'POST', { tool: key, query: q });
    if(r && r.ok){
      out.textContent = r.text || '(没有文本)';
      $('tool-msg').textContent = (r.outcome === 'success' ? '已核验完成 ' : r.outcome === 'failure' ? '未完成 ' : '已返回内容，完成情况待核验 ') + (r.seconds||0) + 's';
    } else {
      out.textContent = '失败: ' + ((r&&r.error)||'未知');
      $('tool-msg').textContent = '失败';
    }
    loadTools();
  });

  /* ---------- ASR ---------- */
  async function loadAsr(){
    var d = await api('asr');
    if(!d){ if($('asr-status')) $('asr-status').innerHTML = '<div class="empty">读取失败</div>'; return; }
    ASR_INFO = { model: d.model || '', url: d.url || '', ready: !!d.ready, has_key: !!d.has_key };
    try { renderRoleCards(); } catch(e) {}

    var st = $('asr-status');
    if(st){
      var keyTag = d.has_key ? '<span class="tag gr">密钥已配</span>' : '<span class="tag rd">缺密钥</span>';
      var readyTag = d.ready ? '<span class="dot ok"></span>就绪' : '<span class="dot" style="background:#c33"></span>未就绪';
      st.innerHTML = readyTag + ' &nbsp;' + keyTag +
        (d.url ? '<div class="hint" style="margin-top:5px">端点：' + esc(d.url) + '</div>' : '') +
        (d.error ? '<div class="hint" style="color:#e06">' + esc(d.error) + '</div>' : '');
    }
    if($('asr-model')) $('asr-model').value = d.model || '';

    var pol = d.policy || {};
    if($('asr-chance')) $('asr-chance').value = num(pol.voice_reply_chance, 50);
    if($('asr-ignore')) $('asr-ignore').value = (pol.ignore_meaningless === false ? '0' : '1');
    asrChanceDesc();

    if($('asr-rules') && (d.rules || []).length){
      $('asr-rules').innerHTML = (d.rules || []).map(function(x){
        return '<div>· ' + esc(x) + '</div>';
      }).join('');
    }
  }

  function asrChanceDesc(){
    var v = num($('asr-chance').value, 50);
    if($('asr-chance-v')) $('asr-chance-v').textContent = v + '%';
    if($('asr-chance-desc')){
      $('asr-chance-desc').textContent =
        '收到语音后：' + v + '% 概率用语音回，' + (100 - v) + '% 概率用文字回。';
    }
  }

  if($('asr-chance')) $('asr-chance').addEventListener('input', asrChanceDesc);

  if($('asr-save')) $('asr-save').addEventListener('click', async function(){
    var payload = {
      voice_reply_chance: num($('asr-chance').value, 50),
      ignore_meaningless: ($('asr-ignore').value === '1')
    };
    var r = await api('asr', 'POST', payload);
    if(r && r.ok){ toast('语音策略已保存', 'ok'); $('asr-msg').textContent = '已保存 ✓'; loadAsr(); }
    else { toast('保存失败', 'err'); $('asr-msg').textContent = '失败'; }
  });

  /* ---------- human-like ---------- */
  var HU_LEVELS = {};

  function huRender(level){
    var d = HU_LEVELS[level] || {name:'?', desc:''};
    var nv = $('hu-level-v'), ds = $('hu-level-desc');
    if(nv) nv.textContent = level + ' ' + d.name;
    if(ds) ds.textContent = d.desc || '';
    // 画出分布条
    var dist = [[60,24,6,10],[52,29,9,10],[45,33,12,10],[35,40,17,8],[25,45,24,6]][level-1] || [45,33,12,10];
    var labels = ['短句', '整句', '长篇', '不回'];
    var cols = ['var(--cyan)', 'var(--grn, #3ee0a0)', 'var(--org, #ffb454)', 'var(--dim2)'];
    var h = '<div class="bar-lab"><span>这一档的输出分布</span><b></b></div>' +
      dist.map(function(p, i){
        return '<div style="display:flex;align-items:center;gap:8px;margin-bottom:5px">' +
          '<span style="width:44px;font-size:11px;color:var(--dim)">' + labels[i] + '</span>' +
          '<div style="flex:1;height:7px;background:#0b1621;border:1px solid var(--line)">' +
          '<div style="width:' + p + '%;height:100%;background:' + cols[i] + '"></div></div>' +
          '<span style="width:36px;text-align:right;font-family:var(--mono);font-size:11px;color:var(--cyan)">' + p + '%</span>' +
          '</div>';
      }).join('');
    var box = $('hu-dist'); if(box) box.innerHTML = h;
  }

  async function loadHuman(){
    var d = await api('human');
    if(!d) return;
    (d.levels || []).forEach(function(x){ HU_LEVELS[x.v] = {name:x.name, desc:x.desc}; });
    var lv = d.talk_level || 3;
    if($('hu-level')) $('hu-level').value = lv;
    huRender(lv);
  }

  if($('hu-level')) $('hu-level').addEventListener('input', function(){
    huRender(parseInt(this.value) || 3);
  });

  if($('hu-save')) $('hu-save').addEventListener('click', async function(){
    var lv = parseInt($('hu-level').value) || 3;
    var r = await api('human', 'POST', { talk_level: lv });
    if(r && r.ok){ toast('话痨程度已设为 ' + lv + ' 档', 'ok'); $('hu-msg').textContent = '已保存 ✓'; }
    else { toast('保存失败', 'err'); $('hu-msg').textContent = '失败'; }
  });

  /* ---------- monitor ---------- */
  async function loadMonitor(){
    var cfg = await api('effective-config');
    var ctrl0 = await api('control');
    var cs = (cfg && cfg.settings) || {};
    var fresh = (ctrl0 && typeof ctrl0.fresh === 'boolean') ? ctrl0.fresh
              : (typeof (cfg||{}).fresh === 'boolean' ? cfg.fresh
              : (typeof cs.fresh === 'boolean' ? cs.fresh : false));
    var h = '';
    h += kv('机器人', fresh ? '<span class="tag gr">在线</span>' : '<span class="tag rd">离线</span>');
    if(cs.age_sec !== undefined || (cfg && cfg.age_sec !== undefined)){ h += kv('最近上报', num((cfg||{}).age_sec !== undefined ? cfg.age_sec : cs.age_sec, 0) + ' 秒前'); }
    h += kv('OneBot WS', '127.0.0.1:3001');
    h += kv('当前人设', esc(((cfg||{}).persona||{}).mode || '--'));
    $('mo-conn').innerHTML = h;

    var play = await api('play');
    var t = (play && play.state && play.state.today) || {};
    var m = '';
    if(t.messages !== undefined){
      m += kv('今日消息', num(t.messages,0) + ' 条');
      m += kv('其中私聊', num(t.private_messages,0) + ' 条');
      m += kv('图片', num(t.images,0) + ' 张');
      m += kv('模型调用', num(t.model_calls,0) + ' 次');
      m += kv('模型错误', num(t.model_errors,0) + ' 次', num(t.model_errors,0) > 0 ? 'bad' : 'ok');
      m += kv('已发送', num(t.sent_replies,0) + ' 条');
    } else {
      m = '<div class="empty">暂无统计（机器人上报后出现）</div>';
    }
    $('mo-msg').innerHTML = m;

    var sys = await raw('/api/status');
    if(sys){
      setBar('mo-cpu', sys.cpu, '');
      setBar('mo-mem', sys.memory, 'am');
      setBar('mo-disk', sys.disk, 'rd');
    }

    var ctrl = await api('control');
    renderOps('mo-events', (ctrl && ctrl.operations) || []);
  }

  function setBar(prefix, pct, cls){
    var p = Math.max(0, Math.min(100, num(pct)));
    var v = $(prefix + '-v'); if(v) v.textContent = p.toFixed(0) + '%';
    var b = $(prefix + '-b'); if(b){ b.className = 'bar ' + (cls||''); b.querySelector('i').style.width = p + '%'; }
  }

  /* ---------- emojis ---------- */
  // 一个库一张表：QQ 收藏由后台搬进库里（qq_face_sync_worker），
  // 面板不再单列一张「QQ 收藏」，否则又是两套东西摆在同一页上。
  // 格子里不铺她的总结文字（主人要的：几百条摘要摊在库里没法筛），
  // 点一张才弹出悬浮窗：那里看她的理解、改写（校准）、删掉（人工筛选）。
  var EM_FILTER = {q: '', src: ''};
  var STK = {cur: null, armed: false};

  function emSourceOf(s){
    return (s.src || (s.tags || []).indexOf('QQ收藏') >= 0) ? 'QQ收藏' : '群里';
  }

  function stkCalibrated(s){
    return String((s && s.meaning_src) || '') === 'human';
  }

  function renderStickers(){
    var box = $('em-stickers'), sk = STATE.stickers || [];
    var q = EM_FILTER.q, src = EM_FILTER.src;
    var shown = sk.filter(function(s){
      if(src === '已校准'){ if(!stkCalibrated(s)) return false; }
      else if(src && emSourceOf(s) !== src) return false;
      if(!q) return true;
      var hay = ((s.tags || []).join(' ') + ' ' + (s.meaning || '') + ' '
                 + (s.note || '') + ' ' + s.id).toLowerCase();
      return hay.indexOf(q) >= 0;
    });
    $('em-stk-n').textContent = sk.length + ' 张';
    var fromQQ = sk.filter(function(s){ return emSourceOf(s) === 'QQ收藏'; }).length;
    var cal = sk.filter(stkCalibrated).length;
    var st = STATE.stickerStats || {};
    var line = $('em-src-line');
    if(line){
      line.textContent = sk.length
        ? ('库里 ' + sk.length + ' 张：群里收的 ' + (sk.length - fromQQ)
           + '，QQ 收藏搬来 ' + fromQQ + '。她发表情就从这个池子里抽。'
           + '其中你校准过 ' + cal + ' 张'
           + (num(st.trash) ? ('，回收目录还留着 ' + st.trash + ' 张（删掉的图不直接抹掉）') : '')
           + '。')
        : '库还是空的 —— 群里有人发图她会自动收，QQ 收藏每半小时搬一次。';
    }
    if(!shown.length){
      box.innerHTML = sk.length
        ? '<div class="empty">这个筛选下没有图（库里共 ' + sk.length + ' 张）</div>'
        : '<div class="empty">库还是空的 —— 群里有人发图她会自动收</div>';
      return;
    }
    box.innerHTML = shown.slice(0, 200).map(function(s){
      var srcUrl = '/admin/api/bot/sticker/' + encodeURIComponent(s.id);
      var dim = (s.dims && s.dims.length === 2) ? (s.dims[0] + '×' + s.dims[1]) : '';
      // 提示语只说「点开能做什么」：把她的总结当 title 挂在格子上，
      // 就等于把摘要摊在界面里，主人要的恰恰不是这个。
      return '<div class="emoji-cell' + (stkCalibrated(s) ? ' cal' : '')
           + '" data-sid="' + esc(s.id) + '" role="button" tabindex="0"'
           + ' title="点开看她的理解，可以改写或删掉">'
           + '<img src="' + esc(srcUrl) + '" style="max-width:56px;max-height:56px" loading="lazy">'
           + '<span>' + esc(emSourceOf(s) === 'QQ收藏' ? '收藏 · 用' + (s.used || 0)
                                                      : '用 ' + (s.used || 0) + ' 次')
           + '</span>' + (dim ? '<small class="dim">' + esc(dim) + '</small>' : '')
           + '</div>';
    }).join('');
  }

  async function stickerPost(sid, verb, payload){
    // 删除与校准都是写操作：光靠 cookie 能被别的页面触发就是 CSRF 面，
    // 所以和「高级设置」同一把令牌。
    var csrf = await csrfToken();
    try{
      var r = await fetch('/admin/api/bot/sticker/' + encodeURIComponent(sid) + '/' + verb, {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        body: JSON.stringify(payload || {})
      });
      var d = await r.json();
      return {status: r.status, data: d};
    }catch(e){ return {status: 0, data: {error: e.message}}; }
  }

  function stkMsg(text, cls){
    var el = $('stk-msg');
    if(!el) return;
    el.textContent = text || '';
    el.className = 'hint' + (cls ? ' ' + cls : '');
  }

  function stkDisarm(){
    STK.armed = false;
    var c = $('stk-confirm');
    if(c) c.classList.remove('on');
    var b = $('stk-del');
    if(b) b.disabled = false;
  }

  function openStk(sid){
    var s = (STATE.stickers || []).filter(function(x){ return x.id === sid; })[0];
    if(!s){ toast('这张已经不在库里了，先刷新', 'err'); return; }
    STK.cur = s;
    STK.orig = String(s.meaning || '');
    stkDisarm();
    $('stk-img').src = '/admin/api/bot/sticker/' + encodeURIComponent(s.id);
    $('stk-title').textContent = s.meaning ? s.meaning : '这张她还没总结出意思';
    var when = s.added ? relTime(s.added) : '--';
    var meta = [
      '<span>来源 <b>' + esc(emSourceOf(s)) + '</b></span>',
      '<span>用过 <b>' + num(s.used) + '</b> 次</span>',
      '<span>收录 <b>' + esc(when) + '</b></span>',
      s.dims && s.dims.length === 2 ? '<span>尺寸 <b>' + esc(s.dims.join('×')) + '</b></span>' : '',
      s.bytes ? '<span>大小 <b>' + (num(s.bytes) / 1024).toFixed(1) + ' KB</b></span>' : '',
      s.kind ? '<span>类型 <b>' + esc(s.kind) + '</b></span>' : '',
      (s.tags || []).length ? '<span>标签 <b>' + esc(s.tags.join('、')) + '</b></span>' : '',
      '<span>id <b>' + esc(s.id) + '</b></span>'
    ].filter(Boolean).join('');
    $('stk-meta').innerHTML = meta;
    $('stk-meaning').value = s.meaning || '';
    // 出处必须说清：她自己猜的、别人教的、你校准的，可信度是三档。
    var src = String(s.meaning_src || '');
    var tip = $('stk-meaning-src');
    if(src === 'human'){
      tip.innerHTML = '<span class="hint">这条是<b>你校准过的</b>'
        + (s.meaning_ts ? ('（' + esc(relTime(s.meaning_ts)) + '）') : '')
        + '，她自己再猜、别人再教都不会盖掉它。</span>';
    }else if(src === 'chat'){
      tip.innerHTML = '<span class="hint">这条是<b>群里有人教她的</b>，'
        + '她自己猜的那种盖不掉它，但你一改就以你为准。</span>';
    }else if(s.meaning){
      tip.innerHTML = '<span class="hint">这条是她<b>自己看图猜的</b>，可能有错 —— 直接改，改完就是她下次用的。</span>';
    }else{
      tip.textContent = '她还没看懂过这张。写下你的判断，她下次发/收这张就按这句理解。';
    }
    stkMsg('');
    $('stk-mask').classList.add('on');
    document.querySelectorAll('.emoji-cell.sel').forEach(function(el){ el.classList.remove('sel'); });
    var cell = document.querySelector('.emoji-cell[data-sid="' + (window.CSS && CSS.escape ? CSS.escape(sid) : sid) + '"]');
    if(cell) cell.classList.add('sel');
    setTimeout(function(){ try{ $('stk-meaning').focus(); }catch(e){} }, 30);
  }

  function closeStk(){
    $('stk-mask').classList.remove('on');
    STK.cur = null;
    stkDisarm();
    document.querySelectorAll('.emoji-cell.sel').forEach(function(el){ el.classList.remove('sel'); });
  }

  async function stkSave(){
    var cur = STK.cur;
    if(!cur) return;
    var box = $('stk-meaning');
    var val = String(box.value || '').trim();
    if(val.length > 80){ stkMsg('控制在 80 字内（现在 ' + val.length + ' 字）', 'err'); return; }
    if(!val && STK.orig){
      // 清空也是一次人工判定：「这张没有固定含义」。不说清就会被当成没改。
      stkMsg('你把它清空了 = 判定「这张没有固定含义」，她的机器猜测不会再填回来。', 'ok');
    }
    box.disabled = true;
    var r = await stickerPost(cur.id, 'meaning', {meaning: val});
    box.disabled = false;
    if(r.status !== 200 || !(r.data || {}).ok){
      stkMsg((r.data && r.data.error) || ('没保存成功（HTTP ' + r.status + '）'), 'err');
      return;
    }
    cur.meaning = val;
    cur.meaning_src = 'human';
    cur.meaning_ts = Date.now() / 1000;
    STATE.stickerStats = r.data.stats || STATE.stickerStats;
    renderStickers();
    openStk(cur.id);
    stkMsg('已校准，她下一次就用这句。', 'ok');
    toast('这条理解已生效', 'ok');
  }

  async function stkDelete(){
    var cur = STK.cur;
    if(!cur) return;
    if(!STK.armed){
      // 两步确认：删除是写操作，不用原生 confirm（面板一贯不许）。
      STK.armed = true;
      $('stk-confirm').classList.add('on');
      $('stk-del').disabled = true;
      stkMsg('先确认一下：删掉她就不会再发这张。', 'err');
      return;
    }
    var r = await stickerPost(cur.id, 'delete', {});
    if(r.status !== 200 || !(r.data || {}).ok){
      stkMsg((r.data && r.data.error) || ('没删掉（HTTP ' + r.status + '）'), 'err');
      stkDisarm();
      return;
    }
    STATE.stickers = (STATE.stickers || []).filter(function(x){ return x.id !== cur.id; });
    STATE.stickerStats = r.data.stats || STATE.stickerStats;
    closeStk();
    renderStickers();
    toast(r.data.trashed ? '已删掉，文件在回收目录里还留着' : '已删掉', 'ok');
  }

  function bindStickerPop(){
    var grid = $('em-stickers');
    if(grid && !grid._stkBound){
      grid._stkBound = true;
      grid.addEventListener('click', function(ev){
        var cell = ev.target.closest ? ev.target.closest('.emoji-cell[data-sid]') : null;
        if(cell) openStk(cell.getAttribute('data-sid'));
      });
      grid.addEventListener('keydown', function(ev){
        var cell = ev.target.closest ? ev.target.closest('.emoji-cell[data-sid]') : null;
        if(cell && (ev.key === 'Enter' || ev.key === ' ')){
          ev.preventDefault();
          openStk(cell.getAttribute('data-sid'));
        }
      });
    }
    [['stk-close', closeStk], ['stk-save', stkSave], ['stk-del', stkDelete],
     ['stk-del-no', stkDisarm]].forEach(function(pair){
      var el = $(pair[0]);
      if(el && !el._stkBound){ el._stkBound = true; el.addEventListener('click', pair[1]); }
    });
    var yes = $('stk-del-yes');
    if(yes && !yes._stkBound){
      yes._stkBound = true;
      yes.addEventListener('click', function(){
        // 二次点击才真删：第一次点「从库里删掉」只是把确认条摊开
        stkDelete();
      });
    }
    var mask = $('stk-mask');
    if(mask && !mask._stkBound){
      mask._stkBound = true;
      mask.addEventListener('click', function(ev){ if(ev.target === mask) closeStk(); });
    }
    document.addEventListener('keydown', function(ev){
      if(ev.key === 'Escape' && $('stk-mask') && $('stk-mask').classList.contains('on')) closeStk();
    });
  }

  async function loadEmojis(){
    if(!STATE.emojisLoaded){
      var d = await api('emojis');
      if(!d || d.error || d.ok === false){
        $('em-stickers').textContent = '表情库读取失败，请刷新重试';
        $('em-builtin').textContent = '表情读取失败，请刷新重试';
        return;
      }
      STATE.emojis = d.emojis || [];
      STATE.stickers = d.stickers || [];
      STATE.stickerStats = d.stats || {};
      STATE.emojisLoaded = true;
    }
    renderStickers();
    renderBuiltin('');
    bindStickerPop();
  }

  function renderBuiltin(q){
    var box = $('em-builtin');
    var arr = STATE.emojis || [];
    if(q) arr = arr.filter(function(e){ return String(e.tag || e.name || e.code || '').indexOf(q) >= 0; });
    if(!arr.length){ box.innerHTML = '<div class="empty">没有匹配的表情</div>'; return; }
    box.innerHTML = arr.slice(0, 300).map(function(e){
      var tag = String(e.tag || e.name || e.code || '');
      var fid = e.face_id;
      var img = (fid !== undefined) ? ('<img src="https://qzonestyle.gtimg.cn/qzone/em/e' + fid + '.gif" style="width:24px;height:24px" loading="lazy" onerror="this.style.display=\'none\'">') : '';
      return '<div class="emoji-cell">' + img +
        '<span>' + esc(tag) + '</span></div>';
    }).join('');
  }

  $('em-search').addEventListener('input', function(){
    renderBuiltin(this.value.trim());
  });

  var stkSearch = $('em-stk-search');
  if(stkSearch) stkSearch.addEventListener('input', function(){
    EM_FILTER.q = this.value.trim().toLowerCase();
    renderStickers();
  });
  document.querySelectorAll('#view-emojis .chip[data-src]').forEach(function(btn){
    btn.addEventListener('click', function(){
      EM_FILTER.src = btn.getAttribute('data-src') || '';
      document.querySelectorAll('#view-emojis .chip[data-src]').forEach(function(b){
        b.classList.toggle('on', b === btn);
      });
      renderStickers();
    });
  });

  /* ---------- billing 账单对账 ---------- */
  function _money(v){
    v = Number(v) || 0;
    if(v >= 1) return '¥' + v.toFixed(2);
    return '¥' + v.toFixed(4);
  }
  function _k(n){
    n = Number(n) || 0;
    return (n / 1000).toFixed(1) + 'K';
  }
  function renderBilling(rows){
    var box = $('bl-result'); if(!box) return;
    if(!rows || !rows.length){
      box.innerHTML = '<div class="empty">还没导入账单</div>';
      return;
    }
    box.innerHTML = rows.map(function(r){
      var b = r.bill || {}, l = r.local || {}, g = r.gap || {};
      function cell(label, billV, localV, unit){
        var d = billV - localV;
        var cls = d === 0 ? 'var(--dim2)' : (d > 0 ? 'var(--amber)' : 'var(--cyan)');
        var sign = d > 0 ? '+' : '';
        return '<div class="bl-cell"><i>' + label + '</i>' +
          '<span>' + (billV == null ? '--' : billV.toLocaleString()) + unit + '</span>' +
          '<small style="color:' + cls + '">本地 ' +
            (localV == null ? '--' : localV.toLocaleString()) + unit +
            (d ? ' (' + sign + d.toLocaleString() + ')' : '') +
          '</small></div>';
      }
      var priceCells = '';
      var p = r.prices || {};
      var names = {miss:'未命中', hit_implicit:'隐式命中', hit_explicit:'显式命中', write:'缓存创建', output:'输出'};
      priceCells = Object.keys(names).map(function(k){
        if(p[k] == null) return '';
        return '<div class="bl-cell"><i>单价 · ' + names[k] + '</i>' +
          '<span>' + p[k] + '</span><small>元/百万 token</small></div>';
      }).join('');

      return '<div class="bl-row">' +
        '<div class="bh"><b>' + esc(r.model) + '</b><em>' + esc(r.date || '--') + '</em></div>' +
        '<div class="bl-grid">' +
          cell('输入合计', b.input_total, l.prompt, '') +
          cell('输出', b.output, l.completion, '') +
          '<div class="bl-cell"><i>花费</i><span>' + _money(b.cost) + '</span>' +
            '<small>本地 ' + _money(l.cost) + '</small></div>' +
        '</div>' +
        '<div class="bl-grid">' + priceCells + '</div>' +
        '<div class="bl-reasons">' +
          (r.reasons || []).map(function(x){ return '· ' + esc(x); }).join('<br>') +
        '</div>' +
      '</div>';
    }).join('');
  }

  async function loadBilling(){
    try{
      var d = await api('billing');
      renderBilling(d && d.parsed);
      renderPrices();
    }catch(e){
      var box = $('bl-result'); if(box) box.innerHTML = '<div class="empty">读取失败</div>';
    }
  }
  async function parseBilling(){
    var box = $('bl-raw');
    var txt = box ? box.value : '';
    if(!txt.trim()){ toast('先粘贴账单原文', 'err'); return; }
    if($('bl-msg')) $('bl-msg').textContent = '解析中…';
    try{
      var r = await api('billing', 'POST', { action:'parse', text: txt });
      if(r && r.ok){
        renderBilling(r.parsed);
        if($('bl-msg')) $('bl-msg').textContent = '已解析 ' + (r.parsed||[]).length + ' 条';
        toast('账单已对账', 'ok');
      }else{
        if($('bl-msg')) $('bl-msg').textContent = '解析失败';
        toast((r && r.error) || '解析失败', 'err');
      }
    }catch(e){
      if($('bl-msg')) $('bl-msg').textContent = '失败';
    }
  }

  // 价目表（含 linlong 中转站的模型）。放在账单卡里，跟对账结果一起看更直观。
  var PRICE_TABLE = [];
  function renderPrices(){
    var box = $('bl-prices'); if(!box) return;
    if(!PRICE_TABLE.length){ box.innerHTML = ''; return; }
    box.innerHTML = '<div style="margin:14px 0 8px;padding-top:11px;border-top:1px dashed var(--line2);'
      + 'display:flex;align-items:center;gap:8px">'
      + '<span class="hint" style="font-size:10px;letter-spacing:1px">单价表 · 元 / 百万 TOKEN</span>'
      + '<span class="hint" style="font-size:9.5px;color:var(--dim2)">角标 tok=按量 · 次/张=按次数计费</span></div>'
      + '<div class="price-grid">' + PRICE_TABLE.map(function(p){
          var tag, detail;
          if(p.kind === 'call'){ tag = '次'; detail = '¥' + p.per_call; }
          else if(p.kind === 'image'){ tag = '张'; detail = '¥' + p.per_image; }
          else {
            tag = 'tok';
            detail = '入' + (p.input == null ? '--' : p.input)
                   + ' 出' + (p.output == null ? '--' : p.output)
                   + (p.cached != null ? ' 中' + p.cached : '')
                   + (p.write != null ? ' 建' + p.write : '');
          }
          return '<div class="price-cell"><b>' + esc(p.model) + '</b>'
               + '<span>' + detail + '</span><em>' + tag + '</em></div>';
        }).join('') + '</div>';
  }

  async function pullBilling(){
    if($('bl-msg')) $('bl-msg').textContent = '拉取中…';
    try{
      var r = await api('billing', 'POST', { action:'pull' });
      if(r && r.ok){
        renderBilling(r.parsed);
        if($('bl-msg')) $('bl-msg').textContent = '已从云端拉取（账单 ¥' + (r.total||0).toFixed(4) + '）';
        toast('账单已从云端同步', 'ok');
      }else{
        if($('bl-msg')) $('bl-msg').textContent = (r && r.error) || '拉取失败';
        toast((r && r.error) || '拉取失败', 'err');
      }
    }catch(e){
      if($('bl-msg')) $('bl-msg').textContent = '拉取异常';
    }
  }

  function bindBilling(){
    var pb = $('bl-pull');
    if(pb && !pb._bound){
      pb._bound = true;
      pb.addEventListener('click', pullBilling);
    }
    var btn = $('bl-parse');
    if(btn && !btn._bound){
      btn._bound = true;
      btn.addEventListener('click', parseBilling);
    }
    var clr = $('bl-clear');
    if(clr && !clr._bound){
      clr._bound = true;
      clr.addEventListener('click', async function(){
        await api('billing', 'POST', { action:'clear' });
        if($('bl-raw')) $('bl-raw').value = '';
        if($('bl-msg')) $('bl-msg').textContent = '已清空';
        renderBilling([]);
      });
    }
  }

  function niceAxisMax(v){
    // 轴按真实数据取整，不留「到某个数就封顶」这种东西：
    // 峰值 213 就画到 250，峰值 7 就画到 8 —— 否则所有天都贴在一条线上，图等于没画。
    if(!(v > 0)) return 1;
    var exp = Math.pow(10, Math.floor(Math.log(v) / Math.LN10));
    var unit = exp / 2 || 0.5;
    return Math.max(1, Math.ceil(v / unit) * unit);
  }

  // 折线图按容器实测宽度画，不靠 viewBox 拉伸：拉伸会把点变椭圆、字变扁，
  // 宽屏上还会缩成一小块贴在左上角。窗口变了就重画一次。
  var LC_STATE = null, LC_TIMER = null;
  window.addEventListener('resize', function(){
    if(!LC_STATE) return;
    clearTimeout(LC_TIMER);
    LC_TIMER = setTimeout(function(){
      lineChart(LC_STATE.box, LC_STATE.vals, LC_STATE.labs, LC_STATE.meta);
    }, 160);
  });

  function smoothPath(pts){
    if(!pts.length) return '';
    if(pts.length === 1) return 'M' + pts[0][0].toFixed(1) + ',' + pts[0][1].toFixed(1);
    var d = 'M' + pts[0][0].toFixed(1) + ',' + pts[0][1].toFixed(1);
    for(var i = 0; i < pts.length - 1; i++){
      var p0 = pts[i - 1] || pts[i], p1 = pts[i];
      var p2 = pts[i + 1], p3 = pts[i + 2] || p2;
      var c1x = p1[0] + (p2[0] - p0[0]) / 6;
      var c1y = p1[1] + (p2[1] - p0[1]) / 6;
      var c2x = p2[0] - (p3[0] - p1[0]) / 6;
      var c2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += ' C' + c1x.toFixed(1) + ',' + c1y.toFixed(1)
        + ' ' + c2x.toFixed(1) + ',' + c2y.toFixed(1)
        + ' ' + p2[0].toFixed(1) + ',' + p2[1].toFixed(1);
    }
    return d;
  }

  function lineChart(box, vals, labs, meta){
    var NS = 'http://www.w3.org/2000/svg';
    box.replaceChildren();
    var W = Math.max(320, Math.round(box.clientWidth || 680));
    var H = 236, L = 46, R = 20, T = 36, B = 32;
    var top = niceAxisMax(Math.max.apply(null, vals.concat([1])));
    var innerW = W - L - R, innerH = H - T - B;
    var baseY = T + innerH;
    var gid = 'lcArea-' + (box.id || 'trend');
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.setAttribute('class', 'lc');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', '近七日每天访问次数折线图');
    function mk(name, attrs){
      var n = document.createElementNS(NS, name);
      Object.keys(attrs || {}).forEach(function(k){ n.setAttribute(k, attrs[k]); });
      return n;
    }
    function el(name, attrs, text){
      var n = mk(name, attrs);
      if(text != null) n.textContent = String(text);
      svg.appendChild(n);
      return n;
    }
    var defs = mk('defs');
    var grad = mk('linearGradient', {id: gid, x1: '0', y1: '0', x2: '0', y2: '1'});
    grad.appendChild(mk('stop', {offset: '0', 'stop-color': '#3ee0ff', 'stop-opacity': '.26'}));
    grad.appendChild(mk('stop', {offset: '.65', 'stop-color': '#3ee0ff', 'stop-opacity': '.07'}));
    grad.appendChild(mk('stop', {offset: '1', 'stop-color': '#3ee0ff', 'stop-opacity': '0'}));
    defs.appendChild(grad);
    svg.appendChild(defs);

    for(var t = 0; t <= 4; t++){
      var val = top * t / 4;
      var y = T + innerH - innerH * (t / 4);
      el('line', {x1: L, y1: y, x2: W - R, y2: y, class: 'lc-grid'});
      el('text', {x: L - 10, y: y + 3.5, class: 'lc-y'}, Math.round(val));
    }
    el('text', {x: L - 10, y: T - 12, class: 'lc-unit'}, '次');

    var pts = vals.map(function(v, i){
      var x = vals.length === 1 ? L + innerW / 2
        : L + innerW * (i / (vals.length - 1));
      return [x, T + innerH - innerH * (num(v) / top)];
    });
    var line = smoothPath(pts);
    if(pts.length > 1){
      el('path', {d: line + ' L' + pts[pts.length - 1][0].toFixed(1) + ',' + baseY.toFixed(1)
        + ' L' + pts[0][0].toFixed(1) + ',' + baseY.toFixed(1) + ' Z',
        class: 'lc-area', fill: 'url(#' + gid + ')'});
    }
    el('path', {d: line, class: 'lc-line'});

    var peak = Math.max.apply(null, vals);
    var peakAt = vals.indexOf(peak);
    pts.forEach(function(p, i){
      var x = Math.min(W - R - 1, Math.max(L + 1, p[0]));
      if(i === peakAt && peak > 0){
        el('circle', {cx: p[0].toFixed(1), cy: p[1].toFixed(1), r: 8, class: 'lc-halo'});
      }
      var c = el('circle', {cx: p[0].toFixed(1), cy: p[1].toFixed(1), r: 3.4,
        class: 'lc-dot' + (i === peakAt ? ' lc-dot-peak' : '')});
      // 悬浮就报「哪天几次」，不用再去看表
      var label = document.createElementNS(NS, 'title');
      label.textContent = (labs[i] || '') + '：' + num(vals[i]) + ' 次';
      c.appendChild(label);
      el('text', {x: x.toFixed(1), y: (p[1] - 11).toFixed(1), class: 'lc-v'}, num(vals[i]));
      el('text', {x: x.toFixed(1), y: H - 10, class: 'lc-x'}, labs[i] || '');
    });
    box.appendChild(svg);
    LC_STATE = {box: box, vals: vals, labs: labs, meta: meta};
    if(meta && meta.counts){
      var note = $('vs-note');
      if(note) note.textContent = meta.counts + '。趋势窗口 ' + num(meta.window_days, 7)
        + ' 天，' + (meta.stored || ('明细保留 ' + num(meta.retention_days, 30) + ' 天')) + '。';
    }
  }

  /* ---------- visits ---------- */
  async function loadVisits(){
    var d = await raw('/admin/api/visits');
    if(!d || !d.ok){ toast('访客数据读取失败', 'err'); return; }
    var s = d.stats || {};
    // TOTAL 用后端那个不会随保留期掉下去的累计数，不再是「近 30 天行数」。
    $('vs-total').textContent = num(('lifetime' in s) ? s.lifetime : s.total, 0);
    $('vs-ips').textContent = num(s.unique_ips, 0);
    $('vs-today').textContent = num(s.today, 0);
    $('vs-loc').textContent = num(s.locations, 0);
    var meta = d.meta || {};
    if(meta.lifetime){
      var tf = $('vs-total').parentNode.querySelector('.f');
      if(tf) tf.textContent = meta.lifetime;
    }
    if(meta.unique_ips){
      var f = $('vs-ips');
      if(f && f.parentNode){
        f.parentNode.querySelector('.f').textContent = meta.unique_ips;
      }
      var f2 = $('vs-today');
      if(f2 && f2.parentNode && meta.today) f2.parentNode.querySelector('.f').textContent = meta.today;
    }

    var tr = d.trend || {};
    var vals = (tr.values || []).map(function(v){ return num(v, 0); });
    var labs = tr.labels || [];
    var tb = $('vs-trend');
    if(!vals.length){ tb.replaceChildren(); shadowText(tb, 'div', '暂无趋势数据', 'empty'); }
    else {
      lineChart(tb, vals, labs, meta);
      var peak = num(tr.peak, 0), at = vals.indexOf(peak);
      $('vs-trend-sum').textContent = '近 ' + vals.length + ' 天共 '
        + num(tr.total, 0) + ' 次｜最高 ' + peak + ' 次（'
        + (at >= 0 ? (labs[at] || '?') : '?') + '）｜轴顶 ' + niceAxisMax(peak);
    }

    var rows = d.rows || [];
    var head = '<tr><th>时间</th><th>IP</th><th>路径</th><th>客户端</th><th>位置</th></tr>';
    if(!rows.length){ $('vs-table').innerHTML = head + '<tr><td colspan="5" class="dim">暂无访问记录</td></tr>'; return; }
    $('vs-table').innerHTML = head + rows.map(function(r){
      return '<tr><td>' + esc(r.time) + '</td><td>' + esc(r.ip) + '</td><td>' + esc(r.path) + '</td>' +
             '<td>' + esc(r.ua) + '</td><td>' + esc(r.location || '--') + '</td></tr>';
    }).join('');
  }

  /* ---------- observer read-only ---------- */
  // 服务端门禁才是权威；这里只是让观察员点保存时立刻得到人话提示，
  // 而不是等一个 403 回来再猜。角色从 /admin/api/me 读，横幅由后端同一会话决定。
  var OBSERVER = false;
  var _origFetch = window.fetch;
  window.fetch = function(url, opts){
    var method = String((opts && opts.method) || 'GET').toUpperCase();
    var u = (typeof url === 'string') ? url : ((url && url.url) || '');
    if(OBSERVER && method !== 'GET' && method !== 'HEAD' && u.indexOf('/admin/') === 0){
      toast('观察员账号只读：不能修改面板', 'warn');
      return Promise.resolve(new Response(JSON.stringify({
        ok:false, error:'观察员账号只读，不能修改面板', observer_readonly:true
      }), {status:403, headers:{'Content-Type':'application/json'}}));
    }
    return _origFetch.apply(window, arguments);
  };
  raw('/admin/api/me').then(function(d){
    if(!d || d.role !== 'observer') return;
    OBSERVER = true;
    document.body.classList.add('observer');
    var b = $('observer-banner'); if(b) b.hidden = false;
    var q = $('btn-qr'); if(q) q.style.display = 'none';
  });

  /* ---------- boot ---------- */
  switchView('overview');
  // 清漪页也要自己刷：一轮巡检可能跑几十分钟，开着面板不该等手动刷新才看见结果。
  setInterval(function(){
    if(document.hidden || STATE.view !== 'qingyi') return;
    loadQingyi();
  }, 60000);
  // 轮询：只刷「会变的那几个数字」，不重拉整页数据。
  // 以前每 15 秒重跑一次 loadOverview，弱网下等于每 15 秒下载几十 KB。
  STATE.timer = setInterval(function(){
    if(document.hidden) return;
    if(STATE.view !== 'overview') return;
    // 首屏若被公网网关 502 整片打空（control 与 config 都没拿到），不要停在
    // 「框架加载、数据全空」的死状态：只要回到总览页就自动重拉，边缘恢复即自愈。
    if(!STATE.control && !STATE.cfg){ loadOverview(); return; }
    var ctrl = STATE.control || {};
    var st = ctrl.state || {};
    var fresh = (typeof ctrl.fresh === 'boolean') ? ctrl.fresh : false;
    var dot = $('dot-bot');
    if(dot){ dot.className = 'dot ' + (fresh ? 'on' : 'off'); }
    if($('top-bot')) $('top-bot').textContent = fresh ? 'ONLINE' : 'OFFLINE';
    if($('ov-bot')) $('ov-bot').textContent = fresh ? '在线' : '离线';
    // 健康卡片轻量刷新（1KB 级别）
    loadHealth();
  }, 20000);
})();
