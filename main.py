# -*- coding: utf-8 -*-
"""微信抢单系统 - 主入口
启动顺序: 
  1. 初始化数据库
  2. 启动 Flask 配置服务器 (port 4876)
  3. 启动微信监听
  4. 启动系统托盘
"""
import os, sys, threading, time, webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import database as db
from server import app, run_server


def main():
    print("=" * 60)
    print("  微信抢单系统 v1.0 (自用版)")
    print("  配置页面: http://127.0.0.1:4876")
    print("=" * 60)
    
    # 1. 初始化数据库
    db.init_db()
    
    # 2. 启动 Flask 服务器（后台线程）
    server_thread = threading.Thread(
        target=run_server, 
        kwargs={"host": "127.0.0.1", "port": 4876},
        daemon=True, 
        name="FlaskServer"
    )
    server_thread.start()
    time.sleep(1)  # 等待服务器启动
    
    # 3. 检查是否需要自动启动
    auto_start = db.get_config("auto_start") == "true"
    
    # 4. 启动微信监听（如果服务状态是 running）
    status = db.get_config("service_status") or "stopped"
    wechat_monitor = None
    
    if status == "running" or auto_start:
        try:
            from wechat_monitor import WeChatMonitor
            wechat_monitor = WeChatMonitor(db.get_config)
            wechat_monitor.start()
            db.set_config("service_status", "running")
        except Exception as e:
            print(f"[启动] 微信监听启动失败: {e}")
    
    # 5. 自动打开浏览器（首次运行）
    if not db.get_config("browser_opened") == "true":
        time.sleep(1)
        webbrowser.open("http://127.0.0.1:4876")
        db.set_config("browser_opened", "true")
    
    # 6. 启动系统托盘
    try:
        from tray_app import run_tray
        run_tray()
    except ImportError:
        print("[托盘] pystray 未安装，使用命令行模式（按 Ctrl+C 退出）")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n正在退出...")
    
    # 清理
    if wechat_monitor:
        wechat_monitor.stop()
    db.set_config("service_status", "stopped")
    print("已退出")


if __name__ == "__main__":
    main()
