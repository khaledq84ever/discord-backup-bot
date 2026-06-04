"""Generate 5 branded INFO images for the BackUp Bot GitHub/tool page.

Same design system as make_promo.py: navy canvas, gold/red brand, RTL-safe.
Output: assets/info/*.png at 1280x640.
"""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "fonts")
OUT = os.path.join(HERE, "info")
os.makedirs(OUT, exist_ok=True)

W, H = 1280, 640
NAVY = (10, 14, 39)
CARD = (20, 26, 58)
CARD_HI = (28, 36, 78)
LINE = (40, 50, 96)
MUTED = (154, 166, 212)
DIM = (118, 130, 175)
WHITE = (255, 255, 255)
RED = (232, 0, 28)
GOLD = (212, 168, 67)
GREEN = (87, 242, 135)
BLUE = (66, 153, 245)
BLURPLE = (88, 101, 242)


def inter(size, weight=700):
    f = ImageFont.truetype(os.path.join(FONTS, "Inter-var.ttf"), size)
    try:
        f.set_variation_by_axes([14, weight])
    except Exception:
        pass
    return f


def T(d, xy, s, font, fill, anchor="la"):
    d.text(xy, s, font=font, fill=fill, anchor=anchor)


def tw(d, s, font):
    b = d.textbbox((0, 0), s, font=font)
    return b[2] - b[0], b[3] - b[1]


def base():
    img = Image.new("RGB", (W, H), NAVY)
    d = ImageDraw.Draw(img)
    # subtle top band
    d.rectangle([0, 0, W, 96], fill=(13, 18, 48))
    d.line([0, 96, W, 96], fill=LINE, width=2)
    # shield mark
    try:
        sh = Image.open(os.path.join(HERE, "logos", "01-backupbot-512.png")).convert("RGBA")
        sh = sh.resize((60, 60))
        img.paste(sh, (40, 18), sh)
    except Exception:
        pass
    T(d, (114, 34), "BackUp Bot", inter(34, 800), WHITE)
    T(d, (W - 40, 40), "discordbackupbot.vercel.app", inter(20, 600), DIM, anchor="ra")
    return img, d


