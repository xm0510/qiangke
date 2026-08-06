import sqlite3, json, os, secrets, hashlib
from datetime import datetime, date, timedelta
from contextlib import contextmanager
from typing import Optional

DB_PATH = os.path.abspath(os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db"),
))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

DEFAULT_ADMIN_PHONE = "15056587110"

def _configured_admin_phone():
    phone = os.environ.get("ADMIN_PHONE", "").strip().strip('"').strip("'")
    if not phone:
        try:
            env_path = os.path.join(os.path.dirname(DB_PATH), ".env")
            with open(env_path, "r", encoding="utf-8") as f:
                for raw in f:
                    if raw.strip().startswith("ADMIN_PHONE="):
                        phone = raw.strip().split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except OSError:
            pass
    return phone if len(phone) == 11 and phone.isdigit() else DEFAULT_ADMIN_PHONE

def get_configured_admin_phone():
    return _configured_admin_phone()

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
        c.execute("CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("INSERT OR IGNORE INTO system_settings(key,value,updated_at) SELECT key,value,updated_at FROM config WHERE key='admin_recovery_password_fingerprint'")
        c.execute("DELETE FROM config WHERE key='admin_recovery_password_fingerprint'")
        invite_code = os.environ.get("REGISTRATION_INVITE_CODE", "").strip().strip('\"').strip("'")
        if invite_code:
            c.execute("INSERT OR IGNORE INTO system_settings(key,value) VALUES ('registration_invite_code',?)", (invite_code,))
        c.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL UNIQUE, nickname TEXT DEFAULT '', password_hash TEXT, password_salt TEXT, is_admin INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, last_login_at DATETIME)")
        c.execute("CREATE TABLE IF NOT EXISTS user_config (user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id,key), FOREIGN KEY(user_id) REFERENCES users(id))")
        c.execute("CREATE TABLE IF NOT EXISTS password_reset_codes (phone TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires_at DATETIME NOT NULL, created_by INTEGER, consumed_at DATETIME)")
        c.execute("DROP TABLE IF EXISTS login_codes")
        c.execute("DROP TABLE IF EXISTS sms_send_log")
        c.execute("CREATE TABLE IF NOT EXISTS auth_tokens (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, expires_at DATETIME, FOREIGN KEY(user_id) REFERENCES users(id))")
        c.execute("CREATE TABLE IF NOT EXISTS monitored_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, group_name TEXT NOT NULL, enabled INTEGER DEFAULT 1, added_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id,group_name))")
        c.execute("CREATE TABLE IF NOT EXISTS grab_records (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, group_name TEXT NOT NULL, message_text TEXT NOT NULL, matched_type TEXT, match_method TEXT, reply_content TEXT, reply_status TEXT DEFAULT 'success', ai_extracted_time TEXT, ai_extracted_day INTEGER, ai_extracted_start TEXT, ai_extracted_duration INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS schedule_entries (id INTEGER PRIMARY KEY AUTOINCREMENT, student_name TEXT DEFAULT '', subject_type TEXT DEFAULT '词汇', day_of_week INTEGER NOT NULL, start_time TEXT NOT NULL, duration_min INTEGER DEFAULT 60, status TEXT DEFAULT 'pending', notes TEXT DEFAULT '', source TEXT DEFAULT 'manual', source_grab_id INTEGER, is_recurring INTEGER DEFAULT 0, recur_end_date TEXT, recur_until_count INTEGER, parent_entry_id INTEGER, price_per_session REAL DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS income_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, month INTEGER DEFAULT 0, week_start TEXT, total_sessions INTEGER DEFAULT 0, total_income REAL DEFAULT 0, snapshot_date DATE DEFAULT CURRENT_DATE)")
        c.execute("CREATE TABLE IF NOT EXISTS activity_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action_type TEXT NOT NULL, description TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("""CREATE TABLE IF NOT EXISTS course_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            feedback_key TEXT NOT NULL,
            feedback_kind TEXT NOT NULL,
            feedback_date TEXT NOT NULL,
            student_name TEXT NOT NULL,
            schedule_entry_ids TEXT NOT NULL DEFAULT '[]',
            class_content TEXT DEFAULT '',
            focus_rating INTEGER DEFAULT 3,
            mastery_rating INTEGER DEFAULT 3,
            problems TEXT DEFAULT '',
            homework TEXT DEFAULT '',
            next_plan TEXT DEFAULT '',
            teacher_notes TEXT DEFAULT '',
            generated_text TEXT DEFAULT '',
            generation_method TEXT DEFAULT 'template',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, feedback_key),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_course_feedback_user_date ON course_feedback(user_id, feedback_date DESC)")
        try: c.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        except: pass
        try: c.execute("ALTER TABLE users ADD COLUMN password_salt TEXT")
        except: pass
        try: c.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        except: pass
        admin_phone = _configured_admin_phone()
        c.execute("UPDATE users SET is_admin=CASE WHEN phone=? THEN 1 ELSE 0 END", (admin_phone,))
        # Migrations for per-user data isolation. Existing unowned rows stay
        # user_id=NULL and are never returned through authenticated APIs.
        try: c.execute("ALTER TABLE monitored_groups ADD COLUMN user_id INTEGER")
        except: pass
        try: c.execute("ALTER TABLE grab_records ADD COLUMN user_id INTEGER")
        except: pass
        try: c.execute("ALTER TABLE activity_log ADD COLUMN user_id INTEGER")
        except: pass
        old_group_schema = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='monitored_groups'").fetchone()
        if old_group_schema and 'group_name TEXT NOT NULL UNIQUE' in (old_group_schema[0] or ''):
            c.execute("ALTER TABLE monitored_groups RENAME TO monitored_groups_old")
            c.execute("CREATE TABLE monitored_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, group_name TEXT NOT NULL, enabled INTEGER DEFAULT 1, added_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id,group_name))")
            c.execute("INSERT OR IGNORE INTO monitored_groups (id,user_id,group_name,enabled,added_at) SELECT id,user_id,group_name,enabled,added_at FROM monitored_groups_old")
            c.execute("DROP TABLE monitored_groups_old")
        # Migrations for anti-forgetting
        try: c.execute("ALTER TABLE schedule_entries ADD COLUMN schedule_date TEXT")
        except: pass
        try: c.execute("ALTER TABLE schedule_entries ADD COLUMN review_interval_days INTEGER")
        except: pass
        try: c.execute("ALTER TABLE schedule_entries ADD COLUMN review_index INTEGER")
        except: pass
        try: c.execute("ALTER TABLE schedule_entries ADD COLUMN user_id INTEGER")
        except: pass
        defaults = {"monitor_mode":"multi","grab_mode":"multi","selected_types":json.dumps(["词汇","阅读","语法","完型","听口","写作","抗遗忘"]),"reply_content":"1","quote_reply":"true","auto_reply_on_screen_change":"true","ai_enabled":"false","ai_api_url":"https://api.deepseek.com/v1/chat/completions","ai_api_key":"","ai_model":"deepseek-chat","ai_default_type":"词汇","reply_delay_ms":"0","auto_start":"false","auto_add_schedule":"false","conflict_detection":"true","conflict_action":"warn_only","grab_conflict_mode":"grab_then_check","recurring_enabled":"false","recurring_auto_generate":"true","recurring_weeks_ahead":"4","recurring_end_mode":"by_date","recurring_end_date":"","income_stats_enabled":"false","default_price":"100","today_reminder":"false","pre_class_reminder":"true","pre_class_minutes":"15","export_format":"text","service_status":"stopped","selected_group_id":""}
        for k,v in defaults.items(): c.execute("INSERT OR IGNORE INTO config (key,value) VALUES (?,?)",(k,v))
        # One-time migration from the former single-user global store. Keep
        # existing settings/data with the earliest account, then restore safe
        # global defaults so new users never inherit another user's API key.
        scoped = c.execute("SELECT value FROM config WHERE key='per_user_data_migration_v1'").fetchone()
        if not scoped:
            first_user = c.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
            if first_user:
                uid = first_user[0]
                c.execute("INSERT OR IGNORE INTO user_config (user_id,key,value) SELECT ?,key,value FROM config WHERE key NOT LIKE '%migration%' AND key NOT LIKE 'demo_data_cleanup%'", (uid,))
                c.execute("DELETE FROM monitored_groups WHERE user_id IS NULL AND EXISTS (SELECT 1 FROM monitored_groups owned WHERE owned.user_id=? AND owned.group_name=monitored_groups.group_name)", (uid,))
                c.execute("UPDATE monitored_groups SET user_id=? WHERE user_id IS NULL", (uid,))
                c.execute("UPDATE grab_records SET user_id=? WHERE user_id IS NULL", (uid,))
                c.execute("UPDATE activity_log SET user_id=? WHERE user_id IS NULL", (uid,))
                c.execute("UPDATE schedule_entries SET user_id=? WHERE user_id IS NULL", (uid,))
            for k,v in defaults.items(): c.execute("UPDATE config SET value=? WHERE key=?", (v,k))
            c.execute("INSERT INTO config (key,value) VALUES ('per_user_data_migration_v1','done')")
        # Older builds defaulted to api.openai.com, which is often unreachable for
        # domestic users. Move untouched installs to the domestic-friendly preset.
        c.execute("UPDATE config SET value=? WHERE key='ai_api_url' AND value='https://api.openai.com/v1/chat/completions' AND COALESCE((SELECT value FROM config WHERE key='ai_api_key'),'')=''", ("https://api.deepseek.com/v1/chat/completions",))
        c.execute("UPDATE config SET value=? WHERE key='ai_model' AND value='gpt-4o-mini' AND COALESCE((SELECT value FROM config WHERE key='ai_api_key'),'')=''", ("deepseek-chat",))
        c.execute("""UPDATE user_config AS target SET value='https://api.deepseek.com/v1/chat/completions'
                     WHERE target.key='ai_api_url' AND target.value='https://api.openai.com/v1/chat/completions'
                       AND COALESCE((SELECT value FROM user_config WHERE user_id=target.user_id AND key='ai_api_key'),'')=''""")
        c.execute("""UPDATE user_config AS target SET value='deepseek-chat'
                     WHERE target.key='ai_model' AND target.value='gpt-4o-mini'
                       AND COALESCE((SELECT value FROM user_config WHERE user_id=target.user_id AND key='ai_api_key'),'')=''""")
        cleanup = c.execute("SELECT value FROM config WHERE key='demo_data_cleanup_v2'").fetchone()
        if not cleanup:
            c.execute("""DELETE FROM schedule_entries
                         WHERE source='manual' AND (
                           (student_name='\u51c6\u9ad8\u4e00\u5973\u751f' AND notes='\u57fa\u7840\u4e2d\u7b49\uff0c\u9700\u8981\u8010\u5fc3') OR
                           (student_name='\u9ad8\u4e2d\u7537\u751f\u5c0f\u9648' AND notes IN ('4.6\u7ea7\u9605\u8bfb','4.6\u7ea7\u8bed\u6cd5')) OR
                           (student_name='\u65b0\u521d\u4e00\u7537\u751f' AND notes='\u5e7d\u9ed8\u9f13\u52b1\u578b') OR
                           (student_name='\u5c0f\u5b66\u4e09\u5e74\u7ea7\u5c0f\u7f8e' AND notes='\u5e73\u65f6\u6297\u9057\u5fd8\u590d\u4e60')
                         )""")
            c.execute("INSERT INTO config (key,value) VALUES ('demo_data_cleanup_v2','done')")
    print("[DB] init done")

