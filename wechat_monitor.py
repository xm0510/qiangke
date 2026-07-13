# -*- coding: utf-8 -*-
"""[WX] Monitor v4.5 - low-latency current desktop chat mode"""
import time, re, json, os, sys, threading, hashlib
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import database as db

_LOG = []
_LOCK = threading.Lock()

def _mlog(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    with _LOCK:
        _LOG.append(f"[{ts}] {msg}")
        if len(_LOG) > 300:
            del _LOG[:-200]
    print(msg)

def get_monitor_log():
    with _LOCK:
        return list(_LOG)


class WeChatMonitor:
    def __init__(self, config_getter, on_grab=None, user_id=None):
        self.get_cfg = config_getter
        self.on_grab = on_grab
        self.user_id = user_id
        self.running = False
        self.thread = None
        self._uia = None
        self._known_hashes = set()
        self._scan_count = 0
        self._current_group = None  # Track which group is currently open
        self._recent_grabs = {}  # group/message debounce to avoid duplicate replies
        self._last_reply_at = {}  # per group cooldown
        self._last_quote_requested = False
        self._last_quote_applied = False
        self._last_send_elapsed_ms = 0

    def _ensure_uia(self):
        if self._uia is None:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except ImportError:
                pass
            import uiautomation as uia
            self._uia = uia

    def start(self):
        if self.running: return
        # Reset volatile UI state on every start. WeChat UIA handles can become stale
        # after stop/start, minimizing, or switching windows.
        self._current_group = None
        self._last_msg_bbox = None
        self._last_img_hashes = {}
        self._scan_count = 0
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        _mlog('[WX] Started v4.5 low-latency current-chat mode')

    def stop(self):
        self.running = False
        _mlog('[WX] Stopped')

    def _loop(self):
        import pythoncom
        verbose = True

        while self.running:
            try:
                # Ensure COM in this thread
                try: pythoncom.CoInitialize()
                except: pass
                
                # UIA must be loaded AFTER COM
                import uiautomation as uia
                self._uia = uia

                wx = self._find_wechat_window(uia)
                if not wx:
                    if verbose or self._scan_count % 10 == 0:
                        _mlog(f'[WX] #{self._scan_count}: no window')
                    time.sleep(1)
                    self._scan_count += 1
                    continue

                if self._scan_count == 0:
                    _mlog(f'[WX] Found: {wx.ClassName}')
                    self._restore_wechat_window(wx, force_foreground=True)

                groups = db.get_monitored_groups(enabled_only=True, user_id=self.user_id)
                # Current desktop chat mode can run even when no group is configured.
                # If a group exists, its name is only used as log/storage label.
                if not groups:
                    groups = [{'group_name': '????'}]

                if verbose or self._scan_count % 3 == 0:
                    _mlog(f'[WX] #{self._scan_count}: current desktop chat mode')

                # Current desktop chat mode:
                # Do NOT Ctrl+F/search/switch groups anymore. The user opens the target
                # WeChat chat on desktop manually; we only monitor the currently visible chat.
                # The first enabled group name is used only as a display/storage label.
                gname = groups[0].get('group_name') if groups else '????'
                gname = gname or '????'
                if verbose or self._scan_count % 10 == 0:
                    _mlog(f'[WX] current desktop chat label: {gname}')
                msgs = self._read_current_chat(wx, group_name=gname)
                if msgs:
                    _mlog(f'[WX] {gname}: {len(msgs)} new msgs')
                    for msg in msgs:
                        self._on_new_message(gname, msg)
                # Screenshot hashing already prevents duplicate work. A single short
                # pause keeps CPU use reasonable without adding a full second of lag.
                time.sleep(0.18)
                self._scan_count += 1
                if self._scan_count > 5:
                    verbose = False

            except Exception as e:
                _mlog(f'[WX] loop err: {e}')
                self._scan_count += 1
                time.sleep(1)
        _mlog('[WX] Exit')

    def _key_tap(self, vk, hold=0.025):
        """Low-level Windows key tap. Avoids uia.SendKeys special-token ord() bugs."""
        import ctypes, time
        user32 = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(hold)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    def _hotkey(self, *keys):
        """Press a Windows hotkey, e.g. _hotkey('ctrl','f')."""
        import ctypes, time
        user32 = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002
        vkmap = {
            'ctrl': 0x11, 'control': 0x11, 'shift': 0x10, 'alt': 0x12,
            'enter': 0x0D, 'esc': 0x1B, 'escape': 0x1B, 'backspace': 0x08,
            'down': 0x28, 'up': 0x26, 'left': 0x25, 'right': 0x27,
            'a': 0x41, 'c': 0x43, 'f': 0x46, 'v': 0x56,
        }
        vks = []
        for k in keys:
            if isinstance(k, int):
                vks.append(k)
            else:
                kk = str(k).lower()
                if kk in vkmap:
                    vks.append(vkmap[kk])
                elif len(kk) == 1:
                    vks.append(ord(kk.upper()))
                else:
                    raise ValueError(f'unknown key: {k}')
        for vk in vks:
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.015)
        for vk in reversed(vks):
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.015)

    def _is_valid_wechat_window(self, w):
        """A UIA window may Exists() but have rect (0,0,0,0) after WeChat refresh/minimize.
        Such stale/hidden handles must not be used for screenshot or clicking.
        """
        try:
            r = w.BoundingRectangle
            width, height = r.width(), r.height()
            if width < 500 or height < 400:
                return False
            # Also reject all-zero rect explicitly.
            if r.left == 0 and r.top == 0 and r.right == 0 and r.bottom == 0:
                return False
            return True
        except:
            return False

    def _restore_wechat_window(self, wx, force_foreground=False):
        """Restore a minimized WeChat window and optionally bring it to the front."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hwnd = int(getattr(wx, 'NativeWindowHandle', 0) or 0)
            if not hwnd or not user32.IsWindow(hwnd):
                return False
            was_iconic = bool(user32.IsIconic(hwnd))
            if was_iconic or not user32.IsWindowVisible(hwnd):
                user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
            if force_foreground or was_iconic:
                foreground = user32.GetForegroundWindow()
                current_thread = kernel32.GetCurrentThreadId()
                foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
                target_thread = user32.GetWindowThreadProcessId(hwnd, None)
                attached_foreground = False
                attached_target = False
                try:
                    if foreground_thread and foreground_thread != current_thread:
                        attached_foreground = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
                    if target_thread and target_thread != current_thread and target_thread != foreground_thread:
                        attached_target = bool(user32.AttachThreadInput(current_thread, target_thread, True))
                    user32.ShowWindowAsync(hwnd, 9)
                    flags = 0x0001 | 0x0002 | 0x0040  # no-size, no-move, show
                    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)  # topmost briefly
                    user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                    user32.SetFocus(hwnd)
                finally:
                    if attached_target:
                        user32.AttachThreadInput(current_thread, target_thread, False)
                    if attached_foreground:
                        user32.AttachThreadInput(current_thread, foreground_thread, False)
                time.sleep(0.25)
            return bool(user32.IsWindowVisible(hwnd)) and not bool(user32.IsIconic(hwnd))
        except Exception as e:
            _mlog(f'[WX] restore window failed: {e}')
            return False

    def _find_wechat_window(self, uia):
        """Find only real top-level WeChat windows; never fall back to an arbitrary window."""
        candidates = []
        seen = set()

        def add_candidate(w, source):
            try:
                hwnd = int(getattr(w, 'NativeWindowHandle', 0) or 0)
                if hwnd and hwnd in seen:
                    return
                name = (getattr(w, 'Name', '') or '').strip()
                cls = getattr(w, 'ClassName', '') or ''
                is_wechat = (
                    cls == 'WeChatMainWndForPC' or
                    (cls == 'Qt51514QWindowIcon' and name in ('\u5fae\u4fe1', 'WeChat', 'Weixin')) or
                    name in ('\u5fae\u4fe1', 'WeChat', 'Weixin')
                )
                if not is_wechat:
                    return
                if hwnd:
                    seen.add(hwnd)
                r = w.BoundingRectangle
                area = max(0, r.width()) * max(0, r.height())
                candidates.append((area, w, source, name, cls, (r.left, r.top, r.right, r.bottom)))
            except:
                pass

        try:
            for w in uia.GetRootControl().GetChildren():
                add_candidate(w, 'desktop')
        except:
            pass
        for spec in (
            {'ClassName': 'WeChatMainWndForPC'},
            {'ClassName': 'Qt51514QWindowIcon', 'Name': '\u5fae\u4fe1'},
            {'Name': '\u5fae\u4fe1'},
            {'Name': 'WeChat'},
            {'Name': 'Weixin'},
        ):
            try:
                w = uia.WindowControl(searchDepth=1, **spec)
                if w.Exists(maxSearchSeconds=0.3):
                    add_candidate(w, str(spec))
            except:
                pass

        candidates.sort(key=lambda x: x[0], reverse=True)
        for _, w, *_ in candidates:
            if self._is_valid_wechat_window(w):
                return w
        # Minimized Qt windows can report an unusable rectangle. Restore only a
        # confirmed WeChat handle, then validate it again.
        for _, w, *_ in candidates:
            if self._restore_wechat_window(w, force_foreground=True) and self._is_valid_wechat_window(w):
                return w
        if candidates and (self._scan_count % 5 == 0):
            _mlog(f'[WX] found only invalid WeChat candidates: {[c[3:] for c in candidates[:3]]}')
        return None

    @staticmethod
    def _clean_scanned_title(raw):
        text = re.sub(r'\s+', '', str(raw or '')).strip()
        if not text:
            return ''
        if '\u7fa4' in text:
            text = text[:text.find('\u7fa4') + 1]
        else:
            text = re.split(r'[|??\[???]', text, maxsplit=1)[0]
        text = text.strip(' .??,?:?;?-_?()??<>??"\'')
        if len(text) < 2 or len(text) > 50:
            return ''
        if text in {'\u641c\u7d22', '\u5fae\u4fe1', 'WeChat', 'Weixin', '\u670d\u52a1\u53f7', '\u8ba2\u9605\u53f7'}:
            return ''
        if re.fullmatch(r'[\d:?/\-.]+', text):
            return ''
        if text.startswith(('[' , '\u3010')):
            return ''
        return text

    def _scan_groups_uia(self, wx):
        names = []
        try:
            wr = wx.BoundingRectangle
            max_x = wr.left + int(wr.width() * 0.42)
            seen_controls = [0]
            def walk(ctrl, depth=0):
                if depth > 14 or seen_controls[0] > 1200:
                    return
                seen_controls[0] += 1
                try:
                    ctype = getattr(ctrl, 'ControlTypeName', '') or ''
                    if ctype == 'ListItemControl':
                        rr = ctrl.BoundingRectangle
                        if rr.left < max_x and rr.top > wr.top + 45 and rr.bottom < wr.bottom:
                            raw = (getattr(ctrl, 'Name', '') or '').splitlines()[0]
                            title = self._clean_scanned_title(raw)
                            if title:
                                names.append(title)
                    for child in ctrl.GetChildren():
                        walk(child, depth + 1)
                except:
                    pass
            walk(wx)
        except:
            pass
        return list(dict.fromkeys(names))

    def _scan_groups_windows_ocr(self, wx):
        """Scan visible Qt WeChat sessions with the free built-in Windows OCR engine."""
        try:
            import subprocess, tempfile, ctypes
            from PIL import ImageGrab
            if not self._restore_wechat_window(wx, force_foreground=True):
                return [], 0
            hwnd = int(getattr(wx, 'NativeWindowHandle', 0) or 0)
            if hwnd and ctypes.windll.user32.GetForegroundWindow() != hwnd:
                _mlog('[SCAN] WeChat could not be brought to foreground; OCR cancelled')
                return [], 0
            time.sleep(0.35)
            wr = wx.BoundingRectangle
            left = wr.left + 45
            top = wr.top + 35
            right = min(wr.left + 455, wr.right - 300)
            bottom = wr.bottom - 20
            if right - left < 260 or bottom - top < 300:
                return [], 0
            script = os.path.join(BASE_DIR, 'windows_ocr.ps1')
            if not os.path.exists(script):
                _mlog('[SCAN] windows_ocr.ps1 missing')
                return [], 0
            with tempfile.TemporaryDirectory(prefix='wechat-scan-') as td:
                image_path = os.path.join(td, 'sessions.png')
                ImageGrab.grab(bbox=(left, top, right, bottom)).save(image_path)
                creationflags = 0x08000000 if os.name == 'nt' else 0
                proc = subprocess.run(
                    ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, '-Path', image_path],
                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                    timeout=30, creationflags=creationflags,
                )
            if proc.returncode != 0:
                _mlog('[SCAN] Windows OCR failed: ' + (proc.stderr or '')[:180])
                return [], 0
            records = json.loads(proc.stdout or '[]')
            if isinstance(records, dict):
                records = [records]
            rows = []
            for item in records:
                try:
                    y = float(item.get('Y', 0)); h = float(item.get('B', 0)) - y
                    if y >= 70:
                        rows.append({'text': item.get('Text', ''), 'x': float(item.get('X', 0)), 'y': y, 'h': h})
                except:
                    pass
            rows.sort(key=lambda x: (x['y'], x['x']))
            gap = max(45.0, (bottom - top) * 0.045)
            clusters = []
            for row in rows:
                if not clusters or row['y'] - clusters[-1][-1]['y'] > gap:
                    clusters.append([row])
                else:
                    clusters[-1].append(row)

            visible_titles = []
            likely_groups = []
            for cluster in clusters:
                min_y = min(x['y'] for x in cluster)
                title_rows = [x for x in cluster if x['h'] >= 13 and x['y'] <= min_y + 22 and x['x'] < (right-left)-45]
                title_rows = [x for x in title_rows if not re.match(r'^\s*[\[\u3010]', x['text'] or '')]
                if not title_rows:
                    continue
                picked = max(title_rows, key=lambda x: (x['h'], len(x['text'] or '')))
                title = self._clean_scanned_title(picked['text'])
                if not title:
                    continue
                visible_titles.append(title)
                compact_cluster = ''.join(re.sub(r'\s+', '', x['text'] or '') for x in cluster)
                likely = ('\u7fa4' in title or bool(re.search(r'[\(\uff08]\d{2,}[\)\uff09]', title)) or
                          bool(re.search(r'[\[\u3010]\d+\u6761[\]\u3011].*[:\uff1a]', compact_cluster)))
                if likely:
                    likely_groups.append(title)
            visible_titles = list(dict.fromkeys(visible_titles))
            likely_groups = list(dict.fromkeys(likely_groups))
            # Prefer likely groups. If OCR cannot classify any row, return visible
            # session titles rather than falsely reporting that scanning is broken.
            return (likely_groups or visible_titles), len(visible_titles)
        except Exception as e:
            _mlog(f'[SCAN] OCR scan error: {e}')
            return [], 0

    def scan_visible_groups(self, wx):
        if not self._restore_wechat_window(wx, force_foreground=True):
            return [], 'none', 0
        names = self._scan_groups_uia(wx)
        if names:
            return names, 'uia', len(names)
        names, visible_count = self._scan_groups_windows_ocr(wx)
        return names, 'windows_ocr', visible_count

    def _switch_to_group(self, wx, group_name):
        """Navigate to a group by clicking it in the sidebar or using Ctrl+F search"""
        if self._current_group == group_name:
            return
        
        uia = self._uia
        if not self._is_valid_wechat_window(wx):
            self._current_group = None
            _mlog(f'[GRP] invalid WeChat window before switch; skip group {group_name}')
            return
        try:
            wx.SetFocus()
            time.sleep(0.3)
            
            # ---------- Method 1: Try clicking the group in sidebar ----------
            found = False
            try:
                def find_group(ctrl, depth=0):
                    nonlocal found
                    if found or depth > 8:
                        return
                    try:
                        name = getattr(ctrl, 'Name', '') or ''
                        ct = getattr(ctrl, 'ControlTypeName', '')
                        if name == group_name and ct not in ('WindowControl', 'TitleBarControl', 'PaneControl'):
                            try:
                                ctrl.Click()
                                time.sleep(0.5)
                                self._current_group = group_name
                                found = True
                                _mlog(f'[GRP] Clicked "{group_name}" in sidebar')
                            except:
                                pass
                            return
                        for ch in ctrl.GetChildren():
                            find_group(ch, depth + 1)
                    except:
                        pass
                find_group(wx)
            except:
                pass
            
            # ---------- Method 2: Ctrl+F search (fallback) ----------
            if not found:
                try:
                    self._hotkey('ctrl', 'f')
                    time.sleep(0.4)
                    self._hotkey('ctrl', 'a')
                    time.sleep(0.05)
                    self._hotkey('backspace')
                    time.sleep(0.1)
                    # Paste group name instead of SendKeys(char) because uiautomation SendKeys
                    # raises "ord() expected a character" for some Chinese/emoji strings.
                    old_clip = ''
                    try: old_clip = uia.GetClipboardText()
                    except: pass
                    try:
                        uia.SetClipboardText(group_name)
                        time.sleep(0.05)
                        self._hotkey('ctrl', 'v')
                    finally:
                        try:
                            if old_clip: uia.SetClipboardText(old_clip)
                        except: pass
                    time.sleep(0.5)
                    self._hotkey('down')
                    time.sleep(0.2)
                    self._hotkey('enter')
                    time.sleep(0.5)
                    self._current_group = group_name
                    _mlog(f'[GRP] Switched to "{group_name}" via Ctrl+F')
                except Exception as e2:
                    _mlog(f'[GRP] Ctrl+F switch failed: {e2}')
            
        except Exception as e:
            _mlog(f'[GRP] Switch err: {e}')

    def _read_current_chat(self, wx, group_name=None):
        """Detect new messages via screenshot hash comparison"""
        try:
            from PIL import ImageGrab
            import hashlib
            
            # Get CEF window rect (relative to screen)
            r = wx.BoundingRectangle
            if r.width() < 500 or r.height() < 400:
                _mlog(f'[TEXT] invalid WeChat window rect; skip read win={(r.left,r.top,r.right,r.bottom)}')
                self._current_group = None
                return []
            
            # CEF window is Chrome_WidgetWin_0 inside WeChat window
            # WeChat layout: CEF starts ~44% from left, ~13% from top
            cef_left = r.left + int(r.width() * 0.29)
            cef_top = r.top + int(r.height() * 0.08)
            cef_right = r.left + int(r.width() * 0.83)
            cef_bottom = r.top + int(r.height() * 0.92)
            
            # Message area: right portion of CEF (excluding sidebar)
            msg_left = cef_left + int((cef_right - cef_left) * 0.35)
            msg_right = cef_right - 10
            msg_top = cef_top + 50
            msg_bottom = cef_bottom - 80
            
            # Normalize/clamp bbox. Some WeChat layouts are narrow; avoid PIL error:
            # "Coordinate 'right' is less than 'left'".
            msg_left = max(r.left + 5, min(msg_left, r.right - 80))
            msg_right = min(r.right - 5, max(msg_right, msg_left + 80))
            msg_top = max(r.top + 45, min(msg_top, r.bottom - 160))
            msg_bottom = min(r.bottom - 90, max(msg_bottom, msg_top + 120))
            if msg_right <= msg_left or msg_bottom <= msg_top:
                _mlog(f'[TEXT] invalid bbox fixed fallback: {(msg_left,msg_top,msg_right,msg_bottom)} win={(r.left,r.top,r.right,r.bottom)}')
                msg_left = r.left + int(r.width() * 0.32)
                msg_right = r.right - 20
                msg_top = r.top + 90
                msg_bottom = r.bottom - 160
            self._last_msg_bbox = (msg_left, msg_top, msg_right, msg_bottom)

            # Take screenshot of message area
            img = ImageGrab.grab(bbox=(msg_left, msg_top, msg_right, msg_bottom))
            
            # Compute hash
            img_hash = hashlib.md5(img.tobytes()).hexdigest()
            
            hash_key = group_name or '_current'
            if not hasattr(self, '_last_img_hashes'):
                self._last_img_hashes = {}
            
            if hash_key not in self._last_img_hashes:
                self._last_img_hashes[hash_key] = img_hash
                _mlog(f'[TEXT] {hash_key}: init hash {img_hash[:8]}... ({img.size})')
                return []
            
            if img_hash == self._last_img_hashes.get(hash_key):
                return []  # No change in this group
            
            _mlog(f'[TEXT] {hash_key}: SCREEN CHANGED! {img.size}')
            self._last_img_hashes[hash_key] = img_hash
            
            # Try clipboard/UIA reading on the CEF window
            all_text = self._try_clipboard_read(wx, (msg_left, msg_top, msg_right, msg_bottom))
            
            # Detect new messages from text
            new_msgs = self._detect_new_messages(all_text, group_name=hash_key)
            lines = [l for l in all_text.split(chr(10)) if l.strip()]
            if lines:
                _mlog(f'[TEXT] {len(lines)} lines: [{lines[-1][:80]}]')
            
            # ?????????? Weixin/??????????????????????????1??
            if not new_msgs and self.get_cfg('auto_reply_on_screen_change') == 'true':
                return [f'[SCREEN_CHANGE] {hash_key} new message but text unreadable; fallback grab']
            
            return new_msgs
        except Exception as e:
            _mlog(f'[TEXT] err: {e}')
            import traceback
            _mlog(traceback.format_exc()[:200])
            return []

    def _try_clipboard_read(self, wx, bbox):
        """Try to read text by clicking CEF area and using Ctrl+A, Ctrl+C"""
        uia = self._uia
        msg_left, msg_top, msg_right, msg_bottom = bbox
        
        # Click in the message area
        cx = (msg_left + msg_right) // 2
        cy = (msg_top + msg_bottom) // 2
        
        try:
            uia.Click(cx, cy)
            time.sleep(0.2)
            
            old_clip = ''
            try: old_clip = uia.GetClipboardText()
            except: pass
            
            # Ctrl+A
            self._hotkey('ctrl', 'a')
            time.sleep(0.15)
            
            # Ctrl+C
            self._hotkey('ctrl', 'c')
            time.sleep(0.08)
            
            text = uia.GetClipboardText() or ''
            
            if old_clip and old_clip != text:
                try: uia.SetClipboardText(old_clip)
                except: pass
            
            return text if text and len(text) > 10 else self._get_all_chat_text(wx)
        except:
            return self._get_all_chat_text(wx)

    def _get_all_chat_text(self, wx):
        """Fallback: collect text from UIA controls"""
        parts = []
        try:
            def collect(ctrl, d=0):
                if d > 18: return
                try:
                    ct = getattr(ctrl, 'ControlTypeName', '')
                    nm = getattr(ctrl, 'Name', '') or ''
                    if nm and len(nm.strip()) > 2 and ct not in ('WindowControl', 'TitleBarControl'):
                        parts.append(nm.strip())
                    for ch in ctrl.GetChildren():
                        collect(ch, d+1)
                except: pass
            collect(wx)
        except: pass
        return chr(10).join(parts)

    def _detect_new_messages(self, text, group_name=None):
        if not text: return []
        noise = {'Weixin', '??', 'WeChat', '??????'}
        lines = [l.strip() for l in text.split(chr(10)) if len(l.strip()) > 3 and l.strip() not in noise]
        new = []
        prefix = (group_name or '') + '|'
        for l in lines[-30:]:
            h = hashlib.md5((prefix + l).encode()).hexdigest()
            if h not in self._known_hashes:
                self._known_hashes.add(h)
                new.append(l)
        if len(self._known_hashes) > 1000:
            self._known_hashes = set(list(self._known_hashes)[-300:])
        return new

    def _on_new_message(self, group_name, msg):
        """Handle one detected message with strong debounce.
        This avoids repeated replies caused by WeChat UI refreshing after our own reply.
        """
        now = time.time()
        is_screen_change = str(msg).startswith('[SCREEN_CHANGE]')
        # The message hash below is the real duplicate guard. Keep only a short
        # group cooldown so two legitimate orders arriving close together are not lost.
        cooldown = 3 if is_screen_change else 2
        last = self._last_reply_at.get(group_name, 0)
        if now - last < cooldown:
            _mlog(f'[SKIP] {group_name}: cooldown {now-last:.1f}s < {cooldown}s')
            return

        msg_key = hashlib.md5((group_name + '|' + str(msg)[:300]).encode('utf-8', errors='ignore')).hexdigest()
        last_same = self._recent_grabs.get(msg_key, 0)
        if now - last_same < 60:
            _mlog(f'[SKIP] {group_name}: duplicate message within 60s')
            return

        started_at = time.perf_counter()
        from ai_matcher import OrderMatcher
        matcher = OrderMatcher(self.get_cfg)
        match = matcher.match(msg)
        if not matcher.apply_mode_filter(match):
            return
        reply = self.get_cfg('reply_content') or '1'
        _mlog(f'[GRAB] {group_name}: {str(msg)[:50]} -> {match.get("type","?")}')
        
        delay = int(self.get_cfg('reply_delay_ms') or '0')
        if delay > 0:
            time.sleep(delay / 1000.0)

        ok = self._send_reply(reply, msg)
        total_ms = int((time.perf_counter() - started_at) * 1000)
        _mlog(
            f'[PERF] match+reply={total_ms}ms send={self._last_send_elapsed_ms}ms '
            f'quote={self._last_quote_applied if self._last_quote_requested else "off"}'
        )
        if ok:
            self._last_reply_at[group_name] = time.time()
            self._recent_grabs[msg_key] = time.time()
            if len(self._recent_grabs) > 500:
                # keep recent-ish entries only
                cutoff = time.time() - 3600
                self._recent_grabs = {k:v for k,v in self._recent_grabs.items() if v > cutoff}

        ext = match.get('extracted', {})
        rid = db.add_grab_record(
            group_name=group_name, message_text=msg,
            matched_type=match.get('type', ''), match_method=match.get('method', ''),
            reply_content=reply, reply_status='success' if ok else 'failed',
            ai_extracted_time=ext.get('time_info', ''),
            ai_extracted_day=ext.get('extracted_day'),
            ai_extracted_start=ext.get('extracted_start'),
            ai_extracted_duration=ext.get('extracted_duration'),
            user_id=self.user_id,
        )
        db.add_log('grab', f'{group_name}:{match.get("type","")} - {str(msg)[:30]}', user_id=self.user_id)
        if self.on_grab:
            self.on_grab(rid, match)

    def _send_reply(self, text, original_msg=None, quote_override=None):
        """Send reply with optional quoting support"""
        send_started_at = time.perf_counter()
        self._last_quote_requested = (self.get_cfg('quote_reply') == 'true') if quote_override is None else bool(quote_override)
        self._last_quote_applied = False
        try:
            self._ensure_uia()
            uia = self._uia
            wx = self._find_wechat_window(uia)
            if not wx: return False
            if not self._restore_wechat_window(wx, force_foreground=True): return False

            try: wx.SetFocus()
            except: pass
            time.sleep(0.05)

            quote = self._last_quote_requested
            quoted = False
            
            if quote:
                _mlog(f'[REPLY] Quoting enabled, will quote then reply')
                try:
                    quoted = self._do_quote_reply(wx, uia, original_msg)
                    self._last_quote_applied = quoted
                    if not quoted:
                        _mlog('[REPLY] Quote not applied; sending direct reply')
                    time.sleep(0.08)
                except Exception as e:
                    _mlog(f'[REPLY] Quote failed: {e}, fallback to direct reply')

            # Find input edit
            edit = self._find_input_edit_deep(wx, uia)
            if edit:
                try: edit.Click(); time.sleep(0.03)
                except: pass
            else:
                try:
                    r = wx.BoundingRectangle
                    uia.Click(r.left + r.width()//2, r.bottom - 80)
                    time.sleep(0.05)
                except: pass

            # Do not Ctrl+A after applying a quote: on some WeChat versions that
            # selects and replaces the quote card itself.
            if not quoted:
                try:
                    self._hotkey('ctrl', 'a')
                    time.sleep(0.02)
                except: pass

            try:
                try: old = uia.GetClipboardText()
                except: old = ''
                uia.SetClipboardText(text)
                time.sleep(0.03)
                self._hotkey('ctrl', 'v')
                time.sleep(0.05)
                try:
                    if old: uia.SetClipboardText(old)
                except: pass
            except:
                for ch in text:
                    try: uia.SendKeys(ch, waitTime=0.015)
                    except: pass

            time.sleep(0.03)
            self._hotkey('enter')
            time.sleep(0.05)
            self._last_send_elapsed_ms = int((time.perf_counter() - send_started_at) * 1000)
            return True
        except Exception as e:
            self._last_send_elapsed_ms = int((time.perf_counter() - send_started_at) * 1000)
            _mlog(f'[REPLY] err: {e}')
            return False

    def _do_quote_reply(self, wx, uia, original_msg=None):
        """Try to quote the newest visible incoming message.

        Strategy order:
        1) If UIA exposes the message text, find that text control and right-click it.
        2) Otherwise right-click likely newest-message positions inside the message bbox.
        3) Search desktop/window menus for ?? / Quote.
        """
        quote_word = '\u5f15\u7528'
        try:
            wr = wx.BoundingRectangle
            deadline = time.perf_counter() + 1.2

            def click_quote_menu():
                # Fast path for standard WeChat context menus.
                for name in (quote_word, 'Quote'):
                    try:
                        item = uia.MenuItemControl(searchDepth=8, Name=name)
                        if item.Exists(maxSearchSeconds=0.12):
                            item.Click()
                            _mlog(f'[QUOTE] clicked direct menu item: {name}')
                            return True
                    except:
                        pass
                seen = []
                def is_quote_name(name):
                    name = (name or '').strip()
                    low = name.lower()
                    return (quote_word in name) or ('quote' in low)

                count = [0]
                def walk(ctrl, depth=0):
                    if time.perf_counter() > deadline or depth > 6 or count[0] > 180:
                        return False
                    count[0] += 1
                    try:
                        name = getattr(ctrl, 'Name', '') or ''
                        ctype = getattr(ctrl, 'ControlTypeName', '') or ''
                        if name and len(seen) < 12 and ctype in ('MenuItemControl', 'TextControl', 'ButtonControl', 'CustomControl'):
                            seen.append(name)
                        if is_quote_name(name):
                            try:
                                ctrl.Click()
                                _mlog(f'[QUOTE] clicked menu item: {name} ({ctype})')
                                return True
                            except Exception as e:
                                _mlog(f'[QUOTE] click menu item failed: {e}')
                        for ch in ctrl.GetChildren():
                            if walk(ch, depth + 1):
                                return True
                    except:
                        pass
                    return False

                for root_getter in (lambda: uia.GetRootControl(), lambda: wx):
                    try:
                        if walk(root_getter()):
                            return True
                    except:
                        pass
                if seen:
                    _mlog('[QUOTE] menu/control names seen: ' + ' | '.join(seen[:8]))
                return False

            def right_click_and_quote(x, y, label):
                if time.perf_counter() > deadline:
                    return False
                try:
                    _mlog(f'[QUOTE] right click {label} ({x},{y})')
                    uia.RightClick(int(x), int(y))
                    time.sleep(0.12)
                    if click_quote_menu():
                        time.sleep(0.06)
                        return True
                    try: self._hotkey('esc')
                    except: pass
                    time.sleep(0.03)
                except Exception as e:
                    _mlog(f'[QUOTE] {label} failed: {e}')
                return False

            # 1) Text-control based quote. This is the most accurate when WeChat UIA exposes text.
            target = str(original_msg or '').strip()
            if target and not target.startswith('[SCREEN_CHANGE]'):
                fragments = []
                compact = re.sub(r'\s+', '', target)
                if len(compact) >= 6:
                    fragments.extend([compact[-18:], compact[:18], compact[-10:]])
                fragments = [f for f in fragments if len(f) >= 6]
                hits = []
                def collect_text_hits(ctrl, depth=0):
                    if depth > 16 or len(hits) >= 12:
                        return
                    try:
                        name = (getattr(ctrl, 'Name', '') or '').strip()
                        ctype = getattr(ctrl, 'ControlTypeName', '') or ''
                        if name and ctype not in ('WindowControl', 'TitleBarControl', 'PaneControl'):
                            ncompact = re.sub(r'\s+', '', name)
                            if any(f in ncompact or ncompact in f for f in fragments):
                                try:
                                    rr = ctrl.BoundingRectangle
                                    if rr.right > rr.left and rr.bottom > rr.top:
                                        hits.append((rr.bottom, rr.left, rr, name[:30]))
                                except:
                                    pass
                        for ch in ctrl.GetChildren():
                            collect_text_hits(ch, depth + 1)
                    except:
                        pass
                collect_text_hits(wx)
                hits.sort(reverse=True)  # newest/lower first
                for _, __, rr, name in hits[:5]:
                    x = rr.left + min(max(20, rr.width() // 2), max(20, rr.width() - 5))
                    y = rr.top + max(8, min(rr.height() // 2, rr.height() - 5))
                    if right_click_and_quote(x, y, f'text:{name}'):
                        return True

            # 2) Coordinate candidates inside last known message area.
            bbox = getattr(self, '_last_msg_bbox', None)
            if bbox:
                left, top, right, bottom = bbox
            else:
                left = wr.left + int(wr.width() * 0.30)
                right = wr.right - 20
                top = wr.top + int(wr.height() * 0.12)
                bottom = wr.bottom - int(wr.height() * 0.22)

            # Incoming bubbles are left aligned. Try the newest area first and
            # keep fallback bounded so quoting never stalls grabbing.
            xs = [left + 90, left + 210]
            ys = [bottom - 40, bottom - 90, bottom - 145]
            candidates = []
            for y in ys:
                for x in xs:
                    if left + 5 <= x <= right - 5 and top + 5 <= y <= bottom - 5:
                        candidates.append((x, y))
            candidates = candidates[:6]

            _mlog(f'[QUOTE] bbox={left},{top},{right},{bottom}; candidates={len(candidates)}')
            for msg_x, msg_y in candidates:
                if right_click_and_quote(msg_x, msg_y, 'candidate'):
                    return True

            _mlog('[QUOTE] quote menu not found after text/candidates; direct reply fallback')
            return False
        except Exception as e:
            _mlog(f'[QUOTE] err: {e}')
            return False

    def _find_input_edit_deep(self, wx, uia):
        """Find the message input EditControl"""
        all_edits = []
        def collect(ctrl, d=0):
            if d > 25: return
            try:
                ct = getattr(ctrl, 'ControlTypeName', '')
                if ct in ('EditControl', 'DocumentControl', 'CustomControl', 'PaneControl', 'GroupControl'):
                    all_edits.append(ctrl)
                for ch in ctrl.GetChildren():
                    collect(ch, d+1)
            except: pass
        collect(wx)
        if not all_edits: return None
        
        # Best match: bottom-area, largest
        try:
            wr = wx.BoundingRectangle
            wb = wr.bottom
            wh = wr.height()
        except:
            wb, wh = 99999, 1000
        
        bottom = []
        for e in all_edits:
            try:
                er = e.BoundingRectangle
                if (er.top + er.bottom) / 2 > wb - wh * 0.35:
                    bottom.append(e)
            except: pass
        
        if bottom:
            return max(bottom, key=lambda e: e.BoundingRectangle.height() * e.BoundingRectangle.width())
        
        best = None
        bw = 0
        for e in all_edits:
            try:
                er = e.BoundingRectangle
                w, h = er.width(), er.height()
                if h > 15 and w > 100 and w > bw:
                    bw, best = w, e
            except: pass
        return best or (all_edits[-1] if all_edits else None)

    def _dump_control_tree(self, wx, max_depth=5, max_children=10):
        lines = []
        def dump(ctrl, d=0, p=''):
            if d > max_depth: return
            try:
                ct = getattr(ctrl, 'ControlTypeName', '?')
                cl = getattr(ctrl, 'ClassName', '?')
                nm = (getattr(ctrl, 'Name', '') or '')[:40]
                try:
                    r = ctrl.BoundingRectangle
                    pos = f'({r.left},{r.top})-({r.right},{r.bottom})'
                except: pos = '(?)'
                lines.append(f'{p}{"  "*d}[{ct}] cls={cl} name="{nm}" {pos}')
                for ch in ctrl.GetChildren()[:max_children]:
                    dump(ch, d+1, p)
            except: pass
        dump(wx)
        return chr(10).join(lines)


_monitor_instance = None

def get_monitor(config_getter=None, on_grab=None):
    global _monitor_instance
    if _monitor_instance is None and config_getter:
        _monitor_instance = WeChatMonitor(config_getter, on_grab)
    return _monitor_instance

def test_reply(text='1'):
    print('=' * 50)
    print(f'[DIAG] Test send: {text}')
    m = WeChatMonitor(lambda k: '1')
    m._ensure_uia()
    uia = m._uia
    wx = m._find_wechat_window(uia)
    if not wx:
        print('[DIAG] Window not found!')
        return False
    print(f'[DIAG] Window: {wx.ClassName}')
    edit = m._find_input_edit_deep(wx, uia)
    if not edit:
        print('[DIAG] Input not found!')
        return False
    print(f'[DIAG] Input: {edit.Name}, {edit.ClassName}')
    ok = m._send_reply(text, None)
    print(f'[DIAG] Result: {"OK" if ok else "FAIL"}')
    return ok








