#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
]
TARGET_SIZE = 1400


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
    raise RuntimeError("未找到可用的中文字体")


def center_crop_square(image: Image.Image) -> Image.Image:
    width, height = image.size
    edge = min(width, height)
    left = (width - edge) // 2
    top = (height - edge) // 2
    cropped = image.crop((left, top, left + edge, top + edge))
    if cropped.size != (TARGET_SIZE, TARGET_SIZE):
        cropped = cropped.resize((TARGET_SIZE, TARGET_SIZE), Image.Resampling.LANCZOS)
    return cropped


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []

    preferred_chunks = re.findall(r"[^：，、；,.!?]+[：，、；,.!?]?", stripped)
    if preferred_chunks:
        lines: list[str] = []
        current = ""
        chunk_fits = True
        for chunk in preferred_chunks:
            candidate = f"{current}{chunk}"
            bbox = draw.textbbox((0, 0), candidate, font=font, spacing=8)
            if current and bbox[2] > max_width:
                lines.append(current.strip())
                current = chunk
            else:
                current = candidate

            if draw.textbbox((0, 0), current, font=font, spacing=8)[2] > max_width:
                chunk_fits = False
                break
        if current.strip():
            lines.append(current.strip())
        if chunk_fits:
            return lines

    lines: list[str] = []
    current = ""
    for ch in stripped:
        candidate = f"{current}{ch}"
        bbox = draw.textbbox((0, 0), candidate, font=font, spacing=8)
        if current and bbox[2] > max_width:
            lines.append(current.strip())
            current = ch
        else:
            current = candidate
    if current.strip():
        lines.append(current.strip())
    return lines


def choose_layout(image: Image.Image, title: str) -> tuple[ImageFont.FreeTypeFont, list[str], int, int]:
    draw = ImageDraw.Draw(image)
    width, height = image.size
    max_width = width - 180

    for font_size in range(104, 55, -4):
        font = load_font(font_size)
        lines = wrap_text(draw, title, font, max_width)
        if not lines or len(lines) > 4:
            continue
        tail = lines[-1].strip(" ：，、；,.")
        if len(lines) > 1 and len(tail) < 3:
            continue
        line_height = int(font_size * 1.22)
        block_height = line_height * len(lines)
        if block_height <= int(height * 0.3):
            return font, lines, line_height, max_width

    fallback_font = load_font(56)
    fallback_lines = wrap_text(draw, title, fallback_font, max_width)[:4]
    return fallback_font, fallback_lines, int(56 * 1.22), max_width


def draw_bottom_gradient(base: Image.Image) -> Image.Image:
    width, height = base.size
    gradient = Image.new("L", (1, height), 0)
    for y in range(height):
        ratio = max(0.0, min(1.0, (y - height * 0.42) / (height * 0.58)))
        alpha = int(220 * (ratio ** 1.8))
        gradient.putpixel((0, y), alpha)
    alpha_mask = gradient.resize((width, height))

    overlay = Image.new("RGBA", (width, height), (4, 10, 18, 0))
    overlay.putalpha(alpha_mask)
    return Image.alpha_composite(base.convert("RGBA"), overlay)


def render_cover(background_path: Path, output_path: Path, title: str) -> None:
    base = Image.open(background_path).convert("RGB")
    base = center_crop_square(base)
    base = draw_bottom_gradient(base)

    width, height = base.size
    font, lines, line_height, _ = choose_layout(base, title)
    if not lines:
        raise RuntimeError("封面标题为空，无法叠字")

    draw = ImageDraw.Draw(base)
    left = 96
    bottom = height - 120
    top = bottom - line_height * len(lines)

    line_widths = [draw.textbbox((0, 0), line, font=font, spacing=8)[2] for line in lines]
    panel_left = left - 34
    panel_top = top - 54
    panel_right = min(width - 72, left + max(line_widths) + 44)
    panel_bottom = bottom + 38

    panel = Image.new("RGBA", base.size, (0, 0, 0, 0))
    panel_draw = ImageDraw.Draw(panel)
    panel_draw.rounded_rectangle(
        (panel_left, panel_top, panel_right, panel_bottom),
        radius=34,
        fill=(6, 12, 22, 138),
        outline=(116, 226, 255, 46),
        width=2,
    )
    panel_draw.rounded_rectangle(
        (panel_left + 28, panel_top + 22, panel_left + 186, panel_top + 30),
        radius=4,
        fill=(103, 226, 255, 210),
    )
    panel = panel.filter(ImageFilter.GaussianBlur(radius=0.3))
    base = Image.alpha_composite(base, panel)
    draw = ImageDraw.Draw(base)

    y = top
    for line in lines:
        draw.text((left + 2, y + 4), line, font=font, fill=(0, 0, 0, 120))
        draw.text((left, y), line, font=font, fill=(245, 249, 255, 248))
        y += line_height

    final_image = base.convert("RGB")
    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        final_image.save(output_path, quality=94, subsampling=0)
    else:
        final_image.save(output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Overlay exact Chinese title text onto a generated cover background")
    parser.add_argument("background")
    parser.add_argument("output")
    parser.add_argument("title")
    args = parser.parse_args()

    render_cover(Path(args.background), Path(args.output), args.title)


if __name__ == "__main__":
    main()
