"""预览图字体解析、合并脚本转义等周边工具的测试。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))


# ──────────────────────────────────────────────────────────────
# render_annotation_preview.py：字体必须在 Linux / macOS 上也能找到
# ──────────────────────────────────────────────────────────────
def _preview_module():
    pytest.importorskip("PIL", reason="需要 Pillow：python scripts/prepare_env.py")
    import render_annotation_preview

    return render_annotation_preview


def test_font_candidates_cover_all_three_platforms():
    module = _preview_module()
    joined = " ".join(module.FONT_CANDIDATES)
    assert "C:/Windows/Fonts" in joined            # Windows
    assert "/System/Library/Fonts" in joined       # macOS
    assert "/usr/share/fonts" in joined            # Linux


def test_find_font_file_returns_existing_path_or_none():
    module = _preview_module()
    found = module.find_font_file()
    assert found is None or Path(found).exists()


def test_find_font_file_rejects_missing_explicit_font():
    module = _preview_module()
    with pytest.raises(FileNotFoundError):
        module.find_font_file("/definitely/not/a/font.ttf")


def test_load_fonts_falls_back_when_nothing_is_installed(monkeypatch, capsys):
    module = _preview_module()
    monkeypatch.setattr(module, "FONT_CANDIDATES", ("/nope/none.ttf",))
    monkeypatch.setattr(module, "_fc_match", lambda: None)
    big, small = module.load_fonts()
    assert big is not None and small is not None
    assert "未找到可用的中文字体" in capsys.readouterr().out


def test_preview_image_is_written(tmp_path):
    module = _preview_module()
    out = tmp_path / "preview.png"
    module.render_preview(
        str(REPO / "examples" / "scene-01-monkey-mountain-banana.png"),
        str(REPO / "examples" / "scene-01-monkey-mountain-banana.annotation.json"),
        str(out),
    )
    assert out.exists() and out.stat().st_size > 0

    from PIL import Image

    with Image.open(out) as image:
        assert image.size == (1672, 941)


def test_preview_cli_reports_invalid_annotation(tmp_path):
    _preview_module()
    bad = tmp_path / "bad.annotation.json"
    bad.write_text('{"elements": []}', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "render_annotation_preview.py"),
         str(REPO / "examples" / "scene-01-monkey-mountain-banana.png"),
         str(bad), str(tmp_path / "x.png")],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 1
    assert "标注校验失败" in result.stderr


# ──────────────────────────────────────────────────────────────
# merge_scenes.py：concat 清单的引号安全
# ──────────────────────────────────────────────────────────────
def test_concat_quote_escapes_single_quotes(tmp_path):
    import merge_scenes

    path = tmp_path / "it's a scene.mp4"
    quoted = merge_scenes._concat_quote(path)
    assert "'\\''" in quoted
    assert "'" not in quoted.replace("'\\''", "")     # 只剩转义后的形式


def test_concat_quote_keeps_plain_paths_intact(tmp_path):
    import merge_scenes

    path = tmp_path / "scene-01.mp4"
    assert merge_scenes._concat_quote(path) == path.resolve().as_posix()


def test_uniform_check_rejects_mixed_specs(monkeypatch):
    """尺寸/帧率不一致时绝不能走 -c copy（会产出 DTS 非单调的坏文件）。"""
    import merge_scenes

    inputs = [Path("a.mp4"), Path("b.mp4")]
    monkeypatch.setattr(
        merge_scenes, "_probe_streams", lambda _: [(480, 270, "12/1"), (720, 400, "24/1")]
    )
    calls: list[bool] = []
    monkeypatch.setattr(
        merge_scenes, "_concat_demuxer",
        lambda ffmpeg, ins, out, copy: (calls.append(copy), False)[1],
    )
    monkeypatch.setattr(merge_scenes, "_concat_filter", lambda *a, **k: True)
    monkeypatch.setattr(merge_scenes.shutil, "which", lambda _: "/usr/bin/ffmpeg")

    assert merge_scenes._ffmpeg_concat(inputs, Path("out.mp4")) is True
    assert True not in calls, "不一致时不应尝试 -c copy"


def test_uniform_check_allows_copy_when_identical(monkeypatch):
    import merge_scenes

    monkeypatch.setattr(
        merge_scenes, "_probe_streams", lambda _: [(480, 270, "12/1"), (480, 270, "12/1")]
    )
    calls: list[bool] = []
    monkeypatch.setattr(
        merge_scenes, "_concat_demuxer",
        lambda ffmpeg, ins, out, copy: (calls.append(copy), True)[1],
    )
    monkeypatch.setattr(merge_scenes.shutil, "which", lambda _: "/usr/bin/ffmpeg")

    assert merge_scenes._ffmpeg_concat([Path("a.mp4"), Path("b.mp4")], Path("o.mp4")) is True
    assert calls == [True]


def test_fps_value_parses_fractions():
    import merge_scenes

    assert merge_scenes._fps_value("24/1") == 24
    assert round(merge_scenes._fps_value("2997/100"), 2) == 29.97
    assert merge_scenes._fps_value("0/0") == 0.0
    assert merge_scenes._fps_value("garbage") == 0.0


# ──────────────────────────────────────────────────────────────
# 预览台：Node 侧逻辑测试的转接（没装 node 就跳过）
# ──────────────────────────────────────────────────────────────
def test_preview_html_logic():
    import shutil

    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 preview.html 逻辑测试")
    result = subprocess.run(
        [node, str(REPO / "tests" / "preview_html.test.mjs")],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
