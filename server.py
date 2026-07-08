# -*- coding: utf-8 -*-
"""微信抢单系统 - Flask API 服务器 v1.2
修复: 测试回复返回详细诊断信息
"""
import json, os, sys, threading, time
from datetime import date, datetime
from flask import Flask, request, jsonify, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import database as db

app = Flask(__name__, static_folder="web/static", static_url_path="/static")

# ==================== 初始化 ====================
db.init_db()

# 全局监控实例
_monitor_instance = None
_monitor_lock = threading.Lock()

def get_monitor():
    global _monitor_instance
    if _monitor_instance is None:
        from wechat_monitor import WeChatMonitor
        def on_grab(record_id, match_result):
            print(f"[系统] 抢单成功! record={record_id} type={match_result.get('type','')}")
        _monitor_instance = WeChatMonitor(db.get_config, on_grab)
    return _monitor_instance

def service_should_run():
    return db.get_config("service_status") in ("running", None)


def _auth_token():
    auth = request.headers.get('Authorization', '')
    if auth.lower().startswith('bearer '):
        return auth.split(' ', 1)[1].strip()
    return request.cookies.get('xm_token', '') or request.args.get('token', '')

def current_user():
    return db.get_user_by_token(_auth_token())

def require_user():
    user = current_user()
    if not user:
        return None, (jsonify({'ok': False, 'error': 'unauthorized'}), 401)
    return user, None

# ==================== 页面路由 ====================
@app.route("/")
def index():
    return send_from_directory(os.path.join(BASE_DIR, "web"), "index.html")

@app.route("/schedule.html")
def schedule_page():
    return send_from_directory(os.path.join(BASE_DIR, "web"), "schedule.html")

@app.route("/landing.html")
def landing_page():
    return send_from_directory(os.path.join(BASE_DIR, "web"), "landing.html")

@app.route("/login.html")
def login_page():
    return send_from_directory(os.path.join(BASE_DIR, "web"), "login.html")

# ==================== ?? API ====================
@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    data = request.get_json(force=True)
    try:
        result = db.login_or_create_user(data.get('phone',''), data.get('code',''))
        resp = jsonify({'ok': True, **result})
        resp.set_cookie('xm_token', result['token'], max_age=30*24*3600, httponly=False, samesite='Lax')
        return resp
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

@app.route("/api/auth/me", methods=["GET"])
def api_auth_me():
    user = current_user()
    return jsonify({'ok': True, 'user': user}) if user else (jsonify({'ok': False, 'error': 'unauthorized'}), 401)

@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    token = _auth_token()
    if token: db.logout_token(token)
    resp = jsonify({'ok': True})
    resp.delete_cookie('xm_token')
    return resp

# ==================== 配置 API ====================
@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(db.get_all_config())

@app.route("/api/config", methods=["POST"])
def api_save_config():
    data = request.get_json(force=True)
    db.save_config_batch(data)
    db.add_log("config_save", f"更新了 {len(data)} 项配置")
    return jsonify({"ok": True})

@app.route("/api/config/<key>", methods=["GET"])
def api_get_config_key(key):
    val = db.get_config(key)
    return jsonify({"key": key, "value": val}) if val else (jsonify({"error": "not found"}), 404)

# ==================== 群组 API ====================
@app.route("/api/groups", methods=["GET"])
def api_get_groups():
    return jsonify(db.get_monitored_groups())

@app.route("/api/groups", methods=["POST"])
def api_add_group():
    data = request.get_json(force=True)
    db.add_group(data["group_name"])
    db.add_log("group_add", f"添加监听群: {data['group_name']}")
    return jsonify({"ok": True})

@app.route("/api/groups/<int:gid>", methods=["DELETE"])
def api_remove_group(gid):
    db.remove_group(gid)
    return jsonify({"ok": True})

@app.route("/api/groups/<int:gid>/toggle", methods=["POST"])
def api_toggle_group(gid):
    data = request.get_json(force=True)
    db.toggle_group(gid, data["enabled"])
    return jsonify({"ok": True})

