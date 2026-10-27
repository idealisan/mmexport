# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("webview") + collect_submodules("bleak") + [
    "objc", "Foundation", "AppKit", "WebKit", "CoreBluetooth", "Quartz",
]

a = Analysis(
    ['gui/app.py'],
    pathex=[],
    binaries=[],
    datas=[('gui/web', 'web'), ('demo/cam_download.py', 'demo')],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'numpy', 'matplotlib', 'PyQt5', 'PySide6'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CameraExport',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CameraExport',
)
app = BUNDLE(
    coll,
    name='CameraExport.app',
    icon=None,
    bundle_identifier='com.local.cameraexport',
    info_plist={
        'CFBundleDisplayName': '相机素材导出',
        'CFBundleShortVersionString': '0.1.0',
        'CFBundleVersion': '1',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '11.0',
        'NSBluetoothAlwaysUsageDescription': '用于通过蓝牙发现相机并读取其 Wi-Fi 热点信息。',
        'NSBluetoothPeripheralUsageDescription': '用于通过蓝牙发现相机并读取其 Wi-Fi 热点信息。',
        'NSLocationWhenInUseUsageDescription': '用于读取当前 Wi-Fi 名称。',
    },
)