def _hash_password(password, salt_hex=None):
    password = str(password or "")
    if len(password) < 8: raise ValueError("\u5bc6\u7801\u81f3\u5c11\u9700\u8981 8 \u4f4d")
    if len(password) > 128: raise ValueError("\u5bc6\u7801\u8fc7\u957f")
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt.hex(), digest.hex()

def apply_admin_recovery_password(phone, password):
    """Reset the configured administrator password once per distinct recovery value."""
    phone = str(phone or "").strip()
    if len(phone) != 11 or not phone.isdigit():
        raise ValueError("\u0041\u0044\u004d\u0049\u004e\u005f\u0050\u0048\u004f\u004e\u0045 \u5fc5\u987b\u662f 11 \u4f4d\u624b\u673a\u53f7")
    fingerprint = hashlib.sha256((phone + "\0" + str(password)).encode("utf-8")).hexdigest()
    recovery_key = "admin_recovery_password_fingerprint"
    with db_cursor() as conn:
        previous = conn.execute("SELECT value FROM system_settings WHERE key=?", (recovery_key,)).fetchone()
        if previous and secrets.compare_digest(previous["value"], fingerprint):
            return False
        salt, digest = _hash_password(password)
        row = conn.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
        if row:
            user_id = row["id"]
            conn.execute(
                "UPDATE users SET password_hash=?,password_salt=?,is_admin=1 WHERE id=?",
                (digest, salt, user_id),
            )
        else:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO users(phone,nickname,password_hash,password_salt,is_admin) VALUES (?,?,?,?,1)",
                (phone, "\u7ba1\u7406\u5458" + phone[-4:], digest, salt),
            )
            user_id = cur.lastrowid
        conn.execute("UPDATE users SET is_admin=CASE WHEN id=? THEN 1 ELSE 0 END", (user_id,))
        conn.execute("DELETE FROM auth_tokens WHERE user_id=?", (user_id,))
        conn.execute(
            "INSERT OR REPLACE INTO system_settings(key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)",
            (recovery_key, fingerprint),
        )
        return True