@app.route("/api/groups/scan", methods=["POST"])
def api_scan_groups():
    """扫描微信窗口获取当前可见的群聊列表"""
    try:
        import uiautomation as uia
        
        groups = []
        wechat = uia.WindowControl(searchDepth=1, ClassName='WeChatMainWndForPC')
        if not wechat.Exists(maxSearchSeconds=2):
            wechat = uia.WindowControl(searchDepth=1, Name='微信')
        
        if not wechat or not wechat.Exists(maxSearchSeconds=1):
            return jsonify({"ok": False, "error": "未找到微信窗口，请确保微信已登录并显示在桌面", "groups": []})
        
        for list_name in ['会话', 'Contacts', '聊天', 'Chats']:
            try:
                session_list = wechat.ListControl(Name=list_name)
                if session_list and session_list.Exists(maxSearchSeconds=0.5):
                    for item in session_list.GetChildren():
                        try:
                            name = item.Name
                            if name and len(name) > 1 and name not in ['微信', 'WeChat', '']:
                                groups.append(name)
                        except:
                            pass
                    if groups:
                        break
            except:
                continue
        
        if not groups:
            try:
                chat_list = wechat.ListControl()
                if chat_list and chat_list.Exists(maxSearchSeconds=1):
                    for item in chat_list.GetChildren():
                        try:
                            name = item.Name
                            if name and len(name) > 1:
                                groups.append(name)
                        except:
                            pass
            except:
                pass
        
        if not groups:
            return jsonify({"ok": True, "groups": [], "tip": "请打开微信主界面（显示聊天列表），再点扫描"})
        
        groups = list(set(groups))
        existing = {g["group_name"] for g in db.get_monitored_groups()}
        added = 0
        for g in groups:
            if g not in existing:
                db.add_group(g)
                added += 1
        
        db.add_log("group_scan", f"扫描到 {len(groups)} 个群，新增 {added} 个")
        
        return jsonify({
            "ok": True,
            "groups": groups,
            "added": added,
            "total": len(groups)
        })
        
    except ImportError:
        return jsonify({"ok": False, "error": "uiautomation 未安装", "groups": []})
    except Exception as e:
        return jsonify({"ok": False, "error": f"扫描失败: {str(e)}", "groups": []})

# ==================== 抢单记录 API ====================
@app.route("/api/grabs", methods=["GET"])
def api_get_grabs():
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    return jsonify(db.get_grab_records(limit, offset))

# ==================== 课表 API ====================
@app.route("/api/schedule", methods=["GET"])
def api_get_schedule():
    user, err = require_user()
    if err: return err
    entries = db.get_all_schedule_entries(user['id'])
    return jsonify(entries)

@app.route("/api/schedule/today", methods=["GET"])
def api_get_today_schedule():
    user, err = require_user()
    if err: return err
    return jsonify([e for e in db.get_all_schedule_entries(user['id']) if e.get('day_of_week') == date.today().isoweekday()])

@app.route("/api/schedule", methods=["POST"])
def api_add_schedule():
    user, err = require_user()
    if err: return err
    data = request.get_json(force=True)
    data['user_id'] = user['id']
    if data.get("_skip_conflict"):
        entry_id = db.add_schedule_entry(data)
        return jsonify({"ok": True, "id": entry_id})
    
    if db.get_config("conflict_detection") == "true":
        conflicts = db.check_schedule_conflict(
            data.get("day_of_week", 1),
            data.get("start_time", "20:00"),
            data.get("duration_min", 60),
            user_id=user['id']
        )
        if conflicts:
            return jsonify({"ok": False, "conflict": True, "conflicts": conflicts})
    
    entry_id = db.add_schedule_entry(data)
    db.add_log("schedule_add", f"添加课表: {data.get('student_name','')} 周{data.get('day_of_week','')} {data.get('start_time','')}")
    return jsonify({"ok": True, "id": entry_id})

