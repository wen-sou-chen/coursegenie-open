"""生成专家头像：512x512 PNG，扁平插画风（书 + 灯泡），输出到 packaging/expert-coursegenie/avatars/expert.png。"""

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512
TOP = (18, 38, 58)
BOTTOM = (31, 122, 140)
OUT = Path(__file__).resolve().parents[1] / "packaging" / "expert-coursegenie" / "avatars" / "expert.png"


def lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def main() -> None:
    image = Image.new("RGB", (SIZE, SIZE), TOP)
    draw = ImageDraw.Draw(image)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        draw.line([(0, y), (SIZE, y)], fill=tuple(lerp(a, b, t) for a, b in zip(TOP, BOTTOM)))

    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    for radius, alpha in ((150, 26), (112, 34), (78, 44)):
        gdraw.ellipse([256 - radius, 176 - radius, 256 + radius, 176 + radius], fill=(255, 214, 102, alpha))
    image = Image.alpha_composite(image.convert("RGBA"), glow)
    draw = ImageDraw.Draw(image)

    for x, y, r in ((96, 104, 5), (420, 92, 4), (438, 300, 3), (74, 300, 4)):
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, 90))

    bulb_center = (256, 172)
    draw.ellipse([bulb_center[0] - 52, bulb_center[1] - 52, bulb_center[0] + 52, bulb_center[1] + 52], fill=(255, 214, 102))
    draw.ellipse([bulb_center[0] - 38, bulb_center[1] - 40, bulb_center[0] + 30, bulb_center[1] + 22], fill=(255, 236, 179))
    draw.rounded_rectangle([232, 218, 280, 242], radius=8, fill=(214, 160, 44))
    for x in (244, 256, 268):
        draw.line([(x, 244), (x, 258)], fill=(214, 160, 44), width=6)

    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.ellipse([116, 404, 400, 444], fill=(0, 0, 0, 70))
    image = Image.alpha_composite(image, shadow)
    draw = ImageDraw.Draw(image)

    left_page = [(112, 300), (252, 286), (252, 414), (112, 436)]
    right_page = [(260, 286), (400, 300), (400, 436), (260, 414)]
    draw.polygon(left_page, fill=(250, 250, 247))
    draw.polygon(right_page, fill=(255, 255, 255))
    draw.line([(112, 300), (252, 286)], fill=(226, 232, 240), width=3)
    draw.line([(260, 286), (400, 300)], fill=(226, 232, 240), width=3)
    draw.rounded_rectangle([248, 286, 264, 416], radius=6, fill=(41, 96, 120))

    for offset in (0, 34, 68):
        draw.line([(136, 330 + offset), (226, 322 + offset)], fill=(45, 138, 158), width=9)
        draw.line([(286, 322 + offset), (376, 330 + offset)], fill=(45, 138, 158), width=9)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(OUT, "PNG", optimize=True)
    print(f"{OUT} {OUT.stat().st_size} B")


if __name__ == "__main__":
    main()