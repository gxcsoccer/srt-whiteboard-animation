#!/usr/bin/env python3
"""
多幕合并：把各场景的白板动画 MP4 按顺序硬切拼接成一条完整视频。

优先级：
  1. 各片尺寸/帧率一致 → ffmpeg concat demuxer + `-c copy`（无损、最快）
  2. 尺寸或帧率不一致 → ffmpeg filter_complex 逐片缩放补边 + 统一帧率再拼
  3. 无 ffmpeg → PyAV 逐帧重编码，缩放到第一段尺寸
单片输入也照常处理。

注意：尺寸不一致时**不能**用 `-c copy`——concat demuxer 那时依然返回 0，
却会产出「容器声明第一片尺寸、实际混着多种尺寸且 DTS 非单调」的坏文件，
所以这里先用 ffprobe 核对，一致才走无损路径。

用法：
  <ENV_PY> merge_scenes.py --inputs a.mp4 b.mp4 c.mp4 --output final.mp4
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np  # noqa: F401  (类型注解与过渡帧处理用)

# 补边颜色：与渲染器画布底色一致（stream_render.Config.canvas_hex）
PAD_COLOR = "0xF6F1E3"


def _concat_quote(path: Path) -> str:
    """
    concat 清单的单引号转义：路径里的 ' 必须写成 '\\'' ，
    否则含单引号的文件名会让清单解析失败，甚至被当成额外的 concat 指令。
    """
    return str(path.resolve().as_posix()).replace("'", "'\\''")


def _probe_streams(inputs: list[Path]) -> list[tuple[int, int, str] | None]:
    """取每片的 (宽, 高, 帧率字符串)。没有 ffprobe 或读不出来的项为 None。"""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return [None] * len(inputs)
    specs: list[tuple[int, int, str] | None] = []
    for path in inputs:
        res = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,r_frame_rate", "-of", "csv=p=0:s=,", str(path)],
            capture_output=True, text=True,
        )
        parts = res.stdout.strip().split(",")
        if res.returncode != 0 or len(parts) < 3:
            specs.append(None)
            continue
        try:
            specs.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            specs.append(None)
    return specs


def _describe(specs: list[tuple[int, int, str] | None], inputs: list[Path]) -> None:
    for path, spec in zip(inputs, specs):
        text = f"{spec[0]}x{spec[1]} @ {spec[2]}" if spec else "探测失败"
        print(f"         {path.name}: {text}")


def _fps_value(rate: str) -> float:
    try:
        return float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _concat_demuxer(ffmpeg: str, inputs: list[Path], output: Path, copy: bool) -> bool:
    """concat demuxer 路径。copy=True 为无损流拷贝，False 为重编码。"""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        for path in inputs:
            f.write(f"file '{_concat_quote(path)}'\n")
        list_path = Path(f.name)
    try:
        tail = ["-c", "copy"] if copy else [
            "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
            "-vf", "scale='trunc(iw/2)*2':'trunc(ih/2)*2'",
        ]
        res = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(list_path), *tail, str(output)],
            capture_output=True, text=True,
        )
        if res.returncode == 0:
            how = "无损拼接" if copy else "重编码拼接"
            print(f"  ffmpeg {how}完成: {output}")
            return True
        how = "-c copy" if copy else "重编码"
        print(f"  [warn] ffmpeg {how} 失败: {res.stderr.strip()[:200]}")
        return False
    finally:
        list_path.unlink(missing_ok=True)


def _concat_filter(
    ffmpeg: str, inputs: list[Path], output: Path, specs: list[tuple[int, int, str] | None]
) -> bool:
    """
    尺寸/帧率不一致时的正确拼法：逐片等比缩放 + 补边到统一画幅、统一帧率，
    再用 filter_complex 的 concat 拼接（concat demuxer 做不到这件事）。
    基准画幅取第一片（长宽取偶数以满足 yuv420p），帧率取各片最大值。
    """
    known = [s for s in specs if s]
    if not known:
        return False
    width, height = known[0][0] // 2 * 2, known[0][1] // 2 * 2
    fps = max((_fps_value(s[2]) for s in known), default=0.0)
    fps_arg = f"{fps:.6f}".rstrip("0").rstrip(".") if fps > 0 else "30"
    print(f"  [..] 归一化到 {width}x{height} @ {fps_arg}fps 后拼接")

    cmd = [ffmpeg, "-y", "-loglevel", "error"]
    for path in inputs:
        cmd += ["-i", str(path)]
    chains = [
        f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={PAD_COLOR},"
        f"fps={fps_arg},setsar=1[v{i}]"
        for i in range(len(inputs))
    ]
    joined = "".join(f"[v{i}]" for i in range(len(inputs)))
    filter_complex = ";".join(chains) + f";{joined}concat=n={len(inputs)}:v=1:a=0[out]"
    cmd += ["-filter_complex", filter_complex, "-map", "[out]",
            "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(output)]

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        print(f"  ffmpeg 归一化拼接完成: {output}")
        return True
    print(f"  [warn] ffmpeg 归一化拼接失败: {res.stderr.strip()[:200]}")
    return False


def build_transition(
    prev_video: Path, next_video: Path, out_path: Path,
    hold_ms: int, erase_ms: int, fps: float | None = None,
) -> Path | None:
    """
    造一段「上一幕停留 → 擦掉 → 露出下一幕起始纸面」的过渡片段。

    没有它的话，幕与幕之间就是硬切回空白画布：上一幕刚画完就瞬间消失，
    观众（和已经开口的旁白）会撞上 1–2 秒空白。这里的做法是：
      1. 上一幕最后一帧停留 hold_ms（≥0.5s，用户要求的下限）
      2. 一块橡皮从左到右擦过，把画面逐列换成下一幕的首帧（干净纸面）
    擦除用下一幕首帧作为目标，所以过渡结束时画面正好等于下一幕的起点，
    拼接处不会闪。
    """
    prev_frame = _last_frame(prev_video)
    next_frame = _first_frame(next_video)
    if prev_frame is None or next_frame is None:
        return None
    if prev_frame.shape != next_frame.shape:
        next_frame = cv2.resize(
            next_frame, (prev_frame.shape[1], prev_frame.shape[0]),
            interpolation=cv2.INTER_AREA,
        )

    rate = fps or _probe_fps(prev_video) or 30.0
    hold_frames = max(1, int(round(hold_ms * rate / 1000)))
    erase_frames = max(1, int(round(erase_ms * rate / 1000)))
    height, width = prev_frame.shape[:2]

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    raw = out_path.with_name(out_path.stem + "_raw.mp4")
    writer = cv2.VideoWriter(str(raw), fourcc, rate, (width, height))
    if not writer.isOpened():
        return None
    try:
        for _ in range(hold_frames):          # 1) 完整画面停留
            writer.write(prev_frame)
        for index in range(1, erase_frames + 1):  # 2) 橡皮擦过
            progress = index / erase_frames
            edge = int(round(width * progress))
            frame = prev_frame.copy()
            if edge > 0:
                frame[:, :edge] = next_frame[:, :edge]
            _draw_eraser(frame, edge, height)
            writer.write(frame)
    finally:
        writer.release()

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return raw
    result = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(raw),
         "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(out_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  [warn] 过渡片段转码失败，改用原始编码: {result.stderr.strip()[:160]}")
        return raw
    raw.unlink(missing_ok=True)
    return out_path


def _draw_eraser(frame: "np.ndarray", edge: int, height: int) -> None:
    """在擦除前沿画一块简易橡皮，让"擦"这个动作看得见。"""
    if edge <= 0 or edge >= frame.shape[1]:
        return
    half_h = max(12, height // 12)
    top = max(0, height // 2 - half_h)
    bottom = min(height, height // 2 + half_h)
    left = max(0, edge - max(10, height // 26))
    right = min(frame.shape[1], edge + max(4, height // 90))
    cv2.rectangle(frame, (left, top), (right, bottom), (120, 120, 120), thickness=-1)
    cv2.rectangle(frame, (left, top), (right, bottom), (60, 60, 60), thickness=2)


def _duration_ms(path: Path) -> float:
    """片段时长（毫秒）。优先 ffprobe，退回 cv2 的帧数/帧率。"""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is not None:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True,
        )
        try:
            return float(result.stdout.strip()) * 1000.0
        except ValueError:
            pass
    capture = cv2.VideoCapture(str(path))
    frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
    rate = capture.get(cv2.CAP_PROP_FPS)
    capture.release()
    return (frames / rate * 1000.0) if frames and rate else 0.0


def _write_timeline(path: Path, inputs: list[Path], transition_ms: int) -> None:
    """
    记录每幕在合并结果中的起始时间。插了过渡之后，第 k 幕整体后移
    k × 过渡时长——旁白要跟着重定时，否则语音会比画面早说。
    配合 parse_srt.py 的 scenes[].cueRange，可用 retime_srt.py 平移字幕。
    """
    scenes = []
    cursor = 0.0
    for index, video in enumerate(inputs):
        duration = _duration_ms(video)
        scenes.append({
            "sceneIndex": index + 1,
            "input": str(video),
            "startMs": int(round(cursor)),
            "durationMs": int(round(duration)),
            "endMs": int(round(cursor + duration)),
        })
        cursor += duration
        if index + 1 < len(inputs):
            cursor += transition_ms
    payload = {
        "transitionMs": transition_ms,
        "totalMs": int(round(cursor)),
        "scenes": scenes,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  时间线已写出: {path}（过渡 {transition_ms}ms/处，总长 {payload['totalMs'] / 1000:.2f}s）")


def _first_frame(path: Path):
    capture = cv2.VideoCapture(str(path))
    ok, frame = capture.read()
    capture.release()
    return frame if ok else None


def _last_frame(path: Path):
    capture = cv2.VideoCapture(str(path))
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    frame = None
    if total > 0:
        capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, total - 1))
        ok, candidate = capture.read()
        if ok:
            frame = candidate
    if frame is None:                     # 有些容器读不到总帧数，退回顺序读到底
        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        while True:
            ok, candidate = capture.read()
            if not ok:
                break
            frame = candidate
    capture.release()
    return frame


def _probe_fps(path: Path) -> float | None:
    capture = cv2.VideoCapture(str(path))
    rate = capture.get(cv2.CAP_PROP_FPS)
    capture.release()
    return rate if rate and rate > 0 else None


def _ffmpeg_concat(inputs: list[Path], output: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return False

    specs = _probe_streams(inputs)
    uniform = all(spec is not None for spec in specs) and len(set(specs)) == 1
    if uniform:
        if _concat_demuxer(ffmpeg, inputs, output, copy=True):
            return True
        if _concat_demuxer(ffmpeg, inputs, output, copy=False):
            return True
    else:
        if any(spec is None for spec in specs):
            print("  [warn] 无法探测各片参数（缺 ffprobe？），跳过无损拼接:")
        else:
            print("  [warn] 各片尺寸/帧率不一致，不能用 -c copy（会产出坏文件）:")
        _describe(specs, inputs)

    if _concat_filter(ffmpeg, inputs, output, specs):
        return True
    # 探测全失败时 filter 路径没有基准画幅可用，最后再试一次 demuxer 重编码
    return _concat_demuxer(ffmpeg, inputs, output, copy=False)


def _pyav_concat(inputs: list[Path], output: Path) -> bool:
    try:
        import av
    except ImportError:
        return False
    first = av.open(str(inputs[0]))
    vs = first.streams.video[0]
    w, h = vs.codec_context.width, vs.codec_context.height
    rate = vs.average_rate
    first.close()

    out = av.open(str(output), mode="w")
    ostream = out.add_stream("h264", rate=rate)
    ostream.width, ostream.height = w, h
    ostream.pix_fmt = "yuv420p"
    ostream.options = {"crf": "24", "preset": "medium"}
    for p in inputs:
        cont = av.open(str(p))
        for frame in cont.decode(video=0):
            if frame.width != w or frame.height != h:
                frame = frame.reformat(width=w, height=h)
            for pkt in ostream.encode(frame):
                out.mux(pkt)
        cont.close()
    for pkt in ostream.encode(None):
        out.mux(pkt)
    out.close()
    print(f"  PyAV 拼接完成: {output}")
    return True


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="按顺序合并多幕白板动画 MP4")
    p.add_argument("--inputs", nargs="+", required=True, help="按播放顺序的 MP4 列表")
    p.add_argument("--output", required=True, help="合并输出路径")
    p.add_argument("--hold-ms", type=int, default=600,
                   help="幕尾完整画面停留毫秒（默认 600，用户要求 ≥500）")
    p.add_argument("--erase-ms", type=int, default=700,
                   help="擦除过渡毫秒（默认 700）；设 0 关闭擦除只保留停留")
    p.add_argument("--timeline-out", default=None,
                   help="把每幕在合并结果中的起始时间写成 JSON，供 SRT 重定时使用")
    args = p.parse_args(argv)

    inputs = [Path(x) for x in args.inputs]
    missing = [str(x) for x in inputs if not x.exists()]
    if missing:
        print(f"[err] 缺少输入文件: {', '.join(missing)}", file=sys.stderr)
        return 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    # 幕与幕之间插入「停留 + 擦除」过渡，避免硬切回空白画布
    segments = list(inputs)
    transitions: list[Path] = []
    hold_ms, erase_ms = max(0, args.hold_ms), max(0, args.erase_ms)
    if len(inputs) > 1 and (hold_ms + erase_ms) > 0:
        temp_dir = Path(tempfile.mkdtemp(prefix="scene_transition_"))
        segments = []
        for index, path in enumerate(inputs):
            segments.append(path)
            if index + 1 >= len(inputs):
                break
            piece = temp_dir / f"transition-{index + 1:02d}.mp4"
            built = build_transition(path, inputs[index + 1], piece, hold_ms, erase_ms)
            if built is None:
                print(f"  [warn] 第 {index + 1}→{index + 2} 幕之间的过渡生成失败，直接硬切")
                continue
            transitions.append(built)
            segments.append(built)
        print(f"  过渡: {len(transitions)} 段（停留 {hold_ms}ms + 擦除 {erase_ms}ms）")

    ok = _ffmpeg_concat(segments, output) or _pyav_concat(segments, output)
    if not ok:
        print("[err] 合并失败：系统无 ffmpeg 且 PyAV 不可用", file=sys.stderr)
        return 1

    if args.timeline_out:
        _write_timeline(Path(args.timeline_out), inputs, hold_ms + erase_ms if transitions else 0)
    print(f"OUTPUT={output.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
