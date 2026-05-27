"""Generate 5 branded promo images for BackUp Bot.

Same design system as the sister bots: navy canvas, vector icons, libraqm RTL.
Output: assets/promo/*.png at 1280x640.
"""
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "fonts")
OUT = os.path.join(HERE, "promo")
os.makedirs(OUT, exist_ok=True)

W, H = 1280, 640

NAVY = (10, 14, 39)
CARD = (20, 26, 58)
CARD_HI = (28, 36, 78)
LINE = (38, 48, 94)
MUTED = (154, 166, 212)
DIM = (118, 130, 175)
WHITE = (255, 255, 255)
RED = (232, 0, 28)
GOLD = (212, 168, 67)
BLURPLE = (88, 101, 242)
GREEN = (87, 242, 135)
BLUE = (66, 153, 245)
PURPLE = (163, 113, 247)
PINK = (236, 84, 178)


def has_ar(s):
    return any("؀" <= c <= "ۿ" or "ݐ" <= c <= "ݿ" for c in s)


def _kw(s):
    return dict(direction="rtl", language="ar") if has_ar(s) else {}


def inter(size, weight=700):
    f = ImageFont.truetype(os.path.join(FONTS, "Inter-var.ttf"), size)
    try:
        f.set_variation_by_axes([14, weight])
    except Exception:
        pass
    return f


def taj(size, bold=True):
    name = "Tajawal-ExtraBold.ttf" if bold else "Tajawal-Bold.ttf"
    return ImageFont.truetype(os.path.join(FONTS, name), size)


def T(d, xy, s, font, fill, anchor="la"):
    d.text(xy, s, font=font, fill=fill, anchor=anchor, **_kw(s))


def tw(d, s, font):
    b = d.textbbox((0, 0), s, font=font, **_kw(s))
    return b[2] - b[0], b[3] - b[1]


def base(glow1=BLURPLE, glow2=GREEN):
    img = Image.new("RGB", (W, H), NAVY)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([W * 0.45, -H * 0.5, W * 1.15, H * 0.6], fill=glow1 + (95,))
    gd.ellipse([-W * 0.25, H * 0.35, W * 0.45, H * 1.25], fill=glow2 + (75,))
    glow = glow.filter(ImageFilter.GaussianBlur(140))
    return Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")


def card(d, xy, fill=CARD, outline=LINE, radius=22, width=2):
    d.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def dot(d, cx, cy, r, fill):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)


