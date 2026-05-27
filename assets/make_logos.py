"""Generate 5 polished logo icon PNGs (transparent + with backgrounds).

Outputs to assets/logos/:
  01-backupbot-512.png   — BackUp Bot square logo with shield mark
  02-musicbot-512.png    — Music Bot square logo with note mark
  03-aibot-512.png       — AI Bot square logo with brain/chat mark
  04-family-hero-1600.png — three logos in a row, banner-style
  05-family-icons-row.png — icons-only strip for headers/footers

All glyphs are vector-drawn (no emoji), with gradients + soft shadows.
"""
import os
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "fonts")
OUT = os.path.join(HERE, "logos")
os.makedirs(OUT, exist_ok=True)


# ─── palette ────────────────────────────────────────────────────────────── #
WHITE = (255, 255, 255)
NAVY = (10, 14, 39)
RED = (232, 0, 28)
GOLD = (212, 168, 67)
BLURPLE = (88, 101, 242)
GREEN = (87, 242, 135)
PURPLE = (163, 113, 247)


def inter(size: int, weight: int = 800):
    f = ImageFont.truetype(os.path.join(FONTS, "Inter-var.ttf"), size)
    try:
        f.set_variation_by_axes([14, weight])
    except Exception:
        pass
    return f


def gradient_box(size: int, c1: tuple, c2: tuple) -> Image.Image:
    """Diagonal gradient from c1 (top-left) to c2 (bottom-right)."""
    img = Image.new("RGB", (size, size), c1)
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            r = int(c1[0] * (1 - t) + c2[0] * t)
            g = int(c1[1] * (1 - t) + c2[1] * t)
            b = int(c1[2] * (1 - t) + c2[2] * t)
            px[x, y] = (r, g, b)
    return img


def rounded_mask(size: int, radius: int) -> Image.Image:
    """Alpha mask shaped like a rounded square."""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def soft_shadow(size: int, radius: int, blur: int = 22,
                color=(0, 0, 0, 90)) -> Image.Image:
    """Soft drop-shadow for a rounded-square shape."""
    pad = blur + 4
    out = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)
    d.rounded_rectangle([pad, pad + 4, pad + size, pad + size + 4],
                        radius=radius, fill=color)
    return out.filter(ImageFilter.GaussianBlur(blur))


