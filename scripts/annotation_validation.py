#!/usr/bin/env python3
"""annotation.json 的排序与轻量校验。

这里不依赖第三方 JSON Schema 库，确保渲染 CLI 在真正分配视频资源前就能给出
可读错误。仓库根目录的 ``annotation.schema.json`` 仍可供编辑器和外部工具使用。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any


VALID_DIRECTIONS = {
    "top_to_bottom",
    "bottom_to_top",
    "left_to_right",
    "right_to_left",
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def ordered_elements(elements: Sequence[dict]) -> list[dict]:
    """按显式 sequence 排序；旧标注缺 sequence 时回退到 startMs 和原始顺序。"""

    indexed = list(enumerate(elements))

    def key(item: tuple[int, dict]) -> tuple[int, int, int, int]:
        index, element = item
        if not isinstance(element, dict):
            return (2, index, index, index)
        sequence = element.get("sequence")
        reveal = element.get("reveal")
        start_ms = reveal.get("startMs", 0) if isinstance(reveal, dict) else 0
        if _is_int(sequence) and sequence > 0:
            return (0, sequence, start_ms if _is_int(start_ms) else 0, index)
        return (1, start_ms if _is_int(start_ms) else 0, index, index)

    return [element for _, element in sorted(indexed, key=key)]


def _check_rect(
    rect: Any,
    path: str,
    canvas_width: int,
    canvas_height: int,
    errors: list[str],
) -> None:
    if not isinstance(rect, dict):
        errors.append(f"{path} 必须是对象")
        return
    values: dict[str, int] = {}
    for field in ("x", "y", "width", "height"):
        value = rect.get(field)
        if not _is_int(value):
            errors.append(f"{path}.{field} 必须是整数")
            continue
        values[field] = value
    if len(values) != 4:
        return
    if values["x"] < 0 or values["y"] < 0:
        errors.append(f"{path} 的 x/y 不能为负数")
    if values["width"] <= 0 or values["height"] <= 0:
        errors.append(f"{path} 的 width/height 必须大于 0")
    if values["x"] + values["width"] > canvas_width:
        errors.append(f"{path} 超出画布右边界")
    if values["y"] + values["height"] > canvas_height:
        errors.append(f"{path} 超出画布下边界")


def _check_point(point: Any, path: str, errors: list[str]) -> None:
    if not isinstance(point, list) or len(point) != 2 or not all(_is_int(v) for v in point):
        errors.append(f"{path} 必须是包含两个整数的数组")


def annotation_errors(data: Any, image_size: tuple[int, int] | None = None) -> list[str]:
    """返回全部可发现的标注错误；``image_size`` 为 (width, height)。"""

    errors: list[str] = []
    if not isinstance(data, dict):
        return ["标注根节点必须是对象"]

    canvas = data.get("canvas")
    if not isinstance(canvas, dict):
        return ["canvas 必须是对象"]
    canvas_width = canvas.get("width")
    canvas_height = canvas.get("height")
    if not _is_int(canvas_width) or canvas_width <= 0:
        errors.append("canvas.width 必须是大于 0 的整数")
    if not _is_int(canvas_height) or canvas_height <= 0:
        errors.append("canvas.height 必须是大于 0 的整数")
    if errors:
        return errors

    if image_size is not None and (canvas_width, canvas_height) != image_size:
        errors.append(
            "canvas 尺寸与原图不一致："
            f"标注为 {canvas_width}x{canvas_height}，原图为 {image_size[0]}x{image_size[1]}"
        )

    scene_duration = data.get("sceneDurationMs")
    if scene_duration is not None and (
        not _is_int(scene_duration) or scene_duration <= 0
    ):
        errors.append("sceneDurationMs 必须是大于 0 的整数")

    elements = data.get("elements")
    if not isinstance(elements, list) or not elements:
        errors.append("elements 必须是非空数组")
        return errors

    sequences: list[int] = []
    latest_end = 0
    previous_end = 0
    for index, element in enumerate(ordered_elements(elements), start=1):
        path = f"elements[{index - 1}]"
        if not isinstance(element, dict):
            errors.append(f"{path} 必须是对象")
            continue

        sequence = element.get("sequence")
        if not _is_int(sequence) or sequence <= 0:
            errors.append(f"{path}.sequence 必须是从 1 开始的正整数")
        else:
            sequences.append(sequence)

        for field in ("id", "label", "narrativeRole", "subtitle"):
            value = element.get(field)
            if not isinstance(value, str) or (field in {"id", "label"} and not value.strip()):
                errors.append(f"{path}.{field} 必须是字符串")

        _check_rect(element.get("region"), f"{path}.region", canvas_width, canvas_height, errors)

        reveal = element.get("reveal")
        if not isinstance(reveal, dict):
            errors.append(f"{path}.reveal 必须是对象")
            continue
        start_ms = reveal.get("startMs")
        duration_ms = reveal.get("durationMs")
        if not _is_int(start_ms) or start_ms < 0:
            errors.append(f"{path}.reveal.startMs 必须是非负整数")
        if not _is_int(duration_ms) or duration_ms <= 0:
            errors.append(f"{path}.reveal.durationMs 必须是大于 0 的整数")
        if _is_int(start_ms) and _is_int(duration_ms) and duration_ms > 0:
            if start_ms < previous_end:
                errors.append(
                    f"{path} 与前一 sequence 时间重叠："
                    f"startMs={start_ms}，前一段结束于 {previous_end}"
                )
            previous_end = max(previous_end, start_ms + duration_ms)
            latest_end = max(latest_end, start_ms + duration_ms)

        direction = reveal.get("direction")
        if direction not in VALID_DIRECTIONS:
            errors.append(f"{path}.reveal.direction 不是支持的方向")
        mask_padding = reveal.get("maskPaddingPx")
        if mask_padding is not None and (not _is_int(mask_padding) or mask_padding < 0):
            errors.append(f"{path}.reveal.maskPaddingPx 必须是非负整数")

        protected = reveal.get("protectedRegions", [])
        if not isinstance(protected, list):
            errors.append(f"{path}.reveal.protectedRegions 必须是数组")
        else:
            for protected_index, rect in enumerate(protected):
                _check_rect(
                    rect,
                    f"{path}.reveal.protectedRegions[{protected_index}]",
                    canvas_width,
                    canvas_height,
                    errors,
                )

        hand_path = element.get("handPath")
        if not isinstance(hand_path, dict):
            errors.append(f"{path}.handPath 必须是对象")
        else:
            _check_point(hand_path.get("start"), f"{path}.handPath.start", errors)
            _check_point(hand_path.get("end"), f"{path}.handPath.end", errors)

    if sequences:
        expected = list(range(1, len(elements) + 1))
        if sorted(sequences) != expected:
            errors.append(f"sequence 必须唯一且连续，期望 {expected}，实际 {sorted(sequences)}")

    if _is_int(scene_duration) and scene_duration < latest_end + 500:
        errors.append(
            "sceneDurationMs 必须至少覆盖最后区域结束后的 500ms："
            f"当前 {scene_duration}，至少需要 {latest_end + 500}"
        )
    return errors


def validate_annotation(data: Any, image_size: tuple[int, int] | None = None) -> None:
    errors = annotation_errors(data, image_size=image_size)
    if errors:
        detail = "\n  - ".join(errors)
        raise ValueError(f"标注校验失败：\n  - {detail}")
