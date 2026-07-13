# -*- coding: utf-8 -*-
"""微信抢单系统 - Flask API 服务器 v1.2
修复: 测试回复返回详细诊断信息
"""
import json, os, sys, threading, time, re, hashlib, secrets
from datetime import date, datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, Response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

def _load_local_env():
    path=os.path.join(BASE_DIR,".env")
    if not os.path.exists(path): return
    with open(path,"r",encoding="utf-8") as f:
        for raw in f:
            line=raw.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            key,value=line.split("=",1)
            os.environ.setdefault(key.strip(),value.strip().strip('"').strip("'"))

_load_local_env()

def _ensure_registration_invite_code():
    code=os.environ.get("REGISTRATION_INVITE_CODE","").strip()
    if code:return code
    code=secrets.token_urlsafe(15)
    os.environ["REGISTRATION_INVITE_CODE"]=code
    try:
        with open(os.path.join(BASE_DIR,".env"),"a",encoding="utf-8") as f:
            f.write("REGISTRATION_INVITE_CODE="+code+"\n")
    except OSError:
        pass
    print("[auth] generated registration invite code: "+code)
    return code

_ensure_registration_invite_code()

import database as db

app = Flask(__name__, static_folder="web/static", static_url_path="/static")

def _normalize_phone(phone):
    phone = re.sub(r"\s+", "", str(phone or ""))
    if not re.fullmatch(r"1[3-9]\d{9}", phone): raise ValueError("\u8bf7\u8f93\u5165\u6b63\u786e\u7684\u4e2d\u56fd\u5927\u9646\u624b\u673a\u53f7")
    return phone

def _reset_code_hash(phone,code):
    return hashlib.sha256(f"{phone}:{code}".encode("utf-8")).hexdigest()

def _login_response(result):
    resp=jsonify({"ok":True,"user":result["user"]})
    resp.set_cookie('xm_token',result['token'],max_age=30*24*3600,httponly=True,secure=request.is_secure,samesite='Lax')
    return resp

db.init_db()

# 全局监控实例
_monitor_instance = None
_monitor_user_id = None
_monitor_lock = threading.RLock()

def get_monitor(user_id=None):
    global _monitor_instance, _monitor_user_id
    with _monitor_lock:
        if _monitor_instance is not None and _monitor_user_id != user_id:
            if _monitor_instance.running: raise RuntimeError("\u5fae\u4fe1\u76d1\u63a7\u6b63\u7531\u53e6\u4e00\u4e2a\u7528\u6237\u4f7f\u7528")
            _monitor_instance = None
        if _monitor_instance is None:
            from wechat_monitor import WeChatMonitor
            def on_grab(record_id, match_result):
                print(f"[SYSTEM] grab success! record={record_id} type={match_result.get('type','')}")
            getter = (lambda key: db.get_config(key, user_id)) if user_id is not None else db.get_config
            _monitor_instance = WeChatMonitor(getter, on_grab, user_id=user_id)
            _monitor_user_id = user_id
        return _monitor_instance

def service_should_run(user_id=None):
    return db.get_config("service_status", user_id) in ("running", None)


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

@app.route("/feedback.html")
def feedback_page():
    return send_from_directory(os.path.join(BASE_DIR, "web"), "feedback.html")

# ==================== ?? API ====================
@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    data=request.get_json(force=True)
    try:
        phone=_normalize_phone(data.get("phone",""))
        result=db.login_with_password(phone,data.get("password",""))
        return _login_response(result)
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),400

@app.route("/api/auth/register", methods=["POST"])
def api_auth_register():
    data=request.get_json(force=True)
    try:
        phone=_normalize_phone(data.get("phone",""))
        expected=os.environ.get("REGISTRATION_INVITE_CODE","")
        supplied=str(data.get("invite_code","")).strip()
        if not expected: raise RuntimeError("REGISTRATION_INVITE_CODE is not configured")
        if not secrets.compare_digest(expected,supplied): raise ValueError("\u9080\u8bf7\u7801\u9519\u8bef")
        result=db.register_or_set_password(phone,data.get("password",""))
        return _login_response(result)
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),400