def logo_base(size: int, c1: tuple, c2: tuple, radius: int = None,
              outline_color=GOLD, outline_w: int = 5) -> Image.Image:
    """A rounded gradient square with an outline + a thin highlight."""
    if radius is None:
        radius = size // 5
    grad = gradient_box(size, c1, c2)
    mask = rounded_mask(size, radius)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(out)
    # Outline
    d.rounded_rectangle([outline_w // 2, outline_w // 2,
                         size - outline_w // 2, size - outline_w // 2],
                        radius=radius, outline=outline_color, width=outline_w)
    # Top-edge highlight
    hl = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    hd.rounded_rectangle([outline_w, outline_w, size - outline_w, size // 4],
                         radius=radius // 2,
                         fill=(255, 255, 255, 22))
    out = Image.alpha_composite(out, hl)
    return out


# ─── glyphs (drawn on the logo face) ────────────────────────────────────── #
def glyph_shield(d, cx, cy, s, color):
    """Shield with a check inside — for BackUp Bot."""
    pts = [(cx, cy - s // 2),
           (cx + s // 2, cy - s // 3),
           (cx + s // 2, cy + s // 6),
           (cx + s // 3, cy + s // 2 - 8),
           (cx, cy + s // 2),
           (cx - s // 3, cy + s // 2 - 8),
           (cx - s // 2, cy + s // 6),
           (cx - s // 2, cy - s // 3)]
    d.polygon(pts, outline=color, fill=None, width=max(6, s // 22))
    w = max(8, s // 16)
    d.line([(cx - s // 3, cy + s // 18),
            (cx - s // 12, cy + s // 4),
            (cx + s // 3, cy - s // 4)],
           fill=color, width=w, joint="curve")


def glyph_note(d, cx, cy, s, color):
    """Eighth-note for Music Bot."""
    r = int(s * 0.18)
    hx = cx - s // 5
    hy = cy + s // 4
    d.ellipse([hx - r, hy - r * 9 // 10, hx + r, hy + r * 9 // 10], fill=color)
    sx1, sy1 = hx + r - 4, hy
    sx2, sy2 = sx1 + 10, cy - s // 2
    d.rounded_rectangle([sx1, sy2, sx2, sy1], radius=3, fill=color)
    # Flag
    d.polygon([(sx2, sy2),
               (sx2 + s // 4, sy2 + s // 10),
               (sx2 + s // 4 - 4, sy2 + s // 4),
               (sx2 + 2, sy2 + s // 7)], fill=color)


def glyph_ai(d, cx, cy, s, color):
    """Brain-spark for AI Bot."""
    r = s // 2 - 6
    # Outer circle
    d.ellipse([cx - r, cy - r, cx + r, cy + r],
              outline=color, width=max(5, s // 22))
    # Internal nodes (graph)
    nodes = [(-r // 2, -r // 3),
             (r // 2, -r // 3),
             (0, 0),
             (-r // 2, r // 3),
             (r // 2, r // 3)]
    for nx, ny in nodes:
        d.ellipse([cx + nx - 8, cy + ny - 8, cx + nx + 8, cy + ny + 8],
                  fill=color)
    w = max(3, s // 50)
    # Connections
    for i, (nx, ny) in enumerate(nodes):
        if i == 2:
            continue
        d.line([(cx + nx, cy + ny), (cx, cy)], fill=color, width=w)
    # Spark at top-right
    sx, sy = cx + r * 3 // 4, cy - r * 3 // 4
    d.polygon([(sx, sy - 10), (sx + 6, sy), (sx, sy + 10), (sx - 6, sy)],
              fill=color)


# ─── 1-3: Per-bot square logos ──────────────────────────────────────────── #
def backupbot_logo(size: int = 512) -> Image.Image:
    base = logo_base(size, (88, 101, 242), (72, 88, 220), radius=size // 5)
    d = ImageDraw.Draw(base)
    glyph_shield(d, size // 2, size // 2, int(size * 0.6), WHITE)
    return base


def musicbot_logo(size: int = 512) -> Image.Image:
    base = logo_base(size, (255, 30, 60), (180, 0, 24), radius=size // 5)
    d = ImageDraw.Draw(base)
    glyph_note(d, size // 2, size // 2, int(size * 0.6), WHITE)
    return base


def aibot_logo(size: int = 512) -> Image.Image:
    base = logo_base(size, (88, 101, 242), (163, 113, 247), radius=size // 5,
                     outline_color=GOLD)
    d = ImageDraw.Draw(base)
    glyph_ai(d, size // 2, size // 2, int(size * 0.65), WHITE)
    return base


def _save(img: Image.Image, name: str, *, transparent: bool = True):
    p = os.path.join(OUT, name)
    if transparent:
        img.save(p, "PNG", optimize=True)
    else:
        # Composite onto white if we want a non-transparent PNG.
        bg = Image.new("RGB", img.size, WHITE)
        bg.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
        bg.save(p, "PNG", optimize=True)
    print(f"  wrote {p} ({os.path.getsize(p) // 1024} KB)")


# ─── 4. Family hero banner ──────────────────────────────────────────────── #
def family_hero():
    W, H = 1600, 700
    img = Image.new("RGB", (W, H), NAVY)
    # soft gradient bg
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([W * 0.35, -H * 0.4, W * 1.05, H * 0.5],
               fill=BLURPLE + (110,))
    gd.ellipse([-W * 0.15, H * 0.4, W * 0.55, H * 1.25],
               fill=RED + (90,))
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGBA")

    d = ImageDraw.Draw(img)
    # Header text
    f_title = inter(80, 900)
    f_sub = inter(28, 600)
    d.text((W // 2, 80), "Discord Bot Family",
           font=f_title, fill=WHITE, anchor="mm")
    d.text((W // 2, 138),
           "three bots, one workspace · AI Chat · Music · Backup",
           font=f_sub, fill=(154, 166, 212), anchor="mm")

    # Three logo cards
    card_size = 280
    gap = 70
    total = 3 * card_size + 2 * gap
    start_x = (W - total) // 2
    cy = 360

    bots = [
        ("AI Bot",     aibot_logo(card_size),     "/ask · /imagine · /model"),
        ("Music Bot",  musicbot_logo(card_size),  "/play · 50-track playlists"),
        ("BackUp Bot", backupbot_logo(card_size), "/backup · /schedule"),
    ]
    f_name = inter(32, 900)
    f_desc = inter(20, 600)
    for i, (name, logo, desc) in enumerate(bots):
        x = start_x + i * (card_size + gap)
        # Shadow
        sh = soft_shadow(card_size, card_size // 5, blur=20)
        img.paste(sh, (x - 26, cy - 26), sh)
        img.paste(logo, (x, cy), logo)
        d.text((x + card_size // 2, cy + card_size + 50),
               name, font=f_name, fill=WHITE, anchor="mm")
        d.text((x + card_size // 2, cy + card_size + 90),
               desc, font=f_desc, fill=(154, 166, 212), anchor="mm")

    img.convert("RGB").save(os.path.join(OUT, "04-family-hero-1600.png"),
                             "PNG", optimize=True)
    print(f"  wrote {OUT}/04-family-hero-1600.png "
          f"({os.path.getsize(os.path.join(OUT, '04-family-hero-1600.png')) // 1024} KB)")


# ─── 5. Family icons row (tiny banner) ──────────────────────────────────── #
def family_icons_row():
    W, H = 1280, 320
    img = Image.new("RGB", (W, H), NAVY)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([W * 0.6, -H, W * 1.3, H], fill=BLURPLE + (90,))
    glow = glow.filter(ImageFilter.GaussianBlur(110))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGBA")
    d = ImageDraw.Draw(img)

    size = 180
    gap = 60
    total = 3 * size + 2 * gap
    start_x = (W - total) // 2
    cy = (H - size) // 2

    logos = [aibot_logo(size), musicbot_logo(size), backupbot_logo(size)]
    for i, logo in enumerate(logos):
        x = start_x + i * (size + gap)
        sh = soft_shadow(size, size // 5, blur=14)
        img.paste(sh, (x - 18, cy - 18), sh)
        img.paste(logo, (x, cy), logo)
        # Tiny connector lines between logos
        if i < 2:
            cx2 = x + size + gap // 2
            d.line([(x + size + 12, cy + size // 2),
                    (x + size + gap - 12, cy + size // 2)],
                   fill=(154, 166, 212), width=3)
            d.ellipse([cx2 - 6, cy + size // 2 - 6,
                       cx2 + 6, cy + size // 2 + 6],
                      fill=GOLD)

    img.convert("RGB").save(os.path.join(OUT, "05-family-icons-row.png"),
                             "PNG", optimize=True)
    print(f"  wrote {OUT}/05-family-icons-row.png "
          f"({os.path.getsize(os.path.join(OUT, '05-family-icons-row.png')) // 1024} KB)")


if __name__ == "__main__":
    print("Generating 5 logo icon PNGs …")
    _save(backupbot_logo(512), "01-backupbot-512.png")
    _save(musicbot_logo(512),  "02-musicbot-512.png")
    _save(aibot_logo(512),     "03-aibot-512.png")
    family_hero()
    family_icons_row()
    print("Done. 5 logos in:", OUT)
