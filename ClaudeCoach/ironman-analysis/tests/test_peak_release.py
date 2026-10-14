"""Peak release numbers move together (13 Oct 2026: app.js?v= sat at 83 from 3.31 to 3.44
while sw.js was bumped every release, and Jamie's phone kept the old script)."""
import re
from pathlib import Path

COACH = Path(__file__).resolve().parents[3] / "coach"


def test_script_version_matches_the_cache_version():
    sw = (COACH / "sw.js").read_text()
    html = (COACH / "app.html").read_text()
    cache = re.search(r"const CACHE = 'peak-v(\d+)'", sw).group(1)
    v = re.search(r'<script src="app\.js\?v=(\d+)"', html).group(1)
    assert v == cache, f"app.html loads app.js?v={v} but sw.js CACHE is peak-v{cache}: bump both"


def test_changelog_leads_with_the_current_version():
    """Settings' What's new (14 Oct 2026) opens on the release you are running."""
    sw = (COACH / "sw.js").read_text()
    js = (COACH / "app.js").read_text()
    ver = re.search(r"const APP_VERSION = '([\d.]+)'", sw).group(1)
    first = re.search(r'var CHANGELOG = \[\s*\["([\d.]+)"', js).group(1)
    assert first == ver, f"sw.js is {ver} but the changelog starts at {first}: add a line"
