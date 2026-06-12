"""Smoke test for the Drive-mirror link plumbing — run with:
    .venv/bin/python tests/smoke_drive_links.py
Imports bot.py for real (catches runtime errors py_compile can't), then checks
_guild_token determinism and every _resolve_restore_link branch.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402  (module import itself is the first assertion)

GID = 787744065801945089
DRIVE_URL = "https://drive.google.com/open?id=TESTONLY"

# _guild_token: deterministic, 24 hex chars, differs per guild.
t1, t2 = bot._guild_token(GID), bot._guild_token(GID + 1)
assert t1 == bot._guild_token(GID) and len(t1) == 24 and t1 != t2, "token derivation broke"

bot._PUBLIC_DOMAIN = "backup-bot-production.up.railway.app"
bot._drive_links.clear()
bot._drive_links[str(GID)] = DRIVE_URL

# Bot-issued Drive link → resolves to the local guild id (no URL download).
assert bot._resolve_restore_link(DRIVE_URL) == (None, GID)

# Our own /latest link → raw=1 appended so the Drive 302 doesn't serve HTML.
latest = bot._latest_link(GID + 1)  # guild WITHOUT a drive link → /latest URL
url, lgid = bot._resolve_restore_link(latest)
assert lgid is None and url == latest + "?raw=1", f"unexpected: {url}"
# Already has raw → untouched.
assert bot._resolve_restore_link(url) == (url, None)

# Foreign URL → passed through untouched.
foreign = "https://cdn.discordapp.com/attachments/1/2/backup.zip"
assert bot._resolve_restore_link(foreign) == (foreign, None)

# _latest_link prefers the Drive link when one exists.
assert bot._latest_link(GID) == DRIVE_URL
assert bot._latest_link(GID + 1).startswith("https://backup-bot-production")

# ---- freshness: a local zip NEWER than the mirror beats the Drive link ---- #
import shutil  # noqa: E402
import time  # noqa: E402

import storage  # noqa: E402

FRESH_GID = 424242424242424242  # fake guild — its data dir is created + removed here
bot._drive_links[str(FRESH_GID)] = DRIVE_URL
bot._drive_links_ts.pop(str(FRESH_GID), None)
try:
    # No local zips + no mirrored-at ts → trust the Drive copy (volume wiped case).
    assert bot._drive_link_is_fresh(FRESH_GID), "no local zip should mean fresh"
    assert bot._latest_link(FRESH_GID) == DRIVE_URL

    # Local zip exists but mirror was never stamped → stale → own /latest link.
    bdir = storage.backups_dir(FRESH_GID)
    with open(os.path.join(bdir, "manual-1.zip"), "w") as f:
        f.write("zip")
    assert not bot._drive_link_is_fresh(FRESH_GID), "unstamped mirror must be stale"
    own = bot._latest_link(FRESH_GID)
    assert own and own.startswith("https://backup-bot-production"), own

    # Mirror stamped AFTER the zip → fresh again → Drive link.
    bot._drive_links_ts[str(FRESH_GID)] = time.time() + 1
    assert bot._drive_link_is_fresh(FRESH_GID)
    assert bot._latest_link(FRESH_GID) == DRIVE_URL

    # A newer backup lands after the stamp → stale → own link.
    later = os.path.join(bdir, "manual-2.zip")
    with open(later, "w") as f:
        f.write("zip")
    os.utime(later, (time.time() + 60, time.time() + 60))
    assert not bot._drive_link_is_fresh(FRESH_GID)
    assert bot._latest_link(FRESH_GID).startswith("https://backup-bot-production")
finally:
    bot._drive_links.pop(str(FRESH_GID), None)
    bot._drive_links_ts.pop(str(FRESH_GID), None)
    shutil.rmtree(storage.guild_dir(FRESH_GID), ignore_errors=True)

# ---- web layer: /latest redirect + raw bypass, set_drive_links endpoint ---- #
import asyncio  # noqa: E402
import json  # noqa: E402

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402


async def _web_checks():
    app = web.Application()
    app.router.add_get("/latest/{token}/{gid}", bot._h_latest)
    app.router.add_post("/admin/{secret}/set_drive_links", bot._h_admin_set_drive_links)
    async with TestClient(TestServer(app)) as c:
        # /latest with a Drive link → 302 to Drive; raw=1 bypasses the redirect.
        r = await c.get(f"/latest/{t1}/{GID}", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == DRIVE_URL, r.status
        r = await c.get(f"/latest/{t1}/{GID}?raw=1", allow_redirects=False)
        assert r.status != 302, "raw=1 must not redirect"
        # Wrong token → 403 regardless of Drive link.
        r = await c.get(f"/latest/{'0' * 24}/{GID}", allow_redirects=False)
        assert r.status == 403
        # set_drive_links: merges even WITHOUT a JSON content-type header.
        r = await c.post(f"/admin/{bot.ADMIN_SECRET}/set_drive_links",
                         data=json.dumps({"999": "https://drive.google.com/open?id=NEW"}))
        assert r.status == 200 and (await r.json())["links"] == 2, await r.text()
        assert bot._drive_links["999"].endswith("NEW")
        assert json.load(open(bot._DRIVE_LINKS_PATH))["999"].endswith("NEW")
        # The POST stamps mirrored-at, so the freshness check trusts the link.
        assert bot._drive_links_ts["999"] > 0
        assert json.load(open(bot._DRIVE_LINKS_TS_PATH))["999"] > 0
        # Stale mirror (local zip newer than the stamp) → /latest serves the
        # local zip itself instead of redirecting to the outdated Drive copy.
        import storage as _st
        sb = _st.backups_dir(GID)
        with open(os.path.join(sb, "manual-web.zip"), "w") as f:
            f.write("zip")
        try:
            bot._drive_links_ts[str(GID)] = 1.0  # stamped long before the zip
            r = await c.get(f"/latest/{t1}/{GID}", allow_redirects=False)
            assert r.status != 302, "stale Drive link must not redirect"
        finally:
            bot._drive_links_ts.pop(str(GID), None)
            import shutil as _sh
            _sh.rmtree(_st.guild_dir(GID), ignore_errors=True)
        # Garbage body → 400, bad secret → 403.
        r = await c.post(f"/admin/{bot.ADMIN_SECRET}/set_drive_links", data="not json")
        assert r.status == 400
        r = await c.post("/admin/wrong/set_drive_links", data="{}")
        assert r.status == 403


asyncio.run(_web_checks())
# Don't leave test links/stamps where the real bot reads them.
os.remove(bot._DRIVE_LINKS_PATH)
os.remove(bot._DRIVE_LINKS_TS_PATH)

print("OK — bot imports clean, token/link resolution + web endpoints all pass")
