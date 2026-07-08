import sqlite3, json, os, secrets
from datetime import datetime, date, timedelta
from contextlib import contextmanager
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

@contextmanager
def db_cursor(commit=True):
    conn = get_db()
    try:
        yield conn
        if commit: conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db_cursor() as conn:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL UNIQUE, nickname TEXT DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP, last_login_at DATETIME)")
        c.execute("CREATE TABLE IF NOT EXISTS auth_tokens (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, expires_at DATETIME, FOREIGN KEY(user_id) REFERENCES users(id))")
        c.execute("CREATE TABLE IF NOT EXISTS monitored_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT NOT NULL UNIQUE, enabled INTEGER DEFAULT 1, added_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS grab_records (id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT NOT NULL, message_text TEXT NOT NULL, matched_type TEXT, match_method TEXT, reply_content TEXT, reply_status TEXT DEFAULT 'success', ai_extracted_time TEXT, ai_extracted_day INTEGER, ai_extracted_start TEXT, ai_extracted_duration INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS schedule_entries (id INTEGER PRIMARY KEY AUTOINCREMENT, student_name TEXT DEFAULT '', subject_type TEXT DEFAULT '词汇', day_of_week INTEGER NOT NULL, start_time TEXT NOT NULL, duration_min INTEGER DEFAULT 60, status TEXT DEFAULT 'pending', notes TEXT DEFAULT '', source TEXT DEFAULT 'manual', source_grab_id INTEGER, is_recurring INTEGER DEFAULT 0, recur_end_date TEXT, recur_until_count INTEGER, parent_entry_id INTEGER, price_per_session REAL DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS income_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, month INTEGER DEFAULT 0, week_start TEXT, total_sessions INTEGER DEFAULT 0, total_income REAL DEFAULT 0, snapshot_date DATE DEFAULT CURRENT_DATE)")
        c.execute("CREATE TABLE IF NOT EXISTS activity_log (id INTEGER PRIMARY KEY AUTOINCREMENT, action_type TEXT NOT NULL, description TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        # Migrations for anti-forgetting
        try: c.execute("ALTER TABLE schedule_entries ADD COLUMN schedule_date TEXT")
        except: pass
        try: c.execute("ALTER TABLE schedule_entries ADD COLUMN review_interval_days INTEGER")
        except: pass
        try: c.execute("ALTER TABLE schedule_entries ADD COLUMN review_index INTEGER")
        except: pass
        try: c.execute("ALTER TABLE schedule_entries ADD COLUMN user_id INTEGER")
        except: pass
        defaults = {"monitor_mode":"multi","grab_mode":"multi","selected_types":json.dumps(["词汇","阅读","语法","完型","听口","写作","抗遗忘"]),"reply_content":"1","quote_reply":"true","auto_reply_on_screen_change":"true","ai_enabled":"false","ai_api_url":"https://api.openai.com/v1/chat/completions","ai_api_key":"","ai_model":"gpt-4o-mini","ai_default_type":"词汇","reply_delay_ms":"0","auto_start":"false","auto_add_schedule":"false","conflict_detection":"true","conflict_action":"warn_only","grab_conflict_mode":"grab_then_check","recurring_enabled":"false","recurring_auto_generate":"true","recurring_weeks_ahead":"4","recurring_end_mode":"by_date","recurring_end_date":"","income_stats_enabled":"false","default_price":"100","today_reminder":"false","pre_class_reminder":"true","pre_class_minutes":"15","export_format":"text","service_status":"stopped","selected_group_id":""}
        for k,v in defaults.items(): c.execute("INSERT OR IGNORE INTO config (key,value) VALUES (?,?)",(k,v))
    print("[DB] init done")

def login_or_create_user(phone, code):
    """Dev login: verification code is fixed to 123456 until SMS/WeChat login is connected."""
    phone = (phone or '').strip()
    code = (code or '').strip()
    if not phone or len(phone) < 5:
        raise ValueError('\u8bf7\u8f93\u5165\u6b63\u786e\u624b\u673a\u53f7')
    if code != '123456':
        raise ValueError('\u9a8c\u8bc1\u7801\u9519\u8bef\uff0c\u6d4b\u8bd5\u9a8c\u8bc1\u7801\u4e3a 123456')
    with db_cursor() as conn:
        row = conn.execute('SELECT * FROM users WHERE phone=?', (phone,)).fetchone()
        if row:
            user_id = row['id']
            conn.execute('UPDATE users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?', (user_id,))
        else:
            cur = conn.cursor()
            cur.execute('INSERT INTO users (phone,nickname,last_login_at) VALUES (?,?,CURRENT_TIMESTAMP)', (phone, '\u7528\u6237'+phone[-4:]))
            user_id = cur.lastrowid
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO auth_tokens (token,user_id,expires_at) VALUES (?,?,datetime('now','+30 days'))", (token, user_id))
        user = dict(conn.execute('SELECT id,phone,nickname,created_at,last_login_at FROM users WHERE id=?', (user_id,)).fetchone())
        return {'token': token, 'user': user}

def get_user_by_token(token):
    token = (token or '').strip()
    if not token: return None
    with db_cursor(commit=False) as conn:
        row = conn.execute("""SELECT u.id,u.phone,u.nickname,u.created_at,u.last_login_at
                              FROM auth_tokens t JOIN users u ON u.id=t.user_id
                              WHERE t.token=? AND (t.expires_at IS NULL OR t.expires_at>datetime('now'))""", (token,)).fetchone()
        return dict(row) if row else None

def logout_token(token):
    with db_cursor() as conn:
        conn.execute('DELETE FROM auth_tokens WHERE token=?', ((token or '').strip(),))

def get_config(key):
    with db_cursor(commit=False) as conn:
        r=conn.execute("SELECT value FROM config WHERE key=?",(key,)).fetchone()
        return r["value"] if r else None

def set_config(key,value):
    with db_cursor() as conn:
        conn.execute("INSERT OR REPLACE INTO config (key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)",(key,str(value)))

def get_all_config():
    with db_cursor(commit=False) as conn:
        return {r["key"]:r["value"] for r in conn.execute("SELECT key,value FROM config").fetchall()}

def save_config_batch(cfgs):
    with db_cursor() as conn:
        for k,v in cfgs.items(): conn.execute("INSERT OR REPLACE INTO config (key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)",(k,str(v)))

def get_monitored_groups(enabled_only=False):
    with db_cursor(commit=False) as conn:
        q="SELECT * FROM monitored_groups" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY group_name"
        return [dict(r) for r in conn.execute(q).fetchall()]

def add_group(name):
    with db_cursor() as conn: conn.execute("INSERT OR IGNORE INTO monitored_groups (group_name) VALUES (?)",(name,))

def remove_group(gid):
    with db_cursor() as conn: conn.execute("DELETE FROM monitored_groups WHERE id=?",(gid,))

def toggle_group(gid,en):
    with db_cursor() as conn: conn.execute("UPDATE monitored_groups SET enabled=? WHERE id=?",(1 if en else 0,gid))

def add_grab_record(group_name,message_text,matched_type="",match_method="",reply_content="",reply_status="success",ai_extracted_time="",ai_extracted_day=None,ai_extracted_start="",ai_extracted_duration=None):
    with db_cursor() as conn:
        c=conn.cursor()
        c.execute("INSERT INTO grab_records (group_name,message_text,matched_type,match_method,reply_content,reply_status,ai_extracted_time,ai_extracted_day,ai_extracted_start,ai_extracted_duration) VALUES (?,?,?,?,?,?,?,?,?,?)",(group_name,message_text,matched_type,match_method,reply_content,reply_status,ai_extracted_time,ai_extracted_day,ai_extracted_start,ai_extracted_duration))
        return c.lastrowid

def get_grab_records(limit=50,offset=0):
    with db_cursor(commit=False) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM grab_records ORDER BY created_at DESC LIMIT ? OFFSET ?",(limit,offset)).fetchall()]

def get_grab_count_today():
    with db_cursor(commit=False) as conn:
        r=conn.execute("SELECT COUNT(*) as cnt FROM grab_records WHERE date(created_at)=date('now','localtime')").fetchone()
        return r["cnt"] if r else 0

def add_schedule_entry(data):
    with db_cursor() as conn:
        c=conn.cursor()
        c.execute("INSERT INTO schedule_entries (user_id,student_name,subject_type,day_of_week,start_time,duration_min,status,notes,source,source_grab_id,is_recurring,recur_end_date,recur_until_count,parent_entry_id,price_per_session,schedule_date,review_interval_days,review_index) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
            data.get("user_id"),
            data.get("student_name",""),
            data.get("subject_type","词汇"),
            data.get("day_of_week",1),
            data.get("start_time","20:00"),
            data.get("duration_min",60),
            data.get("status","pending"),
            data.get("notes",""),
            data.get("source","manual"),
            data.get("source_grab_id"),
            data.get("is_recurring",0),
            data.get("recur_end_date"),
            data.get("recur_until_count"),
            data.get("parent_entry_id"),
            data.get("price_per_session",0),
            data.get("schedule_date"),
            data.get("review_interval_days"),
            data.get("review_index")
        ))
        return c.lastrowid

