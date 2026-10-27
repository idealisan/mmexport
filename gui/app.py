#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面窗口：pywebview（macOS 上是 WKWebView）加载本地 UI，Python 侧负责协议与下载。"""

import os
import subprocess
import sys
import time

import webview

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core import Core, WEB_DIR  # noqa: E402


class Api:
    def __init__(self, core: Core):
        self.core = core
        self.window = None

    # UI 轮询
    def state(self):
        return self.core.snapshot()

    # 动作
    def connect(self, ip, port):
        return self.core.start_connect(ip, int(port))

    def auto_connect(self, ip, port):
        return self.core.start_auto(ip, int(port))

    def refresh(self):
        return self.core.start_refresh()

    def download(self, indices, outdir):
        return self.core.start_download([int(i) for i in indices], outdir)

    def cancel(self):
        return self.core.cancel()

    def choose_dir(self):
        try:
            picked = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as e:
            self.core.log(f"选择目录失败: {e}")
            return None
        if not picked:
            return None
        path = picked[0] if isinstance(picked, (list, tuple)) else picked
        self.core.log(f"输出目录 -> {path}")
        return path

    def open_dir(self, path):
        if path and os.path.isdir(path):
            subprocess.Popen(["open", path])
            return True
        return False


def _setup_log():
    """打包成 .app 后没有终端，把诊断输出写到 ~/Library/Logs/CameraExport.log。"""
    if not getattr(sys, "frozen", False):
        return
    path = os.path.expanduser("~/Library/Logs/CameraExport.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = fh
    print(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} launch ===")


def main():
    _setup_log()
    if "--ble-test" in sys.argv:
        import blepair
        print("[ble-test] 开始扫描（5s）", flush=True)
        try:
            print("[ble-test] OK " + str(blepair.discover_and_pair(log=print, timeout=5, approve_timeout=5)), flush=True)
        except Exception as e:
            print(f"[ble-test] ERR {type(e).__name__}: {e}", flush=True)
        return

    core = Core()
    api = Api(core)
    window = webview.create_window(
        "相机素材导出",
        os.path.join(WEB_DIR, "index.html"),
        js_api=api,
        width=1120,
        height=780,
        min_size=(920, 640),
        background_color="#0f1115",
    )
    api.window = window
    if not os.path.exists(os.path.join(WEB_DIR, "index.html")):
        print(f"[fatal] UI 文件缺失: {os.path.join(WEB_DIR, 'index.html')}", flush=True)

    def after_start():
        try:
            win = webview.windows[0]
            info = win.evaluate_js("document.title + '|' + document.querySelectorAll('#rows tr').length")
            print(f"[ui] {info}", flush=True)
        except Exception as e:
            print(f"[ui] 页面自检失败: {e}", flush=True)

    webview.start(after_start, debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
