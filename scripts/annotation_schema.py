#!/usr/bin/env python3
"""
标注校验（annotation.json）

渲染前把标注里会「崩渲染」或「静默画错」的问题一次列清：
  - canvas 缺失、非法，或与原图长宽比不符（区域会整体错位）
  - region 缺字段/非数值/零面积/越出画布（越界部分过去只会被静默裁掉）
  - reveal.startMs、reveal.durationMs 缺失或非法（缺失会直接抛 KeyError）
  - protectedRegions 结构错误

另有不阻断渲染的提醒（warnings）：sequence 与 startMs 顺序不一致、
区域时间窗重叠、sceneDurationMs 短于最后一个区域的结束时间。

独立使用：
  python annotation_schema.py <标注.json> [图片]
有 error 返回 1；只有 warning 返回 0。
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 渲染器实际读取的字段：缺了就必须报错，而不是让它在渲染中途抛 KeyError
REQUIRED_REVEAL_KEYS = ("startMs", "durationMs")


class AnnotationError(ValueError):
    """标注校验失败。message 里已含逐条中文原因。"""


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_number(value: object) -> bool:
    """数值且有限；bool 不算数值（避免 True 被当成 1 用作坐标）。"""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _check_rect(
    rect: object, where: str, canvas_w: int, canvas_h: int, report: Report
) -> None:
    """校验一个矩形（region 或 protectedRegions 里的一项）。"""
    if not isinstance(rect, dict):
        report.errors.append(f"{where}: 必须是对象，实际是 {type(rect).__name__}")
        return

    missing = [k for k in ("x", "y", "width", "height") if k not in rect]
    if missing:
        report.errors.append(f"{where}: 缺少字段 {', '.join(missing)}")
        return
    bad = [k for k in ("x", "y", "width", "height") if not _is_number(rect[k])]
    if bad:
        report.errors.append(
            f"{where}: {', '.join(bad)} 必须是数值，实际是 "
            + ", ".join(f"{k}={rect[k]!r}" for k in bad)
        )
        return

    x, y, w, h = rect["x"], rect["y"], rect["width"], rect["height"]
    if w < 1 or h < 1:
        report.errors.append(f"{where}: 宽高必须 ≥ 1，实际 width={w}, height={h}")
        return

    if x < 0 or y < 0 or x + w > canvas_w or y + h > canvas_h:
        report.errors.append(
            f"{where}: 区域 ({x}, {y}, {w}x{h}) 超出画布 {canvas_w}x{canvas_h}"
            f"（越界部分会被静默裁掉，请改成画布内的坐标）"
        )
    if any(float(v) != int(v) for v in (x, y, w, h)):
        report.warnings.append(
            f"{where}: 坐标不是整数像素 ({x}, {y}, {w}x{h})，渲染时会四舍五入"
        )


def validate_annotation(
    annotation: object, image_size: tuple[int, int] | None = None
) -> Report:
    """
    校验标注结构。image_size 为原图 (宽, 高)；给了就顺带核对 canvas。
    返回 Report（errors 非空即不可渲染）。
    """
    report = Report()
    if not isinstance(annotation, dict):
        report.errors.append(f"标注根节点必须是对象，实际是 {type(annotation).__name__}")
        return report

    # ── canvas ──
    canvas = annotation.get("canvas")
    canvas_w = canvas_h = None
    if not isinstance(canvas, dict):
        report.errors.append("canvas: 缺失或不是对象，必须写成 {\"width\": W, \"height\": H}")
    else:
        for key in ("width", "height"):
            value = canvas.get(key)
            if not _is_number(value) or value < 1:
                report.errors.append(f"canvas.{key}: 必须是 ≥ 1 的数值，实际是 {value!r}")
        if _is_number(canvas.get("width")) and _is_number(canvas.get("height")):
            canvas_w, canvas_h = int(canvas["width"]), int(canvas["height"])

    if canvas_w and canvas_h and image_size is not None:
        img_w, img_h = image_size
        if (canvas_w, canvas_h) != (img_w, img_h):
            ratio_canvas = canvas_w / canvas_h
            ratio_image = img_w / img_h
            if abs(ratio_canvas - ratio_image) / ratio_image > 0.02:
                report.errors.append(
                    f"canvas {canvas_w}x{canvas_h} 与原图 {img_w}x{img_h} 长宽比不一致"
                    f"（差 >2%），所有区域都会整体错位；请把 canvas 改成原图像素尺寸"
                )
            else:
                report.warnings.append(
                    f"canvas {canvas_w}x{canvas_h} 与原图 {img_w}x{img_h} 不一致"
                    f"（比例接近，区域会按比例缩放）；建议改成原图像素尺寸"
                )

    # ── sceneDurationMs（可选）──
    if "sceneDurationMs" in annotation:
        scene_ms = annotation["sceneDurationMs"]
        if not _is_number(scene_ms) or scene_ms <= 0:
            report.errors.append(
                f"sceneDurationMs: 必须是正数值，实际是 {scene_ms!r}"
            )

    # ── elements ──
    elements = annotation.get("elements")
    if not isinstance(elements, list) or not elements:
        report.errors.append("elements: 缺失或为空，至少需要一个绘制区域")
        return report

    # canvas 不可用时不再做几何校验，避免刷出一堆无意义的越界报错
    geometry_canvas = (canvas_w, canvas_h) if canvas_w and canvas_h else None
    timeline: list[tuple[float, float, str]] = []
    sequences: list[tuple[float, str]] = []

    for index, element in enumerate(elements):
        name = f"elements[{index}]"
        if not isinstance(element, dict):
            report.errors.append(f"{name}: 必须是对象，实际是 {type(element).__name__}")
            continue
        label = element.get("id") or element.get("label")
        if label:
            name = f"elements[{index}]({label})"

        if "region" not in element:
            report.errors.append(f"{name}: 缺少 region")
        elif geometry_canvas:
            _check_rect(element["region"], f"{name}.region", *geometry_canvas, report)

        reveal = element.get("reveal")
        if not isinstance(reveal, dict):
            report.errors.append(
                f"{name}.reveal: 缺失或不是对象，必须含 startMs 与 durationMs"
            )
            continue

        missing = [k for k in REQUIRED_REVEAL_KEYS if k not in reveal]
        if missing:
            report.errors.append(f"{name}.reveal: 缺少 {', '.join(missing)}")
        start_ms, dur_ms = reveal.get("startMs"), reveal.get("durationMs")
        if "startMs" in reveal and (not _is_number(start_ms) or start_ms < 0):
            report.errors.append(
                f"{name}.reveal.startMs: 必须是 ≥ 0 的数值，实际是 {start_ms!r}"
            )
        if "durationMs" in reveal and (not _is_number(dur_ms) or dur_ms <= 0):
            report.errors.append(
                f"{name}.reveal.durationMs: 必须是正数值，实际是 {dur_ms!r}"
            )
        if _is_number(start_ms) and _is_number(dur_ms) and start_ms >= 0 and dur_ms > 0:
            timeline.append((float(start_ms), float(start_ms + dur_ms), name))

        protected = reveal.get("protectedRegions", [])
        if not isinstance(protected, list):
            report.errors.append(
                f"{name}.reveal.protectedRegions: 必须是数组，实际是 {type(protected).__name__}"
            )
        elif geometry_canvas:
            for pindex, prot in enumerate(protected):
                _check_rect(
                    prot,
                    f"{name}.reveal.protectedRegions[{pindex}]",
                    *geometry_canvas,
                    report,
                )

        sequence = element.get("sequence")
        if sequence is not None and not _is_number(sequence):
            report.warnings.append(
                f"{name}.sequence: 建议是数字，实际是 {sequence!r}"
            )
        elif _is_number(sequence) and _is_number(start_ms):
            sequences.append((float(sequence), name))

    # ── 提醒：渲染顺序以 startMs 为准，sequence 只是标注序号 ──
    if len(sequences) == len(timeline) and len(timeline) > 1:
        by_sequence = [name for _, name in sorted(sequences)]
        by_start = [name for _, _, name in sorted(timeline)]
        if by_sequence != by_start:
            report.warnings.append(
                "sequence 顺序与 startMs 顺序不一致：渲染器按 startMs 排序，"
                "sequence 不影响成片；请把时间轴改成与 sequence 相同的顺序"
            )

    # ── 提醒：时间窗重叠（stream 画法是一支笔，重叠会让总时长超出预期）──
    ordered = sorted(timeline)
    for (_, prev_end, prev_name), (next_start, _, next_name) in zip(ordered, ordered[1:]):
        if next_start < prev_end:
            report.warnings.append(
                f"{prev_name} 与 {next_name} 时间窗重叠（{next_start:.0f}ms < {prev_end:.0f}ms）："
                "区域仍会串行绘制，成片会比 sceneDurationMs 更长"
            )

    if timeline and _is_number(annotation.get("sceneDurationMs")):
        last_end = max(end for _, end, _ in timeline)
        if annotation["sceneDurationMs"] < last_end:
            report.warnings.append(
                f"sceneDurationMs={annotation['sceneDurationMs']}ms 短于最后一个区域的结束时间 "
                f"{last_end:.0f}ms：成片会自动延长到 {last_end:.0f}ms + 0.5s 凝视"
            )

    return report


def print_report(report: Report, source: str = "") -> None:
    """按仓库既有的 [err] / [warn] 前缀打印校验结果。"""
    where = f" ({source})" if source else ""
    for message in report.warnings:
        print(f"  [warn] 标注{where}: {message}")
    for message in report.errors:
        print(f"  [err] 标注{where}: {message}")


def ensure_valid(
    annotation: object,
    image_size: tuple[int, int] | None = None,
    source: str = "",
) -> Report:
    """校验并在有 error 时抛 AnnotationError（message 含逐条原因）。"""
    report = validate_annotation(annotation, image_size)
    if not report.ok:
        head = f"标注校验失败（{source}）：" if source else "标注校验失败："
        raise AnnotationError(head + "\n  - " + "\n  - ".join(report.errors))
    return report


def load_annotation(path: str | Path) -> dict:
    """读标注 JSON；文件缺失或 JSON 语法错都抛 AnnotationError。"""
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AnnotationError(f"无法读取标注 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AnnotationError(f"标注 {path} 不是合法 JSON: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("用法: python annotation_schema.py <标注.json> [图片]", file=sys.stderr)
        return 2

    try:
        annotation = load_annotation(argv[0])
    except AnnotationError as exc:
        print(f"[err] {exc}", file=sys.stderr)
        return 1

    image_size = None
    if len(argv) > 1:
        try:
            from PIL import Image  # 仅在需要核对原图尺寸时才依赖 Pillow

            with Image.open(argv[1]) as img:
                image_size = img.size
        except ImportError:
            print("  [warn] 未安装 Pillow，跳过 canvas 与原图尺寸的核对")
        except OSError as exc:
            print(f"[err] 无法读取图片 {argv[1]}: {exc}", file=sys.stderr)
            return 1

    report = validate_annotation(annotation, image_size)
    print_report(report, Path(argv[0]).name)
    if not report.ok:
        print(f"\n校验未通过：{len(report.errors)} 个错误，{len(report.warnings)} 个提醒")
        return 1
    print(f"\n校验通过：{len(report.warnings)} 个提醒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
