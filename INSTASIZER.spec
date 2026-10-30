# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['instasizer.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],  # PySide6(Qt) UI로 바뀌어 Tk는 쓰지 않음
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='INSTASIZER',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX로 압축한 Qt DLL은 실행 시 깨지는 경우가 있어 끔
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
