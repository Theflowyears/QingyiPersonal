"""
Dashboard extension - Unified UI with personality editor, model config, emoji picker
"""
import os
import json
import re
import time
import hmac
import importlib
import secrets
from pathlib import Path
from flask import render_template_string, jsonify, request, session, send_file, Response


DASHBOARD_TEMPLATE_PATH = Path(__file__).parent / "console.html"


def install(app, namespace):
    """Install dashboard routes into the Flask app."""
    
    @app.route("/admin/dashboard")
    def dashboard():
        if not session.get("ceylan_admin"):
            return app.redirect("/admin/login")
        
        template = DASHBOARD_TEMPLATE_PATH.read_text(encoding="utf-8")
        # 填静态资源版本号：文件改动后 URL 变化，浏览器不会用旧缓存
        template = template.replace("{{ASSET_VER}}", _asset_version())
        return template
    
    @app.route("/")
    def index_redirect():
        """Redirect root to dashboard."""
        return app.redirect("/admin/dashboard")
    
    @app.route("/admin/api/bot/personality", methods=["GET", "POST"])
    def api_personality():
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        
        persona_file = Path(os.getenv("DATA_DIR", "./data")) / "persona.json"
        
        if request.method == "GET":
            # Load current personality
            defaults = {
                "name": os.getenv("BOT_PERSONA_NAME", "小助手"),
                "persona": os.getenv("BOT_PERSONA", "你是群聊里的 AI 助手。说话自然、友善、简洁，不暴露内部实现。"),
                "trigger_words": os.getenv("BOT_TRIGGER_WORDS", "小助手,助手"),
            }
            if persona_file.exists():
                try:
                    saved = json.loads(persona_file.read_text(encoding="utf-8"))
                    defaults.update(saved)
                except Exception:
                    pass
            return jsonify(defaults)
        
        # POST - save personality
        data = request.get_json(silent=True) or {}
        persona_file.parent.mkdir(parents=True, exist_ok=True)
        persona_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        # persona.json 历史上没人读，光写这里等于没保存。
        # 触发词要同时写进 persona_policy.json（bot 热加载）和 config.env（重启生效），
        # 否则面板显示和实际生效永远是两回事。
        try:
            raw = data.get("trigger_words")
            words = []
            if isinstance(raw, str):
                words = [x.strip() for x in raw.replace("，", ",").split(",")]
            elif isinstance(raw, (list, tuple)):
                for x in raw:
                    words.extend(str(x).replace("，", ",").split(","))
                words = [x.strip() for x in words]
            words = [w for w in words if w][:12]

            if words:
                pol = Path(os.getenv("DATA_DIR", "./data")) / "persona_policy.json"
                cur = {}
                if pol.exists():
                    try:
                        cur = json.loads(pol.read_text(encoding="utf-8"))
                    except Exception:
                        cur = {}
                cur["triggers"] = words
                if data.get("name"):
                    cur["display_name"] = str(data["name"])[:60]
                pol.parent.mkdir(parents=True, exist_ok=True)
                pol.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                               encoding="utf-8")

                # config.env：走 config_store（原子替换 + 代次可回滚 + 归一重复键）。
                # 以前这里自己 re.sub(count=1) 只改第一处，而 launcher 逐行赋值
                # 是「后出现的赢」—— 同一个键写过两次的话，面板改完重启就被盖回去。
                try:
                    import config_store as _cs
                    _cs.set_values({"BOT_TRIGGER_WORDS": ",".join(words)},
                                   reason="面板改触发词")
                except Exception as _ce:
                    print("[面板] 写 config.env 失败 " + str(_ce)[:60], flush=True)
                print("[面板] 触发词已保存: " + ",".join(words), flush=True)

                # 本进程立刻生效
                try:
                    import sys as _sys
                    _bd = str(Path(__file__).parent.parent / "bot")
                    if _bd not in _sys.path:
                        _sys.path.insert(0, _bd)
                    import project_config as _pcx
                    _pcx.refresh_from_policy()
                except Exception:
                    pass
        except Exception as _e:
            print("[面板] 触发词保存失败 " + str(_e)[:80], flush=True)
            return jsonify({"ok": False, "error": "触发词保存失败: " + str(_e)[:80]}), 500

        return jsonify({"ok": True, "message": "性格设定已保存"})
    
    @app.route("/admin/api/bot/models-config", methods=["GET", "POST"])
    def api_models_config():
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        
        models_file = Path(os.getenv("DATA_DIR", "./data")) / "models_config.json"
        
        if request.method == "GET":
            # Return current model configuration from env
            models = []
            try:
                import sys as _s2
                _bd = str(Path(__file__).parent.parent / "bot")
                if _bd not in _s2.path:
                    _s2.path.insert(0, _bd)
                import llm_router as _lr
            except Exception as _ae:
                _lr = None
                adapt = {"error": str(_ae)[:80]}
            else:
                try:
                    adapt = _lr.adapt_state()
                except Exception as _ae:
                    adapt = {"error": str(_ae)[:80]}

            # 九类模型：文本 / 视觉 / 视频 / 摘要 / 生图 / 语音
            ROLE_SPECS = [
                ("text", "LLM_TEXT_", "文本对话 · 首选", "qwen3.8-flash"),
                ("vision", "LLM_VISION_", "视觉识别", "qwen3.8-flash"),
                ("video", "LLM_VIDEO_", "视频理解 · Omni", "qwen3.8-omni-flash"),
                ("summary", "LLM_SUMMARY_", "记忆摘要", "qwen3.8-flash"),
                ("deep", "LLM_DEEP_", "深度模型 · 长文", "gpt-6.1-sol"),
                ("sonnet", "LLM_SOL_", "深度模型 · SONNET", "claude-sonnet-5-5"),
                ("flare", "FLARE_", "图像生成 · 首选", "gpt-image-2.5-flare"),
                ("image", "QWENIMG_", "图像生成 · 兜底·Qwen", "qwen-image-3.0"),
                ("tts", "TTS_", "语音合成", "qwen3-tts-flash"),
            ]
            for role, prefix, label, default_model in ROLE_SPECS:
                base_url = os.getenv(f"{prefix}BASE_URL", "") or os.getenv(f"{prefix}URL", "")
                model = os.getenv(f"{prefix}MODEL", default_model)
                name = os.getenv(f"{prefix}NAME", label)
                key = os.getenv(f"{prefix}API_KEY", "") or os.getenv("LLM_TEXT_API_KEY", "")
                maxtok = int(os.getenv(f"{prefix}MAX_TOKENS", "2048"))
                timeout = int(os.getenv(f"{prefix}TIMEOUT", "120"))
                effort = os.getenv(f"{prefix}REASONING_EFFORT", "").strip()
                if role == "video":
                    # 没配 LLM_VIDEO_* 时，真正生效的是「视觉通道 + omni」；
                    # 面板必须显示实际那一份，不能显示空白让人以为没配。
                    try:
                        vm = _lr.get_video_model() if _lr else None
                    except Exception:
                        vm = None
                    if vm is not None:
                        base_url = vm.base_url or base_url
                        model = vm.model or model
                        key = vm.api_key or key
                        maxtok = int(vm.max_tokens or maxtok)
                        timeout = int(vm.timeout or timeout)
                        effort = vm.reasoning_effort or effort
                    else:
                        key = ""
                configured = bool(model and key)
                models.append({
                    "role": role,
                    "label": label,
                    "name": name,
                    "base_url": base_url,
                    "model": model,
                    "configured": configured,
                    "max_tokens": maxtok,
                    "timeout": timeout,
                    "reasoning_effort": effort,
                })

            # Also load saved overrides
            if models_file.exists():
                try:
                    saved = json.loads(models_file.read_text(encoding="utf-8"))
                    for m in models:
                        # video 的生效值已经由 llm_router 算过（含视觉通道兜底），
                        # 再用原始覆盖层糊一遍会把地址/Key 变回空白。
                        if m["role"] == "video":
                            if m["role"] in saved:
                                m["has_saved_key"] = bool(saved[m["role"]].get("api_key"))
                            continue
                        if m["role"] in saved:
                            override = saved[m["role"]]
                            m.update({k: v for k, v in override.items() if k != "api_key"})
                            m["has_saved_key"] = bool(override.get("api_key"))
                except Exception:
                    pass
            
            # 生成通道：直连模型 or 云端智能体
            providers = {"image": "builtin", "tts": "builtin", "tts_sing": "builtin"}
            agent_cfg = {}
            if models_file.exists():
                try:
                    saved_all = json.loads(models_file.read_text(encoding="utf-8"))
                    for k in ("image", "tts", "tts_sing"):
                        v = str(((saved_all.get("providers") or {}).get(k)) or "").strip().lower()
                        if v in ("builtin", "agent"):
                            providers[k] = v
                    agent_cfg = saved_all.get("agent") or {}
                except Exception:
                    pass

            agent_status = {
                "enabled": True,
                "agent_id": str(agent_cfg.get("agent_id") or os.getenv("QWEN_AGENT_ID", "")),
                "base": str(agent_cfg.get("base") or os.getenv("QWEN_AGENT_BASE", "https://maas.qianwenaiapi.com/api/v1/agentstudio")),
                "has_key": bool(agent_cfg.get("api_key") or os.getenv("QWEN_AGENT_API_KEY") or os.getenv("LLM_TEXT_API_KEY")),
            }
            agent_status["ready"] = bool(agent_status["agent_id"] and agent_status["has_key"])
            return jsonify({"models": models, "providers": providers, "agent": agent_status, "adapt": adapt})
        
        # POST - save model config
        data = request.get_json(silent=True) or {}

        # 生成通道切换 / Agent 配置
        if str(data.get("action", "")).strip() == "providers":
            cur = {}
            if models_file.exists():
                try:
                    cur = json.loads(models_file.read_text(encoding="utf-8"))
                except Exception:
                    cur = {}
            prov = cur.get("providers") or {}
            for k in ("image", "tts", "tts_sing"):
                v = str((data.get("providers") or {}).get(k, prov.get(k, "builtin"))).strip().lower()
                prov[k] = v if v in ("builtin", "agent") else "builtin"
            cur["providers"] = prov

            ag = dict(cur.get("agent") or {})
            incoming = data.get("agent") or {}
            for field in ("agent_id", "base"):
                if field in incoming:
                    ag[field] = str(incoming.get(field) or "").strip()
            if incoming.get("api_key"):
                ag["api_key"] = str(incoming["api_key"]).strip()
            cur["agent"] = ag

            try:
                models_file.parent.mkdir(parents=True, exist_ok=True)
                models_file.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                return jsonify({"error": "保存失败: " + str(e)[:80]}), 500
            return jsonify({"ok": True, "providers": prov})

        role = str(data.get("role", "text")).strip().lower()
        if role not in ("text", "vision", "video", "summary", "image", "tts"):
            return jsonify({"error": "未知模型类型: " + role}), 400
        
        existing = {}
        if models_file.exists():
            try:
                existing = json.loads(models_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        
        effort = str(data.get("reasoning_effort", "") or "").strip().lower()
        if effort not in ("low", "medium", "high", "none", "minimal"):
            effort = ""  # Default
        existing[role] = {
            "name": data.get("name", ""),
            "base_url": data.get("base_url", ""),
            "model": data.get("model", "qwen3.8-flash"),
            "max_tokens": data.get("max_tokens", 2048),
            "timeout": data.get("timeout", 120),
            "reasoning_effort": effort,
        }
        # Only save API key if provided
        if data.get("api_key"):
            existing[role]["api_key"] = data["api_key"]
        
        models_file.parent.mkdir(parents=True, exist_ok=True)
        models_file.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        return jsonify({"ok": True, "message": f"{role} 模型配置已保存"})
    
    @app.route("/admin/api/bot/emojis", methods=["GET"])
    def api_emojis():
        """内置表情 + 她自己的斗图库。

        原先这里只返回内置表情，斗图库（真的会发出去的那批图）在面板上完全看不见。
        现在一并返回，免得「表情库」点进去只有一堆小黄脸。
        """
        out = {"emojis": [], "stickers": [], "stats": {}}
        try:
            import sys
            bot_dir = Path(__file__).parent.parent / "bot"
            if str(bot_dir) not in sys.path:
                sys.path.insert(0, str(bot_dir))
            import msg_enhance
            out["emojis"] = msg_enhance.get_emoji_list()
        except Exception as e:
            out["error"] = str(e)
        try:
            import sticker_store as _ss
            out["stickers"] = _ss.all_stickers(300)
            out["stats"] = _ss.stats()
        except Exception as e:
            out["sticker_error"] = str(e)
        return jsonify(out)
    

    # --------------------------------------------------------
    # 文风指纹面板 API
    # Web 与 Bot 是两个进程，设定写入 data/style_policy.json，
    # Bot 侧构建提示词前读取该文件，实现跨进程热更新。
    # --------------------------------------------------------

    def _style_policy_path():
        return Path(os.getenv("DATA_DIR", "./data")) / "style_policy.json"


    def _load_prompt_snapshot():
        """读 bot 落的真实提示词快照。读不到就返回空 dict，不猜。"""
        p = Path(os.getenv("DATA_DIR", "./data")) / "prompt_snapshot.json"
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _render_prompt_snapshot(snap):
        """拼成可读的一整份提示词（按真实顺序：system 在前，user 在后）。

        中间会有一条「……（中间省略 N 条）……」，那是真的省略 —— 别当成她看到过的东西。
        """
        parts = []
        msgs = snap.get("messages") or []
        total = snap.get("count")
        if total and int(total) > len(msgs):
            parts.append("（这一轮共 %d 条消息，下面只展开开头与结尾；"
                         "省略的部分她确实看到过，但这里没存）" % int(total))
        for i, m in enumerate(msgs):
            body = str((m or {}).get("text") or "")
            parts.append("── [%d] %s（%s 字）" % (
                i + 1, str((m or {}).get("role") or "?"),
                (m or {}).get("chars", len(body))))
            parts.append(body)
        return "\n".join(parts)

    def _load_style_policy():
        p = _style_policy_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}


    def _save_style_policy(patch):
        p = _style_policy_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        cur = _load_style_policy()
        cur.update({k: v for k, v in patch.items() if v is not None})
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return cur


    def _import_style_engine():
        import sys
        bot_dir = str(Path(__file__).parent.parent / "bot")
        if bot_dir not in sys.path:
            sys.path.insert(0, bot_dir)
        import style_engine
        importlib.reload(style_engine)
        return style_engine


    @app.route("/admin/api/bot/style", methods=["GET", "POST"])
    def api_style():
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401

        policy = _load_style_policy()

        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            patch = {}
            if "enabled" in data:
                patch["enabled"] = bool(data["enabled"])
            if "default_book" in data:
                patch["default_book"] = str(data["default_book"]).strip().upper()
            if "long_threshold" in data:
                try:
                    v = int(data["long_threshold"])
                except (TypeError, ValueError):
                    return jsonify({"error": "long_threshold 必须是整数"}), 400
                patch["long_threshold"] = max(0, min(1000, v))
            if "samples_per_call" in data:
                try:
                    v = int(data["samples_per_call"])
                except (TypeError, ValueError):
                    return jsonify({"error": "samples_per_call 必须是整数"}), 400
                patch["samples_per_call"] = max(0, min(6, v))
            if "flirt_enabled" in data:
                patch["flirt_enabled"] = bool(data["flirt_enabled"])
            if "flirt_max_lines" in data:
                try:
                    v = int(data["flirt_max_lines"])
                except (TypeError, ValueError):
                    return jsonify({"error": "flirt_max_lines 必须是整数"}), 400
                patch["flirt_max_lines"] = max(1, min(12, v))
            if "flirt_gap" in data:
                try:
                    v = float(data["flirt_gap"])
                except (TypeError, ValueError):
                    return jsonify({"error": "flirt_gap 必须是数字"}), 400
                patch["flirt_gap"] = round(max(0.1, min(5.0, v)), 2)
            _save_style_policy(patch)
            return jsonify({"ok": True, "policy": patch, "message": "已保存"})

        try:
            se = _import_style_engine()
        except Exception as e:
            return jsonify({"error": "无法导入 style_engine: " + str(e)}), 500

        st = se.style_status()
        st.update({
            "enabled": policy.get("enabled", st["enabled"]),
            "default_book": policy.get("default_book", st["default_book"]),
            "long_threshold": policy.get("long_threshold", st["long_threshold"]),
            "samples_per_call": policy.get("samples_per_call", st["samples_per_call"]),
        })
        # 书名要跟着 default_book 走，否则切了书还显示旧名字
        _bk = st["default_book"]
        try:
            st["default_book_name"] = se.BOOK_NAMES.get(_bk, _bk)
        except Exception:
            st["default_book_name"] = _bk
        st["books"] = [
            {"key": k, "name": v, "attrs": list(se.BOOK_PROFILES[k].keys())}
            for k, v in se.BOOK_NAMES.items()
        ]
        # 暧昧模块参数：面板可调，存 style_policy.json，bot 侧热读取
        st["flirt_enabled"] = policy.get("flirt_enabled", True)
        st["flirt_max_lines"] = policy.get("flirt_max_lines", 12)
        st["flirt_gap"] = policy.get("flirt_gap", 0.9)
        return jsonify(st)


    def _toolkit():
        import sys
        bot_dir = str(Path(__file__).parent.parent / "bot")
        if bot_dir not in sys.path:
            sys.path.insert(0, bot_dir)
        import toolkit
        return toolkit

    @app.route("/admin/api/bot/tools", methods=["GET"])
    def api_tools():
        """工具清单 + 调用记录，给她做的活留个账。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        try:
            tk = _toolkit()
        except Exception as e:
            return jsonify({"error": "无法加载 toolkit: " + str(e)}), 500

        agent_ok = False
        try:
            import qwen_agent as _qa
            agent_ok = _qa.configured()
        except Exception:
            pass

        # 分流策略说明：面板上要看得懂什么样的问题走哪条路
        routing = {
            "cloud_when": ["明确点名塔罗/周易/运势", "主人开口", "私聊", "被 @ 点名"],
            "local_when": ["群里其他人泛问「帮我算算」"],
            "ack_enabled": True,
            "ack_samples": [
                "唔……我摇一摇，等一下",
                "行，让我算算",
                "别催，正抽着呢",
                "让我起个卦……稍等",
            ],
            "ack_note": "云端要 40~60 秒，所以先抛一句把人接住，算完再正经回。",
        }

        return jsonify({
            "tools": tk.all_tools(),
            "stats": tk.stats(),
            "calls": tk.recent_calls(40),
            "agent_ready": agent_ok,
            "routing": routing,
        })

    @app.route("/admin/api/bot/tools/test", methods=["POST"])
    def api_tool_test():
        """手动试一个工具，面板上能直接看效果。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        key = str(data.get("tool") or "").strip()
        q = str(data.get("query") or "").strip() or "帮我看一下"
        try:
            tk = _toolkit()
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        if key not in tk.TOOLS:
            return jsonify({"error": "没有这个工具: " + key}), 400

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(tk.run_agent_tool(
                key, q, session='verify:panel', evidence_origin='verification'))
        except Exception as e:
            return jsonify({"ok": False, "error": type(e).__name__ + ": " + str(e)[:120]}), 500
        finally:
            loop.close()
        return jsonify(res)

    def _asr_module():
        import sys
        bot_dir = str(Path(__file__).parent.parent / "bot")
        if bot_dir not in sys.path:
            sys.path.insert(0, bot_dir)
        import asr
        return asr

    @app.route("/admin/api/bot/asr/model", methods=["POST"])
    def api_asr_model():
        """改 ASR 的模型 ID / 端点 / 密钥。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        cfg_path = Path(os.getenv("DATA_DIR", "./data")) / "models_config.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
        except Exception:
            cfg = {}
        asr = dict(cfg.get("asr") or {})
        if "model" in data:
            asr["model"] = str(data["model"]).strip()[:80]
        if "base_url" in data:
            asr["base_url"] = str(data["base_url"]).strip()[:200]
        if data.get("api_key"):
            asr["api_key"] = str(data["api_key"]).strip()[:200]
        cfg["asr"] = asr
        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            return jsonify({"error": "写入失败: " + str(e)}), 500
        return jsonify({"ok": True, "asr": {k: ("***" if k == "api_key" and v else v)
                                             for k, v in asr.items()}})


    @app.route("/admin/api/bot/asr", methods=["GET", "POST"])
    def api_asr():
        """语音识别模型状态 + 语音回应策略。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401

        policy_path = Path(os.getenv("DATA_DIR", "./data")) / "voice_policy.json"

        def _load_policy():
            try:
                if policy_path.is_file():
                    return json.loads(policy_path.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
            return {}

        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            cur = _load_policy()
            if "voice_reply_chance" in data:
                try:
                    v = int(data["voice_reply_chance"])
                except (TypeError, ValueError):
                    return jsonify({"error": "voice_reply_chance 必须是整数"}), 400
                cur["voice_reply_chance"] = max(0, min(100, v))
            if "ignore_meaningless" in data:
                cur["ignore_meaningless"] = bool(data["ignore_meaningless"])
            if "asr_enabled" in data:
                cur["asr_enabled"] = bool(data["asr_enabled"])
            try:
                policy_path.parent.mkdir(parents=True, exist_ok=True)
                policy_path.write_text(json.dumps(cur, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
            except Exception as e:
                return jsonify({"error": "写入失败: " + str(e)}), 500
            return jsonify({"ok": True, "policy": cur})

        st = {"enabled": False, "model": "fun-asr-flash-8k-realtime",
              "url": "", "has_key": False, "ready": False}
        try:
            st = _asr_module().status()
        except Exception as e:
            st["error"] = "无法加载 asr: " + str(e)

        cur = _load_policy()
        st["policy"] = {
            "voice_reply_chance": int(cur.get("voice_reply_chance", 50)),
            "ignore_meaningless": bool(cur.get("ignore_meaningless", True)),
            "asr_enabled": bool(cur.get("asr_enabled", True)),
        }
        st["rules"] = [
            "任何语音条都先转成文字",
            "识别出无意义文本（嗯啊、噪声、空白）就完全不回应",
            "有内容时按概率：文字回复 / 语音回复",
        ]
        return jsonify(st)


    @app.route("/admin/api/bot/cost", methods=["GET", "POST"])
    def api_cost():
        """Token 与缓存统计：看得见省了多少。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        try:
            import sys
            bot_dir = str(Path(__file__).parent.parent / "bot")
            if bot_dir not in sys.path:
                sys.path.insert(0, bot_dir)
            import llm_router
        except Exception as e:
            return jsonify({"error": "无法加载 llm_router: " + str(e)}), 500

        if request.method == "POST" and (request.get_json(silent=True) or {}).get("reset"):
            llm_router.reset_usage()
            return jsonify({"ok": True})

        st = llm_router.usage_stats()
        # 附上当前关键配置，面板上一目了然
        import image_gen
        st["image"] = {
            "model": image_gen.IMAGE_GEN_MODEL,
            "size": image_gen.IMAGE_GEN_SIZE,
            "extend": image_gen.IMAGE_GEN_EXTEND,
        }
        return jsonify(st)


    @app.route("/admin/api/bot/draws", methods=["GET"])
    def api_draws():
        """画图流程：挂起任务 + 最近记录，看得见她查了什么。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        try:
            import sys
            bot_dir = str(Path(__file__).parent.parent / "bot")
            if bot_dir not in sys.path:
                sys.path.insert(0, bot_dir)
            import draw_flow
        except Exception as e:
            return jsonify({"error": "无法加载 draw_flow: " + str(e)}), 500

        return jsonify({
            "pending": draw_flow.pending_list(),
            "stats": draw_flow.draw_stats(),
            "recent": draw_flow.recent_draws(20),
            "flow": [
                "认出要画什么（人物/场景）",
                "强制检索：明日方舟走 PRTS，其余走网络搜图",
                "对参考图做详细外观提取，多图综合成可还原的描述",
                "先把查到的东西简短回给用户（可以据此喊停）",
                "生图；期间用户说「错了/别画了」就立刻终止并认错",
            ],
        })


    @app.route("/admin/api/bot/human", methods=["GET", "POST"])
    def api_human():
        """人味调节：话痨程度等。写 data/human_policy.json，bot 即时热读。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401

        path = Path(os.getenv("DATA_DIR", "./data")) / "human_policy.json"

        def _load():
            try:
                if path.is_file():
                    return json.loads(path.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
            return {}

        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            cur = _load()
            if "talk_level" in data:
                try:
                    v = int(data["talk_level"])
                except (TypeError, ValueError):
                    return jsonify({"error": "talk_level 必须是整数"}), 400
                cur["talk_level"] = max(1, min(5, v))
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception as e:
                return jsonify({"error": "写入失败: " + str(e)}), 500
            return jsonify({"ok": True, "policy": cur})

        cur = _load()
        return jsonify({
            "talk_level": int(cur.get("talk_level") or 3),
            "levels": [
                {"v": 1, "name": "冷淡", "desc": "60% 只说几个字 / 24% 整句 / 6% 长篇 / 10% 不回"},
                {"v": 2, "name": "话少", "desc": "52% 短句 / 29% 整句 / 9% 长篇 / 10% 不回"},
                {"v": 3, "name": "正常", "desc": "45% 短句 / 33% 整句 / 12% 长篇 / 10% 不回（推荐）"},
                {"v": 4, "name": "健谈", "desc": "35% 短句 / 40% 整句 / 17% 长篇 / 8% 不回"},
                {"v": 5, "name": "话痨", "desc": "25% 短句 / 45% 整句 / 24% 长篇 / 6% 不回"},
            ],
        })


    @app.route("/admin/api/bot/style/reload", methods=["POST"])
    def api_style_reload():
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        try:
            se = _import_style_engine()
            se._pool.load(force=True)
            return jsonify({"ok": True, "sample_total": se._pool.total})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


    @app.route("/admin/api/bot/style/preview", methods=["POST"])
    def api_style_preview():
        """先看真实提示词，再退回文风块。

        「提示词预览」以前的回答是现场重新拼一段文风块 —— 那是我们以为她会
        看到的，不是她真的看到的。bot 侧现在每轮落一份最终 messages 的快照
        （`data/prompt_snapshot.json`，0600），这里优先把它端出来。
        没有快照（重启后她还没说过话）就明说没有，再给文风块当兜底。
        """
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        mode = "long" if data.get("mode") == "long" else "chat"
        kind = "dm" if str(data.get("scope") or "") == "dm" else "group"
        snap = _load_prompt_snapshot().get(kind)
        if isinstance(snap, dict) and snap.get("messages"):
            return jsonify({"ok": True, "source": "real", "kind": kind,
                            "ts": snap.get("ts"), "mode": snap.get("mode"),
                            "total_chars": snap.get("total_chars"),
                            "prompt": _render_prompt_snapshot(snap),
                            "block": snap.get("messages")})
        policy = _load_style_policy()
        try:
            se = _import_style_engine()
            if not policy.get("enabled", True):
                return jsonify({"block": "", "length": 0, "mode": mode,
                                "source": "style_block",
                                "note": "文风块已关闭，且还没有真实提示词快照"})
            block = se.build_style_block(
                book=policy.get("default_book"),
                mode=mode,
                include_samples=int(policy.get("samples_per_call", se.SAMPLES_PER_CALL)) > 0,
            )
            return jsonify({"block": block, "length": len(block), "mode": mode,
                            "source": "style_block",
                            "note": "这是文风块，不是她真的看到的提示词；"
                                    "她在该场景说过一次话之后就会有真实快照"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


    @app.route("/admin/api/bot/persona-mode", methods=["GET", "POST"])
    def api_persona_mode():
        """人设切换：whale(内置示例人格) / generic(通用助手)。写入 data/persona_policy.json 供 bot 读取。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        policy_path = Path(os.getenv("DATA_DIR", "./data")) / "persona_policy.json"

        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            mode = str(data.get("mode", "")).strip().lower()
            if mode not in ("whale", "generic"):
                return jsonify({"error": "mode must be whale or generic"}), 400
            patch = {"mode": mode}
            if "custom_persona" in data:
                patch["custom_persona"] = str(data["custom_persona"])[:2000]
            if "display_name" in data:
                patch["display_name"] = str(data["display_name"])[:60]

            # 触发词：这是 bot 判「这句话是不是她自己说的」的依据，
            # 以前面板根本不存它，所以改完永远变回去。现在两处都写：
            #   ① persona_policy.json —— bot 会热加载（refresh_from_policy）
            #   ② config.env —— 重启后仍然生效
            if "trigger_words" in data:
                raw = data["trigger_words"]
                if isinstance(raw, str):
                    words = [x.strip() for x in raw.replace("，", ",").split(",")]
                elif isinstance(raw, (list, tuple)):
                    words = []
                    for x in raw:
                        words.extend(str(x).replace("，", ",").split(","))
                    words = [x.strip() for x in words]
                else:
                    words = []
                words = [w for w in words if w][:12]
                if words:
                    patch["triggers"] = words
                    try:
                        import config_store as _cs
                        _cs.set_values({"BOT_TRIGGER_WORDS": ",".join(words)},
                                       reason="面板保存人设")
                    except Exception as _ee:
                        print("[面板] 写 config.env 失败 " + str(_ee)[:60], flush=True)

            policy_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = policy_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(patch, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(policy_path)

            # 立刻让本进程生效（不用等 bot 那边轮询）
            try:
                import sys as _sys
                _bd = str(Path(__file__).parent.parent / "bot")
                if _bd not in _sys.path:
                    _sys.path.insert(0, _bd)
                import project_config as _pcw
                _pcw.refresh_from_policy()
            except Exception:
                pass

            return jsonify({"ok": True, "mode": mode, "triggers": patch.get("triggers", [])})

        cur = {"mode": os.getenv("BOT_PERSONA_MODE", "whale").strip().lower() or "whale"}
        if policy_path.exists():
            try:
                cur.update(json.loads(policy_path.read_text(encoding="utf-8")))
            except Exception:
                pass

        # 触发词有三个历史来源，按优先级统一：
        #   ① persona_policy.json（面板保存的，最权威）
        #   ② persona.json（旧版面板写的，虽然没人读但别丢用户的改动）
        #   ③ whale_persona.PERSONA_META（代码里硬编码的兜底）
        # 以前只认 ③，所以面板里改完一刷新就"变回去"了。
        _trig = None
        if "triggers" in cur and cur["triggers"]:
            _trig = cur["triggers"]
        if _trig is None:
            try:
                _pj = Path(os.getenv("DATA_DIR", "./data")) / "persona.json"
                if _pj.exists():
                    _saved = json.loads(_pj.read_text(encoding="utf-8"))
                    if _saved.get("trigger_words"):
                        _trig = _saved["trigger_words"]
            except Exception:
                pass
        meta = []
        try:
            import sys
            bot_dir = str(Path(__file__).parent.parent / "bot")
            if bot_dir not in sys.path:
                sys.path.insert(0, bot_dir)
            import whale_persona
            m = whale_persona.PERSONA_META
            meta.append(m)
            cur.setdefault("display_name", m.get("display_name", ""))
            if _trig:
                cur["triggers"] = _trig
            else:
                cur.setdefault("triggers", m.get("trigger_words", []))
        except Exception:
            pass
        cur["meta"] = meta
        return jsonify(cur)


    @app.route("/admin/api/bot/effective-config")
    def api_effective_config():
        """回读机器人当前真正生效的配置，供面板确认设置是否已生效。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        data_dir = Path(os.getenv("DATA_DIR", "./data"))
        result = {}

        pp = data_dir / "persona_policy.json"
        persona = {"mode": os.getenv("BOT_PERSONA_MODE", "whale"), "source": "env-default"}
        if pp.exists():
            try:
                loaded = json.loads(pp.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    persona.update(loaded)
                    persona["source"] = "panel-saved"
                    persona["has_custom"] = bool(str(loaded.get("custom_persona", "")).strip())
            except Exception as e:
                persona["error"] = str(e)[:120]
        result["persona"] = persona

        stp = data_dir / "control-v4" / "state.json"
        settings = {"available": False}
        if stp.exists():
            try:
                st = json.loads(stp.read_text(encoding="utf-8"))
                age = time.time() - float(st.get("sampled", 0))
                settings = {
                    "available": True,
                    "age_sec": round(age, 1),
                    "fresh": 0 <= age < 90,
                    "reply_tokens": (st.get("settings") or {}).get("reply_tokens"),
                    "reply_chars": (st.get("settings") or {}).get("reply_chars"),
                    "bubbles": (st.get("settings") or {}).get("bubbles"),
                    "delay_ms": (st.get("settings") or {}).get("delay_ms"),
                    "style_note": (st.get("settings") or {}).get("style_note", ""),
                }
            except Exception as e:
                settings = {"available": False, "error": str(e)[:120]}
        result["settings"] = settings

        sp = data_dir / "style_policy.json"
        style = {"enabled": True}
        if sp.exists():
            try:
                style.update(json.loads(sp.read_text(encoding="utf-8")))
                style["source"] = "panel-saved"
            except Exception:
                pass
        result["style"] = style

        # 提供控制面 CSRF，让 dashboard 能直接提交 settings（同 session）
        if not session.get("bot_control_csrf"):
            session["bot_control_csrf"] = secrets.token_urlsafe(32)
        result["control_csrf"] = session.get("bot_control_csrf")

        # 顶层也放一份在线状态，方便前端直接读（settings 里那份保留兼容）
        result["fresh"] = bool(settings.get("fresh"))
        result["age_sec"] = settings.get("age_sec")

        result["server_time"] = time.time()
        return jsonify(result)


    @app.route("/admin/api/bot/qq-faces", methods=["GET"])
    def api_qq_faces():
        """返回用户 QQ 收藏的表情包（fetch_custom_face），供表情库面板展示。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"urls": _qq_face_cache_payload(), "note": "QQ 收藏表情（斗图用）"})

    @app.route("/admin/api/bot/sticker/<sid>/meaning", methods=["POST"])
    def api_sticker_meaning(sid):
        """主人校准她对某一张表情包的理解：写进去的那条就是她现在用的那条。

        为什么不让在网格里直接改：库里几百张，一格一个输入框既点不准，
        也分不清「这张图是什么意思」和「她这时候该不该发」。悬浮窗一次只看一张。
        空含义也是一次人工判断（「这张没有固定意思」），比留着一个错的总结强。
        """
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        token = str(session.get("bot_control_csrf") or "")
        if not token or not hmac.compare_digest(token,
                                                request.headers.get("X-CSRF-Token", "")):
            return jsonify({"error": "安全校验失败，请刷新页面"}), 403
        body = request.get_json(silent=True) or {}
        meaning = str(body.get("meaning") or "").strip()[:80]
        if len(str(body.get("meaning") or "")) > 80:
            return jsonify({"error": "这条总结请控制在 80 字内"}), 400
        try:
            import sticker_store as _ss
        except Exception as e:
            return jsonify({"error": "模块加载失败: " + str(e)[:100]}), 500
        if not _ss.sticker_path(sid):
            return jsonify({"error": "库里没有这张（可能刚被删掉，请刷新）"}), 404
        # 空串 = 主人判定「这张没有固定含义」，也要落住，不能被机器再猜一遍填回去
        ok = _ss.set_meaning(sid, meaning, source="human")
        if not ok:
            return jsonify({"error": "没写进去：这张已不在库里"}), 404
        return jsonify({"ok": True, "id": sid, "meaning": meaning,
                        "meaning_src": "human", "stats": _ss.stats()})

    @app.route("/admin/api/bot/sticker/<sid>/delete", methods=["POST"])
    def api_sticker_delete(sid):
        """人工筛选：把她自动收进来但你不想要的那张剔掉。

        文件不直接抹掉，先移进 data/stickers_trash（上限 TRASH_KEEP）。
        点错一张常用的梗图，成本应该是能捞回来。
        """
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        token = str(session.get("bot_control_csrf") or "")
        if not token or not hmac.compare_digest(token,
                                                request.headers.get("X-CSRF-Token", "")):
            return jsonify({"error": "安全校验失败，请刷新页面"}), 403
        try:
            import sticker_store as _ss
        except Exception as e:
            return jsonify({"error": "模块加载失败: " + str(e)[:100]}), 500
        got = _ss.delete_sticker(sid)
        if not got.get("ok"):
            return jsonify({"error": got.get("reason") or "库里没有这张",
                            "stats": _ss.stats()}), 404
        return jsonify({"ok": True, "id": sid, "trashed": got.get("trashed") or "",
                        "stats": _ss.stats()})

    @app.route("/admin/api/bot/sticker/<sid>", methods=["GET"])
    def api_sticker_img(sid):
        """取一张斗图的图片。只认索引里的 id，且路径必须在库目录内。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401

        def _botmod(name):
            import sys as _sys
            from pathlib import Path as _P
            _bot = str(_P(__file__).resolve().parent.parent / "bot")
            if _bot not in _sys.path:
                _sys.path.insert(0, _bot)
            return __import__(name)

        try:
            ss = _botmod("sticker_store")
            p = ss.sticker_path(sid)
        except Exception:
            p = ""
        if not p:
            return jsonify({"error": "not found"}), 404
        return send_file(p, conditional=True)

    @app.route("/media/qv/<token>")
    def api_quick_video(token):
        """给视频理解模型取一次临时媒体。

        链接是 HMAC 签名、带 TTL、不可猜；只服务 media_public 登记过的文件，
        不做目录浏览。模型端取完就过期，不能当公开视频站用。
        """
        try:
            import sys as _sys
            from pathlib import Path as _P
            _bot = str(_P(__file__).resolve().parent.parent / "bot")
            if _bot not in _sys.path:
                _sys.path.insert(0, _bot)
            import media_public
            path = media_public.resolve(token)
        except Exception:
            path = ""
        if not path or not Path(path).is_file():
            return jsonify({"error": "not found"}), 404
        return send_file(path, conditional=True, mimetype="video/mp4")

    def _sticker_store():
        try:
            import sys as _sys
            from pathlib import Path as _P
            _bot = str(_P(__file__).resolve().parent.parent / "bot")
            if _bot not in _sys.path:
                _sys.path.insert(0, _bot)
            import sticker_store
            return sticker_store
        except Exception:
            return None

    @app.route("/admin/api/bot/qq-face")
    def api_qq_face_img():
        """收藏表情走服务端代理。

        p.qpic.cn 认 Referer，浏览器带着面板的地址直连就是坏图；
        而且面板经 https 隧道访问，图取不到就只会显示一排破图标。
        只按序号取，序号来自 bot 同步的列表，不接受任意 URL。
        """
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        ss = _sticker_store()
        if ss is None:
            return jsonify({"error": "模块加载失败"}), 500
        try:
            idx = int(request.args.get("i") or "")
        except (TypeError, ValueError):
            return jsonify({"error": "index 无效"}), 400
        urls = ss.qq_face_urls()
        if idx < 0 or idx >= len(urls):
            return jsonify({"error": "out_of_range"}), 404
        import asyncio
        try:
            body, info = asyncio.run(ss.fetch_qq_face(urls[idx]))
        except RuntimeError:
            body, info = None, "事件循环不可用"
        if not body:
            return jsonify({"error": info or "取不到"}), 502
        from flask import Response as _Resp
        resp = _Resp(body, mimetype=info)
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp

    def _qq_face_cache_payload():
        # 从 bot 写的缓存文件读，避免 web 侧另开 WS 连接
        p = Path(os.getenv("DATA_DIR", "./data")) / "qq_faces.json"
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if 0 <= time.time() - float(d.get("ts", 0)) < 900:
                    return d.get("urls") or []
            except Exception:
                pass
        return []


    def _whisper():
        """愿望墙模块（在 bot/ 下）。"""
        import sys as _sys
        from pathlib import Path as _P
        _bot = str(_P(__file__).resolve().parent.parent / "bot")
        if _bot not in _sys.path:
            _sys.path.insert(0, _bot)
        os.environ.setdefault("DATA_DIR", str(_P(os.getenv("DATA_DIR", "./data"))))
        import whisper
        return whisper

    @app.route("/admin/api/bot/emotion-shadow", methods=["GET"])
    def api_emotion_shadow():
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        try:
            import sys
            bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
            if bot_dir not in sys.path:
                sys.path.append(bot_dir)
            import emotion_shadow
            import valence_candidates
            candidate_detail = request.args.get("candidate_detail", "0") == "1"
            # 默认不扫描全部历史，也不通过候选recent建库；详情必须明确请求。
            out = emotion_shadow.status(include_valence=candidate_detail)
            out["candidate_detail"] = bool(candidate_detail)
            out["valence_candidates"] = (valence_candidates.recent_readonly(20)
                                         if candidate_detail else {"ok": False, "status": "not_loaded"})
            return jsonify(out)
        except Exception as exc:
            return jsonify({"ok": False, "mode": "shadow", "expression_control": False,
                            "error": type(exc).__name__}), 503

    @app.route("/admin/api/bot/cognition-control", methods=["POST"])
    def api_cognition_control():
        """Explicit administrator instructions only; never infer consent from chat."""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        import hmac
        expected = str(session.get("bot_control_csrf") or "")
        supplied = str(request.headers.get("X-CSRF-Token") or "")
        if not expected or not hmac.compare_digest(expected, supplied):
            return jsonify({"error": "csrf"}), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "json_object_required"}), 400
        reason = str(body.get("reason") or "").strip()
        if not reason or len(reason) > 2000:
            return jsonify({"error": "explicit_reason_required"}), 400
        import sys
        bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
        if bot_dir not in sys.path:
            sys.path.append(bot_dir)
        import self_model
        try:
            operation = body.get("operation")
            if operation == "consent":
                recipient = str(body.get("recipient") or "")
                action = str(body.get("action") or "")
                if not recipient.isdigit() or action not in (
                        "send", "dm", "group", "send_private_msg", "send_group_msg"):
                    raise ValueError("invalid_scope")
                result = self_model.record_consent(recipient, action,
                                                  body.get("decision"), reason)
            elif operation == "revoke_consent":
                result = self_model.revoke_consent(str(body.get("id") or ""), reason)
            elif operation == "retract_evidence":
                result = self_model.retract_evidence(str(body.get("id") or ""), reason)
            else:
                raise ValueError("unsupported_operation")
            return jsonify({"ok": True, "result": result, "stage3_authorized": False})
        except (ValueError, TypeError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": type(exc).__name__}), 503

    @app.route("/admin/api/bot/readiness", methods=["GET"])
    def api_readiness():
        """阶段2.5 准入判定：只读账本，把每一项的真实状态摊开。

        原来面板上显眼的是校准 10/10 这种好数字，可准入看的是另一码事
        （动作带不带证据、终态有没有回执、自我修订挂没挂具体对话）。
        两套混在一起，「检查通过」就会被读成「可以进阶段3」。
        读不到就如实报 unknown —— 这里出错绝不能默认给个绿灯。
        """
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        try:
            import sys
            bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
            if bot_dir not in sys.path:
                sys.path.append(bot_dir)
            import phase_audit
            try:
                window = int(request.args.get("window", "86400"))
            except (TypeError, ValueError):
                window = 86400
            # 候选全历史反事实很贵，且不属于准入判定；默认不跑，需显式 candidate_replay=1。
            replay = request.args.get("candidate_replay") == "1"
            out = phase_audit.audit_readiness(window_seconds=window,
                                              include_candidate_replay=replay)
            out["transport_ok"] = True
            return jsonify(out)
        except Exception as exc:
            return jsonify({"ok": False, "error": type(exc).__name__,
                            "phase25": {"status": "unknown", "checks": [],
                                        "blockers": ["audit_unavailable"]},
                            "stage3": {"authorized": False, "expression_control": False,
                                       "stage3a": "blocked", "stage3b": "blocked"}}), 503

    def _qingyi_admin():
        return session.get("ceylan_admin") is True

    def _qingyi_csrf_ok():
        expected = str(session.get("bot_control_csrf") or "")
        supplied = str(request.headers.get("X-CSRF-Token") or "")
        return bool(expected and hmac.compare_digest(expected, supplied))

    def _qingyi_modules():
        import sys
        bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
        if bot_dir not in sys.path:
            sys.path.insert(0, bot_dir)
        import qingyi_preflight_jobs
        import qingyi_roadmap
        return qingyi_preflight_jobs, qingyi_roadmap

    def _qingyi_plan():
        """自动驾驶最近一轮的计划快照，按**当前**授权账本重新分桶。

        分桶只算一次：ops_channel.plan_view 同时给面板、/ops plan 和分发器用，
        三端不再各算一套（以前面板现算、QQ 读旧快照，主人批完两边对不上）。
        """
        try:
            import sys
            bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
            if bot_dir not in sys.path:
                sys.path.insert(0, bot_dir)
            import ops_channel
            view = ops_channel.plan_view()
        except Exception:
            return {}
        return {"at": view.get("at", ""), "doable": view.get("doable", []),
                "approval": view.get("approval", []), "waiting": view.get("waiting", []),
                "blocked": view.get("blocked", [])}

    def _qingyi_ops_state():
        try:
            path = os.path.join(os.getenv("DATA_DIR", "./data"), "ops", "state.json")
            with open(path, encoding="utf-8") as f:
                return json.load(f) or {}
        except (OSError, ValueError):
            return {}

    def _qingyi_round_report():
        """自动驾驶每轮推送的检测快照。它比「上次保存的作业报告」新得多。"""
        try:
            path = os.path.join(os.getenv("DATA_DIR", "./data"), "ops", "preflight.json")
            with open(path, encoding="utf-8") as f:
                rep = json.load(f) or {}
        except (OSError, ValueError):
            return None
        return rep if rep.get("tasks") else None

    @app.route("/admin/api/bot/qingyi/decision", methods=["POST"])
    def api_qingyi_decision():
        """面板上直接批准/否决单个任务。走的是同一个人工授权账本。"""
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        if not _qingyi_csrf_ok():
            return jsonify({"error": "csrf"}), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) - {"task_id", "decision", "note"}:
            return jsonify({"ok": False, "error": "invalid_request"}), 400
        decision = str(body.get("decision") or "")
        if decision not in ("approve", "reject"):
            return jsonify({"ok": False, "error": "invalid_decision"}), 400
        try:
            import sys
            bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
            if bot_dir not in sys.path:
                sys.path.insert(0, bot_dir)
            import ops_channel
            ok, message = ops_channel.record_decision(
                str(body.get("task_id") or ""), decision,
                str(body.get("note") or "面板操作")[:300], by="panel")
            return jsonify({"ok": ok, "message": message}), (200 if ok else 400)
        except Exception as exc:
            return jsonify({"ok": False, "error": type(exc).__name__}), 503

    @app.route("/admin/api/bot/qingyi/stage3-signoff", methods=["POST"])
    def api_qingyi_stage3_signoff():
        """主人本人对灰度接管签字。只记人工授权凭证，不翻 expression_control。

        授予前必须工程+自然全过（phase25 review_ready）；不满足就拒绝。
        表达接管链路 C06 未接线，静态锁恒 False，签字不会改变她对外表达。
        """
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        if not _qingyi_csrf_ok():
            return jsonify({"error": "csrf"}), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) - {"granted", "note"}:
            return jsonify({"ok": False, "error": "invalid_request"}), 400
        granted = bool(body.get("granted"))
        import sys
        bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
        if bot_dir not in sys.path:
            sys.path.insert(0, bot_dir)
        import ops_channel
        ready, blockers = False, ["audit_unavailable"]
        try:
            import phase_audit
            rep = phase_audit.audit_readiness()
            ready = (rep.get("phase25") or {}).get("status") == "review_ready"
            blockers = (rep.get("phase25") or {}).get("blockers") or []
        except Exception:
            pass
        if granted and not ready:
            return jsonify({"ok": False, "ready": False, "expression_control": False,
                            "message": "工程/自然门未全过，暂不能签字授权",
                            "blockers": blockers}), 400
        try:
            ok, message = ops_channel.record_stage3_signoff(
                granted, by="panel-owner", note=str(body.get("note") or "")[:300])
            return jsonify({"ok": ok, "message": message, "ready": ready,
                            "expression_control": False,
                            "signoff": ops_channel.stage3_signoff()})
        except Exception as exc:
            return jsonify({"ok": False, "error": type(exc).__name__,
                            "expression_control": False}), 503

    @app.route("/admin/api/bot/qingyi/stage3-signoff", methods=["GET"])
    def api_qingyi_stage3_signoff_get():
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        import sys
        bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
        if bot_dir not in sys.path:
            sys.path.insert(0, bot_dir)
        import ops_channel
        ready, blockers = False, ["audit_unavailable"]
        try:
            import phase_audit
            rep = phase_audit.audit_readiness()
            ready = (rep.get("phase25") or {}).get("status") == "review_ready"
            blockers = (rep.get("phase25") or {}).get("blockers") or []
        except Exception:
            pass
        return jsonify({"ok": True, "ready": ready, "blockers": blockers,
                        "expression_control": False,
                        "signoff": ops_channel.stage3_signoff()})

    def _evidence_module():
        import sys
        bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
        if bot_dir not in sys.path:
            sys.path.insert(0, bot_dir)
        import evidence_repair
        return evidence_repair

    @app.route("/admin/api/bot/qingyi/evidence-intervene", methods=["GET"])
    def api_qingyi_evidence_intervene():
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        try:
            er = _evidence_module()
            status = {}
            try:
                p = os.path.join(os.getenv("DATA_DIR", "./data"), "ops",
                                 "consolidation_status.json")
                with open(p, encoding="utf-8") as f:
                    status = json.load(f) or {}
            except (OSError, ValueError):
                pass
            return jsonify({"ok": True,
                            "unlinked": er.unlinked_actions(),
                            "self_view": er.self_view_stats(),
                            "consolidation": status})
        except Exception as exc:
            return jsonify({"ok": False, "error": type(exc).__name__}), 503

    @app.route("/admin/api/bot/qingyi/repair-action", methods=["POST"])
    def api_qingyi_repair_action():
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        if not _qingyi_csrf_ok():
            return jsonify({"error": "csrf"}), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) - {"action_id"}:
            return jsonify({"ok": False, "error": "invalid_request"}), 400
        try:
            res = _evidence_module().repair_action(str(body.get("action_id") or ""))
            return jsonify(res), (200 if res["ok"] else 400)
        except Exception as exc:
            return jsonify({"ok": False, "error": type(exc).__name__}), 503

    @app.route("/admin/api/bot/qingyi/run-consolidation", methods=["POST"])
    def api_qingyi_run_consolidation():
        """人工触发整理/历史回顾。后台线程跑，立即返回，避免网关 504。

        mode=new 整点整理（新对话）；mode=history 从近 N 天历史做自我修订。
        结果写 ops/consolidation_status.json，面板刷新读取。
        """
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        if not _qingyi_csrf_ok():
            return jsonify({"error": "csrf"}), 403
        body = request.get_json(silent=True) or {}
        mode = str(body.get("mode") or "history")
        import threading
        import sys
        bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
        if bot_dir not in sys.path:
            sys.path.insert(0, bot_dir)

        def _run():
            import asyncio
            import json as _json
            # web 进程默认不带模型密钥，先加载 config.env，否则 LLM 调用 RuntimeError。
            try:
                cfgp = os.path.join(os.getenv("DATA_DIR", "./data"), "..", "config.env")
                if not os.path.isfile(cfgp):
                    cfgp = "/opt/qqbot/config.env"
                for line in open(cfgp, encoding="utf-8"):
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            except OSError:
                pass
            import persona_growth
            import llm_router

            async def call_llm(prompt):
                return await llm_router.call_text(
                    [{"role": "user", "content": prompt}], max_tokens=900, timeout=90)

            try:
                if mode == "new":
                    res = asyncio.run(persona_growth.consolidate(call_llm))
                else:
                    res = asyncio.run(persona_growth.reflect_history(call_llm, days=14))
            except Exception as exc:
                res = {"ok": False, "reason": type(exc).__name__}
            try:
                p = os.path.join(os.getenv("DATA_DIR", "./data"), "ops",
                                 "consolidation_status.json")
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    _json.dump({"mode": mode, "at": time.time(), "result": res}, f,
                               ensure_ascii=False)
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "started": True, "mode": mode})

    def _annotate_module():
        import sys
        bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
        if bot_dir not in sys.path:
            sys.path.insert(0, bot_dir)
        import annotation_queue
        return annotation_queue

    def _qingyi_takeover():
        try:
            import sys
            bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
            if bot_dir not in sys.path:
                sys.path.insert(0, bot_dir)
            import expression_gate
            g = expression_gate.gate_status()
            return {"active": bool(g.get("active")),
                    "granted": bool(g.get("signoff_granted")),
                    "ready": bool(g.get("ready"))}
        except Exception:
            return {"active": False, "granted": False, "ready": False}

    @app.route("/admin/api/bot/annotate/queue", methods=["GET"])
    def api_annotate_queue():
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        try:
            aq = _annotate_module()
            return jsonify({"ok": True, "samples": aq.samples(limit=8),
                            "status": aq.status()})
        except Exception as exc:
            return jsonify({"ok": False, "error": type(exc).__name__}), 503

    @app.route("/admin/api/bot/annotate/status", methods=["GET"])
    def api_annotate_status():
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        try:
            return jsonify({"ok": True, **_annotate_module().status()})
        except Exception as exc:
            return jsonify({"ok": False, "error": type(exc).__name__}), 503

    @app.route("/admin/api/bot/annotate/submit", methods=["POST"])
    def api_annotate_submit():
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        if not _qingyi_csrf_ok():
            return jsonify({"error": "csrf"}), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) - {"store", "id", "event_ids", "note", "group_id"}:
            return jsonify({"ok": False, "error": "invalid_request"}), 400
        try:
            aq = _annotate_module()
            res = aq.submit(str(body.get("store") or ""), str(body.get("id") or ""),
                            body.get("event_ids") or [], str(body.get("note") or ""),
                            group_id=body.get("group_id"), by="panel")
            return jsonify({**res, "status": aq.status()}), (200 if res["ok"] else 400)
        except Exception as exc:
            return jsonify({"ok": False, "error": type(exc).__name__}), 503

    @app.route("/admin/api/bot/annotate/autolink", methods=["POST"])
    def api_annotate_autolink():
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        if not _qingyi_csrf_ok():
            return jsonify({"error": "csrf"}), 403
        body = request.get_json(silent=True) or {}
        try:
            limit = max(1, min(500, int(body.get("limit") or 100)))
        except (TypeError, ValueError):
            limit = 100
        try:
            aq = _annotate_module()
            res = aq.auto_link(limit=limit)
            return jsonify({"ok": True, **res, "status": aq.status()})
        except Exception as exc:
            return jsonify({"ok": False, "error": type(exc).__name__}), 503

    @app.route("/admin/api/bot/annotate/delete", methods=["POST"])
    def api_annotate_delete():
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        if not _qingyi_csrf_ok():
            return jsonify({"error": "csrf"}), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) - {"store", "id", "group_id"}:
            return jsonify({"ok": False, "error": "invalid_request"}), 400
        try:
            aq = _annotate_module()
            res = aq.delete(str(body.get("store") or ""), str(body.get("id") or ""),
                            group_id=body.get("group_id"), by="panel")
            return jsonify({**res, "status": aq.status()}), (200 if res["ok"] else 400)
        except Exception as exc:
            return jsonify({"ok": False, "error": type(exc).__name__}), 503

    @app.route("/admin/api/bot/annotate/confirm", methods=["POST"])
    def api_annotate_confirm():
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        if not _qingyi_csrf_ok():
            return jsonify({"error": "csrf"}), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) - {"store", "id", "note", "group_id"}:
            return jsonify({"ok": False, "error": "invalid_request"}), 400
        try:
            aq = _annotate_module()
            res = aq.confirm(str(body.get("store") or ""), str(body.get("id") or ""),
                             str(body.get("note") or ""), group_id=body.get("group_id"),
                             by="panel")
            return jsonify({**res, "status": aq.status()}), (200 if res["ok"] else 400)
        except Exception as exc:
            return jsonify({"ok": False, "error": type(exc).__name__}), 503

    @app.route("/admin/api/bot/qingyi/organic", methods=["GET"])
    def api_qingyi_organic():
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        try:
            path = os.path.join(os.getenv("DATA_DIR", "./data"), "ops", "organic_history.json")
            with open(path, encoding="utf-8") as f:
                hist = json.load(f) or []
            return jsonify({"ok": True, "history": hist[-60:]})
        except (OSError, ValueError):
            return jsonify({"ok": True, "history": []})

    @app.route("/admin/api/bot/qingyi/capabilities", methods=["GET"])
    def api_qingyi_capabilities():
        if not _qingyi_admin():
            return jsonify({"error":"unauthorized"}),401
        try:
            _qingyi_modules()
            import capability_audit
            return jsonify(capability_audit.status())
        except Exception as exc:
            return jsonify({"ok":False,"error":type(exc).__name__}),503

    @app.route("/admin/api/bot/qingyi/capabilities/evaluate", methods=["POST"])
    def api_qingyi_capability_evaluate():
        if not _qingyi_admin():
            return jsonify({"error":"unauthorized"}),401
        if not _qingyi_csrf_ok():
            return jsonify({"error":"csrf"}),403
        try:
            _qingyi_modules()
            import capability_audit
            report=capability_audit.evaluate()
            return jsonify({"ok":report["failed"]==0,"evaluation":report})
        except Exception as exc:
            return jsonify({"ok":False,"error":type(exc).__name__}),503

    @app.route("/admin/api/bot/qingyi/overview", methods=["GET"])
    def api_qingyi_overview():
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        try:
            jobs, roadmap = _qingyi_modules()
            latest = jobs.latest()
            report = jobs.read_report(latest["run_id"]) if latest else None
            # 目录版本变了以后，旧报告里的任务状态已经过时（新任务/新默认值会被
            # 旧快照盖住）。代码/解释器 binding 或数据根变了同样过时（§23.4）：
            # 只认与当前目录和当前身份都一致的报告，否则回退到实时目录。
            import qingyi_preflight as _qp
            identity = _qp.report_identity()

            def _is_fresh(rep):
                return bool(rep and rep.get("tasks") is not None
                            and rep.get("catalog_version") == _qp.CATALOG_VERSION
                            and rep.get("binding_digest") == identity["binding_digest"]
                            and rep.get("data_identity_digest") == identity["data_identity_digest"])

            # 优先用本轮巡检刚测出来的那份：任务状态才不会慢一拍
            # （刚做完的还写着「拟议」，主人当然会以为没同步）。
            source = "job"
            round_report = _qingyi_round_report()
            if _is_fresh(round_report):
                report = round_report
                source = "round"
            fresh = _is_fresh(report)
            if fresh:
                progress = {"stages": report.get("stages", []),
                            "tasks": report.get("tasks", []),
                            "validation": report.get("validation", {"ok": False, "reason": "report_invalid"})}
            else:
                progress = roadmap.summary(projection={"tasks": {}})
            return jsonify({
                "ok": True,
                "overview_reads_only": True,
                "latest_job": latest,
                "report": report,
                "report_source": source,
                # 前端拿它跟报告逐字段比对；只给布尔会在轮询换新报告后失真。
                "identity": {**identity, "catalog_version": _qp.CATALOG_VERSION},
                # 与 QQ 通知同源：同一份 frontier 决定「可做/待批」。
                "next": _qingyi_plan(),
                # 人工授权账本：面板据此显示「已批准/已否决」并决定按钮状态。
                "approvals": {k: (v or {}).get("decision")
                              for k, v in (_qingyi_ops_state().get("approvals") or {}).items()},
                # 接管真实状态（签字+前置全过=生效），不再用静态 authorization 冒充。
                "takeover": _qingyi_takeover(),
                **progress,
                "authorization": {"stage3": False, "expression_control": False,
                                  "deployment": False, "automatic_progress": False},
            })
        except Exception as exc:
            return jsonify({"ok": False, "error": type(exc).__name__}), 503

    @app.route("/admin/api/bot/qingyi/preflight", methods=["POST"])
    def api_qingyi_preflight():
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        if not _qingyi_csrf_ok():
            return jsonify({"error": "csrf"}), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) - {"profile"}:
            return jsonify({"error": "invalid_preflight_request"}), 400
        profile = body.get("profile", "evidence")
        if profile not in ("evidence", "isolated"):
            return jsonify({"error": "invalid_preflight_request"}), 400
        try:
            jobs, _ = _qingyi_modules()
            result = jobs.start(profile=profile)
            return jsonify({"ok": result.get("state") in ("queued", "running"),
                            "authorization_granted": False, **result}), (202 if result.get("state") == "queued" else 200)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": type(exc).__name__}), 503

    @app.route("/admin/api/bot/qingyi/preflight/<run_id>", methods=["GET"])
    def api_qingyi_preflight_status(run_id):
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        if not re.fullmatch(r"[0-9a-f]{32}", str(run_id or "")):
            return jsonify({"error": "invalid_run_id"}), 400
        jobs, _ = _qingyi_modules()
        row = jobs.get(run_id)
        if row is None:
            return jsonify({"error": "not_found"}), 404
        report = jobs.read_report(run_id) if row.get("state") in ("pass", "blocked", "unknown") else None
        return jsonify({"ok": True, "job": row, "report": report})

    @app.route("/admin/api/bot/qingyi/preflight/<run_id>/download", methods=["GET"])
    def api_qingyi_preflight_download(run_id):
        if not _qingyi_admin():
            return jsonify({"error": "unauthorized"}), 401
        if not re.fullmatch(r"[0-9a-f]{32}", str(run_id or "")):
            return jsonify({"error": "invalid_run_id"}), 400
        jobs, _ = _qingyi_modules()
        row = jobs.get(run_id)
        if row is None or row.get("state") not in ("pass", "blocked", "unknown"):
            return jsonify({"error": "not_found"}), 404
        report = jobs.read_report(run_id)
        if report is None:
            return jsonify({"error": "report_missing"}), 404
        return Response(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False),
                        mimetype="application/json", headers={
                            "Content-Disposition": 'attachment; filename="qingyi-%s.json"' % run_id,
                            "Cache-Control": "no-store"})

    @app.route("/admin/api/bot/activity")
    def api_activity():
        """她自己查过什么、刷到什么、记住了什么、主动讲过什么。

        只读，且只给 admin session —— 这份流水里会有她查到的原文片段，
        不该挂在没人管的地址上。
        """
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401

        def _load(name):
            import sys as _sys
            from pathlib import Path as _P
            _bot = str(_P(__file__).resolve().parent.parent / "bot")
            if _bot not in _sys.path:
                _sys.path.insert(0, _bot)
            return __import__(name)

        out = {"ok": True, "events": [], "counts": {}, "curiosity": {},
               "bili": {}, "errors": []}
        try:
            al = _load("activity_log")
            out["events"] = al.recent(limit=60)
            out["counts"] = al.stats()
        except Exception as e:
            out["errors"].append("activity_log: %s" % type(e).__name__)
        try:
            cu = _load("curiosity")
            topics = cu.all_topics()[:20]
            out["curiosity"] = {
                "stats": cu.stats(),
                "topics": [{"topic": t.get("topic"), "state": t.get("state"),
                            "seed_from": t.get("seed_from"),
                            "score": cu.score_at(t.get("topic") or ""),
                            "mentions": t.get("mentions"),
                            "fails": t.get("fails")}
                           for t in topics]}
        except Exception as e:
            out["errors"].append("curiosity: %s" % type(e).__name__)
        try:
            bl = _load("bili")
            out["bili"] = {"stats": bl.stats(), "notes": bl.latest_notes(10)}
        except Exception as e:
            out["errors"].append("bili: %s" % type(e).__name__)
        return jsonify(out)

    def _media():
        """上传校验模块（在 bot/ 下，和 whisper 同一套导入路子）。"""
        try:
            import sys as _sys
            from pathlib import Path as _P
            _bot = str(_P(__file__).resolve().parent.parent / "bot")
            if _bot not in _sys.path:
                _sys.path.insert(0, _bot)
            import media_upload
            return media_upload
        except Exception:
            return None

    @app.route("/admin/api/bot/photo", methods=["GET", "POST"])
    def api_photo():
        """给她发一张图（悄悄话通道），并看她记下的看图笔记。

        安全口径逐条写死：只认 admin session；文件名一律由内容哈希生成，
        客户端给的名字只用于取扩展名参与白名单判断；类型走扩展名 + 文件头双重，
        并且两者必须一致；大小有上限；落盘限定在 uploads 目录内。
        不收 svg —— 它能带脚本，落进可访问目录就是存储型 XSS。
        """
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        mod = _media()
        if mod is None:
            return jsonify({"error": "模块加载失败"}), 500
        w = _whisper()

        if request.method == "GET":
            try:
                import vision_notes as _vn
                notes = _vn.latest_notes(scene="private", limit=20)
                return jsonify({"ok": True,
                                "notes": notes,
                                "queue": (w.snapshot() or {}).get("images") or [],
                                "stats": mod.stats()})
            except Exception as e:
                return jsonify({"error": str(e)[:120]}), 500

        f = request.files.get("photo") or request.files.get("file")
        if f is None:
            return jsonify({"error": "没收到文件"}), 400
        try:
            body = f.read()
        except Exception as e:
            return jsonify({"error": "读取失败: " + str(e)[:80]}), 400
        name = str(f.filename or "")
        try:
            path = mod.save_upload(body, name)
        except Exception as e:
            # 校验不过就是 4xx，别把原因吞成 500
            return jsonify({"error": str(e)[:140]}), 400
        caption = str(request.form.get("caption") or "")[:120]
        rec = None
        try:
            rec = w.add_image(path, by="father", caption=caption)
        except Exception as e:
            return jsonify({"ok": False, "error": "入队失败: " + str(e)[:80],
                            "path": path}), 500
        # token 是「天/文件名」形式的相对标记：愿望墙要带图时只能回传这个，
        # 绝对路径永不交给前端，回读时再按标记重新校验一遍。
        return jsonify({"ok": True, "image": rec, "token": mod.token(path),
                        "bytes": len(body or b"")})


    @app.route("/admin/api/bot/photo/file")
    def api_photo_file():
        """回读一张已经落在 uploads 里的图（愿望墙缩略图用）。

        只认 `YYYY-MM-DD/<哈希>.<白名单扩展名>` 这一种标记；不合法就 404，
        不去猜调用方想要哪个文件。管理会话门控与 API 一致。
        """
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        mod = _media()
        if mod is None:
            return jsonify({"error": "模块加载失败"}), 500
        path = mod.resolve(request.args.get("name")
                           or request.args.get("token") or "")
        if not path:
            return jsonify({"error": "not_found"}), 404
        from flask import send_file as _send
        resp = _send(path, conditional=True)
        resp.headers["Cache-Control"] = "private, max-age=300"
        return resp


    @app.route("/admin/api/bot/wishes", methods=["GET", "POST"])
    def api_wishes():
        """愿望墙 + 悄悄话。只活在控制台，不进 QQ，但会写进她的记忆。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        try:
            w = _whisper()
        except Exception as e:
            return jsonify({"error": "模块加载失败: " + str(e)[:100]}), 500

        if request.method == "GET":
            data = w.snapshot()
            data["ok"] = True
            return jsonify(data)

        body = request.get_json(silent=True) or {}
        action = str(body.get("action") or "").strip()
        text = str(body.get("text") or "").strip()

        try:
            if action == "say":
                if not text:
                    return jsonify({"error": "说点什么"}), 400
                w.add_chat("father", text)
                w.push_request("chat", text)
                return jsonify({"ok": True, "queued": True})

            if action == "reply_wish":
                wid = str(body.get("wish_id") or "").strip()
                if not wid or not text:
                    return jsonify({"error": "缺少 wish_id 或内容"}), 400
                w.reply_wish(wid, "father", text)
                w.push_request("wish", text, wid)
                return jsonify({"ok": True, "queued": True})

            if action == "add_wish":
                if not text:
                    return jsonify({"error": "写点什么"}), 400
                # 带的图必须是刚才真的上传成功、且还能按标记解析回来的文件；
                # 校验不过就照写愿望，但把「图没挂上」说清楚，不静默吞掉。
                mark = str(body.get("image") or "").strip()[:80]
                image_error = ""
                if mark:
                    mod = _media()
                    if mod is None or not mod.resolve(mark):
                        image_error = "图不在 uploads 里，只记了文字"
                        mark = ""
                rec = w.add_wish(text, body.get("kind") or "want",
                                 by="father", image=mark)
                return jsonify({"ok": True, "wish": rec,
                                "image_error": image_error})

            if action == "close_wish":
                ok = w.close_wish(str(body.get("wish_id") or ""))
                return jsonify({"ok": bool(ok)})

            return jsonify({"error": "未知操作: " + action}), 400
        except Exception as e:
            return jsonify({"error": "失败: " + str(e)[:120]}), 500

    @app.route("/admin/api/bot/persona-self", methods=["GET", "POST"])
    def api_persona_self():
        """她的人格成长档案。

        【同步铁律】读和写都必须走 persona_store（SQLite），
        不能直接改 self.json —— 那是导出备份，机器人只认数据库。
        之前面板写 JSON，机器人读 SQLite，手工加的记忆会静默丢失。
        """
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401

        import sys as _sys
        bot_dir = str(Path(__file__).parent.parent / "bot")
        if bot_dir not in _sys.path:
            _sys.path.insert(0, bot_dir)
        import persona_store as ps

        data_dir = Path(os.getenv("DATA_DIR", "./data"))
        journal = data_dir / "persona" / "journal.jsonl"
        FIELDS = ("facts", "experiences", "likes", "dislikes", "curious")

        def _profile():
            prof = {k: ps.items(k) for k in FIELDS}
            for k in ("self_view", "voice", "stats", "revision", "updated",
                      "last_consolidated"):
                v = ps.get_meta(k)
                if v is not None:
                    prof[k] = v
            return prof

        out = {"ok": True, "exists": True, "profile": _profile(), "journal_lines": 0,
               "backend": "sqlite", "counts": ps.counts()}
        try:
            if journal.exists():
                with open(journal, encoding="utf-8") as f:
                    out["journal_lines"] = sum(1 for _ in f)
        except Exception:
            pass

        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            action = str(body.get("action") or "update").strip()
            try:
                if action == "add":
                    field = str(body.get("field") or "facts").strip()
                    text = str(body.get("text") or "").strip()
                    if field not in FIELDS:
                        return jsonify({"error": "不支持的字段: " + field}), 400
                    if not text:
                        return jsonify({"error": "内容不能为空"}), 400
                    ps.add(field, text)

                elif action == "remove":
                    field = str(body.get("field") or "facts").strip()
                    idx = int(body.get("index", -1))
                    arr = ps.items(field)
                    if 0 <= idx < len(arr):
                        ps.delete(field, arr[idx])

                elif action == "edit":
                    field = str(body.get("field") or "facts").strip()
                    idx = int(body.get("index", -1))
                    text = str(body.get("text") or "").strip()
                    arr = ps.items(field)
                    if 0 <= idx < len(arr) and text:
                        ps.delete(field, arr[idx])
                        ps.add(field, text)

                elif action == "self_view":
                    ps.set_meta("self_view", str(body.get("text") or "")[:600])

                else:
                    return jsonify({"error": "未知操作: " + action}), 400

                prof = _profile()
                prof["revision"] = int(prof.get("revision", 0) or 0) + 1
                prof["updated"] = time.time()
                ps.set_meta("revision", prof["revision"])
                ps.set_meta("updated", prof["updated"])

                # 同时刷一份 JSON 导出，方便人工查看/备份（失败不影响）
                try:
                    sf = data_dir / "persona" / "self.json"
                    sf.parent.mkdir(parents=True, exist_ok=True)
                    sf.write_text(json.dumps(prof, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
                except Exception:
                    pass

                out["profile"] = prof
                out["counts"] = ps.counts()
                return jsonify({"ok": True, "profile": prof,
                                "counts": out["counts"]})
            except Exception as e:
                return jsonify({"error": "写入失败: " + str(e)[:100]}), 500

        return jsonify(out)

    def _load_custom_memory():
        dd = Path(os.getenv("DATA_DIR", "./data")) / "memory" / "custom_memory.json"
        try:
            if dd.exists():
                return (json.loads(dd.read_text(encoding="utf-8")) or {}).get("entries") or []
        except Exception:
            pass
        return []

    def _save_custom_memory(entries):
        dd = Path(os.getenv("DATA_DIR", "./data")) / "memory" / "custom_memory.json"
        dd.parent.mkdir(parents=True, exist_ok=True)
        clean = []
        for it in entries or []:
            txt = str((it or {}).get("text") or "").strip()
            if not txt:
                continue
            clean.append({
                "id": str((it or {}).get("id") or secrets.token_hex(4)),
                "text": txt[:300],
                "ts": float((it or {}).get("ts") or time.time()),
            })
        clean = clean[-60:]
        tmp = dd.with_suffix(".tmp")
        tmp.write_text(json.dumps({"entries": clean}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(dd)
        return clean

    @app.route("/admin/api/bot/custom-memory", methods=["GET", "POST"])
    def api_custom_memory():
        """主人亲手写的记忆：增删改查。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        if request.method == "GET":
            items = _load_custom_memory()
            return jsonify({"entries": items, "count": len(items)})
        data = request.get_json(silent=True) or {}
        action = str(data.get("action") or "add").strip().lower()
        try:
            if action == "add":
                text = str(data.get("text") or "").strip()
                if not text:
                    return jsonify({"error": "内容为空"}), 400
                items = _load_custom_memory()
                items.append({"id": secrets.token_hex(4), "text": text[:300], "ts": time.time()})
                items = _save_custom_memory(items)
                return jsonify({"ok": True, "entries": items, "count": len(items)})
            if action == "delete":
                eid = str(data.get("id") or "").strip()
                items = [x for x in _load_custom_memory() if str(x.get("id")) != eid]
                items = _save_custom_memory(items)
                return jsonify({"ok": True, "entries": items, "count": len(items)})
            if action == "replace":
                items = _save_custom_memory(data.get("entries") or [])
                return jsonify({"ok": True, "entries": items, "count": len(items)})
            return jsonify({"error": "未知操作"}), 400
        except Exception as e:
            return jsonify({"error": str(e)[:150]}), 500

    def _asset_version():
        """用静态资源文件的 mtime 当版本号：文件一改 URL 就变，缓存不会读到旧内容。"""
        try:
            best = 0
            d = Path(__file__).parent / "static"
            for name in ("console.css", "console.js"):
                f = d / name
                if f.exists():
                    best = max(best, int(f.stat().st_mtime))
            return str(best or 1)
        except Exception:
            return "1"

    @app.route("/admin/assets/<name>", methods=["GET"])
    def admin_asset(name):
        """控制台的 CSS/JS。

        以前这两坨是内联在 console.html 里的（合计 95KB），
        每次打开面板都要重新下载一遍。拆出来后浏览器能缓存，
        日常访问只需拿 25KB 的 HTML。
        """
        if name not in ("console.css", "console.js"):
            return ("not found", 404)
        d = Path(__file__).parent / "static"
        f = d / name
        if not f.exists():
            return ("not found", 404)
        mime = "text/css; charset=utf-8" if name.endswith(".css") else \
               "application/javascript; charset=utf-8"
        body = f.read_text(encoding="utf-8")
        # 用 app.make_response，避免依赖 flask.Response 是否被导入
        resp = app.make_response(body)
        resp.headers["Content-Type"] = mime
        # 强缓存一天；URL 带 ?v=<mtime>，改了文件 URL 会变，不会读到旧版本
        resp.headers["Cache-Control"] = "public, max-age=86400"
        resp.headers["ETag"] = '"%s"' % _asset_version()
        return resp

    @app.after_request
    def _security_headers(resp):
        """安全响应头。

        HSTS：让浏览器之后一律用 HTTPS，减少移动端「不安全」提示。
        CSP：全部资源都是同源内联/外链，不放开任何第三方，降低被误报的概率。
        """
        try:
            if request.path.startswith("/admin"):
                resp.headers.setdefault(
                    "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                resp.headers.setdefault(
                    "Content-Security-Policy",
                    "default-src 'self'; "
                    "img-src 'self' data: blob:; "
                    "style-src 'self' 'unsafe-inline'; "
                    "script-src 'self' 'unsafe-inline'; "
                    "connect-src 'self'; "
                    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
                resp.headers.setdefault("X-Frame-Options", "DENY")
                resp.headers.setdefault("X-Content-Type-Options", "nosniff")
                resp.headers.setdefault("Referrer-Policy", "same-origin")
        except Exception:
            pass
        return resp

    @app.route("/admin/api/bot/napcat/status", methods=["GET"])
    def api_napcat_status():
        """NapCat 登录状态：在线 / 待扫码 / 容器没跑。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized", "need_login": True}), 401
        import subprocess as _sp
        out = {"state": "unknown", "port_ok": False, "online": False,
               "has_qr": False, "qr_age": -1, "container": ""}
        try:
            r = _sp.run(["docker", "inspect", "napcat", "--format", "{{.State.Status}}"],
                        capture_output=True, text=True, timeout=15)
            out["container"] = (r.stdout or "").strip()
        except Exception:
            pass
        # 3001 端口（OneBot WS）
        try:
            import socket as _sk
            s1 = _sk.socket(); s1.settimeout(2)
            s1.connect(("127.0.0.1", 3001)); s1.close()
            out["port_ok"] = True
        except Exception:
            out["port_ok"] = False

        # 日志判断：最近出现「二维码已保存」之后有没有「Adapter opened」
        try:
            r = _sp.run(["docker", "logs", "--tail", "60", "napcat"],
                        capture_output=True, text=True, timeout=20)
            txt = (r.stdout or "") + (r.stderr or "")
            last_qr = txt.rfind("二维码已保存")
            last_ok = max(txt.rfind("Adapter opened"), txt.rfind("适配器初始化完成"))
            if last_qr > last_ok:
                out["has_qr"] = True
                out["state"] = "waiting_scan"
            elif last_ok > 0:
                out["online"] = True
                out["state"] = "online"
            else:
                out["state"] = "unknown"
        except Exception:
            pass
        return jsonify({"ok": True, "data": out})

    @app.route("/admin/api/bot/napcat/qr", methods=["GET"])
    def api_napcat_qr():
        """返回容器里最新的登录二维码 PNG。

        容器内的 cache/ 没有挂载到宿主机（宿主机 /opt/napcat/cache 是旧文件），
        所以每次现从容器拷，保证拿到的是当前有效的那张。
        """
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized", "need_login": True}), 401
        import subprocess as _sp
        import io as _io
        tmp = "/tmp/napcat_qr_%d.png" % int(__import__("time").time())
        try:
            r = _sp.run(["docker", "cp", "napcat:/app/napcat/cache/qrcode.png", tmp],
                        capture_output=True, text=True, timeout=25)
            if not os.path.isfile(tmp):
                return jsonify({"ok": False,
                                "error": "没拿到二维码（可能已登录或容器没跑）",
                                "stderr": (r.stderr or "")[:120]}), 404
            with open(tmp, "rb") as f:
                data = f.read()
            try:
                os.unlink(tmp)
            except Exception:
                pass
            from flask import Response as _Resp
            resp = _Resp(data, mimetype="image/png")
            resp.headers["Cache-Control"] = "no-store, max-age=0"
            return resp
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:120]}), 500

    @app.route("/admin/api/bot/tokens", methods=["GET"])
    def api_tokens():
        """今日 token 用量（按模型汇总）。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized", "need_login": True}), 401
        import sys as _sys
        _bd = str(Path(__file__).parent.parent / "bot")
        if _bd not in _sys.path:
            _sys.path.insert(0, _bd)
        try:
            import token_stats as _ts
            return jsonify({"ok": True, "today": _ts.today(),
                            "recent": _ts.recent(7),
                            "prices": _ts.price_table()})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:150]}), 500

    @app.route("/admin/api/bot/billing", methods=["GET", "POST"])
    def api_billing():
        """账单对账：粘贴控制台账单 → 解析 → 和本地记录比对。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        import sys as _sys
        _bd = str(Path(__file__).parent.parent / "bot")
        if _bd not in _sys.path:
            _sys.path.insert(0, _bd)
        try:
            import billing as _bl
            import token_stats as _ts
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:150]}), 500

        # 存盘位置：data/billing_baseline.json（最近一次粘贴的账单）
        store = Path(os.getenv("DATA_DIR", "./data")) / "billing_baseline.json"

        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            action = str(body.get("action") or "parse")
            if action == "pull":
                # 直接从阿里云 BSS 拉账单，不用再手动粘贴
                try:
                    import aliyun_bss as _bss
                    if not _bss.configured():
                        return jsonify({"ok": False,
                                        "error": "阿里云 AK/SK 未配置"}), 400
                    day = str(body.get("date") or "").strip() or None
                    bill = _bss.fetch_bill(day=day)
                    if not bill.get("models"):
                        return jsonify({"ok": False,
                                        "error": "这一天还没有计费数据（账单有延迟）"}), 400
                    try:
                        store.parent.mkdir(parents=True, exist_ok=True)
                        store.write_text(json.dumps({"parsed": {
                            bill["date"]: {m: {k: list(v) for k, v in cat.items()}
                                           for m, cat in bill["models"].items()}},
                            "source": "bss"}, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
                    return jsonify({"ok": True, "source": "bss",
                                    "parsed": _billing_view(_bl, {
                                        bill["date"]: bill["models"]}),
                                    "total": bill.get("total"),
                                    "items": bill.get("items")})
                except Exception as e:
                    return jsonify({"ok": False, "error": str(e)[:150]}), 500
            if action == "clear":
                try:
                    store.unlink()
                except FileNotFoundError:
                    pass
                return jsonify({"ok": True, "cleared": True})
            text = str(body.get("text") or "")
            parsed = _bl.parse(text)
            if not parsed:
                return jsonify({"ok": False,
                                "error": "没解析出内容，确认粘贴的是控制台账单原文"}), 400
            try:
                store.parent.mkdir(parents=True, exist_ok=True)
                store.write_text(json.dumps({"raw": text[:20000], "parsed": {
                    d: {m: {k: list(v) for k, v in cat.items()} for m, cat in ms.items()}
                    for d, ms in parsed.items()}}, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            return jsonify({"ok": True, "parsed": _billing_view(_bl, parsed)})

        # GET：有存盘就回放，顺便和当天本地记录比一次
        parsed = {}
        if store.exists():
            try:
                saved = json.loads(store.read_text(encoding="utf-8"))
                parsed = {d: {m: {k: tuple(v) for k, v in cat.items()}
                              for m, cat in ms.items()}
                          for d, ms in (saved.get("parsed") or {}).items()}
            except Exception:
                parsed = {}
        return jsonify({"ok": True, "parsed": _billing_view(_bl, parsed),
                        "today": _ts.today()})

    def _billing_view(_bl, parsed):
        """把解析结果整理成前端好渲染的形状，并附上对账结论。"""
        try:
            import token_stats as _ts2
            today_rows = {m.get("model"): m for m in
                          (_ts2.today().get("models") or [])}
        except Exception:
            today_rows = {}
        out = []
        for day in sorted(parsed.keys(), reverse=True):
            for model, cat in (parsed.get(day) or {}).items():
                local = today_rows.get(model) or {}
                # compare 的第 2 个参数是「这个模型的类目数据」，不是模型名
                row = _bl.compare(day, cat, local)
                row["model"] = model
                out.append(row)
        out.sort(key=lambda x: (x.get("date") or "", x.get("model") or ""), reverse=True)
        return out

    @app.route("/admin/api/bot/health", methods=["GET", "POST"])
    def api_health():
        """服务健康状态 + watchdog 配置。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        import sys as _sys
        _bd = str(Path(__file__).parent.parent / "bot")
        if _bd not in _sys.path:
            _sys.path.insert(0, _bd)

        # 让 watchdog 用和服务一致的数据目录
        os.environ.setdefault("DATA_DIR",
                              str(Path(os.getenv("DATA_DIR", "./data")).resolve()))

        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            if str(body.get("action") or "") == "recheck":
                try:
                    import watchdog as _wd
                    d = _wd.status()
                    d["probe"] = _probe_view()
                    return jsonify({"ok": True, "data": d})
                except Exception as e:
                    return jsonify({"error": str(e)[:120]}), 500
            return jsonify({"error": "未知操作"}), 400

        try:
            import watchdog as _wd
            d = _wd.status()
            d["probe"] = _probe_view()
            return jsonify({"ok": True, "data": d})
        except Exception as e:
            return jsonify({"error": str(e)[:150]}), 500

    def _probe_view():
        """探针结果：掉线时长、原因、shm、崩溃次数、多余账号。"""
        try:
            import login_probe as _lp
            return _lp.status()
        except Exception as e:
            return {"state": "unknown", "error": str(e)[:80]}

    @app.route("/admin/api/bot/affection", methods=["GET", "POST"])
    def api_affection():
        """她对每个人的好感度。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        import sys as _sys
        _bd = str(Path(__file__).parent.parent / "bot")
        if _bd not in _sys.path:
            _sys.path.insert(0, _bd)
        import affection as af

        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            act = str(body.get("action") or "").strip()
            try:
                if act == "reset":
                    af.reset(body.get("uid") or None)
                    return jsonify({"ok": True, "list": af.snapshot()})
                if act == "bump":
                    uid = str(body.get("uid") or "").strip()
                    delta = float(body.get("delta") or 0)
                    if not uid:
                        return jsonify({"error": "缺少 uid"}), 400
                    af.bump(uid, delta, "面板手动")
                    return jsonify({"ok": True, "list": af.snapshot()})
                if act == "set":
                    uid = str(body.get("uid") or "").strip()
                    if not uid:
                        return jsonify({"error": "缺少 uid"}), 400
                    af.set_score(uid, float(body.get("score") or 0), "面板手动设定")
                    return jsonify({"ok": True, "list": af.snapshot()})
                return jsonify({"error": "未知操作"}), 400
            except Exception as e:
                return jsonify({"error": str(e)[:120]}), 500

        return jsonify({"ok": True, "list": af.snapshot(),
                        "scale": {"min": af.MIN_SCORE, "max": af.MAX_SCORE,
                                  "daily_cap": af.DAILY_CAP}})

    @app.route("/admin/api/bot/memories", methods=["GET"])
    def api_memories():
        """列出机器人记住的所有会话（私聊/群聊），供面板查看长期记忆。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        data_dir = Path(os.getenv("DATA_DIR", "./data"))
        store = data_dir / "memory" / "histories.json"
        sessions = []
        if store.exists():
            try:
                raw = json.loads(store.read_text(encoding="utf-8"))
                for key, rec in (raw or {}).items():
                    turns = rec.get("turns") or []
                    if not turns:
                        continue
                    last = turns[-1].get("content", "") if turns else ""
                    sessions.append({
                        "key": key,
                        "kind": rec.get("kind", "other"),
                        "user_id": rec.get("user_id"),
                        "group_id": rec.get("group_id"),
                        "turns": len(turns),
                        "updated": rec.get("updated", 0),
                        "preview": str(last)[:80],
                    })
            except Exception as e:
                return jsonify({"error": str(e)[:150], "sessions": []})
        # 带上长期记忆摘要
        summaries = {}
        mem_file = data_dir / "memory" / "private_memory.json"
        if mem_file.exists():
            try:
                summaries = json.loads(mem_file.read_text(encoding="utf-8")) or {}
            except Exception:
                summaries = {}
        group_sums = {}
        gm_file = data_dir / "group_memory_state.json"
        if gm_file.exists():
            try:
                gs = json.loads(gm_file.read_text(encoding="utf-8")) or {}
                group_sums = gs.get("group_memory_summary") or {}
            except Exception:
                group_sums = {}
        for s in sessions:
            key = s.get("key")
            s["summary"] = summaries.get(key) or (group_sums.get(s.get("group_id") or "") if s.get("kind") == "group" else "") or ""
        sessions.sort(key=lambda x: x.get("updated", 0), reverse=True)
        return jsonify({"sessions": sessions, "count": len(sessions)})

    @app.route("/admin/api/bot/memories/<path:key>", methods=["GET"])
    def api_memory_detail(key):
        """看某个会话的完整对话记录。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        data_dir = Path(os.getenv("DATA_DIR", "./data"))
        store = data_dir / "memory" / "histories.json"
        if not store.exists():
            return jsonify({"error": "not found", "turns": []}), 404
        try:
            raw = json.loads(store.read_text(encoding="utf-8"))
        except Exception as e:
            return jsonify({"error": str(e)[:150], "turns": []}), 500
        rec = (raw or {}).get(key)
        if not rec:
            return jsonify({"error": "session not found", "turns": []}), 404
        return jsonify(rec)


    @app.route("/admin/api/bot/memories/rebuild", methods=["POST"])
    def api_memory_rebuild():
        """请求机器人重建某个会话的长期记忆（异步，bot 轮询执行）。"""
        if not session.get("ceylan_admin"):
            return jsonify({"error": "unauthorized"}), 401
        body = request.get_json(silent=True) or {}
        key = (body.get("key") or "").strip()
        kind = (body.get("kind") or "private").strip()
        if not key:
            return jsonify({"error": "缺少 key"}), 400
        data_dir = Path(os.getenv("DATA_DIR", "./data"))
        mem_dir = data_dir / "memory"
        try:
            mem_dir.mkdir(parents=True, exist_ok=True)
            req_file = mem_dir / "rebuild_requests.json"
            reqs = {}
            if req_file.exists():
                try:
                    reqs = json.loads(req_file.read_text(encoding="utf-8")) or {}
                except Exception:
                    reqs = {}
            req_id = secrets.token_hex(8)
            reqs[req_id] = {"key": key, "kind": kind, "ts": time.time()}
            tmp = req_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(reqs, ensure_ascii=False), encoding="utf-8")
            tmp.replace(req_file)
            return jsonify({"ok": True, "id": req_id, "note": "已提交，机器人会在 20 秒内处理"})
        except Exception as e:
            return jsonify({"error": str(e)[:150]}), 500

    print("[Dashboard] Routes installed: dashboard, personality, persona-mode, models-config, style, effective-config, qq-faces, memories, memory-rebuild", flush=True)
