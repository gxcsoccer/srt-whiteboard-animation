import json
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from annotation_validation import ordered_elements, validate_annotation


FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)


def load_fonts() -> tuple[ImageFont.ImageFont, ImageFont.ImageFont]:
    """跨平台加载中文字体；找不到时退回 Pillow 内置字体而不是崩溃。"""

    candidates = list(FONT_CANDIDATES)
    override = os.environ.get("SRT_WB_FONT")
    if override:
        candidates.insert(0, override)
    for font_file in candidates:
        try:
            return ImageFont.truetype(font_file, 28), ImageFont.truetype(font_file, 18)
        except OSError:
            continue
    print(
        "[warn] 未找到可用的中文字体，标签回退为 Pillow 内置字体；"
        "可用 SRT_WB_FONT=<字体路径> 指定。",
        file=sys.stderr,
    )
    return ImageFont.load_default(), ImageFont.load_default()


def main(image_path: str, annotation_path: str, output_path: str) -> None:
    image = Image.open(image_path).convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    _, small_font = load_fonts()
    colors = [(38, 103, 255, 225), (255, 105, 92, 225), (41, 167, 102, 225), (181, 100, 255, 225)]

    data = json.loads(Path(annotation_path).read_text(encoding="utf-8"))
    validate_annotation(data, image_size=image.size)
    for index, element in enumerate(ordered_elements(data["elements"]), start=1):
        region = element["region"]
        x, y = region["x"], region["y"]
        right, bottom = x + region["width"], y + region["height"]
        color = colors[(index - 1) % len(colors)]
        fill = (*color[:3], 24)
        draw.rounded_rectangle((x, y, right, bottom), radius=12, outline=color, width=4, fill=fill)
        draw.ellipse((x + 8, y + 8, x + 44, y + 44), fill=color)
        draw.text((x + 19, y + 8), str(index), anchor="ma", font=small_font, fill="white")
        label = f"{index}. {element['label']}  {element['reveal']['direction']}"
        draw.rounded_rectangle((x + 52, y + 8, min(right - 8, x + 52 + len(label) * 19), y + 46), radius=6, fill=(255, 255, 255, 225))
        draw.text((x + 60, y + 12), label, font=small_font, fill=color)
        start = tuple(element["handPath"]["start"])
        end = tuple(element["handPath"]["end"])
        draw.line((start, end), fill=color, width=4)
        draw.polygon((end, (end[0] - 13, end[1] - 7), (end[0] - 13, end[1] + 7)), fill=color)

    result = Image.alpha_composite(image, overlay).convert("RGB")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    result.save(output_path, quality=95)


if __name__ == "__main__":
    main(*sys.argv[1:4])
