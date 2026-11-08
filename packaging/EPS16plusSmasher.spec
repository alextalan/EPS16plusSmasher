# PyInstaller spec for EPS16plusSmasher.app
# Use packaging/build_app.sh rather than calling this directly.
# -*- mode: python ; coding: utf-8 -*-
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

a = Analysis(
    [os.path.join(ROOT, 'EPS16plusSmasher.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    # mido picks its MIDI backend by name at runtime; static analysis can't
    # see that, so name the rtmidi pieces explicitly.
    hiddenimports=['rtmidi', 'rtmidi._rtmidi', 'mido.backends.rtmidi'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['mido.backends.portmidi', 'mido.backends.pygame',
              'mido.backends.amidi', 'numpy', 'PIL', 'matplotlib'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='EPS16plusSmasher',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,        # same architecture as the build Mac
    codesign_identity=None,  # ad-hoc signature; not notarized
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               name='EPS16plusSmasher')

app = BUNDLE(
    coll,
    name='EPS16plusSmasher.app',
    icon=None,
    bundle_identifier='io.github.eps16plussmasher',
    version='1.0.0',
    info_plist={
        'CFBundleName': 'EPS16plusSmasher',
        'CFBundleDisplayName': 'EPS16plusSmasher',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'NSHighResolutionCapable': True,
        'LSApplicationCategoryType': 'public.app-category.music',
    },
)
