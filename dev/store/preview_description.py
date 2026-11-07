"""Preview static/description/index.html roughly as the Odoo Apps Store shows it.

The store strips <style> and <script> and renders the rest inside its own page,
which loads Bootstrap 5, Font Awesome 4 and Open Sans. This script does the same
and writes one screenshot per tab into dev/store/preview/.

    python3 dev/store/preview_description.py
"""
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESC = ROOT / 'rex_resend_connector' / 'static' / 'description'
OUT = ROOT / 'dev' / 'store' / 'preview'
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

html = (DESC / 'index.html').read_text(encoding='utf-8')
html = re.sub(r'<(style|script)\b.*?</\1>', '', html, flags=re.S | re.I)
tabs = re.findall(r'data-bs-toggle="tab" href="#([\w-]+)"', html)

page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<base href="{DESC.as_uri()}/">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/css/font-awesome.min.css">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600;700;800&display=swap">
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</head><body style="font-family:'Open Sans',Helvetica,sans-serif;color:#646464;background:#fff">
<div id="module-description"><div class="oe_styling_v8" style="padding:24px 0">{html}</div></div>
<script>
window.addEventListener('load', () => {{
  const t = new URLSearchParams(location.search).get('tab');
  const a = t && document.querySelector('[href="#' + t + '"]');
  if (a) {{ bootstrap.Tab.getOrCreateInstance(a).show(); }}
}});
</script></body></html>"""

OUT.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as tmp:
    src = Path(tmp) / 'preview.html'
    src.write_text(page, encoding='utf-8')
    # Headless Chrome will not make a window narrower than about 500px, so the
    # phone view is a 390px iframe inside a wider window.
    phone = Path(tmp) / 'phone.html'
    phone.write_text(
        '<body style="margin:0;background:#ddd">'
        f'<iframe src="{src.as_uri()}?tab={tabs[0]}" '
        'style="width:390px;height:5200px;border:0;background:#fff"></iframe></body>',
        encoding='utf-8')
    shots = [(f'{tab}.png', f'{src.as_uri()}?tab={tab}', 1400, 3400) for tab in tabs]
    shots.append((f'{tabs[0]}-mobile.png', phone.as_uri(), 600, 5200))
    for name, url, width, height in shots:
        out = OUT / name
        subprocess.run([
            CHROME, '--headless=new', '--disable-gpu', '--hide-scrollbars',
            '--allow-file-access-from-files', f'--user-data-dir={tmp}/profile',
            '--force-device-scale-factor=1', f'--window-size={width},{height}',
            '--virtual-time-budget=6000', f'--screenshot={out}', url,
        ], check=True, capture_output=True)
        print('wrote', out.relative_to(ROOT))