@app.route("/api/auth/admin/invite-code", methods=["GET"])
def api_auth_admin_invite_code():
    user,err=require_user()
    if err:return err
    if not user.get("is_admin"):return jsonify({"ok":False,"error":"forbidden"}),403
    return jsonify({"ok":True,"invite_code":os.environ.get("REGISTRATION_INVITE_CODE","")})

@app.route("/api/auth/admin/reset-code", methods=["POST"])
def api_auth_admin_reset_code():
    user,err=require_user()
    if err:return err
    if not user.get("is_admin"): return jsonify({"ok":False,"error":"forbidden"}),403
    data=request.get_json(force=True)
    try:
        phone=_normalize_phone(data.get("phone",""))
        code=''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(10))
        db.save_password_reset_code(phone,_reset_code_hash(phone,code),user['id'],15)
        return jsonify({"ok":True,"reset_code":code,"expires_in":900})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),400

@app.route("/api/auth/reset-password", methods=["POST"])
def api_auth_reset_password():
    data=request.get_json(force=True)
    try:
        phone=_normalize_phone(data.get("phone",""))
        code=str(data.get("reset_code","")).strip()
        db.reset_password_with_code(phone,_reset_code_hash(phone,code),data.get("password",""))
        result=db.login_with_password(phone,data.get("password",""))
        return _login_response(result)
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),400

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
    user, err = require_user()
    if err: return err
    return jsonify(db.get_all_config(user['id']))

@app.route("/api/config", methods=["POST"])
def api_save_config():
    user, err = require_user()
    if err: return err
    data = request.get_json(force=True)
    db.save_config_batch(data, user['id'])
    db.add_log("config_save", f"\u66f4\u65b0\u4e86 {len(data)} \u9879\u914d\u7f6e", user['id'])
    return jsonify({"ok": True})

@app.route("/api/config/<key>", methods=["GET"])
def api_get_config_key(key):
    user, err = require_user()
    if err: return err
    val = db.get_config(key, user['id'])
    return jsonify({"key": key, "value": val}) if val is not None else (jsonify({"error": "not found"}), 404)

@app.route("/api/groups", methods=["GET"])
def api_get_groups():
    user, err = require_user()
    if err: return err
    return jsonify(db.get_monitored_groups(user_id=user['id']))

@app.route("/api/groups", methods=["POST"])
def api_add_group():
    user, err = require_user()
    if err: return err
    data = request.get_json(force=True)
    db.add_group(data["group_name"], user['id'])
    db.add_log("group_add", f"添加监听群: {data['group_name']}", user['id'])
    return jsonify({"ok": True})

@app.route("/api/groups/<int:gid>", methods=["DELETE"])
def api_remove_group(gid):
    user, err = require_user()
    if err: return err
    db.remove_group(gid, user['id'])
    return jsonify({"ok": True})

@app.route("/api/groups/<int:gid>/toggle", methods=["POST"])
def api_toggle_group(gid):
    user, err = require_user()
    if err: return err
    data = request.get_json(force=True)
    db.toggle_group(gid, data["enabled"], user['id'])
    return jsonify({"ok": True})