def _session_for_user(conn, user_id):
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO auth_tokens (token,user_id,expires_at) VALUES (?,?,datetime('now','+30 days'))", (token,user_id))
    user = dict(conn.execute("SELECT id,phone,nickname,is_admin,created_at,last_login_at FROM users WHERE id=?",(user_id,)).fetchone())
    return {"token":token,"user":user}

def register_or_set_password(phone,password):
    salt,digest=_hash_password(password)
    admin_phone=_configured_admin_phone()
    with db_cursor() as conn:
        row=conn.execute("SELECT * FROM users WHERE phone=?",(phone,)).fetchone()
        if row and row['password_hash']: raise ValueError("\u8d26\u53f7\u5df2\u5b58\u5728\uff0c\u8bf7\u76f4\u63a5\u767b\u5f55")
        if row:
            user_id=row['id']
            conn.execute("UPDATE users SET password_hash=?,password_salt=?,last_login_at=CURRENT_TIMESTAMP WHERE id=?",(digest,salt,user_id))
        else:
            is_admin = 1 if (admin_phone and phone == admin_phone) else (0 if admin_phone else (1 if conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]==0 else 0))
            cur=conn.cursor();cur.execute("INSERT INTO users(phone,nickname,password_hash,password_salt,is_admin,last_login_at) VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",(phone,'\u7528\u6237'+phone[-4:],digest,salt,is_admin));user_id=cur.lastrowid
        if admin_phone:
            if phone == admin_phone:
                conn.execute("UPDATE users SET is_admin=CASE WHEN id=? THEN 1 ELSE 0 END",(user_id,))
            else:
                conn.execute("UPDATE users SET is_admin=0 WHERE id=?",(user_id,))
        return _session_for_user(conn,user_id)

