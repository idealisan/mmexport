# 相机 Wi-Fi 素材下载工具

通过相机自带的热点，列出并下载原始视频/照片。纯 Python 标准库，不需要官方 SDK、不需要官方 App、不需要抓包。

## 快速开始

```bash
# 1. 相机：设置 > 无线连接（打开）> Wi-Fi 频段 2.4G，屏幕上会显示热点名和密码
# 2. 电脑连上该热点，确认拿到 192.168.2.x 地址
ipconfig getifaddr en0

cd demo
python3 run_test.py        # 环境 → 握手 → 注册 → 回放 → 列表 → HEAD → 下载 1MB
```

产物落在 `demo/test-run-<时间>/`：`summary.txt`、`run.log`、`udp.bin`、`manifest.bin`。

## 目录

| 路径 | 说明 |
|---|---|
| `demo/run_test.py` | 一键全流程自测，失败会停在对应阶段并给出排查提示 |
| `demo/cam_download.py` | 协议实现 + 单条命令（probe / list / download） |
| `demo/test_loopback.py` | 无设备自测：本机假相机跑通握手与列表 |

## 协议速览

| 环节 | 做法 |
|---|---|
| 连接 | 相机 AP `192.168.2.1`(WPA2) → TCP 7001 配对请求 → UDP 9004 握手 |
| 控制 | `[8B 传输头][12B 路由头][协议帧]`，CRC8/CRC16，`cmd_set/cmd_id` |
| 列表 | `0x00/0x26` 查询 → 多条 `0x00/0x27` 分片，剥 10B 子头拼接成 CompositePack |
| 路径 | 清单里 `1a … 01` 是不带扩展名的路径，文件名在同记录的 `0d` 字段 |
| 下载 | `GET /v2?storage=<n>&path=<完整路径>`，支持 HEAD / Range 断点续传 |

更多细节见 `demo/README.md`（阶段含义与排错表）。

## 图形界面

pywebview 开一个本地窗口（macOS 上是 WKWebView），Python 侧跑协议与下载，界面负责选片、看进度。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # 只有 pywebview
.venv/bin/python gui/app.py
```

功能：连接/重连、刷新列表、全选/多选、选输出目录、逐个下载并显示速度与剩余时间、随时取消（保留 .part，下次续传）、运行日志。

| 路径 | 说明 |
|---|---|
| `gui/app.py` | 窗口与 JS API |
| `gui/core.py` | 后台任务线程与状态机（UI 每 400ms 轮询） |
| `gui/web/index.html` | 界面（单文件，无前端依赖） |

> 连接相机热点期间电脑没有外网，属正常现象。

## 说明

- 仅用于自己拥有的设备；连相机热点时电脑会断网，属正常现象。
- 仓库不放固件/大文件，也不包含刷机实现。