@app.route("/api/groups/scan", methods=["POST"])
def api_scan_groups():
    user, err = require_user()
    if err: return err
    try:
        from wechat_monitor import WeChatMonitor
        monitor = WeChatMonitor(lambda key: db.get_config(key, user['id']), user_id=user['id'])
        monitor._ensure_uia()
        wechat = monitor._find_wechat_window(monitor._uia)
        if not wechat:
            return jsonify({
                "ok": False,
                "error": "\u672a\u627e\u5230\u5fae\u4fe1\u7a97\u53e3\uff0c\u8bf7\u786e\u4fdd\u7535\u8111\u5fae\u4fe1\u5df2\u767b\u5f55",
                "groups": []
            })

        groups, method, visible_count = monitor.scan_visible_groups(wechat)
        groups = list(dict.fromkeys(g.strip() for g in groups if str(g).strip()))
        if not groups:
            return jsonify({
                "ok": True, "groups": [], "total": 0, "added": 0,
                "scan_method": method, "visible_count": visible_count,
                "tip": "\u5df2\u663e\u793a\u5fae\u4fe1\uff0c\u4f46\u672a\u8bc6\u522b\u5230\u7fa4\u804a\u3002\u8bf7\u505c\u7559\u5728\u804a\u5929\u5217\u8868\u540e\u91cd\u8bd5\uff0c\u4e5f\u53ef\u624b\u52a8\u6dfb\u52a0\u7fa4\u540d\u3002"
            })

        existing = {g["group_name"] for g in db.get_monitored_groups(user_id=user['id'])}
        added = 0
        for name in groups:
            if name not in existing:
                db.add_group(name, user['id'])
                added += 1
        db.add_log("group_scan", f"\u626b\u63cf\u5230 {len(groups)} \u4e2a\u7fa4\uff0c\u65b0\u589e {added} \u4e2a\uff0c\u65b9\u5f0f: {method}", user['id'])
        tip = ("\u65b0\u7248\u5fae\u4fe1\u4f7f\u7528\u672c\u673a Windows OCR \u626b\u63cf\u5f53\u524d\u53ef\u89c1\u4f1a\u8bdd\u3002"
               "\u5982\u6709\u975e\u7fa4\u804a\u8bef\u8bc6\uff0c\u53ef\u5728\u5217\u8868\u4e2d\u5220\u9664\u3002") if method == 'windows_ocr' else "\u5df2\u4ece\u5fae\u4fe1\u63a7\u4ef6\u6811\u626b\u63cf\u53ef\u89c1\u4f1a\u8bdd\u3002"
        return jsonify({
            "ok": True, "groups": groups, "added": added, "total": len(groups),
            "visible_count": visible_count, "scan_method": method, "tip": tip
        })
    except ImportError as e:
        return jsonify({"ok": False, "error": "\u7fa4\u804a\u626b\u63cf\u4f9d\u8d56\u4e0d\u5b8c\u6574: " + str(e), "groups": []})
    except Exception as e:
        return jsonify({"ok": False, "error": "\u626b\u63cf\u5931\u8d25: " + str(e), "groups": []})

# ==================== 抢单记录 API ====================
@app.route("/api/grabs", methods=["GET"])
def api_get_grabs():
    user, err = require_user()
    if err: return err
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    return jsonify(db.get_grab_records(limit, offset, user['id']))

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