@app.route("/api/schedule/<int:eid>", methods=["PUT"])
def api_update_schedule(eid):
    user, err = require_user()
    if err: return err
    data = request.get_json(force=True)
    db.update_schedule_entry(eid, data, user['id'])
    return jsonify({"ok": True})

@app.route("/api/schedule/<int:eid>", methods=["DELETE"])
def api_delete_schedule(eid):
    user, err = require_user()
    if err: return err
    db.delete_schedule_entry(eid, user['id'])
    return jsonify({"ok": True})

@app.route("/api/schedule/conflict-check", methods=["POST"])
def api_check_conflict():
    user, err = require_user()
    if err: return err
    data = request.get_json(force=True)
    conflicts = db.check_schedule_conflict(
        data.get("day_of_week", 1),
        data.get("start_time", "20:00"),
        data.get("duration_min", 60),
        data.get("exclude_id"),
        user_id=user['id']
    )
    return jsonify({"has_conflict": len(conflicts) > 0, "conflicts": conflicts})


# ==================== 抗遗忘 Review API ====================

@app.route("/api/schedule/<int:eid>/reviews", methods=["GET"])
def api_get_reviews(eid):
    """Get all review entries for a parent schedule entry"""
    user, err = require_user()
    if err: return err
    reviews = db.get_review_entries_for_parent(eid, user['id'])
    return jsonify(reviews)

@app.route("/api/schedule/<int:eid>/generate-reviews", methods=["POST"])
def api_generate_reviews(eid):
    """Generate anti-forgetting review entries for a schedule entry"""
    user, err = require_user()
    if err: return err
    data = request.get_json(force=True)
    intervals = data.get("intervals", [1, 2, 4, 7, 15, 30])
    schedule_date = data.get("schedule_date", None)
    student_name = data.get("student_name", None)
    start_time = data.get("start_time", None)
    duration_min = data.get("duration_min", None)
    
    try:
        created_ids = db.generate_review_entries(
            parent_id=eid,
            intervals=intervals,
            student_name=student_name,
            start_time=start_time,
            duration_min=duration_min,
            schedule_date=schedule_date,
            user_id=user['id']
        )
        db.add_log("review_gen", f"为课表#{eid}生成了{len(created_ids)}条抗遗忘复习 (间隔: {intervals})")
        return jsonify({"ok": True, "created": len(created_ids), "ids": created_ids})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()})

@app.route("/api/schedule/<int:eid>/reviews", methods=["DELETE"])
def api_delete_reviews(eid):
    """Delete all review entries for a parent schedule entry"""
    user, err = require_user()
    if err: return err
    db.delete_review_entries(eid, user['id'])
    db.add_log("review_del", f"删除了课表#{eid}的所有抗遗忘复习")
    return jsonify({"ok": True})

@app.route("/api/schedule/week", methods=["GET"])
def api_get_schedule_for_week():
    """Get schedule entries for a specific week (including date-specific reviews)"""
    from datetime import date, timedelta
    week_str = request.args.get("week_start", "")
    if week_str:
        from datetime import datetime
        week_start = datetime.strptime(week_str, "%Y-%m-%d").date()
    else:
        today = date.today()
        week_start = today - timedelta(days=today.isoweekday() - 1)
    
    user, err = require_user()
    if err: return err
    entries = db.get_all_schedule_entries_for_week(week_start, user['id'])
    return jsonify({"week_start": week_start.strftime("%Y-%m-%d"), "entries": entries})

# ==================== 收入统计 API ====================
@app.route("/api/income", methods=["GET"])
def api_get_income():
    user, err = require_user()
    if err: return err
    # ??????????????????????????????
    entries = db.get_all_schedule_entries(user['id'])
    total = sum(float(e.get('price_per_session') or 0) for e in entries if e.get('status') == 'completed' and e.get('source') != 'review')
    return jsonify({'total_income': total, 'entries': entries})