def login_with_password(phone,password):
    with db_cursor() as conn:
        row=conn.execute("SELECT * FROM users WHERE phone=?",(phone,)).fetchone()
        if not row or not row['password_hash']: raise ValueError("\u8d26\u53f7\u672a\u6ce8\u518c\u6216\u5c1a\u672a\u8bbe\u7f6e\u5bc6\u7801")
        _,digest=_hash_password(password,row['password_salt'])
        if not secrets.compare_digest(row['password_hash'],digest): raise ValueError("\u624b\u673a\u53f7\u6216\u5bc6\u7801\u9519\u8bef")
        admin_phone=_configured_admin_phone()
        conn.execute("UPDATE users SET is_admin=CASE WHEN phone=? THEN 1 ELSE 0 END",(admin_phone,))
        conn.execute("UPDATE users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?",(row['id'],))
        return _session_for_user(conn,row['id'])

def save_password_reset_code(phone,code_hash,created_by,expires_minutes=15):
    with db_cursor() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE phone=?",(phone,)).fetchone(): raise ValueError("\u8d26\u53f7\u4e0d\u5b58\u5728")
        conn.execute("INSERT OR REPLACE INTO password_reset_codes(phone,code_hash,expires_at,created_by,consumed_at) VALUES (?,?,datetime('now',?),?,NULL)",(phone,code_hash,f'+{int(expires_minutes)} minutes',created_by))