@app.route("/api/reminders/upcoming", methods=["GET"])
def api_upcoming_reminders():
    user, err = require_user()
    if err: return err
    cfg = db.get_all_config(user['id'])
    today_enabled = cfg.get("today_reminder") == "true"
    pre_enabled = cfg.get("pre_class_reminder") == "true"
    try:
        pre_minutes = int(cfg.get("pre_class_minutes") or 15)
    except Exception:
        pre_minutes = 15
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    today_dow = now.date().isoweekday()
    items = []
    for e in db.get_all_schedule_entries(user['id']):
        if e.get("status") == "cancelled":
            continue
        if e.get("schedule_date"):
            if e.get("schedule_date") != today_str:
                continue
        elif int(e.get("day_of_week") or 0) != today_dow:
            continue
        try:
            start = datetime.strptime(today_str + " " + (e.get("start_time") or "00:00"), "%Y-%m-%d %H:%M")
        except Exception:
            continue
        minutes_until = int((start - now).total_seconds() // 60)
        if pre_enabled and 0 <= minutes_until <= pre_minutes:
            level, reason = "urgent", f"{minutes_until} 分钟后上课"
        elif today_enabled:
            level, reason = "today", "今日课程"
        else:
            continue
        item = dict(e)
        item["minutes_until"] = minutes_until
        item["reminder_level"] = level
        item["reminder_reason"] = reason
        item["display_time"] = start.strftime("%H:%M")
        items.append(item)
    items.sort(key=lambda x: (x.get("minutes_until", 99999), x.get("start_time") or ""))
    return jsonify({"ok": True, "items": items[:8], "pre_class_minutes": pre_minutes})

@app.route("/api/schedule", methods=["POST"])
def api_add_schedule():
    user, err = require_user()
    if err: return err
    data = request.get_json(force=True)
    data['user_id'] = user['id']
    if data.get("_skip_conflict"):
        entry_id = db.add_schedule_entry(data)
        return jsonify({"ok": True, "id": entry_id})
    
    if db.get_config("conflict_detection", user['id']) == "true":
        conflicts = db.check_schedule_conflict(
            data.get("day_of_week", 1),
            data.get("start_time", "20:00"),
            data.get("duration_min", 60),
            user_id=user['id']
        )
        if conflicts:
            return jsonify({"ok": False, "conflict": True, "conflicts": conflicts})
    
    entry_id = db.add_schedule_entry(data)
    db.add_log("schedule_add", f"添加课表: {data.get('student_name','')} 周{data.get('day_of_week','')} {data.get('start_time','')}", user['id'])
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
        db.add_log("review_gen", f"为课表#{eid}生成了{len(created_ids)}条抗遗忘复习 (间隔: {intervals})", user['id'])
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
    db.add_log("review_del", f"删除了课表#{eid}的所有抗遗忘复习", user['id'])
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

# ==================== Course feedback and mobile calendar ====================

def _feedback_student_key(name):
    normalized=re.sub(r"\s+","",str(name or "")).casefold() or "unnamed"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

def _feedback_key(kind,feedback_date,student_name,entry_ids):
    return f"main:{int(entry_ids[0])}:{feedback_date}" if kind=="main" else f"review:{_feedback_student_key(student_name)}:{feedback_date}"

def _entry_occurs_on(entry,target):
    if entry.get("schedule_date"): return entry.get("schedule_date")==target.strftime("%Y-%m-%d")
    return int(entry.get("day_of_week") or 0)==target.isoweekday()

def _feedback_subjects(entries,user_id):
    parent_ids=[int(e["parent_entry_id"]) for e in entries if e.get("parent_entry_id")]
    source=db.get_schedule_entries_by_ids(parent_ids,user_id) if parent_ids else entries
    values=[]
    for entry in source:
        value=str(entry.get("subject_type") or "").strip()
        if value and value not in values and value!="\u6297\u9057\u5fd8": values.append(value)
    return "\u3001".join(values) or "\u8bfe\u7a0b\u77e5\u8bc6\u70b9"

def _serialize_feedback_entry(entry):
    return {"id":entry.get("id"),"student_name":entry.get("student_name") or "","subject_type":entry.get("subject_type") or "","start_time":entry.get("start_time") or "","duration_min":entry.get("duration_min") or 60,"source":entry.get("source") or "manual","review_interval_days":entry.get("review_interval_days"),"parent_entry_id":entry.get("parent_entry_id")}

def _feedback_candidates(target,user_id):
    entries=db.get_schedule_entries_for_date(target,user_id);target_text=target.strftime("%Y-%m-%d")
    candidates=[];review_groups={}
    for entry in entries:
        if entry.get("source")=="review":
            review_groups.setdefault(_feedback_student_key(entry.get("student_name")),[]).append(entry)
        else:
            ids=[entry["id"]];key=_feedback_key("main",target_text,entry.get("student_name"),ids)
            candidates.append({"feedback_key":key,"feedback_kind":"main","feedback_date":target_text,"student_name":entry.get("student_name") or "\u672a\u547d\u540d\u5b66\u751f","schedule_entry_ids":ids,"review_count":0,"suggested_content":_feedback_subjects([entry],user_id),"entries":[_serialize_feedback_entry(entry)],"existing_feedback":db.get_course_feedback_by_key(key,user_id)})
    for group in review_groups.values():
        student=group[0].get("student_name") or "\u672a\u547d\u540d\u5b66\u751f";ids=[e["id"] for e in group]
        key=_feedback_key("review",target_text,student,ids)
        candidates.append({"feedback_key":key,"feedback_kind":"review","feedback_date":target_text,"student_name":student,"schedule_entry_ids":ids,"review_count":len(ids),"suggested_content":_feedback_subjects(group,user_id),"entries":[_serialize_feedback_entry(e) for e in group],"existing_feedback":db.get_course_feedback_by_key(key,user_id)})
    candidates.sort(key=lambda c:(min((e.get("start_time") or "99:99") for e in c["entries"]),c["student_name"],c["feedback_kind"]))
    return candidates

def _validated_feedback_payload(data,user_id):
    date_text=str(data.get("feedback_date") or "").strip()
    try: target=datetime.strptime(date_text,"%Y-%m-%d").date()
    except: raise ValueError("\u8bf7\u9009\u62e9\u6b63\u786e\u7684\u53cd\u9988\u65e5\u671f")
    kind=str(data.get("feedback_kind") or "").strip()
    if kind not in ("main","review"): raise ValueError("\u53cd\u9988\u7c7b\u578b\u65e0\u6548")
    raw_ids=data.get("schedule_entry_ids") or []
    requested={int(x) for x in raw_ids if str(x).isdigit()}
    entries=db.get_schedule_entries_by_ids(raw_ids,user_id)
    if not entries or {e["id"] for e in entries}!=requested: raise ValueError("\u8bfe\u7a0b\u4e0d\u5b58\u5728\u6216\u65e0\u6743\u64cd\u4f5c")
    if any(not _entry_occurs_on(e,target) for e in entries): raise ValueError("\u8bfe\u7a0b\u4e0e\u53cd\u9988\u65e5\u671f\u4e0d\u5339\u914d")
    if kind=="main" and (len(entries)!=1 or entries[0].get("source")=="review"): raise ValueError("\u4e3b\u8bfe\u53cd\u9988\u5fc5\u987b\u5bf9\u5e94\u4e00\u8282\u4e3b\u8bfe")
    if kind=="review":
        if any(e.get("source")!="review" for e in entries): raise ValueError("\u6297\u9057\u5fd8\u53cd\u9988\u53ea\u80fd\u5173\u8054\u590d\u4e60\u8bfe")
        normalized={re.sub(r"\s+","",str(e.get("student_name") or "")).casefold() for e in entries}
        if len(normalized)!=1: raise ValueError("\u4e0d\u540c\u5b66\u751f\u7684\u6297\u9057\u5fd8\u53cd\u9988\u4e0d\u80fd\u5408\u5e76")
        selected=next((c for c in _feedback_candidates(target,user_id) if c["feedback_kind"]=="review" and _feedback_student_key(c["student_name"])==_feedback_student_key(entries[0].get("student_name"))),None)
        if selected: entries=db.get_schedule_entries_by_ids(selected["schedule_entry_ids"],user_id)
    student=entries[0].get("student_name") or "\u672a\u547d\u540d\u5b66\u751f";entry_ids=[e["id"] for e in entries]
    def limited(name,n=2000): return str(data.get(name) or "").strip()[:n]
    try: focus=max(1,min(5,int(data.get("focus_rating") or 3)))
    except: focus=3
    try: mastery=max(1,min(5,int(data.get("mastery_rating") or 3)))
    except: mastery=3
    return {"feedback_key":_feedback_key(kind,date_text,student,entry_ids),"feedback_kind":kind,"feedback_date":date_text,"student_name":student,"schedule_entry_ids":entry_ids,"review_count":len(entry_ids) if kind=="review" else 0,"class_content":limited("class_content") or _feedback_subjects(entries,user_id),"focus_rating":focus,"mastery_rating":mastery,"problems":limited("problems"),"homework":limited("homework"),"next_plan":limited("next_plan"),"teacher_notes":limited("teacher_notes"),"generated_text":limited("generated_text",6000),"generation_method":str(data.get("generation_method") or "manual")[:30]}

@app.route("/api/feedback/candidates",methods=["GET"])
def api_feedback_candidates():
    user,err=require_user()
    if err:return err
    date_text=request.args.get("date") or date.today().strftime("%Y-%m-%d")
    try: target=datetime.strptime(date_text,"%Y-%m-%d").date()
    except: return jsonify({"ok":False,"error":"invalid date"}),400
    return jsonify({"ok":True,"date":date_text,"candidates":_feedback_candidates(target,user["id"])})

@app.route("/api/feedback",methods=["GET"])
def api_feedback_list():
    user,err=require_user()
    if err:return err
    items=db.list_course_feedback(user["id"],request.args.get("limit",50,type=int),request.args.get("student"))
    return jsonify({"ok":True,"items":items})

@app.route("/api/feedback/<int:feedback_id>",methods=["GET"])
def api_feedback_detail(feedback_id):
    user,err=require_user()
    if err:return err
    item=db.get_course_feedback_by_id(feedback_id,user["id"])
    return jsonify({"ok":True,"item":item}) if item else (jsonify({"ok":False,"error":"not found"}),404)

@app.route("/api/feedback/generate",methods=["POST"])
def api_feedback_generate():
    user,err=require_user()
    if err:return err
    try:
        raw=request.get_json(force=True);payload=_validated_feedback_payload(raw,user["id"])
        from feedback_generator import generate_feedback
        result=generate_feedback(payload,lambda key:db.get_config(key,user["id"]),bool(raw.get("use_ai")))
        payload["generated_text"]=result["text"];payload["generation_method"]=result["method"]
        feedback_id=db.upsert_course_feedback(payload,user["id"])
        db.add_log("feedback_generate",f"{payload['student_name']} {payload['feedback_date']} {payload['feedback_kind']}",user["id"])
        return jsonify({"ok":True,"id":feedback_id,"feedback":payload,"text":result["text"],"method":result["method"],"warning":result["warning"]})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),400

