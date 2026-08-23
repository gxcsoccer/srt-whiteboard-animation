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
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

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
    args = p.parse_args(argv)

    inputs = [Path(x) for x in args.inputs]
    missing = [str(x) for x in inputs if not x.exists()]
    if missing:
        print(f"[err] 缺少输入文件: {', '.join(missing)}", file=sys.stderr)
        return 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    if _ffmpeg_concat(inputs, output) or _pyav_concat(inputs, output):
        print(f"OUTPUT={output.resolve()}")
        return 0
    print("[err] 合并失败：系统无 ffmpeg 且 PyAV 不可用", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