def rrect(d, box, r, fill, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def chip(d, x, y, text, color):
    w, _ = tw(d, text, inter(22, 700))
    rrect(d, [x, y, x + w + 36, y + 46], 23, CARD_HI, outline=color, width=2)
    T(d, (x + 18, y + 10), text, inter(22, 700), color)
    return x + w + 36 + 16


def footer(d, text):
    T(d, (40, H - 50), text, inter(20, 600), DIM)
    T(d, (W - 40, H - 50), "🛡  full · isolated · no-limits", inter(20, 700), GOLD, anchor="ra")


# 1 — FULL SERVER CLONE -------------------------------------------------------
def img1():
    img, d = base()
    T(d, (40, 130), "Full Server Clone", inter(64, 800), WHITE)
    T(d, (40, 210), "Back up one server, rebuild it whole on a fresh one.", inter(28, 600), MUTED)

    def server_card(x, title, sub, color):
        rrect(d, [x, 290, x + 360, 520], 22, CARD, outline=LINE, width=2)
        rrect(d, [x, 290, x + 360, 352], 22, CARD_HI)
        T(d, (x + 28, 308), title, inter(30, 800), color)
        rows = ["Roles · colors · perms", "Categories · rooms",
                "All chat history", "Images · files", "Emojis · settings"]
        for i, rrow in enumerate(rows):
            yy = 372 + i * 28
            d.ellipse([x + 28, yy + 6, x + 40, yy + 18], fill=color)
            T(d, (x + 54, yy), rrow, inter(21, 600), MUTED)
    server_card(40, "SOURCE", "", BLUE)
    server_card(880, "NEW SERVER", "", GREEN)
    # arrow
    T(d, (W // 2, 392), "clone", inter(26, 800), GOLD, anchor="mm")
    d.line([440, 420, 840, 420], fill=GOLD, width=6)
    for px in (840,):
        d.polygon([(px, 408), (px, 432), (px + 22, 420)], fill=GOLD)
    footer(d, "Source keeps its data · target filled from the very first message")
    img.save(os.path.join(OUT, "info-01-full-clone.png"))


# 2 — NO LIMITS ---------------------------------------------------------------
def img2():
    img, d = base()
    T(d, (40, 130), "No Limits", inter(64, 800), WHITE)
    T(d, (40, 210), "If a server clones, it clones ALL of it — to the full.", inter(28, 600), MUTED)
    cards = [
        ("MESSAGES", "unlimited", "every message from day one", GREEN),
        ("SIZE", "up to 5 GB / server", "fair-use cap to protect the volume", GOLD),
        ("TIME", "100 min · 12 h · 1 day", "runs as long as it takes", BLUE),
    ]
    x = 40
    for title, big, sub, color in cards:
        rrect(d, [x, 300, x + 380, 520], 22, CARD, outline=LINE, width=2)
        T(d, (x + 28, 326), title, inter(24, 800), color)
        T(d, (x + 28, 366), big, inter(40, 800), WHITE)
        T(d, (x + 28, 436), sub, inter(21, 600), MUTED)
        x += 400
    footer(d, "Interrupted? It RESUMES — re-run finishes exactly where it stopped")
    img.save(os.path.join(OUT, "info-02-no-limits.png"))


# 3 — ARCHITECTURE ------------------------------------------------------------
def img3():
    img, d = base()
    T(d, (40, 128), "How it works", inter(56, 800), WHITE)
    boxes = [
        (40, "Discord", "gateway + REST", BLURPLE),
        (300, "Backup engine", "scrape all rooms\n+ threads + forums", BLUE),
        (580, "Per-guild store", "SQLite + sha256\nattachments", GOLD),
        (860, "Restore engine", "roles → rooms →\nmessages (resume)", GREEN),
    ]
    for x, title, sub, color in boxes:
        rrect(d, [x, 240, x + 230, 400], 18, CARD, outline=color, width=2)
        T(d, (x + 20, 262), title, inter(26, 800), color)
        yy = 304
        for ln in sub.split("\n"):
            T(d, (x + 20, yy), ln, inter(19, 600), MUTED)
            yy += 26
        if x < 860:
            d.line([x + 230, 320, x + 270, 320], fill=DIM, width=4)
            d.polygon([(x + 262, 312), (x + 262, 328), (x + 280, 320)], fill=DIM)
    rrect(d, [40, 450, 1240, 560], 18, CARD_HI, outline=LINE, width=2)
    T(d, (64, 472), "Remote control API", inter(26, 800), RED)
    T(d, (64, 512), "/admin/<secret>/cmd  →  diag · scan · verify_clone · backup · dedup",
      inter(22, 600), MUTED)
    footer(d, "One web server: backups + downloads + AI control plane")
    img.save(os.path.join(OUT, "info-03-architecture.png"))


# 4 — PER-ROOM VERIFY ---------------------------------------------------------
def img4():
    img, d = base()
    T(d, (40, 130), "Per-room Verification", inter(54, 800), WHITE)
    T(d, (40, 200), "After a clone, every room is checked: did its chat land?", inter(26, 600), MUTED)
    rows = [("#chatting", "3892", "3892", GREEN, "full"),
            ("#programming", "978", "978", GREEN, "full"),
            ("#images", "2.1 GB", "2.1 GB", GREEN, "full"),
            ("#archive", "44120", "44120", GREEN, "full")]
    y = 270
    rrect(d, [40, y, 1240, y + 50], 12, CARD_HI)
    for tx, lbl in ((64, "ROOM"), (640, "SOURCE"), (840, "TARGET"), (1060, "STATUS")):
        T(d, (tx, y + 12), lbl, inter(22, 800), DIM)
    y += 60
    for room, s, t, color, st in rows:
        rrect(d, [40, y, 1240, y + 56], 12, CARD, outline=LINE, width=1)
        T(d, (64, y + 14), room, inter(24, 700), WHITE)
        T(d, (640, y + 14), s, inter(24, 600), MUTED)
        T(d, (840, y + 14), t, inter(24, 600), MUTED)
        d.ellipse([1060, y + 20, 1078, y + 38], fill=color)
        T(d, (1090, y + 14), st, inter(24, 800), color)
        y += 64
    footer(d, "12/12 rooms full · counts matched source-to-target")
    img.save(os.path.join(OUT, "info-04-verify.png"))


# 5 — REMOTE CONTROL / COMMANDS ----------------------------------------------
def img5():
    img, d = base()
    T(d, (40, 130), "Slash + Remote Control", inter(50, 800), WHITE)
    T(d, (40, 198), "12 slash commands in Discord — plus a full AI control API.", inter(25, 600), MUTED)
    cmds = ["/backup", "/restore", "/verify", "/status", "/report", "/copy",
            "/download", "/dedup", "/schedule", "/search", "/stats", "/help"]
    x, y = 40, 268
    for c in cmds:
        w, _ = tw(d, c, inter(24, 700))
        rrect(d, [x, y, x + w + 40, y + 52], 14, CARD, outline=BLURPLE, width=2)
        T(d, (x + 20, y + 12), c, inter(24, 700), WHITE)
        x += w + 40 + 16
        if x > 1080:
            x, y = 40, y + 66
    rrect(d, [40, 470, 1240, 560], 16, CARD_HI, outline=GOLD, width=2)
    T(d, (64, 490), "GET/POST /admin/<secret>/cmd?do=", inter(24, 800), GOLD)
    T(d, (64, 524), "diag · scan · integrity_all · backup_all · dedup · prune · verify_clone",
      inter(20, 600), MUTED)
    footer(d, "Drive every backup & restore from chat — read console, fix, verify")
    img.save(os.path.join(OUT, "info-05-control.png"))


for fn in (img1, img2, img3, img4, img5):
    fn()
print("✅ wrote 5 info images to", OUT)
