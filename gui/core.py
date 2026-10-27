import copy
import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "demo"))
import cam_download as lib  # noqa: E402

DEFAULT_OUT = os.path.expanduser("~/Movies/CameraExport")


class Core:
    """单工作线程 + 轮询状态：UI 只读 snapshot()，所有耗时操作都在后台跑。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._busy = False
        self._cam = None
        self._state = {
            "phase": "idle",          # idle / connecting / listing / downloading / done / error
            "message": "未连接",
            "connected": False,
            "device": "",
            "ip": "192.168.2.1",
            "port": 9004,
            "files": [],
            "outdir": DEFAULT_OUT,
            "progress": {"done": 0, "total": 0, "speed": 0, "name": "", "index": 0, "count": 0},
            "log": [],
        }

    # ---------- state ----------
    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def _update(self, **kw):
        with self._lock:
            self._state.update(kw)

    def _set_progress(self, **kw):
        with self._lock:
            self._state["progress"].update(kw)

    def log(self, msg: str):
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            log = self._state["log"]
            log.append(f"{stamp}  {msg}")
            del log[:-300]
        print(f"[gui] {msg}", flush=True)

    def _fail(self, msg, exc=None):
        if exc is not None:
            self.log(f"错误: {exc}")
            self.log(traceback.format_exc(limit=2).strip().splitlines()[-1])
        self._update(phase="error", message=msg)

    def _begin(self, phase, message):
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        self._cancel.clear()
        self._update(phase=phase, message=message)
        return True

    def _end(self):
        with self._lock:
            self._busy = False

    def _thread(self, fn, *args):
        t = threading.Thread(target=fn, args=args, daemon=True)
        t.start()
        return {"ok": True}

    # ---------- jobs ----------
    def start_connect(self, ip: str, port: int):
        if not self._begin("connecting", "正在连接..."):
            return {"ok": False, "error": "busy"}
        self._update(ip=ip, port=int(port), files=[], device="")
        self.log(f"连接 {ip}:{port}")
        return self._thread(self._job_connect, ip, int(port))

    def start_refresh(self):
        if not self._begin("listing", "读取文件列表..."):
            return {"ok": False, "error": "busy"}
        return self._thread(self._job_list)

    def start_download(self, indices, outdir: str):
        if not self._begin("downloading", "准备下载..."):
            return {"ok": False, "error": "busy"}
        self._update(outdir=outdir)
        return self._thread(self._job_download, list(indices), outdir)

    def cancel(self):
        self._cancel.set()
        self.log("已请求取消")
        return {"ok": True}

    # ---------- internals ----------
    def _close_cam(self):
        try:
            if self._cam:
                self._cam.close()
        except Exception:
            pass
        self._cam = None

    def _ensure_cam(self):
        with self._lock:
            ip, port = self._state["ip"], self._state["port"]
        self._close_cam()
        cam = lib.Camera(ip, port, verbose=False)
        if not cam.open_link():
            raise RuntimeError("握手失败：确认电脑已连上相机 Wi-Fi")
        cam.register()
        version = cam.get_version()
        cam.enter_playback()
        self._cam = cam
        return version

    def _fetch_files(self):
        files = self._cam.list_media()
        with self._lock:
            ip = self._state["ip"]
        out = []
        for i, f in enumerate(files):
            if self._cancel.is_set():
                break
            size = lib.head_size(ip, f["store"], f["path"])
            out.append({"idx": i, "name": f["name"], "path": f["path"],
                        "store": f["store"], "size": size})
            if i % 10 == 0:
                self._update(message=f"读取文件信息 {i + 1}/{len(files)}")
        return out

    def _job_connect(self, ip, port):
        try:
            version = self._ensure_cam()
            self._update(connected=True, device=version.replace("\x00", " | ") if version else "相机",
                         message="已连接，读取列表...")
            self.log(f"已连接 {version!r}")
            files = self._fetch_files()
            self._update(files=files, phase="idle",
                         message=f"已连接 · {len(files)} 个文件")
            self.log(f"列表完成：{len(files)} 个文件")
        except Exception as e:
            self._fail(f"连接失败：{e}", e)
        finally:
            self._end()

    def _job_list(self):
        try:
            if not self._cam:
                self.log("重新建立连接")
                version = self._ensure_cam()
                self._update(connected=True, device=version or "相机")
            files = self._fetch_files()
            self._update(files=files, phase="idle", message=f"{len(files)} 个文件")
        except Exception as e:
            self._fail(f"读取列表失败：{e}", e)
        finally:
            self._end()

    def _job_download(self, indices, outdir):
        try:
            with self._lock:
                files = list(self._state["files"])
                ip = self._state["ip"]
            queue = [files[i] for i in indices if 0 <= i < len(files)]
            if not queue:
                self._update(phase="idle", message="没有选中的文件")
                return
            os.makedirs(outdir, exist_ok=True)
            ok = 0
            for k, f in enumerate(queue):
                if self._cancel.is_set():
                    break
                dest = os.path.join(outdir, f["name"])
                if os.path.exists(dest) and (not f["size"] or os.path.getsize(dest) == f["size"]):
                    self.log(f"跳过（已存在）{f['name']}")
                    ok += 1
                    continue
                self._set_progress(done=0, total=f["size"], speed=0, name=f["name"],
                                   index=k + 1, count=len(queue))
                self._update(message=f"下载中 {k + 1}/{len(queue)} · {f['name']}")
                self.log(f"开始下载 {f['name']} ({f['size'] / 1048576:.1f} MB)")

                last = {"t": time.time(), "bytes": 0}
                def progress(done, total, _k=k, _f=f):
                    now = time.time()
                    if now - last["t"] >= 0.15:
                        speed = (done - last["bytes"]) / max(now - last["t"], 1e-6)
                        last["t"], last["bytes"] = now, done
                        self._set_progress(done=done, total=total or _f["size"], speed=speed,
                                           name=_f["name"], index=_k + 1, count=len(queue))

                got = lib.download_file(ip, f["store"], f["path"], dest,
                                        progress=progress, should_stop=self._cancel.is_set)
                if got:
                    ok += 1
                    self.log(f"完成 {f['name']}")
                elif self._cancel.is_set():
                    self.log("已取消")
                    break
                else:
                    self.log(f"失败 {f['name']}（可重试，会断点续传）")
            msg = f"已取消，完成 {ok}/{len(queue)}" if self._cancel.is_set() else f"完成 {ok}/{len(queue)}"
            self._update(phase="idle", message=msg)
            self.log(msg)
        except Exception as e:
            self._fail(f"下载出错：{e}", e)
        finally:
            self._end()