@app.route("/api/feedback",methods=["POST"])
def api_feedback_save():
    user,err=require_user()
    if err:return err
    try:
        payload=_validated_feedback_payload(request.get_json(force=True),user["id"])
        if not payload["generated_text"]: raise ValueError("\u8bf7\u5148\u751f\u6210\u6216\u586b\u5199\u53cd\u9988\u6b63\u6587")
        feedback_id=db.upsert_course_feedback(payload,user["id"])
        db.add_log("feedback_save",f"{payload['student_name']} {payload['feedback_date']} {payload['feedback_kind']}",user["id"])
        return jsonify({"ok":True,"id":feedback_id,"feedback":db.get_course_feedback_by_id(feedback_id,user["id"])})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),400

def _ics_escape(value):
    return str(value or "").replace("\\","\\\\").replace(";","\\;").replace(",","\\,").replace("\r","").replace("\n","\\n")

def _calendar_target_date(entry,date_text=None):
    if date_text:
        target=datetime.strptime(date_text,"%Y-%m-%d").date()
        if not _entry_occurs_on(entry,target): raise ValueError("\u8bfe\u7a0b\u5728\u8be5\u65e5\u671f\u4e0d\u4e0a\u8bfe")
        return target
    if entry.get("schedule_date"): return datetime.strptime(entry["schedule_date"],"%Y-%m-%d").date()
    today=date.today();return today+timedelta(days=(int(entry.get("day_of_week") or 1)-today.isoweekday())%7)