def reset_password_with_code(phone,code_hash,new_password):
    salt,digest=_hash_password(new_password)
    with db_cursor() as conn:
        row=conn.execute("SELECT * FROM password_reset_codes WHERE phone=?",(phone,)).fetchone()
        if not row or row['consumed_at'] is not None: raise ValueError("\u91cd\u7f6e\u7801\u65e0\u6548")
        if conn.execute("SELECT datetime('now')>=?",(row['expires_at'],)).fetchone()[0]: raise ValueError("\u91cd\u7f6e\u7801\u5df2\u8fc7\u671f")
        if not secrets.compare_digest(row['code_hash'],code_hash): raise ValueError("\u91cd\u7f6e\u7801\u9519\u8bef")
        conn.execute("UPDATE users SET password_hash=?,password_salt=? WHERE phone=?",(digest,salt,phone))
        conn.execute("UPDATE password_reset_codes SET consumed_at=CURRENT_TIMESTAMP WHERE phone=?",(phone,))
        conn.execute("DELETE FROM auth_tokens WHERE user_id=(SELECT id FROM users WHERE phone=?)",(phone,))

def get_user_by_token(token):
    token = (token or '').strip()
    if not token: return None
    with db_cursor(commit=False) as conn:
        row = conn.execute("""SELECT u.id,u.phone,u.nickname,u.is_admin,u.created_at,u.last_login_at
                              FROM auth_tokens t JOIN users u ON u.id=t.user_id
                              WHERE t.token=? AND (t.expires_at IS NULL OR t.expires_at>datetime('now'))""", (token,)).fetchone()
        return dict(row) if row else None

def logout_token(token):
    with db_cursor() as conn:
        conn.execute('DELETE FROM auth_tokens WHERE token=?', ((token or '').strip(),))

def get_primary_admin_user_id():
    with db_cursor(commit=False) as conn:
        row = conn.execute("SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
        return row["id"] if row else None

def get_config(key,user_id=None):
    with db_cursor(commit=False) as conn:
        if user_id is not None:
            r=conn.execute("SELECT value FROM user_config WHERE user_id=? AND key=?",(user_id,key)).fetchone()
            if r: return r["value"]
        r=conn.execute("SELECT value FROM config WHERE key=?",(key,)).fetchone()
        return r["value"] if r else None

def set_config(key,value,user_id=None):
    with db_cursor() as conn:
        if user_id is not None: conn.execute("INSERT OR REPLACE INTO user_config (user_id,key,value,updated_at) VALUES (?,?,?,CURRENT_TIMESTAMP)",(user_id,key,str(value)))
        else: conn.execute("INSERT OR REPLACE INTO config (key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)",(key,str(value)))

def get_system_setting(key, default=None):
    with db_cursor(commit=False) as conn:
        row = conn.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def set_system_setting(key, value):
    with db_cursor() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_settings(key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)",
            (key, str(value)),
        )