def update_schedule_entry(eid,data,user_id=None):
    allowed=["student_name","subject_type","day_of_week","start_time","duration_min","status","notes","price_per_session","is_recurring","recur_end_date","recur_until_count","schedule_date","review_interval_days","review_index"]
    fields=[f"{k}=?" for k in data if k in allowed]
    vals=[data[k] for k in data if k in allowed]
    if fields:
        fields.append("updated_at=CURRENT_TIMESTAMP"); vals.append(eid)
        with db_cursor() as conn:
            if user_id is not None:
                vals.append(user_id)
                conn.execute(f"UPDATE schedule_entries SET {','.join(fields)} WHERE id=? AND user_id=?",vals)
            else:
                conn.execute(f"UPDATE schedule_entries SET {','.join(fields)} WHERE id=?",vals)

def delete_schedule_entry(eid,user_id=None):
    with db_cursor() as conn:
        if user_id is not None:
            conn.execute("DELETE FROM schedule_entries WHERE id=? AND user_id=?",(eid,user_id))
        else:
            conn.execute("DELETE FROM schedule_entries WHERE id=?",(eid,))

def get_all_schedule_entries(user_id=None):
    with db_cursor(commit=False) as conn:
        if user_id is not None:
            return [dict(r) for r in conn.execute("SELECT * FROM schedule_entries WHERE user_id=? AND status != 'cancelled' ORDER BY day_of_week, start_time",(user_id,)).fetchall()]
        return [dict(r) for r in conn.execute("SELECT * FROM schedule_entries WHERE status != 'cancelled' ORDER BY day_of_week, start_time").fetchall()]