# ==================== 统计 API ====================
@app.route("/api/stats", methods=["GET"])
def api_get_stats():
    stats = db.get_db_stats()
    stats["service_status"] = db.get_config("service_status") or "stopped"
    return jsonify(stats)

# ==================== 日志 API ====================
@app.route("/api/logs", methods=["GET"])
def api_get_logs():
    limit = request.args.get("limit", 50, type=int)
    return jsonify(db.get_recent_logs(limit))

# ==================== 服务控制 ====================
_monitor_thread = None

def _start_monitor_thread():
    global _monitor_thread
    try:
        monitor = get_monitor()
        if not monitor.running:
            monitor.start()
            print("[服务] 微信监控已启动")
    except Exception as e:
        print(f"[服务] 启动监控失败: {e}")

def _stop_monitor_thread():
    global _monitor_thread
    try:
        if _monitor_instance and _monitor_instance.running:
            _monitor_instance.stop()
            print("[服务] 微信监控已停止")
    except Exception as e:
        print(f"[服务] 停止监控失败: {e}")

@app.route("/api/service/start", methods=["POST"])
def api_service_start():
    db.set_config("service_status", "running")
    db.add_log("service_start", "服务已启动")
    t = threading.Thread(target=_start_monitor_thread, daemon=True)
    t.start()
    return jsonify({"ok": True})

@app.route("/api/service/stop", methods=["POST"])
def api_service_stop():
    db.set_config("service_status", "stopped")
    _stop_monitor_thread()
    db.add_log("service_stop", "服务已停止")
    return jsonify({"ok": True})

@app.route("/api/service/pause", methods=["POST"])
def api_service_pause():
    db.set_config("service_status", "paused")
    _stop_monitor_thread()
    db.add_log("service_pause", "服务已暂停")
    return jsonify({"ok": True})

@app.route("/api/service/resume", methods=["POST"])
def api_service_resume():
    db.set_config("service_status", "running")
    t = threading.Thread(target=_start_monitor_thread, daemon=True)
    t.start()
    db.add_log("service_resume", "服务已恢复")
    return jsonify({"ok": True})

@app.route("/api/service/status", methods=["GET"])
def api_service_status():
    return jsonify({"status": db.get_config("service_status") or "stopped"})


