from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "app" / "static" / "tutorial-guide.png"
WIDTH = 1200
HEIGHT = 1600


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    filename = "segoeuib.ttf" if bold else "segoeui.ttf"
    candidates = [
        Path("C:/Windows/Fonts") / filename,
        Path("/usr/share/fonts/truetype/dejavu")
        / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default(size=size)


def rounded_gradient() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT))
    pixels = image.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            left_glow = max(0.0, 1.0 - ((x - 70) ** 2 + (y - 1250) ** 2) ** 0.5 / 760)
            right_glow = max(0.0, 1.0 - ((x - 1130) ** 2 + (y - 190) ** 2) ** 0.5 / 720)
            red = int(10 + 32 * left_glow + 22 * right_glow)
            green = int(9 + 10 * left_glow + 5 * right_glow)
            blue = int(20 + 78 * left_glow + 78 * right_glow)
            pixels[x, y] = (red, green, blue)
    return image


def wrap(draw: ImageDraw.ImageDraw, text: str, text_font: ImageFont.ImageFont, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=text_font) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def text_block(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    text_font: ImageFont.ImageFont,
    fill: str,
    width: int,
    spacing: int = 10,
) -> int:
    x, y = xy
    line_height = text_font.size + spacing
    for line in wrap(draw, text, text_font, width):
        draw.text((x, y), line, font=text_font, fill=fill)
        y += line_height
    return y


def pill(draw: ImageDraw.ImageDraw, xy: tuple[int, int], label: str, color: str) -> None:
    x, y = xy
    draw.rounded_rectangle((x, y, x + 116, y + 44), radius=22, fill=color)
    draw.text((x + 58, y + 22), label, font=font(22, True), fill="#ffffff", anchor="mm")


def step_card(
    draw: ImageDraw.ImageDraw,
    top: int,
    number: str,
    title: str,
    items: list[str],
    accent: str,
) -> None:
    left, right = 68, WIDTH - 68
    bottom = top + 246
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=34,
        fill="#171625",
        outline="#35324d",
        width=2,
    )
    draw.rounded_rectangle((left, top, left + 12, bottom), radius=6, fill=accent)
    draw.text((left + 42, top + 34), number, font=font(56, True), fill=accent)
    draw.text((left + 154, top + 42), title, font=font(34, True), fill="#ffffff")
    y = top + 104
    for item in items:
        draw.ellipse((left + 158, y + 10, left + 172, y + 24), fill=accent)
        y = text_block(
            draw,
            (left + 194, y),
            item,
            font(26),
            "#c8c5d4",
            right - left - 242,
            spacing=8,
        )
        y += 13


def device_card(
    draw: ImageDraw.ImageDraw,
    x: int,
    title: str,
    app: str,
    path: str,
    accent: str,
    icon: str,
) -> None:
    y, width, height = 1040, 336, 370
    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=30,
        fill="#171625",
        outline="#35324d",
        width=2,
    )
    draw.ellipse((x + 26, y + 26, x + 94, y + 94), fill=accent)
    draw.text(
        (x + 60, y + 60),
        icon,
        font=font(32, True),
        fill="#ffffff",
        anchor="mm",
    )
    draw.text((x + 26, y + 118), title, font=font(29, True), fill="#ffffff")
    pill(draw, (x + 26, y + 166), app, accent)
    text_block(
        draw,
        (x + 26, y + 232),
        path,
        font(23),
        "#bbb7ca",
        width - 52,
        spacing=8,
    )


def main() -> None:
    image = rounded_gradient()

    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((820, -220, 1420, 380), fill=(91, 57, 255, 115))
    glow_draw.ellipse((-260, 1080, 360, 1710), fill=(56, 28, 180, 95))
    glow = glow.filter(ImageFilter.GaussianBlur(110))
    image = Image.alpha_composite(image.convert("RGBA"), glow)
    draw = ImageDraw.Draw(image)

    # Original geometric mark: a violet diamond with a play symbol.
    draw.polygon([(72, 82), (116, 38), (160, 82), (116, 126)], fill="#6757ff")
    draw.polygon([(104, 63), (104, 101), (135, 82)], fill="#ffffff")
    draw.text((184, 50), "TELEGRAM STREMIO", font=font(29, True), fill="#dedbea")
    draw.text((68, 172), "SET UP. SEND. STREAM.", font=font(58, True), fill="#ffffff")
    draw.text(
        (70, 252),
        "Your private Telegram-to-Google Drive workflow",
        font=font(27),
        fill="#aaa6bb",
    )

    step_card(
        draw,
        330,
        "01",
        "CONNECT",
        [
            "Send /connect to the bot.",
            "Connect Telegram with phone code or QR, then add Google Drive.",
        ],
        "#785cff",
    )
    step_card(
        draw,
        606,
        "02",
        "SEND A FILE",
        [
            "Optional: select a Drive with /remote and a folder with /dir.",
            "Send or forward an authorized file. Wait for Upload completed.",
        ],
        "#00a8ff",
    )

    draw.text((68, 920), "03  WATCH FROM GOOGLE DRIVE", font=font(37, True), fill="#ffffff")
    draw.line((68, 982, WIDTH - 68, 982), fill="#3e3a5a", width=2)

    device_card(
        draw,
        68,
        "iPhone / iPad",
        "VLC",
        "Network > Cloud Services > Google Drive > Play",
        "#8b5cf6",
        "iOS",
    )
    device_card(
        draw,
        432,
        "Android",
        "RS + VLC",
        "RS File Manager > Google Drive > Open with VLC",
        "#008cff",
        "A",
    )
    device_card(
        draw,
        796,
        "Android TV",
        "RS + VLC",
        "RS File Manager > Google Drive > Open with VLC",
        "#00b6aa",
        "TV",
    )

    draw.rounded_rectangle(
        (68, 1452, WIDTH - 68, 1532),
        radius=24,
        fill="#242136",
    )
    draw.text(
        (WIDTH // 2, 1483),
        "Use only files you own or are authorized to access.",
        font=font(23, True),
        fill="#d8d5e4",
        anchor="mm",
    )
    draw.text(
        (WIDTH // 2, 1514),
        "Need a check? Send /status",
        font=font(20),
        fill="#9d98b2",
        anchor="mm",
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(OUTPUT, "PNG", optimize=True)
    print(f"Generated {OUTPUT} ({WIDTH}x{HEIGHT})")


if __name__ == "__main__":
    main()