def get_schedule_for_day(target_date):
    dw=target_date.isoweekday()
    with db_cursor(commit=False) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM schedule_entries WHERE day_of_week=? AND status NOT IN ('cancelled') ORDER BY start_time",(dw,)).fetchall()]

def get_today_schedule():
    return get_schedule_for_day(date.today())

def check_schedule_conflict(day_of_week,start_time,duration_min,exclude_id=None,user_id=None):
    ns=datetime.strptime(start_time,"%H:%M"); ne=ns+timedelta(minutes=duration_min)
    with db_cursor(commit=False) as conn:
        q="SELECT * FROM schedule_entries WHERE day_of_week=? AND status NOT IN ('cancelled')"; params=[day_of_week]
        if user_id is not None:
            q += " AND user_id=?"; params.append(user_id)
        if exclude_id: q+=" AND id != ?"; params.append(exclude_id)
        rows=conn.execute(q,params).fetchall()
    conflicts=[]
    for row in rows:
        e=dict(row)
        es=datetime.strptime(e["start_time"],"%H:%M"); ee=es+timedelta(minutes=e["duration_min"])
        if ns<ee and ne>es: conflicts.append(e)
    return conflicts

def get_income_stats(year=None,month=None):
    with db_cursor(commit=False) as conn:
        w="WHERE status IN ('confirmed','completed') AND price_per_session > 0"
        by_type=[{"type":r["subject_type"],"count":r["cnt"],"total":r["total"]} for r in conn.execute(f"SELECT subject_type, COUNT(*) as cnt, SUM(price_per_session) as total FROM schedule_entries {w} GROUP BY subject_type ORDER BY total DESC").fetchall()]
        r=conn.execute(f"SELECT COUNT(*) as cnt, SUM(price_per_session) as total FROM schedule_entries {w}").fetchone()
        return {"by_type":by_type,"total_sessions":r["cnt"] or 0,"total_income":r["total"] or 0}

def add_log(action_type,description=""):
    with db_cursor() as conn: conn.execute("INSERT INTO activity_log (action_type,description) VALUES (?,?)",(action_type,description))

def get_recent_logs(limit=100):
    with db_cursor(commit=False) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()]

def get_db_stats():
    with db_cursor(commit=False) as conn:
        c=conn.cursor()
        return {"total_grabs":c.execute("SELECT COUNT(*) FROM grab_records").fetchone()[0],"total_schedules":c.execute("SELECT COUNT(*) FROM schedule_entries").fetchone()[0],"active_groups":c.execute("SELECT COUNT(*) FROM monitored_groups WHERE enabled=1").fetchone()[0],"today_grabs":get_grab_count_today()}

# ==================== 抗遗忘 Review Generation ====================

