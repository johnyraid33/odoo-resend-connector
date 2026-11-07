"""Render dev/store/cover.html to the module cover images (1600x900).

Needs Google Chrome. Run from the repository root:

    python3 dev/store/render_cover.py
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'dev' / 'store' / 'cover.html'
TARGETS = [
    ROOT / 'rex_resend_connector' / 'static' / 'description' / 'img' / 'cover.png',
    ROOT / 'rex_resend_connector' / 'images' / 'cover.png',
]
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

with tempfile.TemporaryDirectory() as tmp:
    out = Path(tmp) / 'cover.png'
    subprocess.run([
        CHROME, '--headless=new', '--disable-gpu', '--hide-scrollbars',
        '--allow-file-access-from-files', f'--user-data-dir={tmp}/profile',
        '--force-device-scale-factor=1', '--window-size=1600,900',
        f'--screenshot={out}', SOURCE.as_uri(),
    ], check=True, capture_output=True)
    for target in TARGETS:
        shutil.copyfile(out, target)
        print('wrote', target.relative_to(ROOT))