def get_all_config(user_id=None):
    with db_cursor(commit=False) as conn:
        cfg={r["key"]:r["value"] for r in conn.execute("SELECT key,value FROM config").fetchall()}
        if user_id is not None: cfg.update({r["key"]:r["value"] for r in conn.execute("SELECT key,value FROM user_config WHERE user_id=?",(user_id,)).fetchall()})
        return cfg

def save_config_batch(cfgs,user_id=None):
    with db_cursor() as conn:
        if user_id is not None:
            for k,v in cfgs.items(): conn.execute("INSERT OR REPLACE INTO user_config (user_id,key,value,updated_at) VALUES (?,?,?,CURRENT_TIMESTAMP)",(user_id,k,str(v)))
        else:
            for k,v in cfgs.items(): conn.execute("INSERT OR REPLACE INTO config (key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)",(k,str(v)))

def get_monitored_groups(enabled_only=False,user_id=None):
    with db_cursor(commit=False) as conn:
        q="SELECT * FROM monitored_groups WHERE 1=1"; params=[]
        if user_id is not None: q += " AND user_id=?"; params.append(user_id)
        if enabled_only: q += " AND enabled=1"
        return [dict(r) for r in conn.execute(q+" ORDER BY group_name",params).fetchall()]

def add_group(name,user_id=None):
    with db_cursor() as conn: conn.execute("INSERT OR IGNORE INTO monitored_groups (user_id,group_name) VALUES (?,?)",(user_id,name))

def remove_group(gid,user_id=None):
    with db_cursor() as conn:
        if user_id is None: conn.execute("DELETE FROM monitored_groups WHERE id=?",(gid,))
        else: conn.execute("DELETE FROM monitored_groups WHERE id=? AND user_id=?",(gid,user_id))

def toggle_group(gid,en,user_id=None):
    with db_cursor() as conn:
        if user_id is None: conn.execute("UPDATE monitored_groups SET enabled=? WHERE id=?",(1 if en else 0,gid))
        else: conn.execute("UPDATE monitored_groups SET enabled=? WHERE id=? AND user_id=?",(1 if en else 0,gid,user_id))

def add_grab_record(group_name,message_text,matched_type="",match_method="",reply_content="",reply_status="success",ai_extracted_time="",ai_extracted_day=None,ai_extracted_start="",ai_extracted_duration=None,user_id=None):
    with db_cursor() as conn:
        c=conn.cursor()
        c.execute("INSERT INTO grab_records (user_id,group_name,message_text,matched_type,match_method,reply_content,reply_status,ai_extracted_time,ai_extracted_day,ai_extracted_start,ai_extracted_duration) VALUES (?,?,?,?,?,?,?,?,?,?,?)",(user_id,group_name,message_text,matched_type,match_method,reply_content,reply_status,ai_extracted_time,ai_extracted_day,ai_extracted_start,ai_extracted_duration))
        return c.lastrowid

def get_grab_records(limit=50,offset=0,user_id=None):
    with db_cursor(commit=False) as conn:
        if user_id is None: return [dict(r) for r in conn.execute("SELECT * FROM grab_records ORDER BY created_at DESC LIMIT ? OFFSET ?",(limit,offset)).fetchall()]
        return [dict(r) for r in conn.execute("SELECT * FROM grab_records WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",(user_id,limit,offset)).fetchall()]

def get_grab_count_today(user_id=None):
    with db_cursor(commit=False) as conn:
        if user_id is None: r=conn.execute("SELECT COUNT(*) as cnt FROM grab_records WHERE date(created_at)=date('now','localtime')").fetchone()
        else: r=conn.execute("SELECT COUNT(*) as cnt FROM grab_records WHERE user_id=? AND date(created_at)=date('now','localtime')",(user_id,)).fetchone()
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

def get_schedule_entry(eid,user_id=None):
    with db_cursor(commit=False) as conn:
        if user_id is None:
            row=conn.execute("SELECT * FROM schedule_entries WHERE id=?",(eid,)).fetchone()
        else:
            row=conn.execute("SELECT * FROM schedule_entries WHERE id=? AND user_id=?",(eid,user_id)).fetchone()
        return dict(row) if row else None