# Vector icons
def icon_check(d, cx, cy, s, color):
    w = max(4, s // 7)
    d.line([(cx - s // 2, cy), (cx - s // 8, cy + s // 3),
            (cx + s // 2, cy - s // 2)], fill=color, width=w)


def icon_disk(d, cx, cy, s, color):
    """Floppy / save icon."""
    d.rounded_rectangle([cx - s // 2, cy - s // 2, cx + s // 2, cy + s // 2],
                        radius=4, outline=color, width=3, fill=None)
    # tab on top
    d.rectangle([cx - s // 3, cy - s // 2, cx + s // 3, cy - s // 6],
                outline=color, width=2)
    # slot in tab
    d.rectangle([cx - s // 5, cy - s // 2 + 4, cx - s // 12, cy - s // 4],
                fill=color)
    # label rectangle
    d.rectangle([cx - s // 2 + 6, cy + s // 12, cx + s // 2 - 6, cy + s // 2 - 6],
                fill=color)


def icon_shield(d, cx, cy, s, color):
    pts = [(cx, cy - s // 2),
           (cx + s // 2, cy - s // 4),
           (cx + s // 2, cy + s // 6),
           (cx, cy + s // 2),
           (cx - s // 2, cy + s // 6),
           (cx - s // 2, cy - s // 4)]
    d.polygon(pts, outline=color, fill=None, width=4)
    # Inner check
    icon_check(d, cx, cy, s // 2, color)


def icon_folder(d, cx, cy, s, color):
    # Tab
    d.rounded_rectangle([cx - s // 2, cy - s // 3, cx - s // 6, cy - s // 6],
                        radius=3, fill=color)
    # Body
    d.rounded_rectangle([cx - s // 2, cy - s // 6, cx + s // 2, cy + s // 2],
                        radius=6, fill=color)


def icon_users(d, cx, cy, s, color):
    # Two heads + two shoulders
    r1 = s // 6
    d.ellipse([cx - s // 3 - r1, cy - s // 3, cx - s // 3 + r1, cy - s // 3 + 2 * r1], fill=color)
    d.ellipse([cx + s // 3 - r1, cy - s // 3, cx + s // 3 + r1, cy - s // 3 + 2 * r1], fill=color)
    d.rounded_rectangle([cx - s // 2 - 6, cy, cx, cy + s // 2], radius=10, fill=color)
    d.rounded_rectangle([cx, cy, cx + s // 2 + 6, cy + s // 2], radius=10, fill=color)


def icon_image(d, cx, cy, s, color):
    d.rounded_rectangle([cx - s // 2, cy - s // 2, cx + s // 2, cy + s // 2],
                        radius=6, outline=color, width=3, fill=None)
    # Sun
    d.ellipse([cx - s // 4, cy - s // 3, cx - s // 4 + s // 7, cy - s // 3 + s // 7], fill=color)
    # Mountain
    d.polygon([(cx - s // 2 + 6, cy + s // 2 - 6),
               (cx, cy - 4),
               (cx + s // 4, cy + s // 4),
               (cx + s // 2 - 6, cy + s // 2 - 6)], fill=color)


def icon_chat(d, cx, cy, s, color):
    d.rounded_rectangle([cx - s // 2, cy - s // 2,
                         cx + s // 2, cy + s // 4],
                        radius=10, outline=color, width=3, fill=None)
    # Tail
    d.polygon([(cx - s // 6, cy + s // 4),
               (cx - s // 3, cy + s // 2),
               (cx, cy + s // 4)], outline=color, fill=color)


def icon_role(d, cx, cy, s, color):
    """Crown for roles."""
    pts = [(cx - s // 2, cy + s // 4),
           (cx - s // 2, cy - s // 6),
           (cx - s // 4, cy + s // 12),
           (cx, cy - s // 2),
           (cx + s // 4, cy + s // 12),
           (cx + s // 2, cy - s // 6),
           (cx + s // 2, cy + s // 4)]
    d.polygon(pts, fill=color)


def icon_clock(d, cx, cy, s, color):
    r = s // 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=3, fill=None)
    w = max(3, s // 12)
    # 12 o'clock hand
    d.line([(cx, cy), (cx, cy - r + 8)], fill=color, width=w)
    # 3 o'clock hand
    d.line([(cx, cy), (cx + r * 2 // 3, cy)], fill=color, width=w)


def icon_zip(d, cx, cy, s, color):
    """Box with a strap."""
    d.rounded_rectangle([cx - s // 2, cy - s // 2, cx + s // 2, cy + s // 2],
                        radius=6, fill=color)
    # Strap
    d.rectangle([cx - s // 10, cy - s // 2, cx + s // 10, cy + s // 2], fill=NAVY)
    # Knob
    d.ellipse([cx - s // 8, cy - s // 6, cx + s // 8, cy + s // 6], fill=color)


def chip(d, xy, label, font, fill_bg, fill_fg, pad_x=18, pad_y=10, radius=18,
         leading_dot=None):
    x, y = xy
    pre = 0
    if leading_dot is not None:
        pre = 22
    tw_, th_ = tw(d, label, font)
    w = tw_ + pad_x * 2 + pre
    h = th_ + pad_y * 2
    d.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=fill_bg)
    if leading_dot is not None:
        dot(d, x + pad_x + 8, y + h // 2, 6, leading_dot)
    T(d, (x + pad_x + pre, y + pad_y - 2), label, font, fill_fg)
    return w, h


def save(img, name):
    p = os.path.join(OUT, name)
    img.save(p, "PNG", optimize=True)
    print(f"  wrote {p} ({os.path.getsize(p) // 1024} KB)")


# --------------------------------------------------------------------------- #
#  1. HERO
# --------------------------------------------------------------------------- #
def hero():
    img = base(glow1=BLURPLE, glow2=GREEN)
    d = ImageDraw.Draw(img)

    # Mark — shield with check
    mx, my, ms = 90, 220, 200
    d.rounded_rectangle([mx, my, mx + ms, my + ms], radius=44, fill=BLURPLE,
                        outline=GOLD, width=4)
    icon_shield(d, mx + ms // 2, my + ms // 2, 130, WHITE)

    T(d, (340, 200), "BackUp Bot", inter(96, 900), WHITE)
    T(d, (340, 305), "save every Discord server, forever", inter(38, 600), MUTED)
    T(d, (340, 360), "احفظ سيرفرك من الحذف للأبد", taj(34), GOLD)

    chips = [
        ("messages · embeds · reactions", BLURPLE, WHITE, WHITE),
        ("members · roles · admins",      GREEN,   NAVY,  NAVY),
        ("images & attachments downloaded", GOLD, NAVY, NAVY),
        ("auto-snapshot every N hours",   PURPLE, WHITE, WHITE),
    ]
    x = 340
    for label, bg, fg, dotc in chips:
        w, h = chip(d, (x, 460), label, inter(20, 700), bg, fg,
                    pad_x=18, pad_y=11, radius=22, leading_dot=dotc)
        x += w + 10

    T(d, (W // 2, H - 38),
      "github.com/khaledq84ever/discord-backup-bot",
      inter(20, 500), DIM, anchor="mm")
    save(img, "01-hero.png")


# --------------------------------------------------------------------------- #
#  2. WHAT IT SAVES
# --------------------------------------------------------------------------- #
def saves():
    img = base(glow1=GREEN, glow2=BLURPLE)
    d = ImageDraw.Draw(img)
    T(d, (60, 50), "Everything it captures", inter(54, 900), WHITE)
    T(d, (60, 118), "your whole server on disk — searchable, exportable, yours",
      inter(22, 500), MUTED)
    T(d, (60, 154), "كل شي في السيرفر محفوظ ومحمي من الحذف",
      taj(24), GOLD)

    items = [
        (icon_folder, BLURPLE, "Channels",     "كل الرومات",       "topics · perms · category · slowmode"),
        (icon_chat,   GOLD,    "Messages",     "كل رسالة",         "content · embeds · reactions · mentions · pins"),
        (icon_image,  RED,     "Attachments",  "الصور والمرفقات",  "downloaded before CDN URLs expire"),
        (icon_users,  GREEN,   "Members",      "الأعضاء والأدمنية", "names · joins · roles · admin flag"),
        (icon_role,   PURPLE,  "Roles",        "الرولات",          "permissions bitfield · color · members"),
        (icon_clock,  PINK,    "Auto-runs",    "نسخ تلقائي",       "/schedule N — every N hours"),
    ]
    col_w, row_h, gap = 380, 120, 16
    x0, y0 = 60, 220
    for i, (ic, color, en, ar, desc) in enumerate(items):
        r, c = divmod(i, 3)
        x = x0 + c * (col_w + gap)
        y = y0 + r * (row_h + gap)
        card(d, [x, y, x + col_w, y + row_h])
        d.rounded_rectangle([x, y, x + 8, y + row_h], radius=4, fill=color)
        # Icon
        d.rounded_rectangle([x + 22, y + 24, x + 22 + 60, y + 24 + 60],
                            radius=12, fill=CARD_HI)
        ic(d, x + 22 + 30, y + 24 + 30, 32, color)
        # Text
        T(d, (x + 100, y + 18), en, inter(22, 800), WHITE)
        T(d, (x + 100, y + 48), ar, taj(18), GOLD)
        T(d, (x + 100, y + 76), desc, inter(16, 500), MUTED)
    save(img, "02-what-it-saves.png")


# --------------------------------------------------------------------------- #
#  3. COMMANDS
# --------------------------------------------------------------------------- #
def commands():
    img = base(glow1=BLURPLE, glow2=GOLD)
    d = ImageDraw.Draw(img)
    T(d, (60, 50), "Slash commands", inter(56, 900), WHITE)
    T(d, (60, 120), "7 commands — Manage Server only", inter(24, 500), MUTED)

    cmds = [
        ("/backup",          "full server snapshot",     BLURPLE),
        ("/backup_channel",  "one channel only",         GOLD),
        ("/status",          "last backup summary",      GREEN),
        ("/download",        "fetch latest .zip",        RED),
        ("/schedule",        "auto every N hours",       PURPLE),
        ("/search",          "search archived messages", PINK),
        ("/help",            "show all commands",        BLUE),
    ]
    col_w, row_h, gap = 380, 95, 14
    x0, y0 = 60, 200
    for i, (name, desc, accent) in enumerate(cmds):
        r, c = divmod(i, 3)
        x = x0 + c * (col_w + gap)
        y = y0 + r * (row_h + gap)
        card(d, [x, y, x + col_w, y + row_h])
        d.rounded_rectangle([x, y, x + 8, y + row_h], radius=4, fill=accent)
        T(d, (x + 24, y + 22), name, inter(26, 800), WHITE)
        T(d, (x + 24, y + 60), desc, inter(18, 500), MUTED)
    save(img, "03-commands.png")


# --------------------------------------------------------------------------- #
#  4. PROGRESS / STATUS EMBED MOCKUP
# --------------------------------------------------------------------------- #
def progress():
    img = base(glow1=GREEN, glow2=BLURPLE)
    d = ImageDraw.Draw(img)
    T(d, (60, 50), "Live progress", inter(56, 900), WHITE)
    T(d, (60, 120), "the embed updates every 5 s while the backup runs",
      inter(22, 500), MUTED)

    mx, my, mw, mh = 90, 190, W - 180, 400
    card(d, [mx, my, mx + mw, my + mh],
         fill=(32, 34, 47), outline=(40, 42, 60), radius=18)
    d.rounded_rectangle([mx, my, mx + 6, my + mh], radius=3, fill=GREEN)

    # Header
    icon_disk(d, mx + 56, my + 50, 38, GREEN)
    T(d, (mx + 100, my + 26), "Backup complete", inter(28, 900), WHITE)

    # Server name field
    icon_folder(d, mx + 42, my + 99, 18, BLURPLE)
    T(d, (mx + 60, my + 90), "Server", inter(16, 700), MUTED)
    T(d, (mx + 32, my + 116), "Khaled's Community", inter(24, 800), WHITE)

    # Progress bar
    T(d, (mx + 32, my + 160), "Progress", inter(16, 700), MUTED)
    bar_x, bar_y, bar_w, bar_h = mx + 32, my + 188, mw - 64, 24
    d.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
                        radius=12, fill=(40, 42, 60))
    d.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
                        radius=12, fill=GREEN)
    T(d, (mx + mw - 40, bar_y - 2), "100%", inter(16, 800), WHITE, anchor="ra")
    T(d, (mx + 32, bar_y + 36), "84 / 84 channels", inter(18, 600), MUTED)

    # Stat row
    stats = [
        ("Messages",    "152,847",  GOLD,   icon_chat),
        ("Attachments", "12,309",   RED,    icon_image),
        ("Downloaded",  "4.2 GB",   GREEN,  icon_disk),
        ("Elapsed",     "8 min 12 s", BLUE, icon_clock),
    ]
    sy = my + 280
    sx = mx + 32
    sw_ = (mw - 64 - 30) // 4
    for label, val, color, ic in stats:
        d.rounded_rectangle([sx, sy, sx + sw_, sy + 90], radius=12,
                            fill=(40, 42, 60))
        d.rounded_rectangle([sx, sy, sx + sw_, sy + 4], radius=2, fill=color)
        ic(d, sx + 24, sy + 24, 16, color)
        T(d, (sx + 44, sy + 16), label, inter(14, 700), MUTED)
        T(d, (sx + 14, sy + 42), val, inter(26, 800), WHITE)
        sx += sw_ + 10
    save(img, "04-progress.png")


# --------------------------------------------------------------------------- #
#  5. ADD TO DISCORD / DEPLOY CTA
# --------------------------------------------------------------------------- #
def cta():
    img = base(glow1=GOLD, glow2=BLURPLE)
    d = ImageDraw.Draw(img)
    T(d, (W // 2, 110), "Protect your server",
      inter(70, 900), WHITE, anchor="mm")
    T(d, (W // 2, 190),
      "self-host free on Railway · /data volume for persistence",
      inter(24, 500), MUTED, anchor="mm")
    T(d, (W // 2, 234),
      "تشغيل ذاتي مجاني · حماية كاملة لسيرفرك", taj(24), GOLD, anchor="mm")

    # Primary button
    bw_, bh_ = 460, 96
    bx = (W - bw_) // 2
    by = 310
    d.rounded_rectangle([bx, by, bx + bw_, by + bh_], radius=22, fill=BLURPLE)
    icon_shield(d, bx + 56, by + bh_ // 2, 36, WHITE)
    T(d, (bx + 110, by + bh_ // 2 - 4),
      "Add to Discord", inter(34, 800), WHITE, anchor="lm")

    # Secondary buttons
    sw_, sh_ = 220, 64
    sy = by + bh_ + 28
    pairs = [("GitHub", CARD_HI, WHITE), ("Live site", CARD_HI, GOLD)]
    total = sw_ * 2 + 24
    sx = (W - total) // 2
    for label, bg, fg in pairs:
        d.rounded_rectangle([sx, sy, sx + sw_, sy + sh_],
                            radius=18, fill=bg, outline=LINE, width=2)
        T(d, (sx + sw_ // 2, sy + sh_ // 2 - 2),
          label, inter(24, 700), fg, anchor="mm")
        sx += sw_ + 24

    T(d, (W // 2, H - 40),
      "github.com/khaledq84ever/discord-backup-bot  ·  backupbot-app.vercel.app",
      inter(18, 600), DIM, anchor="mm")
    save(img, "05-cta.png")


if __name__ == "__main__":
    print("Generating BackUp Bot promo images …")
    hero()
    saves()
    commands()
    progress()
    cta()
    print("Done. 5 images in:", OUT)
