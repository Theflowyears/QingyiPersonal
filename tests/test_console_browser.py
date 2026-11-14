"""真实浏览器里跑这一轮改过的三页：人格（重排）、表情库（并成一张表）、访客（累计口径）。

值得专门跑：这三处全是 DOM 结构与渲染口径的改动，最容易「代码看着对、页面是空的」。
面板以前就出过「永远加载中」—— JS 引用的 id 被删了，抛错以后界面停在占位文案上。

跑法：
    EMOTION_BROWSER_TEST=1 ../venv/Scripts/python.exe -m unittest test_console_browser
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / 'bot', ROOT / 'website'):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

STICKERS = [
    {"id": "aaa111", "tags": ["QQ收藏"], "meaning": "她以为是惊讶", "meaning_src": "vision",
     "note": "从 QQ 收藏搬来的",
     "used": 3, "added": 2, "bytes": 4096, "dims": [120, 120], "kind": "gif",
     "src": "https://p.qpic.cn/from-qq-collection.gif"},
    {"id": "bbb222", "tags": ["群里", "惊讶"], "meaning": "", "meaning_src": "",
     "note": "群里收的",
     "used": 1, "added": 1, "bytes": 2048, "dims": [96, 80], "kind": "gif", "src": ""},
]

ADVANCED_SETTINGS = {
    "BILI_AUTO_MAX_PER_DAY": 5,
    "BILI_AUTO_MIN_GAP_HOURS": 2,
    "CURIOSITY_MAX_PER_DAY": 5,
    "CURIOSITY_SHARE_MAX_PER_DAY": 1,
    "CURIOSITY_SHARE_MIN_INTERVAL_HOURS": 6,
}

MODELS = [
    {"role": "text", "label": "文本对话 · 首选", "name": "text", "base_url": "https://x",
     "model": "qwen3.8-flash", "configured": True, "max_tokens": 2048, "timeout": 120,
     "reasoning_effort": ""},
    {"role": "vision", "label": "视觉识别", "name": "vision", "base_url": "https://x",
     "model": "qwen3.8-flash", "configured": True, "max_tokens": 2048, "timeout": 120,
     "reasoning_effort": ""},
    {"role": "video", "label": "视频理解 · Omni", "name": "video", "base_url": "https://x",
     "model": "qwen3.8-omni-flash", "configured": True, "max_tokens": 2048,
     "timeout": 120, "reasoning_effort": ""},
    {"role": "summary", "label": "记忆摘要", "name": "summary", "base_url": "https://x",
     "model": "qwen3.8-flash", "configured": True, "max_tokens": 2048, "timeout": 120,
     "reasoning_effort": ""},
    {"role": "deep", "label": "深度模型 · 长文", "name": "deep", "base_url": "https://x",
     "model": "gpt-6.1-sol", "configured": True, "max_tokens": 2048, "timeout": 120,
     "reasoning_effort": ""},
    {"role": "sonnet", "label": "深度模型 · SONNET", "name": "sonnet", "base_url": "https://x",
     "model": "claude-sonnet-5-5", "configured": True, "max_tokens": 2048, "timeout": 120,
     "reasoning_effort": ""},
    {"role": "flare", "label": "图像生成 · 首选", "name": "flare", "base_url": "https://x",
     "model": "gpt-image-2.5-flare", "configured": True, "max_tokens": 2048, "timeout": 120,
     "reasoning_effort": ""},
    {"role": "image", "label": "图像生成 · 兜底·Qwen", "name": "image", "base_url": "https://x",
     "model": "qwen-image-3.0", "configured": True, "max_tokens": 2048, "timeout": 120,
     "reasoning_effort": ""},
    {"role": "tts", "label": "语音合成", "name": "tts", "base_url": "https://x",
     "model": "qwen3-tts-flash", "configured": True, "max_tokens": 2048, "timeout": 120,
     "reasoning_effort": ""},
]

ACTIVITY = {
    "ok": True,
    "events": [{"kind": "search", "what": "卫戍协议 多人联机", "out": "记住了",
                "ts": 1762000000, "ref": "bocha", "detail": "明日方舟"}],
    "counts": {"by_kind": {"search": 1, "watch": 2, "share": 0}},
    "curiosity": {"stats": {"topics": 2}, "topics": [
        {"topic": "卫戍协议", "state": "已查", "seed_from": "group_heat", "score": 8.0}]},
    "bili": {"stats": {"mode": "meta"}, "notes": [
        {"title": "一个很长的视频标题用来检查卡片会不会被撑成两层楼", "owner": "UP",
         "duration": 82, "ts": 1762000000, "watch_depth": "视频+字幕", "vision_ok": True,
         "vision_model": "qwen3.8-omni-flash", "has_subtitle": True, "comments_seen": 3,
         "reaction": "看完了，画面里那个瞬间挺有意思的。"},
        {"title": "第二个视频", "owner": "别人", "duration": 31, "ts": 1761990000,
         "watch_depth": "仅字幕", "vision_ok": False, "vision_reason": "额度用完",
         "has_subtitle": True, "comments_seen": 0, "reaction": ""},
    ]},
    "errors": [],
}

SHADOW = {
    "ok": True, "mode": "shadow", "running": True, "expression_control": False,
    "calibration": "collecting", "parameter_hash": "f" * 64,
    "state": {"valence": 0.01, "arousal": 0.3, "control": 0.7, "attention": 0.1},
    "diagnostics": {
        "freshness": {"heartbeat_age_seconds": 5, "stale": False},
        "dimensions": {k: {"implemented": True, "observed": True,
                           "nonzero_events": 1, "positive": 1, "negative": 0,
                           "min": 0.0, "max": 0.04}
                       for k in ("valence", "arousal", "control", "attention")},
        "by_kind": {}, "by_rule": {}, "tools": {},
        "backlog": {"known": True, "pending": 0, "ledger_consistent": True,
                    "oldest_age_seconds": 0},
        "continuity": {"observed_seconds": 90000, "gap_seconds": 0,
                       "open_gap_seconds": 0, "gap_ratio": 0.0,
                       "current_streak_seconds": 100},
        "evidence_replay": {"ok": True, "checked_events": 1,
                            "source_missing": 0, "appraisal_mismatches": 0},
    },
    "calibration_detail": {"checks": [], "passed": 0, "total": 0, "progress": 0,
                           "replay": {"ok": True, "max_error": 0, "events": 1},
                           "thresholds": {}},
    "retrospective": {"mode": "retrospective", "counts_for_live": False,
                      "events": 1, "effective_events": 1, "excluded_events": 0,
                      "day": "2026-11-08", "source_start": 0, "source_end": 1,
                      "rule_version": "v2", "dimensions": {}},
    "timeline": [],
    "valence_samples": {
        "ok": True,
        "stats": {"total": 2, "included": 1, "excluded": 1},
        "rows": [
            {"at": 1762000000, "text": "谢谢你", "included": True,
             "exclusion_reason": "", "appraisal": {"rule": "integrated_praise"},
             "candidate": {"stance": "praise", "confidence": 0.9,
                           "reason": "明确谢她", "model": "fake", "state": "done"}},
            {"at": 1761990000, "text": "今天吃什么", "included": False,
             "exclusion_reason": "semantic_neutral", "appraisal": {"rule": "integrated_neutral"},
             "candidate": {"stance": "neutral", "confidence": 0.8,
                           "reason": "普通聊天", "model": "fake", "state": "done"}},
            {"at": 1761980000, "text": "等待判定", "included": False,
             "exclusion_reason": "semantic_pending", "candidate": {"state": "pending"}},
            {"at": 1761970000, "text": "判定出错", "included": False,
             "exclusion_reason": "semantic_error", "candidate": {"state": "error"}},
        ],
    },
}

QINGYI_OVERVIEW_TASKS = [
    {"id": "A02", "title": "记忆聚合幂等", "stage": "tonight",
     "implementation": "implemented", "engineering": "pass",
     "organic": "not_required", "authorization": "not_required",
     "dev_ready": True, "release_ready": True, "blocked_by": [], "acceptance_requires": []},
    {"id": "R04", "title": "一跳扩散", "stage": "ripple",
     "implementation": "planned", "engineering": "not_verified",
     "organic": "not_required", "authorization": "not_required",
     "dev_ready": False, "release_ready": False, "blocked_by": ["R02"], "acceptance_requires": ["R02"]},
]
QINGYI_STAGES = [{"id": "tonight", "name": "当前可推进", "purpose": "测试"},
                 {"id": "ripple", "name": "Ripple", "purpose": "测试"}]
QINGYI_OVERVIEW = {
    "ok": True, "overview_reads_only": True, "latest_job": None,
    "report": {"schema_version": 1, "catalog_version": "fixture", "run_id": "a" * 32,
               "profile": "evidence", "scope_status": "blocked", "checks": [
                   {"id": "catalog", "status": "pass", "message": "目录完整", "counts": {}},
                   {"id": "ripple", "status": "not_implemented", "message": "未实现", "counts": {}}
               ],
               "stages": QINGYI_STAGES,
               "tasks": QINGYI_OVERVIEW_TASKS,
               "validation": {"ok": True},
               "authorization": {"stage3": False, "expression_control": False}},
    "stages": QINGYI_STAGES,
    "tasks": QINGYI_OVERVIEW_TASKS,
    "validation": {"ok": True},
    "authorization": {"stage3": False, "expression_control": False},
}

VISITS = {
    "ok": True,
    # total 是保留期窗口内的行数，lifetime 才是累计 —— 面板必须显示后者。
    "stats": {"total": 40, "unique_ips": 7, "locations": 0, "today": 5,
              "lifetime": 1234, "lifetime_since": "2026-09-30",
              "lifetime_until": "2026-11-07", "stored": 40},
    "trend": {"values": [3, 5, 8, 4, 9, 6, 12],
              "labels": ["11/01", "11/02", "11/03", "11/04", "11/05", "11/06", "11/07"],
              "dates": ["2026-11-0%d" % (i + 1) for i in range(7)],
              "peak": 12, "total": 47},
    "meta": {"window_days": 7, "retention_days": 30, "dedupe_seconds": 60,
             "counts": "一条 = 一次公开页面的 GET 请求；后台自己、接口、静态资源都不算访客",
             "lifetime": "累计 1234 次，从 2026-09-30 开始记",
             "stored": "明细只留最近 30 天（现在 40 条），累计不受它影响",
             "unique_ips": "近 30 天内出现过的不同 IP 数，不是「人数」",
             "today": "滚动 24 小时，不是自然日",
             "locations": "访客主动共享过位置的记录数"},
    "rows": [{"time": "11-07 10:23:08", "ip": "1.2.3.4", "path": "/",
              "ua": "浏览器", "location": "--"}],
}

DRIVER = r'''<script>
window.addEventListener('load', function(){
  function waitFor(check){ return new Promise(function(resolve,reject){
    var start=Date.now(); var timer=setInterval(function(){
      if(check()){ clearInterval(timer); resolve(); }
      else if(Date.now()-start>9000){clearInterval(timer);reject(new Error('timed out'));}
    },40);
  }); }
  function cells(){ return document.querySelectorAll('#em-stickers .emoji-cell').length; }
  function mark(k){ document.documentElement.dataset[k]='passed'; }
  function hs(sel){ return [].slice.call(document.querySelectorAll(sel))
    .map(function(e){return e.getBoundingClientRect().height;}); }
  function spread(a){ return a.length ? Math.max.apply(null,a)-Math.min.apply(null,a) : 999; }
  async function run(){
    document.querySelector('[data-view="persona"]').click();
    await waitFor(function(){return document.querySelectorAll('#view-persona .grp').length>=4;});
    if(!document.querySelector('#view-persona .grid.g2 .card'))throw new Error('人格页没成组');
    mark('personaGroups');

    // 她自己看过什么：三列列表等高，B站卡片长短内容也不许把高度拉歪
    await waitFor(function(){return document.querySelectorAll('#ex-watch .exv').length>=2;});
    if(spread(hs('#ex-watch .exv'))>1)
      throw new Error('B站卡片高度不一致：' + hs('#ex-watch .exv').join(','));
    if(spread(hs('.ex-col .pm-list.sm'))>1)
      throw new Error('三列列表高度不一致：' + hs('.ex-col .pm-list.sm').join(','));
    mark('exploringEvenHeights');

    mark('qingyiOverview');
    document.querySelector('[data-view="qingyi"]').click();
    await waitFor(function(){return document.getElementById('qingyi-stage-summary').textContent.indexOf('fixture')>=0;});
    if(!document.querySelector('#qingyi-stages .qy-task[data-task-id="A02"]'))
      throw new Error('清漪阶段没按目录渲染');

    // 一键检测：只发一次POST，跟随同一个run轮询到终态，终态后按钮/下载恢复。
    document.getElementById('qingyi-run').click();
    await waitFor(function(){return document.getElementById('qingyi-message').textContent.indexOf('检测运行中')>=0;},
      '未进入运行态');
    await waitFor(function(){return document.getElementById('qingyi-message').textContent.indexOf('blocked')>=0;},
      '轮询没有落到终态');
    if(document.getElementById('qingyi-run').disabled) throw new Error('终态后运行按钮仍禁用');
    if(document.getElementById('qingyi-download').disabled) throw new Error('有报告时下载仍禁用');
    if(!document.querySelector('#qingyi-checks .rd-row')) throw new Error('终态报告没有渲染检查项');
    mark('qingyiRunFlow');

    document.querySelector('[data-qingyi-tab="shadow"]').click();
    await waitFor(function(){return !/等待读取/.test(document.getElementById('emotion-shadow-summary').textContent);});
    if(document.querySelector('#valence-samples-list .cand-row'))
      throw new Error('效价候选详情在展开前就加载了');
    if(document.getElementById('valence-samples-brief').textContent.indexOf('展开后按需')<0)
      throw new Error('影子页没有说明候选详情按需加载');
    document.getElementById('emotion-shadow-toggle').click();
    await waitFor(function(){return document.querySelectorAll('#valence-samples-list .cand-row').length>=2;});
    if(document.getElementById('valence-samples-sum').textContent.indexOf('已判定')<0)
      throw new Error('效价样本没报统计');
    if(document.getElementById('valence-samples-brief').textContent.indexOf('展开后按需')>=0)
      throw new Error('展开后效价摘要没有更新');
    if(document.getElementById('valence-samples-list').getBoundingClientRect().height!==136)
      throw new Error('效价样本列表高度改变');
    if(document.getElementById('valence-cand'))throw new Error('旧候选面板仍存在');
    var sampleText=document.getElementById('valence-samples-list').textContent;
    if(sampleText.indexOf('纳入影子')<0 || sampleText.indexOf('semantic_neutral')<0 || sampleText.indexOf('90%')<0)
      throw new Error('样本判定、置信度或排除原因缺失');
    if(sampleText.indexOf('待标注')<0 || sampleText.indexOf('标注失败')<0)
      throw new Error('未区分待标注与标注失败');
    mark('valenceCandidates');

    document.querySelector('[data-view="emojis"]').click();
    await waitFor(function(){return cells()>0;});
    if(document.getElementById('em-faces'))throw new Error('QQ 收藏还是单独一张表');
    var imgs=[].slice.call(document.querySelectorAll('#em-stickers .emoji-cell img'));
    if(!imgs.length)throw new Error('斗图库没渲染出图');
    if(imgs[0].getAttribute('src').indexOf('/admin/api/bot/sticker/') !== 0)
      throw new Error('图库还在让浏览器直连外部地址：' + imgs[0].getAttribute('src'));
    mark('stickersOneGrid');
    var before=cells();
    document.querySelector('#view-emojis .chip[data-src="QQ收藏"]').click();
    await waitFor(function(){return cells()<before;});
    mark('stickersFilter');

    // 5) 表情卡的悬浮窗：点一张才谈她的理解，改完要标成校准过，删要两步
    document.querySelectorAll('#view-emojis .chip[data-src]')[0].click();
    await waitFor(function(){return cells()>=2;});
    var cell=document.querySelector('#em-stickers .emoji-cell[data-sid="aaa111"]');
    if(!cell)throw new Error('格子上没有 data-sid，点不开窗');
    if(cell.title.indexOf('惊讶')>=0)throw new Error('摘要还铺在格子的 title 里：'+cell.title);
    cell.click();
    await waitFor(function(){return document.getElementById('stk-mask').classList.contains('on');});
    var img=document.getElementById('stk-img');
    if(img.getAttribute('src').indexOf('/admin/api/bot/sticker/aaa111')!==0)
      throw new Error('悬浮窗预览不是走真实那一份：'+img.getAttribute('src'));
    var box=document.getElementById('stk-meaning');
    if(box.value!=='她以为是惊讶')throw new Error('窗里没带出她当前的理解：'+box.value);
    if(document.getElementById('stk-meaning-src').textContent.indexOf('自己看图猜的')<0)
      throw new Error('没说清这条是她猜的还是主人校准的');
    if(document.getElementById('stk-confirm').classList.contains('on'))
      throw new Error('一开窗就把删除确认摊开了');
    mark('stickerPopupOpens');

    box.value='这张是无语，不是惊讶';
    document.getElementById('stk-save').click();
    await waitFor(function(){
      var c=document.querySelector('#em-stickers .emoji-cell[data-sid="aaa111"]');
      return c && c.classList.contains('cal');
    });
    if(document.getElementById('stk-meaning').value!=='这张是无语，不是惊讶')
      throw new Error('保存后窗里的值没跟上');
    if(document.getElementById('em-src-line').textContent.indexOf('你校准过 1 张')<0)
      throw new Error('库顶的口径没报校准张数：'+document.getElementById('em-src-line').textContent);
    mark('stickerCalibrateSaved');

    document.getElementById('stk-del').click();
    await waitFor(function(){return document.getElementById('stk-confirm').classList.contains('on');});
    if(!document.querySelector('#em-stickers .emoji-cell[data-sid="aaa111"]'))
      throw new Error('第一次点删除就真的删了（要两步）');
    document.getElementById('stk-del-yes').click();
    await waitFor(function(){return !document.querySelector('#em-stickers .emoji-cell[data-sid="aaa111"]');});
    if(document.getElementById('stk-mask').classList.contains('on'))
      throw new Error('删完悬浮窗还挂着');
    mark('stickerDeleteTwoStep');

    // 模型矩阵：视频卡必须在，九张卡等高
    document.querySelector('[data-view="models"]').click();
    await waitFor(function(){
      return !!document.querySelector('#md-roles .role-card[data-role="video"]');
    });
    var vc=document.querySelector('#md-roles .role-card[data-role="video"]');
    if(vc.textContent.indexOf('qwen3.8-omni-flash')<0)
      throw new Error('视频卡没显示实际模型：' + vc.textContent);
    if(spread(hs('#md-roles .role-card'))>1)
      throw new Error('模型卡高度不一致：' + hs('#md-roles .role-card').join(','));
    mark('modelVideo');

    document.querySelector('[data-view="visits"]').click();
    await waitFor(function(){return document.getElementById('vs-total').textContent==='1234';});
    var svg=document.querySelector('#vs-trend svg');
    if(!svg)throw new Error('折线图没画出来');
    if(!svg.querySelector('.lc-area'))throw new Error('趋势图没有面积层');
    var h=svg.getBoundingClientRect().height;
    if(!(h>=200 && h<=260))throw new Error('折线图渲染高度=' + h);
    var vb=svg.getAttribute('viewBox').split(' ').map(Number);
    var boxes=[].slice.call(svg.querySelectorAll('.lc-v,.lc-x')).map(function(t){return t.getBBox();});
    var minX=Math.min.apply(null,boxes.map(function(b){return b.x;}));
    var maxX=Math.max.apply(null,boxes.map(function(b){return b.x+b.width;}));
    if(minX<0 || maxX>vb[2])
      throw new Error('折线图文字被裁：' + minX + '~' + maxX + ' / ' + vb[2]);
    document.documentElement.dataset.chartHeight=Math.round(h);
    mark('visitsLifetime');
    mark('visitsChart');

    // 4) 高级设置里的自主次数：值要读得出来，摘要要跟着填
    document.querySelector('[data-view="advanced"]').click();
    await waitFor(function(){return document.getElementById('ad-bili-max').value==='5';});
    if(document.getElementById('ad-cur-max').value !== '5')
      throw new Error('自主搜索额度没显示：' + document.getElementById('ad-cur-max').value);
    if(document.getElementById('ad-share-max').value !== '1')
      throw new Error('主动分享额度没显示');
    var note=document.getElementById('ad-auto-note');
    if(!note || note.textContent.indexOf('刷 5 条视频') < 0)
      throw new Error('自主额度摘要没生成：' + (note ? note.textContent : '无'));
    mark('advancedAutonomy');

    document.querySelector('[data-view="style"]').click();
    await waitFor(function(){return document.getElementById('tool-stats').textContent.indexOf('共 3 次')>=0;});
    if(document.getElementById('tool-stats').textContent.indexOf('未知 2')<0)throw new Error('工具未知被算成成功');
    if(document.getElementById('tool-log').textContent.indexOf('待核验')<0)throw new Error('未知结果仍标成成功');
    mark('toolOutcome');
  }
  run().catch(function(e){document.documentElement.dataset.uiError=e.message;});
});
</script>'''


@unittest.skipUnless(os.getenv('EMOTION_BROWSER_TEST') == '1',
                     'explicit local headless-browser smoke test')
class ConsoleUiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='console_data_')
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {'DATA_DIR': tmp.name,
                                      'PERSONA_DATA_DIR': str(Path(tmp.name) / 'persona')})
        env.start()
        self.addCleanup(env.stop)

    def test_reflowed_panels_render_in_a_real_browser(self):
        from flask import Flask, jsonify, redirect, request, session
        from werkzeug.serving import make_server
        import ceylan_dashboard

        chrome = next((p for p in (shutil.which('google-chrome'),
                                   'C:/Program Files/Google/Chrome/Application/chrome.exe',
                                   'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
                       if p and Path(p).is_file()), None)
        self.assertIsNotNone(chrome, 'Chromium is required for this smoke test')

        with tempfile.TemporaryDirectory(prefix='consoleui_') as tmp:
            app = Flask(__name__, static_folder=None)
            app.secret_key = 'local-ui-test-only'
            # 悬浮窗的两次写操作要真落到这份 fixture 上，否则「保存后格子有没有
            # 变成校准过」测的只是前端自己骗自己。
            POOL = [dict(x) for x in STICKERS]
            HITS = {'meaning': [], 'delete': []}
            QY_RUN = {'posts': 0, 'polls': 0, 'csrf': []}

            def _stats():
                return {"count": len(POOL), "max": 500, "tags": {},
                        "calibrated": len([x for x in POOL
                                           if x.get("meaning_src") == "human"]),
                        "meaning_known": len([x for x in POOL if x.get("meaning")]),
                        "trash": len(HITS["delete"]), "trash_keep": 200}

            @app.before_request
            def fixtures():
                if request.path.startswith('/admin/api/bot/sticker/') and request.method == 'POST':
                    sid = request.path.split('/')[5]
                    if request.headers.get('X-CSRF-Token') != 'fixture-csrf':
                        return jsonify({"error": "安全校验失败，请刷新页面"}), 403
                    if request.path.endswith('/meaning'):
                        body = request.get_json(silent=True) or {}
                        HITS['meaning'].append((sid, body.get('meaning')))
                        for x in POOL:
                            if x['id'] == sid:
                                x['meaning'] = (body.get('meaning') or '').strip()
                                x['meaning_src'] = 'human'
                        return jsonify({"ok": True, "stats": _stats()})
                    if request.path.endswith('/delete'):
                        HITS['delete'].append(sid)
                        POOL[:] = [x for x in POOL if x['id'] != sid]
                        return jsonify({"ok": True, "trashed": "/data/stickers_trash/" + sid,
                                        "stats": _stats()})
                    return jsonify({"error": "unknown verb"}), 404
                if request.path == '/admin/api/bot/effective-config':
                    # 面板拿 CSRF 就靠这个；不给就会测出「写操作没带令牌」
                    return jsonify({"control_csrf": "fixture-csrf", "settings": {}})
                if request.path == '/admin/api/bot/emojis':
                    return jsonify({"emojis": [{"tag": "[微笑]", "face_id": 1}] * 3,
                                    "stickers": POOL,
                                    "stats": _stats()})
                if request.path == '/admin/api/bot/tools':
                    return jsonify({'tools': [], 'agent_ready': True,
                        'stats': {'total': 3, 'ok': 1, 'fail': 0, 'unknown': 2, 'avg_seconds': 1,
                                  'outcomes': {'success': 1, 'failure': 0, 'unknown': 2}},
                        'calls': [{'time': 'now', 'tool': 'tarot', 'ok': True, 'extra': {'outcome': 'unknown'}, 'seconds': 1}]})
                if request.path == '/admin/api/visits':
                    return jsonify(VISITS)
                if request.path == '/admin/api/bot/control':
                    # 高级设置页要读 state.settings 才知道输入框该显示什么值
                    return jsonify({"ok": True, "fresh": True,
                                    "state": {"settings": ADVANCED_SETTINGS,
                                              "settings_revision": "f" * 64,
                                              "groups": []},
                                    "operations": []})
                if request.path == '/admin/api/bot/models-config':
                    return jsonify({"models": MODELS, "providers": {},
                                    "agent": {}, "adapt": {}})
                if request.path == '/admin/api/bot/activity':
                    return jsonify(ACTIVITY)
                if request.path == '/admin/api/bot/qingyi/overview':
                    return jsonify(QINGYI_OVERVIEW)
                if request.path == '/admin/api/bot/qingyi/preflight' and request.method == 'POST':
                    QY_RUN['posts'] += 1
                    QY_RUN['csrf'].append(request.headers.get('X-CSRF-Token'))
                    return jsonify({'ok': True, 'state': 'queued', 'run_id': 'd' * 32,
                                    'authorization_granted': False}), 202
                if request.path == '/admin/api/bot/qingyi/preflight/' + 'd' * 32:
                    QY_RUN['polls'] += 1
                    if QY_RUN['polls'] < 2:
                        return jsonify({'ok': True, 'report': None,
                                        'job': {'run_id': 'd' * 32, 'state': 'running',
                                                'started': 1, 'error': ''}})
                    return jsonify({'ok': True, 'report': QINGYI_OVERVIEW['report'],
                                    'job': {'run_id': 'd' * 32, 'state': 'blocked',
                                            'started': 1, 'error': ''}})
                if request.path == '/admin/api/bot/emotion-shadow':
                    candidate_detail = request.args.get('candidate_detail') == '1'
                    fixture = dict(SHADOW)
                    fixture['candidate_detail'] = candidate_detail
                    fixture['valence_samples'] = SHADOW['valence_samples'] if candidate_detail else None
                    fixture['valence_candidates'] = (
                        {'ok': True, 'stats': {'pending': 1, 'done': 2, 'error': 1,
                                               'hit': 1, 'missed': 0, 'mismatch': 0},
                         'rows': []}
                        if candidate_detail else {'ok': False, 'status': 'not_loaded'})
                    return jsonify(fixture)
                if request.path.startswith('/admin/api/'):
                    return jsonify({})

            @app.get('/local-ui-fixture')
            def login():
                session['ceylan_admin'] = True
                return redirect('/admin/dashboard')

            ceylan_dashboard.install(app, {})

            @app.after_request
            def inject(resp):
                if request.path == '/admin/dashboard':
                    resp.set_data(resp.get_data(as_text=True).replace(
                        '</body>', DRIVER + '</body>'))
                return resp

            server = make_server('127.0.0.1', 0, app, threaded=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = subprocess.run(
                    [chrome, '--headless=new', '--disable-gpu', '--disable-extensions',
                     '--no-first-run', '--no-default-browser-check',
                     '--user-data-dir=' + str(Path(tmp) / 'profile'),
                     '--window-size=1600,1000',
                     '--virtual-time-budget=20000', '--dump-dom',
                     'http://127.0.0.1:%s/local-ui-fixture' % server.server_port],
                    capture_output=True, text=True, encoding='utf-8',
                    errors='replace', timeout=90)
                html = result.stdout
                for expected in ('data-persona-groups="passed"',
                                 'data-exploring-even-heights="passed"',
                                 'data-qingyi-overview="passed"',
                                 'data-qingyi-run-flow="passed"',
                                 'data-valence-candidates="passed"',
                                 'data-stickers-one-grid="passed"',
                                 'data-stickers-filter="passed"',
                                 'data-sticker-popup-opens="passed"',
                                 'data-sticker-calibrate-saved="passed"',
                                 'data-sticker-delete-two-step="passed"',
                                 'data-model-video="passed"',
                                 'data-visits-lifetime="passed"',
                                 'data-visits-chart="passed"',
                                 'data-advanced-autonomy="passed"',
                                 'data-tool-outcome="passed"'):
                    self.assertIn(expected, html,
                                  (html[-3000:] if html else '')
                                  + "\n" + result.stderr[-1500:])
                self.assertNotIn('data-ui-error', html,
                                 '浏览器里报错了：%s' % _attr(html, 'data-ui-error'))
                self.assertTrue(_attr(html, 'data-chart-height'),
                                "折线图渲染高度没报回来")
                self.assertEqual(1, QY_RUN['posts'], '一键检测应只发一次POST')
                self.assertEqual(['fixture-csrf'], QY_RUN['csrf'], '检测POST缺CSRF')
                self.assertGreaterEqual(QY_RUN['polls'], 2, '没有轮询到终态')
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_observer_banner_shows_and_writes_never_leave_the_browser(self):
        from flask import Flask, jsonify, redirect, request, session
        from werkzeug.serving import make_server
        import ceylan_dashboard

        chrome = next((p for p in (shutil.which('google-chrome'),
                                   'C:/Program Files/Google/Chrome/Application/chrome.exe',
                                   'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
                       if p and Path(p).is_file()), None)
        self.assertIsNotNone(chrome, 'Chromium is required for this smoke test')

        with tempfile.TemporaryDirectory(prefix='observerui_') as tmp:
            app = Flask(__name__, static_folder=None)
            app.secret_key = 'local-ui-test-only'
            POSTS = []

            @app.before_request
            def fixtures():
                if request.method != 'GET' and request.path.startswith('/admin/api/'):
                    POSTS.append(request.path)
                    return jsonify({"ok": True})
                if request.path == '/admin/api/me':
                    return jsonify({"ok": True, "role": "observer", "username": "watcher"})
                if request.path == '/admin/api/bot/effective-config':
                    return jsonify({"control_csrf": "fixture-csrf", "settings": {}})
                if request.path == '/admin/api/bot/control':
                    return jsonify({"ok": True, "fresh": True,
                                    "state": {"settings": ADVANCED_SETTINGS,
                                              "settings_revision": "f" * 64,
                                              "groups": []},
                                    "operations": []})
                if request.path.startswith('/admin/api/'):
                    return jsonify({})

            @app.get('/local-ui-fixture')
            def login():
                session['ceylan_admin'] = True
                session['ceylan_role'] = 'observer'
                return redirect('/admin/dashboard')

            ceylan_dashboard.install(app, {})

            driver = '''<script>
(function(){
  function waitFor(cond){return new Promise(function(resolve,reject){
    var start=Date.now();var timer=setInterval(function(){
      try{if(cond()){clearInterval(timer);resolve();return;}}catch(e){}
      if(Date.now()-start>9000){clearInterval(timer);reject(new Error('timed out'));}
    },40);} );}
  function mark(k){document.documentElement.dataset[k]='passed';}
  async function run(){
    await waitFor(function(){var b=document.getElementById('observer-banner');
      return b && !b.hidden;});
    if(!document.body.classList.contains('observer'))throw new Error('body 没标 observer');
    if(document.getElementById('btn-qr').style.display!=='none')
      throw new Error('观察员还能看到扫码登录按钮');
    mark('observerBanner');

    // 真的去点「立即检查」（它会 POST health）：前端必须拦下来并说人话
    document.getElementById('hd-recheck').click();
    await waitFor(function(){
      return [].some.call(document.querySelectorAll('#toast .toast'),function(t){
        return t.textContent.indexOf('观察员账号只读')>=0;});});
    mark('observerToast');

    var r=await fetch('/admin/api/bot/personality',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'x'})});
    if(r.status!==403)throw new Error('拦截没返回 403：'+r.status);
    var d=await r.json();
    if(!d.observer_readonly)throw new Error('拦截响应没带只读标记');
    mark('observerFetchBlocked');
  }
  run().catch(function(e){document.documentElement.dataset.uiError=e.message;});
})();
</script>'''

            @app.after_request
            def inject(resp):
                if request.path == '/admin/dashboard':
                    resp.set_data(resp.get_data(as_text=True).replace(
                        '</body>', driver + '</body>'))
                return resp

            server = make_server('127.0.0.1', 0, app, threaded=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = subprocess.run(
                    [chrome, '--headless=new', '--disable-gpu', '--disable-extensions',
                     '--no-first-run', '--no-default-browser-check',
                     '--user-data-dir=' + str(Path(tmp) / 'profile'),
                     '--window-size=1600,1000',
                     '--virtual-time-budget=20000', '--dump-dom',
                     'http://127.0.0.1:%s/local-ui-fixture' % server.server_port],
                    capture_output=True, text=True, encoding='utf-8',
                    errors='replace', timeout=90)
                html = result.stdout
                for expected in ('data-observer-banner="passed"',
                                 'data-observer-toast="passed"',
                                 'data-observer-fetch-blocked="passed"'):
                    self.assertIn(expected, html,
                                  (html[-3000:] if html else '')
                                  + "\n" + result.stderr[-1500:])
                self.assertNotIn('data-ui-error', html,
                                 '浏览器里报错了：%s' % _attr(html, 'data-ui-error'))
                self.assertEqual(POSTS, [], '观察员的写请求不该到达服务器：%s' % POSTS)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


@unittest.skipUnless(os.getenv('EMOTION_BROWSER_TEST') == '1',
                     'explicit local headless-browser regression tests')
class ConsoleRequestTests(unittest.TestCase):
    def run_browser(self, case, assertions):
        """Faults live at the HTTP boundary; console.js, fetch and DOM stay real."""
        from flask import Flask, Response, g, jsonify, redirect, request, session
        from werkzeug.serving import make_server
        import ceylan_dashboard

        chrome = next((p for p in (shutil.which('google-chrome'),
                                   'C:/Program Files/Google/Chrome/Application/chrome.exe',
                                   'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
                       if p and Path(p).is_file()), None)
        self.assertIsNotNone(chrome, 'Chromium is required for explicit browser tests')
        with tempfile.TemporaryDirectory(prefix='console_requests_') as tmp, patch.dict(
                os.environ, {'DATA_DIR': tmp, 'PERSONA_DATA_DIR': str(Path(tmp) / 'persona')}):
            app = Flask(__name__, static_folder=None)
            app.secret_key = 'local-request-test-only'
            phase = {'name': 'base'}
            hits, report = {}, {}
            lock = threading.Lock()
            release, started, reported = threading.Event(), threading.Event(), threading.Event()
            overview_paths = ('control', 'effective-config', 'play', '/api/status')

            @app.before_request
            def fixtures():
                path = request.path
                if not (path.startswith('/admin/api/') or path == '/api/status'):
                    return None
                key = path.rsplit('/', 1)[-1] if path != '/api/status' else path
                with lock:
                    current = phase['name']
                    g.fixture_phase = current
                    counter = (current, key, request.method)
                    hits[counter] = hits.get(counter, 0) + 1
                    count = hits[counter]
                    if current == 'a' and all(hits.get(('a', p, 'GET')) for p in overview_paths):
                        started.set()
                gateway = lambda status: Response('<h1>local gateway failure</h1>', status=status,
                                                   content_type='text/html')
                if case == 'transient' and key == 'emojis' and count == 1:
                    return gateway(502)
                if case.startswith('raw-transient-') and key == 'visits' and count == 1:
                    return gateway(int(case.rsplit('-', 1)[-1]))
                if case == 'flaky3' and key == 'visits' and count <= 2:
                    # 公网 Cloudflare 边缘约 30%/请求随机 502：连前两次也一起失败。
                    return gateway(502)
                if case == 'emoji-exhausted' and key == 'emojis' and current == 'base':
                    return gateway(502)
                if case == 'shadow-error' and key == 'emotion-shadow':
                    return gateway(502)
                if case == 'write-error' and request.method != 'GET':
                    return gateway(503)
                if case == 'write-slow' and request.method == 'POST':
                    release.wait(9)
                    return jsonify(ok=True, text='工具真实返回', outcome='success', seconds=9,
                                   data={'checklist': []})
                if case == 'partial-error' and current == 'b' and key in ('control', 'effective-config', '/api/status'):
                    return jsonify(ok=False, error='local unavailable'), 503
                stalled = current == 'b' and (
                    (case in ('play-headers', 'play-body') and key == 'play') or
                    (case == 'status-body' and key == '/api/status') or
                    (case == 'write-body' and request.method == 'POST'))
                if stalled:
                    if case == 'play-headers':
                        release.wait(30)
                        return jsonify({})
                    def body():
                        yield '{"ok":'
                        release.wait(30)
                        yield 'true}'
                    return Response(body(), content_type='application/json')
                newer = current == 'b'
                if key == 'control':
                    return jsonify(ok=True, fresh=True, state={
                        'groups': [{'group_id': str(i)} for i in range(2 if newer else 1)],
                        'settings': {'reply_chars': 88 if newer else 45}}, operations=[{
                            'id': 'new-B' if newer else 'old-A', 'action': 'new-B' if newer else 'old-A',
                            'created': 1762000000, 'status': 'done'}])
                if key == 'effective-config':
                    return jsonify(control_csrf='fixture-csrf', fresh=True, age_sec=2,
                                   settings={}, persona={'mode': 'new-B' if newer else 'old-A'})
                if key == 'play':
                    return jsonify(state={'today': {'messages': 22 if newer else 11,
                                                    'private_messages': 3, 'sent_replies': 4}})
                if key == '/api/status':
                    return jsonify(uptime=10800 if newer else 7200, cpu=12, memory=34,
                                   disk=56, services={})
                if key == 'emojis':
                    # No external built-in image URLs are needed by these regressions.
                    return jsonify(emojis=[], stickers=STICKERS,
                                   stats={'count': 2, 'max': 500, 'tags': {}, 'trash': 0})
                if key == 'visits':
                    return jsonify(VISITS)
                if path.startswith('/admin/api/bot/sticker/'):
                    return Response(status=204)
                if key == 'emotion-shadow':
                    return jsonify(SHADOW)
                return jsonify({})

            @app.get('/local-ui-fixture')
            def login():
                session['ceylan_admin'] = True
                return redirect('/admin/dashboard')

            @app.post('/local-phase/<name>')
            def change_phase(name):
                phase['name'] = name
                return jsonify(ok=True)

            @app.get('/local-started')
            def a_started():
                return jsonify(ok=started.wait(5))

            @app.post('/local-release')
            def release_a():
                release.set()
                return jsonify(ok=True)

            @app.post('/local-report')
            def browser_report():
                report.update(request.get_json())
                reported.set()
                return jsonify(ok=True)

            ceylan_dashboard.install(app, {})
            driver = r'''<script>
window.addEventListener('load', async function(){
  function text(id){return document.getElementById(id).textContent;}
  function assert(ok, why){if(!ok)throw new Error(why);}
  function click(view){document.querySelector('[data-view="'+view+'"]').click();}
  function refresh(){document.getElementById('btn-refresh').click();}
  function waitFor(check, why){return new Promise(function(resolve,reject){
    var start=performance.now(); var timer=setInterval(function(){
      if(check()){clearInterval(timer);resolve();}
      else if(performance.now()-start>11000){clearInterval(timer);reject(new Error(why));}
    },40);
  });}
  async function phase(name){await fetch('/local-phase/'+name,{method:'POST'});}
  try {
    await waitFor(function(){return text('ov-msgs')==='11';}, 'initial overview did not load');
    ASSERTIONS
    await fetch('/local-report',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ok:true})});
  } catch(e){await fetch('/local-report',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({error:e.message})});}
});
</script>'''.replace('ASSERTIONS', assertions)

            @app.after_request
            def inject(resp):
                if request.path == '/admin/dashboard':
                    resp.set_data(resp.get_data(as_text=True).replace('</body>', driver + '</body>'))
                if getattr(g, 'fixture_phase', None) == 'a' and (
                        request.path.rsplit('/', 1)[-1] in overview_paths or request.path == '/api/status'):
                    data = resp.get_data()
                    def delayed_snapshot():
                        # Send no-store headers first: otherwise Chromium's cache lock
                        # serializes identical GETs and B cannot overtake A at all.
                        yield b' '
                        release.wait(30)
                        yield data
                    resp = Response(delayed_snapshot(), content_type='application/json')
                resp.headers['Cache-Control'] = 'no-store'
                return resp

            server = make_server('127.0.0.1', 0, app, threaded=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            process = None
            try:
                # Real wall-clock timers: virtual-time-budget can mask a fetch/body stall.
                process = subprocess.Popen([
                    chrome, '--headless=new', '--disable-gpu', '--disable-extensions',
                    '--disable-background-networking', '--no-first-run', '--no-default-browser-check',
                    '--user-data-dir=' + str(Path(tmp) / 'profile'), '--window-size=1600,1000',
                    '--remote-debugging-port=0',
                    'http://127.0.0.1:%s/local-ui-fixture' % server.server_port],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.assertTrue(reported.wait(25), 'browser did not report: %s; hits=%s' % (case, hits))
                self.assertNotIn('error', report, '%s: %s; hits=%s' % (case, report, hits))
                self.assertTrue(report.get('ok'), report)
                return dict(hits)
            finally:
                release.set()
                if process is not None:
                    process.terminate()
                    process.wait(timeout=15)
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_transient_html_502_recovers_without_a_second_click(self):
        # Removing safe GET retry leaves the real sticker grid empty.
        hits = self.run_browser('transient', r'''
    click('emojis');
    await waitFor(function(){return document.querySelectorAll('#em-stickers .emoji-cell').length===2;},
      'HTML 502 did not recover to the sticker grid');
''')
        self.assertEqual(hits.get(('base', 'emojis', 'GET')), 2)

    def test_raw_get_recovers_from_transient_html_503_and_504(self):
        # raw() must use the same bounded, safe retry as api().
        for status in (503, 504):
            with self.subTest(status=status):
                hits = self.run_browser('raw-transient-%s' % status, r'''
    click('visits');
    await waitFor(function(){return text('vs-total')==='1234';}, 'raw GET did not recover');
''')
                self.assertEqual(hits.get(('base', 'visits', 'GET')), 2)

    def test_failed_emojis_are_not_cached_as_an_empty_library(self):
        # Setting emojisLoaded on failure prevents a later navigation from recovering.
        hits = self.run_browser('emoji-exhausted', r'''
    click('emojis');
    await waitFor(function(){return !text('em-stickers').includes('加载中');},
      'failed library never settled');
    var failedText=text('em-stickers');
    await phase('b'); click('overview'); click('emojis');
    await waitFor(function(){return document.querySelectorAll('#em-stickers .emoji-cell').length===2;},
      'failed emoji load was cached permanently');
    assert(/失败|不可读/.test(failedText), 'failed library was shown as empty instead of retryable');
''')
        self.assertEqual(hits.get(('base', 'emojis', 'GET')), 3, 'bounded retry: at most two automatic retries')
        self.assertEqual(hits.get(('b', 'emojis', 'GET')), 1)

    def test_recovers_from_three_consecutive_gateway_502s(self):
        # The public edge 502s ~30% of requests; two attempts left ~9% of reads
        # permanently blank, which is exactly the "frame but no data" symptom.
        hits = self.run_browser('flaky3', r'''
    click('visits');
    await waitFor(function(){return text('vs-total')==='1234';},
      'three-attempt retry did not recover the visits panel');
''')
        self.assertEqual(hits.get(('base', 'visits', 'GET')), 3)

    def test_stalled_headers_or_body_finish_and_keep_previous_values(self):
        # A timer cleared at response headers misses the two body-stall cases.
        for case in ('play-headers', 'play-body', 'status-body'):
            with self.subTest(case=case):
                hits = self.run_browser(case, r'''
    var oldResource=document.getElementById('ov-res').innerHTML;
    await phase('b'); var start=performance.now(); refresh();
    await waitFor(function(){return text('ov-groups')==='2';},
      'one stalled request blocked the overview beyond 11 seconds');
    assert(performance.now()-start<11000, 'overview exceeded its time bound');
    if('CASE'==='status-body'){
      assert(text('ov-msgs')==='22', 'healthy play response did not update');
      assert(text('ov-uptime')==='2.0h', 'failed status reset uptime');
      assert(document.getElementById('ov-res').innerHTML===oldResource, 'failed status erased resources');
    } else {
      assert(text('ov-msgs')==='11', 'failed play reset previous message count');
      assert(text('ov-msgs-f')==='私聊 3 · 发送 4', 'failed play erased previous breakdown');
      assert(text('ov-uptime')==='3.0h', 'healthy status response did not update');
    }
'''.replace('CASE', case))
                key = '/api/status' if case == 'status-body' else 'play'
                self.assertEqual(hits.get(('b', key, 'GET')), 1, 'timeouts must not be replayed')

    def test_failed_overview_reads_do_not_erase_existing_control_or_config(self):
        # Treating HTTP-error JSON as successful data erases known groups/settings.
        hits = self.run_browser('partial-error', r'''
    var oldNode=document.getElementById('ov-node').innerHTML;
    var oldOps=document.getElementById('ov-ops').innerHTML;
    await phase('b'); refresh();
    await waitFor(function(){return text('ov-msgs')==='22';}, 'healthy play did not render');
    assert(text('ov-groups')==='1', 'failed control erased groups');
    assert(text('ov-bot')==='在线', 'failed reads invented offline status');
    assert(document.getElementById('ov-node').innerHTML===oldNode, 'failed config erased node settings');
    assert(document.getElementById('ov-ops').innerHTML===oldOps, 'failed control erased operations');
    assert(text('ov-uptime')==='2.0h', 'failed status erased uptime');
''')
        for key in ('control', 'effective-config', '/api/status'):
            self.assertEqual(hits.get(('b', key, 'GET')), 3)

    def test_shadow_failure_ends_loading_in_both_sample_summaries(self):
        # The main failure branch used to leave the sample summaries loading forever.
        self.run_browser('shadow-error', r'''
    click('qingyi');
    await waitFor(function(){return document.getElementById('qingyi-message').textContent.length>0;},
      'qingyi overview did not settle');
    document.querySelector('[data-qingyi-tab="shadow"]').click();
    await waitFor(function(){return text('emotion-shadow-summary').includes('暂不可读');},
      'shadow error did not settle');
    ['valence-samples-brief','valence-samples-sum'].forEach(function(id){
      assert(!text(id).includes('加载中'), id+' still loading after failure');
      assert(/不可读|失败/.test(text(id)), id+' has no error state');
    });
    assert(!document.querySelector('#valence-samples-list .cand-row'), 'failed shadow kept stale samples');
''')

    def test_older_overview_a_cannot_overwrite_newer_refresh_b(self):
        # Without an overview generation check, released A replaces B's DOM/state.
        self.run_browser('out-of-order', r'''
    await phase('a'); refresh();
    var started=await (await fetch('/local-started')).json();
    assert(started.ok, 'A did not start all four reads');
    await phase('b'); refresh();
    await waitFor(function(){return text('ov-msgs')==='22';}, 'B did not render before A');
    await fetch('/local-release',{method:'POST'});
    await waitFor(function(){return performance.getEntriesByType('resource').filter(function(e){
      var p=new URL(e.name).pathname;
      return ['/admin/api/bot/control','/admin/api/bot/effective-config','/admin/api/bot/play','/api/status'].includes(p);
    }).length>=12;}, 'A responses did not finish');
    await new Promise(function(resolve){requestAnimationFrame(function(){requestAnimationFrame(resolve);});});
    assert(text('ov-msgs')==='22', 'old A overwrote B messages: '+text('ov-msgs'));
    assert(text('ov-groups')==='2', 'old A overwrote B groups');
    assert(text('ov-uptime')==='3.0h', 'old A overwrote B uptime');
    assert(text('ov-node').includes('new-B') && text('ov-node').includes('88 字'), 'old A overwrote B config');
    assert(text('ov-ops').includes('new-B'), 'old A overwrote B operations');
''')

    def test_write_errors_are_never_automatically_retried(self):
        # Reusing GET retry logic for POST can execute a write twice.
        hits = self.run_browser('write-error', r'''
    document.getElementById('hd-recheck').click();
    await waitFor(function(){return text('hd-msg')==='检查失败';}, 'failed write was reported as checked');
''')
        self.assertEqual(hits.get(('base', 'health', 'POST')), 1)

    def test_slow_tool_write_keeps_waiting_after_the_read_deadline(self):
        hits = self.run_browser('write-slow', r'''
    click('style');
    document.getElementById('tool-pick').innerHTML='<option value="tarot">tarot</option>';
    document.getElementById('tool-run').click();
    await waitFor(function(){return text('tool-out')==='工具真实返回';},
      'slow write was aborted at the GET deadline');
    assert(text('tool-msg').includes('已核验完成'), 'tool success was reported as failure');
''')
        self.assertEqual(hits.get(('base', 'test', 'POST')), 1)


def _attr(html, name):
    import re
    m = re.search(r'%s="([^"]*)"' % re.escape(name), html or '')
    return m.group(1) if m else None


if __name__ == '__main__':
    unittest.main()
