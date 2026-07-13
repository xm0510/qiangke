# -*- coding: utf-8 -*-
"""微信抢单系统 - 系统托盘图标 + 提醒模块"""
import os, sys, json, threading, time, webbrowser
from datetime import datetime, date, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import database as db


class TrayApp:
    """系统托盘应用"""
    
    def __init__(self, user_id=None):
        self.tray_icon = None
        self.running = True
        self.reminder_thread = None
        self.user_id = user_id

    def _owner_id(self):
        if self.user_id is None:
            self.user_id = db.get_primary_admin_user_id()
        return self.user_id
    
    def setup(self):
        """设置托盘图标"""
        try:
            import pystray
            from PIL import Image, ImageDraw
            
            # 创建简单的图标
            icon_img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(icon_img)
            # 画一个圆形图标
            draw.ellipse([8, 8, 56, 56], fill='#4CAF50', outline='#388E3C', width=2)
            draw.text((20, 18), "抢", fill='white')
            
            # 创建菜单
            menu = pystray.Menu(
                pystray.MenuItem("打开配置页面", self.open_config),
                pystray.MenuItem("打开课表", self.open_schedule),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("开始抢单", self.start_service),
                pystray.MenuItem("暂停抢单", self.pause_service),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("关于", self.show_about),
                pystray.MenuItem("退出", self.quit_app),
            )
            
            self.tray_icon = pystray.Icon(
                "WeChatGrabber",
                icon_img,
                "微信抢单系统",
                menu
            )
            
            print("[托盘] 图标已创建")
        except ImportError:
            print("[托盘] pystray 或 Pillow 未安装，托盘功能不可用")
            print("[托盘] 请运行: pip install pystray Pillow")
    
    def run(self):
        """启动托盘"""
        if self.tray_icon:
            # 启动提醒线程
            self.reminder_thread = threading.Thread(target=self._reminder_loop, daemon=True)
            self.reminder_thread.start()
            
            self.tray_icon.run()
    
    def open_config(self):
        """打开配置页面"""
        webbrowser.open("http://127.0.0.1:4876")
    
    def open_schedule(self):
        """打开课表页面"""
        webbrowser.open("http://127.0.0.1:4876/schedule.html")
    
    def start_service(self):
        """开始抢单"""
        user_id = self._owner_id()
        if user_id is None:
            return self._show_notification("\u5fae\u4fe1\u62a2\u5355\u7cfb\u7edf", "\u8bf7\u5148\u5728\u7f51\u9875\u5b8c\u6210\u7ba1\u7406\u5458\u8d26\u53f7\u6ce8\u518c")
        db.set_config("service_status", "running", user_id)
        db.add_log("tray", "手动启动服务", user_id)
        self._show_notification("微信抢单系统", "已开始监听抢单")
    
    def pause_service(self):
        """暂停抢单"""
        user_id = self._owner_id()
        if user_id is None:
            return self._show_notification("\u5fae\u4fe1\u62a2\u5355\u7cfb\u7edf", "\u8bf7\u5148\u5728\u7f51\u9875\u5b8c\u6210\u7ba1\u7406\u5458\u8d26\u53f7\u6ce8\u518c")
        db.set_config("service_status", "paused", user_id)
        db.add_log("tray", "手动暂停服务", user_id)
        self._show_notification("微信抢单系统", "已暂停抢单")
    
    def show_about(self):
        """显示关于"""
        self._show_notification(
            "微信抢单系统 v1.0",
            "自用版 | 家教辅导群自动抢单\n配置页面: http://127.0.0.1:4876"
        )
    
    def quit_app(self):
        """退出应用"""
        self.running = False
        user_id = self._owner_id()
        if user_id is not None:
            db.set_config("service_status", "stopped", user_id)
            db.add_log("tray", "应用退出", user_id)
        if self.tray_icon:
            self.tray_icon.stop()
        os._exit(0)
    
    def _show_notification(self, title: str, message: str):
        """显示系统通知"""
        if self.tray_icon:
            try:
                self.tray_icon.notify(message, title)
            except:
                pass
    
    def _reminder_loop(self):
        """提醒循环：检查今日课表和课前提醒"""
        last_reminder_date = None
        last_pre_class_checks = {}
        
        while self.running:
            try:
                now = datetime.now()
                today = now.date()
                user_id = self._owner_id()
                if user_id is None:
                    time.sleep(30)
                    continue
                
                # 每日提醒（早上8点或首次开机）
                if db.get_config("today_reminder", user_id) == "true":
                    if last_reminder_date != today and now.hour >= 8:
                        schedule = db.get_today_schedule(user_id)
                        if schedule:
                            msg = f"今日 {today.strftime('%m月%d日')} 课程:\n"
                            for s in schedule:
                                status_map = {
                                    "pending": "待确认", "confirmed": "已确认",
                                    "completed": "已完成"
                                }
                                st = status_map.get(s["status"], s["status"])
                                msg += f"  {s['start_time']} {s['student_name']} {s['subject_type']} [{st}]\n"
                            self._show_notification("今日课表提醒", msg)
                        last_reminder_date = today
                
                # 课前提醒
                if db.get_config("pre_class_reminder", user_id) == "true":
                    pre_min = int(db.get_config("pre_class_minutes", user_id) or "15")
                    schedule = db.get_today_schedule(user_id)
                    for s in schedule:
                        key = f"{s['id']}_{s['start_time']}"
                        reminder_time = datetime.strptime(s["start_time"], "%H:%M") - timedelta(minutes=pre_min)
                        reminder_dt = datetime.combine(today, reminder_time.time())
                        
                        if key not in last_pre_class_checks:
                            if now >= reminder_dt and now < datetime.combine(today, datetime.strptime(s["start_time"], "%H:%M").time()):
                                status_map = {
                                    "pending": "待确认", "confirmed": "已确认"
                                }
                                st = status_map.get(s["status"], s["status"])
                                self._show_notification(
                                    f"课前提醒 ({pre_min}分钟后)",
                                    f"{s['start_time']} {s['student_name']} {s['subject_type']} [{st}]"
                                )
                                last_pre_class_checks[key] = True
                
                time.sleep(30)  # 30秒检查一次
                
            except Exception as e:
                time.sleep(60)


def run_tray(user_id=None):
    """启动系统托盘"""
    tray = TrayApp(user_id)
    tray.setup()
    if tray.tray_icon:
        tray.run()
    else:
        print("[托盘] 无托盘图标，使用命令行模式")
        # 命令行模式：保持运行
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("再见!")


if __name__ == "__main__":
    db.init_db()
    run_tray()