def get_schedule_entries_by_ids(entry_ids,user_id):
    ids=[]
    for value in entry_ids or []:
        try: ids.append(int(value))
        except: pass
    ids=list(dict.fromkeys(ids))
    if not ids: return []
    marks=','.join('?' for _ in ids)
    with db_cursor(commit=False) as conn:
        rows=conn.execute(f"SELECT * FROM schedule_entries WHERE user_id=? AND id IN ({marks})",[user_id]+ids).fetchall()
        found={r['id']:dict(r) for r in rows}
        return [found[i] for i in ids if i in found]

def get_schedule_entries_for_date(target_date,user_id):
    if isinstance(target_date,str):
        target=datetime.strptime(target_date,"%Y-%m-%d").date()
    else:
        target=target_date
    date_text=target.strftime("%Y-%m-%d")
    dow=target.isoweekday()
    with db_cursor(commit=False) as conn:
        rows=conn.execute("""SELECT * FROM schedule_entries
                             WHERE user_id=? AND status!='cancelled'
                               AND ((schedule_date IS NOT NULL AND schedule_date=?)
                                 OR (schedule_date IS NULL AND day_of_week=?))
                             ORDER BY start_time,id""",(user_id,date_text,dow)).fetchall()
        return [dict(r) for r in rows]

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

def get_schedule_for_day(target_date,user_id=None):
    dw=target_date.isoweekday()
    with db_cursor(commit=False) as conn:
        if user_id is not None:
            return [dict(r) for r in conn.execute("SELECT * FROM schedule_entries WHERE user_id=? AND day_of_week=? AND status NOT IN ('cancelled') ORDER BY start_time",(user_id,dw)).fetchall()]
        return [dict(r) for r in conn.execute("SELECT * FROM schedule_entries WHERE day_of_week=? AND status NOT IN ('cancelled') ORDER BY start_time",(dw,)).fetchall()]

def get_today_schedule(user_id=None):
    return get_schedule_for_day(date.today(),user_id)

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

def get_income_stats(year=None,month=None,user_id=None):
    with db_cursor(commit=False) as conn:
        w="WHERE status IN ('confirmed','completed') AND price_per_session > 0"
        params=[]
        if user_id is not None:
            w += " AND user_id=?"
            params.append(user_id)
        by_type=[{"type":r["subject_type"],"count":r["cnt"],"total":r["total"]} for r in conn.execute(f"SELECT subject_type, COUNT(*) as cnt, SUM(price_per_session) as total FROM schedule_entries {w} GROUP BY subject_type ORDER BY total DESC",params).fetchall()]
        r=conn.execute(f"SELECT COUNT(*) as cnt, SUM(price_per_session) as total FROM schedule_entries {w}",params).fetchone()
        return {"by_type":by_type,"total_sessions":r["cnt"] or 0,"total_income":r["total"] or 0}

def add_log(action_type,description="",user_id=None):
    with db_cursor() as conn: conn.execute("INSERT INTO activity_log (user_id,action_type,description) VALUES (?,?,?)",(user_id,action_type,description))