# ==================== 测试回复 - 返回详细诊断 (线程安全版) ====================
@app.route("/api/test-reply", methods=["POST"])
def api_test_reply():
    """测试回复功能 - 逐步诊断并返回详细信息（COM 线程安全）"""
    data = request.get_json(force=True)
    text = data.get("text", "1")
    quote = data.get("quote", True)

    diag = {"ok": False, "steps": [], "msg": ""}

    try:
        # COM 初始化（uiautomation 在 Flask 线程中需要 COM）
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except ImportError:
            pass

        from wechat_monitor import WeChatMonitor

        def getter(k):
            if k == "quote_reply":
                return "true" if quote else "false"
            return db.get_config(k) or ""

        m = WeChatMonitor(getter)
        
        # Step 0: 初始化 uiautomation
        try:
            m._ensure_uia()
            uia = m._uia
            diag["steps"].append("step0_ok: uiautomation 加载成功")
        except Exception as e:
            diag["steps"].append(f"step0_fail: uiautomation 加载失败 - {str(e)}")
            diag["msg"] = f"uiautomation 加载失败: {str(e)}"
            return jsonify(diag)

        # Step 1: 找微信窗口
        try:
            wx = m._find_wechat_window(uia)
        except Exception as e:
            diag["steps"].append(f"step1_fail: 查找微信窗口异常 - {str(e)}")
            diag["msg"] = f"查找微信窗口异常: {str(e)}"
            return jsonify(diag)
            
        if not wx:
            diag["steps"].append("step1_fail: 未找到微信窗口 - 请确认微信已登录并显示在桌面上")
            diag["msg"] = "未找到微信窗口！请确认微信已登录并显示在桌面上"
            return jsonify(diag)
        diag["steps"].append(f"step1_ok: 找到微信窗口 (Class={wx.ClassName}, Name={wx.Name})")

        # Step 2: 找输入框
        try:
            edit = m._find_input_edit_deep(wx, uia)
        except Exception as e:
            diag["steps"].append(f"step2_fail: 查找输入框异常 - {str(e)}")
            diag["msg"] = f"查找输入框异常: {str(e)}"
            return jsonify(diag)
            
        if not edit:
            diag["steps"].append("step2_fail: 未找到输入框 - 请在微信中打开任意聊天窗口")
            diag["msg"] = "未找到输入框！请在微信中打开任意聊天窗口（点进群聊或好友对话）"
            return jsonify(diag)
        diag["steps"].append(f"step2_ok: 找到输入框 (Name='{edit.Name}', Class={edit.ClassName})")

        # Step 3: 发送
        try:
            ok = m._send_reply(text, None)
            diag["steps"].append(f"step3: send_reply 返回 {ok}")
        except Exception as e:
            diag["steps"].append(f"step3_fail: 发送异常 - {str(e)}")
            diag["msg"] = f"发送过程异常: {str(e)}"
            return jsonify(diag)

        if ok:
            diag["ok"] = True
            diag["msg"] = f"发送成功！已向微信窗口发送 '{text}'，请检查聊天窗口"
        else:
            diag["ok"] = False
            diag["msg"] = "发送失败！请查看服务器控制台日志排查具体原因"

        # COM 清理
        try:
            pythoncom.CoUninitialize()
        except:
            pass

        return jsonify(diag)

    except ImportError as e:
        return jsonify({"ok": False, "msg": f"缺少模块: {str(e)}", "steps": [f"import_error: {str(e)}"]})
    except Exception as e:
        import traceback
        return jsonify({
            "ok": False,
            "msg": f"未知异常: {str(e)}",
            "steps": [f"exception: {traceback.format_exc()}"]
        })


# ==================== 诊断: 转储微信控件树 ====================
@app.route("/api/diagnose", methods=["POST"])
def api_diagnose():
    """诊断端点：转储微信窗口控件树结构"""
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except:
        pass
    
    try:
        from wechat_monitor import WeChatMonitor
        m = WeChatMonitor(lambda k: "1")
        m._ensure_uia()
        uia = m._uia
        
        wx = m._find_wechat_window(uia)
        if not wx:
            return jsonify({"ok": False, "msg": "未找到微信窗口"})
        
        tree = m._dump_control_tree(wx, max_depth=5, max_children=15)
        
        # Also try to find input
        edit = m._find_input_edit_deep(wx, uia)
        edit_info = None
        if edit:
            try:
                er = edit.BoundingRectangle
                edit_info = {
                    "name": edit.Name,
                    "class_name": edit.ClassName,
                    "control_type": edit.ControlTypeName,
                    "rect": f"({er.left},{er.top})-({er.right},{er.bottom})"
                }
            except:
                edit_info = {"name": edit.Name, "class_name": edit.ClassName}
        
        return jsonify({
            "ok": True,
            "window": {"class": wx.ClassName, "name": wx.Name},
            "input_found": edit is not None,
            "input_info": edit_info,
            "control_tree": tree
        })
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "msg": str(e), "trace": traceback.format_exc()})


# ==================== 监控日志 ====================
@app.route("/api/monitor-log", methods=["GET"])
def api_monitor_log():
    try:
        from wechat_monitor import get_monitor_log
        logs = get_monitor_log()
        return jsonify({"ok": True, "logs": logs[-100:]})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ==================== 启动服务器 ====================
def run_server(host="127.0.0.1", port=4876, auto_start=False):
    print(f"[服务器] 启动中 http://{host}:{port}")
    if auto_start or db.get_config("service_status") == "running":
        _start_monitor_thread()
    app.run(host=host, port=port, debug=False, threaded=True)




# ==================== ?? ====================
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 4876))
    host = "0.0.0.0"
    print(f"[???] ??? http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)