def get_review_entries_for_parent(parent_id,user_id=None):
    with db_cursor(commit=False) as conn:
        if user_id is not None:
            return [dict(r) for r in conn.execute("SELECT * FROM schedule_entries WHERE parent_entry_id=? AND user_id=? AND source='review' AND status!='cancelled' ORDER BY schedule_date",(parent_id,user_id)).fetchall()]
        return [dict(r) for r in conn.execute("SELECT * FROM schedule_entries WHERE parent_entry_id=? AND source='review' AND status!='cancelled' ORDER BY schedule_date",(parent_id,)).fetchall()]

def generate_review_entries(parent_id, intervals, student_name=None, start_time=None, duration_min=None, schedule_date=None, user_id=None):
    with db_cursor(commit=False) as conn:
        if user_id is not None:
            parent = conn.execute("SELECT * FROM schedule_entries WHERE id=? AND user_id=?", (parent_id,user_id)).fetchone()
        else:
            parent = conn.execute("SELECT * FROM schedule_entries WHERE id=?", (parent_id,)).fetchone()
        if not parent: return []
    parent = dict(parent)
    if schedule_date:
        base_date = datetime.strptime(schedule_date, "%Y-%m-%d").date()
    elif parent.get("schedule_date"):
        base_date = datetime.strptime(parent["schedule_date"], "%Y-%m-%d").date()
    else:
        base_date = date.today()
        target_dow = parent["day_of_week"]
        days_ahead = target_dow - base_date.isoweekday()
        if days_ahead <= 0: days_ahead += 7
        base_date = base_date + timedelta(days=days_ahead)
    name = student_name or parent.get("student_name", "")
    stime = start_time or parent.get("start_time", "20:00")
    dur = duration_min or parent.get("duration_min", 30)
    created_ids = []
    with db_cursor() as conn:
        if user_id is not None:
            conn.execute("DELETE FROM schedule_entries WHERE parent_entry_id=? AND user_id=? AND source='review' AND status='pending'",(parent_id,user_id))
        else:
            conn.execute("DELETE FROM schedule_entries WHERE parent_entry_id=? AND source='review' AND status='pending'",(parent_id,))
        for idx, interval_days in enumerate(intervals):
            review_date = base_date + timedelta(days=interval_days)
            review_date_str = review_date.strftime("%Y-%m-%d")
            day_of_week = review_date.isoweekday()
            c = conn.cursor()
            c.execute("""INSERT INTO schedule_entries (student_name,subject_type,day_of_week,start_time,duration_min,status,notes,source,parent_entry_id,is_recurring,price_per_session,schedule_date,review_interval_days,review_index) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                name,"抗遗忘",day_of_week,stime,dur,"pending",
                f"第{interval_days}天复习 (主课: {base_date.strftime('%m/%d')})",
                "review",parent_id,0,0,review_date_str,interval_days,idx+1))
            created_ids.append(c.lastrowid)
    return created_ids

def delete_review_entries(parent_id,user_id=None):
    with db_cursor() as conn:
        if user_id is not None:
            conn.execute("DELETE FROM schedule_entries WHERE parent_entry_id=? AND user_id=? AND source='review'",(parent_id,user_id))
        else:
            conn.execute("DELETE FROM schedule_entries WHERE parent_entry_id=? AND source='review'",(parent_id,))

def get_all_schedule_entries_for_week(week_start_date,user_id=None):
    week_start = week_start_date
    week_end = week_start + timedelta(days=6)
    week_start_str = week_start.strftime("%Y-%m-%d")
    week_end_str = week_end.strftime("%Y-%m-%d")
    with db_cursor(commit=False) as conn:
        if user_id is not None:
            recurring = conn.execute("SELECT * FROM schedule_entries WHERE user_id=? AND schedule_date IS NULL AND status != 'cancelled' ORDER BY day_of_week, start_time",(user_id,)).fetchall()
            dated = conn.execute("SELECT * FROM schedule_entries WHERE user_id=? AND schedule_date IS NOT NULL AND schedule_date >= ? AND schedule_date <= ? AND status != 'cancelled' ORDER BY schedule_date, start_time",(user_id,week_start_str,week_end_str)).fetchall()
        else:
            recurring = conn.execute("SELECT * FROM schedule_entries WHERE schedule_date IS NULL AND status != 'cancelled' ORDER BY day_of_week, start_time").fetchall()
            dated = conn.execute("SELECT * FROM schedule_entries WHERE schedule_date IS NOT NULL AND schedule_date >= ? AND schedule_date <= ? AND status != 'cancelled' ORDER BY schedule_date, start_time",(week_start_str,week_end_str)).fetchall()
        return [dict(r) for r in recurring] + [dict(r) for r in dated]