def get_recent_logs(limit=100,user_id=None):
    with db_cursor(commit=False) as conn:
        if user_id is None: return [dict(r) for r in conn.execute("SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()]
        return [dict(r) for r in conn.execute("SELECT * FROM activity_log WHERE user_id=? ORDER BY created_at DESC LIMIT ?",(user_id,limit)).fetchall()]

def get_db_stats(user_id=None):
    with db_cursor(commit=False) as conn:
        c=conn.cursor()
        if user_id is None: return {"total_grabs":c.execute("SELECT COUNT(*) FROM grab_records").fetchone()[0],"total_schedules":c.execute("SELECT COUNT(*) FROM schedule_entries").fetchone()[0],"active_groups":c.execute("SELECT COUNT(*) FROM monitored_groups WHERE enabled=1").fetchone()[0],"today_grabs":get_grab_count_today()}
        return {"total_grabs":c.execute("SELECT COUNT(*) FROM grab_records WHERE user_id=?",(user_id,)).fetchone()[0],"total_schedules":c.execute("SELECT COUNT(*) FROM schedule_entries WHERE user_id=?",(user_id,)).fetchone()[0],"active_groups":c.execute("SELECT COUNT(*) FROM monitored_groups WHERE user_id=? AND enabled=1",(user_id,)).fetchone()[0],"today_grabs":get_grab_count_today(user_id)}

# ==================== Course feedback ====================

def get_course_feedback_by_key(feedback_key,user_id):
    with db_cursor(commit=False) as conn:
        row=conn.execute("SELECT * FROM course_feedback WHERE user_id=? AND feedback_key=?",(user_id,feedback_key)).fetchone()
        if not row: return None
        data=dict(row)
        try: data['schedule_entry_ids']=json.loads(data.get('schedule_entry_ids') or '[]')
        except: data['schedule_entry_ids']=[]
        return data

def get_course_feedback_by_id(feedback_id,user_id):
    with db_cursor(commit=False) as conn:
        row=conn.execute("SELECT * FROM course_feedback WHERE id=? AND user_id=?",(feedback_id,user_id)).fetchone()
        if not row: return None
        data=dict(row)
        try: data['schedule_entry_ids']=json.loads(data.get('schedule_entry_ids') or '[]')
        except: data['schedule_entry_ids']=[]
        return data

def list_course_feedback(user_id,limit=50,student_name=None):
    with db_cursor(commit=False) as conn:
        q="SELECT * FROM course_feedback WHERE user_id=?";params=[user_id]
        if student_name:
            q+=" AND student_name=?";params.append(student_name)
        q+=" ORDER BY feedback_date DESC,updated_at DESC LIMIT ?";params.append(max(1,min(int(limit),200)))
        result=[]
        for row in conn.execute(q,params).fetchall():
            data=dict(row)
            try: data['schedule_entry_ids']=json.loads(data.get('schedule_entry_ids') or '[]')
            except: data['schedule_entry_ids']=[]
            result.append(data)
        return result

def upsert_course_feedback(data,user_id):
    fields=(
        'feedback_key','feedback_kind','feedback_date','student_name','schedule_entry_ids',
        'class_content','focus_rating','mastery_rating','problems','homework','next_plan',
        'teacher_notes','generated_text','generation_method'
    )
    values=[]
    for field in fields:
        value=data.get(field,'')
        if field=='schedule_entry_ids': value=json.dumps(value or [],ensure_ascii=False)
        values.append(value)
    with db_cursor() as conn:
        conn.execute("""INSERT INTO course_feedback
          (user_id,feedback_key,feedback_kind,feedback_date,student_name,schedule_entry_ids,class_content,
           focus_rating,mastery_rating,problems,homework,next_plan,teacher_notes,generated_text,generation_method)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(user_id,feedback_key) DO UPDATE SET
            feedback_kind=excluded.feedback_kind,feedback_date=excluded.feedback_date,
            student_name=excluded.student_name,schedule_entry_ids=excluded.schedule_entry_ids,
            class_content=excluded.class_content,focus_rating=excluded.focus_rating,
            mastery_rating=excluded.mastery_rating,problems=excluded.problems,
            homework=excluded.homework,next_plan=excluded.next_plan,
            teacher_notes=excluded.teacher_notes,generated_text=excluded.generated_text,
            generation_method=excluded.generation_method,updated_at=CURRENT_TIMESTAMP""",
          [user_id]+values)
        row=conn.execute("SELECT id FROM course_feedback WHERE user_id=? AND feedback_key=?",(user_id,data['feedback_key'])).fetchone()
        return row['id']

# ==================== Anti-forgetting Review Generation ====================

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
            c.execute("""INSERT INTO schedule_entries (user_id,student_name,subject_type,day_of_week,start_time,duration_min,status,notes,source,parent_entry_id,is_recurring,price_per_session,schedule_date,review_interval_days,review_index) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                user_id,name,"抗遗忘",day_of_week,stime,dur,"pending",
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