def _ics_for_entries(entries_with_dates,user_id):
    from zoneinfo import ZoneInfo
    local_tz=ZoneInfo("Asia/Shanghai");utc=ZoneInfo("UTC")
    now_utc=datetime.now(local_tz).astimezone(utc).strftime("%Y%m%dT%H%M%SZ")
    lines=["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//Xiangmo Course//CN","CALSCALE:GREGORIAN","METHOD:PUBLISH"]
    for entry,target in entries_with_dates:
        start_time=datetime.strptime(entry.get("start_time") or "20:00","%H:%M").time()
        start=datetime.combine(target,start_time,tzinfo=local_tz);end=start+timedelta(minutes=int(entry.get("duration_min") or 60))
        uid=hashlib.sha256(f"{user_id}:{entry['id']}:{target.isoformat()}".encode()).hexdigest()+"@xiangmo"
        summary=(entry.get("student_name") or "\u672a\u547d\u540d\u5b66\u751f")+" \u00b7 "+(entry.get("subject_type") or "\u8bfe\u7a0b")
        description=("\u6297\u9057\u5fd8\u590d\u4e60" if entry.get("source")=="review" else "\u4e3b\u8bfe")
        if entry.get("notes"): description+="\n"+str(entry["notes"])
        lines += ["BEGIN:VEVENT",f"UID:{uid}",f"DTSTAMP:{now_utc}",f"DTSTART:{start.astimezone(utc).strftime('%Y%m%dT%H%M%SZ')}",f"DTEND:{end.astimezone(utc).strftime('%Y%m%dT%H%M%SZ')}",f"SUMMARY:{_ics_escape(summary)}",f"DESCRIPTION:{_ics_escape(description)}"]
        for minutes in (30,10): lines += ["BEGIN:VALARM",f"TRIGGER:-PT{minutes}M","ACTION:DISPLAY",f"DESCRIPTION:{_ics_escape(summary)}","END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)+"\r\n"

@app.route("/api/schedule/<int:eid>/calendar.ics",methods=["GET"])
def api_schedule_calendar(eid):
    user,err=require_user()
    if err:return err
    try:
        entry=db.get_schedule_entry(eid,user["id"])
        if not entry:return jsonify({"ok":False,"error":"not found"}),404
        target=_calendar_target_date(entry,request.args.get("date"));content=_ics_for_entries([(entry,target)],user["id"])
        return Response(content,content_type="text/calendar; charset=utf-8",headers={"Content-Disposition":f"attachment; filename=course-{eid}-{target.isoformat()}.ics"})
    except Exception as e:return jsonify({"ok":False,"error":str(e)}),400

@app.route("/api/calendar/day.ics",methods=["GET"])
def api_calendar_day():
    user,err=require_user()
    if err:return err
    try:
        target=datetime.strptime(request.args.get("date") or date.today().strftime("%Y-%m-%d"),"%Y-%m-%d").date()
        entries=db.get_schedule_entries_for_date(target,user["id"])
        if not entries:return jsonify({"ok":False,"error":"\u5f53\u5929\u6ca1\u6709\u53ef\u5bfc\u51fa\u7684\u8bfe\u7a0b"}),404
        content=_ics_for_entries([(entry,target) for entry in entries],user["id"])
        return Response(content,content_type="text/calendar; charset=utf-8",headers={"Content-Disposition":f"attachment; filename=courses-{target.isoformat()}.ics"})
    except Exception as e:return jsonify({"ok":False,"error":str(e)}),400

# ==================== 收入统计 API ====================
@app.route("/api/income", methods=["GET"])
def api_get_income():
    user, err = require_user()
    if err: return err
    # Only completed main-course entries contribute to income.
    entries = db.get_all_schedule_entries(user['id'])
    total = sum(float(e.get('price_per_session') or 0) for e in entries if e.get('status') == 'completed' and e.get('source') != 'review')
    return jsonify({'total_income': total, 'entries': entries})

# ==================== 统计 API ====================
@app.route("/api/stats", methods=["GET"])
def api_get_stats():
    user, err = require_user()
    if err: return err
    stats = db.get_db_stats(user['id'])
    stats["service_status"] = db.get_config("service_status", user['id']) or "stopped"
    return jsonify(stats)

@app.route("/api/logs", methods=["GET"])
def api_get_logs():
    user, err = require_user()
    if err: return err
    limit = request.args.get("limit", 50, type=int)
    return jsonify(db.get_recent_logs(limit, user['id']))

_monitor_thread = None

def _start_monitor_thread(user_id):
    with _monitor_lock:
        monitor = get_monitor(user_id)
        if not monitor.running: monitor.start()
        return monitor

def _stop_monitor_thread(user_id):
    with _monitor_lock:
        if _monitor_instance and _monitor_instance.running:
            if _monitor_user_id != user_id: raise RuntimeError("\u65e0\u6743\u505c\u6b62\u5176\u4ed6\u7528\u6237\u7684\u5fae\u4fe1\u76d1\u63a7")
            _monitor_instance.stop()

@app.route("/api/service/start", methods=["POST"])
def api_service_start():
    user, err = require_user()
    if err: return err
    try:
        _start_monitor_thread(user['id'])
        db.set_config("service_status", "running", user['id'])
        db.add_log("service_start", "\u670d\u52a1\u5df2\u542f\u52a8", user['id'])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 409

@app.route("/api/service/stop", methods=["POST"])
def api_service_stop():
    user, err = require_user()
    if err: return err
    try:
        _stop_monitor_thread(user['id'])
        db.set_config("service_status", "stopped", user['id'])
        db.add_log("service_stop", "\u670d\u52a1\u5df2\u505c\u6b62", user['id'])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 409

@app.route("/api/service/pause", methods=["POST"])
def api_service_pause():
    user, err = require_user()
    if err: return err
    try:
        _stop_monitor_thread(user['id'])
        db.set_config("service_status", "paused", user['id'])
        db.add_log("service_pause", "\u670d\u52a1\u5df2\u6682\u505c", user['id'])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 409

@app.route("/api/service/resume", methods=["POST"])
def api_service_resume():
    return api_service_start()

@app.route("/api/service/status", methods=["GET"])
def api_service_status():
    user, err = require_user()
    if err: return err
    running = bool(_monitor_instance and _monitor_instance.running and _monitor_user_id == user['id'])
    status = "running" if running else (db.get_config("service_status", user['id']) or "stopped")
    if status == "running" and not running: status = "stopped"
    return jsonify({"status": status})

@app.route("/api/test-reply", methods=["POST"])
def api_test_reply():
    user, err = require_user()
    if err: return err
    data = request.get_json(force=True)
    text = str(data.get("text", "1"))
    quote = bool(data.get("quote", True))
    started = time.perf_counter()
    try:
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except ImportError:
            pass
        monitor = get_monitor(user['id'])
        ok = monitor._send_reply(text, data.get("original_msg"), quote_override=quote)
        elapsed = int((time.perf_counter() - started) * 1000)
        result = {
            "ok": ok,
            "elapsed_ms": elapsed,
            "quote_requested": quote,
            "quote_applied": monitor._last_quote_applied,
            "steps": [f"send_reply={ok}", f"elapsed_ms={elapsed}", f"quote_applied={monitor._last_quote_applied}"],
        }
        if not ok:
            result["msg"] = "\u53d1\u9001\u5931\u8d25\uff0c\u8bf7\u786e\u8ba4\u5fae\u4fe1\u5df2\u767b\u5f55\u5e76\u6253\u5f00\u76ee\u6807\u804a\u5929"
        elif quote and not monitor._last_quote_applied:
            result["msg"] = f"\u5df2\u53d1\u9001 {text}\uff0c\u4f46\u672a\u80fd\u5f15\u7528\u6700\u540e\u4e00\u6761\u6d88\u606f\uff1b\u8bf7\u67e5\u770b\u76d1\u63a7\u65e5\u5fd7"
        else:
            result["msg"] = f"\u5df2\u53d1\u9001 {text}" + ("\uff0c\u5e76\u6210\u529f\u5f15\u7528\u6d88\u606f" if quote else "")
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "steps": [f"exception={e}"]}), 500

@app.route("/api/diagnose", methods=["POST"])
def api_diagnose():
    user, err = require_user()
    if err: return err
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
    user, err = require_user()
    if err: return err
    if _monitor_user_id != user['id']:
        return jsonify({"ok": True, "logs": []})
    try:
        from wechat_monitor import get_monitor_log
        logs = get_monitor_log()
        return jsonify({"ok": True, "logs": logs[-100:]})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ==================== 启动服务器 ====================
def run_server(host="127.0.0.1", port=4876, auto_start=False):
    print(f"[服务器] 启动中 http://{host}:{port}")
    if auto_start:
        print("[service] auto-start deferred until a user logs in")
    app.run(host=host, port=port, debug=False, threaded=True)




# ==================== ?? ====================
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 4876))
    host = "0.0.0.0"
    print(f"[server] listening on http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)
