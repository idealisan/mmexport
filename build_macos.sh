#!/usr/bin/env bash
# 构建 macOS .app（PyInstaller）。产物：dist/CameraExport.app
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

.venv/bin/pip install -q -r requirements.txt pyinstaller
.venv/bin/pyinstaller --noconfirm --clean CameraExport.spec

# 本地 ad-hoc 签名，减少 Gatekeeper 的奇怪行为；没有开发者证书也能跑
codesign --force --deep --sign - dist/CameraExport.app >/dev/null 2>&1 || true

du -sh dist/CameraExport.app
echo "完成：dist/CameraExport.app"
