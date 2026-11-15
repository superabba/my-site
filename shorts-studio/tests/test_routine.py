"""루틴 방식(API 없음) 쇼츠: 원고 검사, 차트·사진, 영상 정리, 워크플로."""
import copy
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest
import yaml
from PIL import Image

from shorts import daily
from shorts.econ import charts, content, photos
from shorts.ffmpeg import FFMPEG
from shorts.render import RenderResult

SAMPLE = json.loads((Path(__file__).parent / "sample_shorts.json").read_text(encoding="utf-8"))
REPO = Path(__file__).resolve().parents[2]


def sample():
    return copy.deepcopy(SAMPLE)


# ---------------------------------------------------------------- 원고 검사

def test_sample_passes():
    assert content.validate(sample()) == []


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d["scenes"][0].update(narration="안녕 😀 하세요 여러분"), "이모지"),
    (lambda d: d["scenes"][1].update(image_prompt="경제 사진을 그려 줘 한글 금지 위반입니다"), "image_prompt"),
    (lambda d: d["scenes"][2]["chart"].update(type="pie"), "chart.type"),
    (lambda d: d["scenes"][1]["chart"].update(value=150), "gauge.value"),
    (lambda d: d.update(sources=["https://a"]), "sources"),
    (lambda d: d.update(mood="angry"), "mood"),
    (lambda d: d["scenes"][3]["chart"]["steps"].pop(), "impact.steps"),
    (lambda d: [s.update(chart=None) for s in d["scenes"]], "차트"),
])
def test_validate_catches(mutate, needle):
    d = sample()
    mutate(d)
    assert any(needle in e for e in content.validate(d)), content.validate(d)


def test_too_long_script_rejected():
    d = sample()
    for s in d["scenes"]:
        s["narration"] = "가나다라마바사아자차" * 7
    assert any("너무 깁니다" in e for e in content.validate(d))


# ---------------------------------------------------------------- 차트

@pytest.mark.parametrize("i", range(5))
def test_every_chart_draws(i):
    ch = sample()["scenes"][i]["chart"]
    for prog in (0.0, 0.5, 1.0):
        img = charts.chart_card(ch, prog)
        assert img.mode == "RGBA" and img.size == (1080, charts.CARD_H + 80)
    assert img.getbbox() is not None


def test_compare_bar_lengths_follow_values():
    spec = charts.normalize_chart({"type": "compare", "bars": [{"name": "a", "value": 3.2}, {"name": "b", "value": 1.6}]})
    assert [b["val"] for b in spec["bars"]] == [100, 50]


def test_chart_center_matches_render_constant():
    from shorts import render
    assert abs(render.CHART_CY - charts.CARD_CENTER_Y) < 1e-9


# ---------------------------------------------------------------- 사진

def test_theme_not_hijacked_by_single_syllable():
    # '확정은', '다음은' 처럼 '은' 이 들어간 문장이 금 테마로 가지 않는다
    assert photos.theme_scores("다음은 한국은행이에요. 금통위에서 금리가 오를 수 있어요")[0][1] == "11"
    assert photos.theme_scores("은값이 올랐어요")[0][1] == "07"


def test_fallback_avoids_consecutive_repeat():
    a = photos.pick_fallback("환율이 올랐어요")
    b = photos.pick_fallback("달러 강세예요", prev=a.name[:2])
    assert a.name[:2] == "01" and b.name[:2] != "01"


def test_fallback_prefers_unused_photos_within_one_video():
    first = photos.pick_fallback("물가가 올랐어요")
    second = photos.pick_fallback("장바구니 물가 부담", used={first.name[:2]})
    assert first.name[:2] == "04" and second.name[:2] != "04"


def test_fit_portrait_landscape_and_portrait():
    assert photos.fit_portrait(Image.new("RGB", (1376, 768), "red")).size == (1080, 1920)
    assert photos.fit_portrait(Image.new("RGB", (768, 1344), "red")).size == (1080, 1920)


def test_prepare_media_falls_back_when_flux_down(tmp_path, monkeypatch):
    monkeypatch.setattr(photos, "fetch_flux", lambda *a, **k: None)
    script = {"scenes": [dict(s) for s in sample()["scenes"]], "visual_style": "x"}
    stats = photos.prepare_media(script, tmp_path)
    assert stats == {"flux": 0, "fallback": 5, "gradient": 0}
    assert all(Path(s["media"]).exists() for s in script["scenes"])


def test_prepare_media_uses_flux_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(photos, "fetch_flux", lambda *a, **k: Image.new("RGB", (1080, 1920), "blue"))
    script = {"scenes": [dict(s) for s in sample()["scenes"]], "visual_style": ""}
    assert photos.prepare_media(script, tmp_path)["flux"] == 5


# ---------------------------------------------------------------- inbox와 영상 정리

def test_inbox_pick(tmp_path):
    for n in ("20261110-0530-old", "20261114-0530-a", "20261115-0530-b"):
        (tmp_path / n).mkdir()
        (tmp_path / n / "shorts.json").write_text("{}", encoding="utf-8")
    (tmp_path / "20261115-nofile").mkdir()
    assert daily.inbox_pick(tmp_path, today=date(2026, 11, 15)).name == "20261115-0530-b"
    assert daily.inbox_pick(tmp_path, today=date(2026, 11, 20)) is None  # 너무 오래됨
    assert daily.inbox_pick(tmp_path / "없음", today=date(2026, 11, 15)) is None


def test_make_from_content_packages_everything(tmp_path, monkeypatch):
    src = tmp_path / "20261115-0530-t"
    src.mkdir()
    (src / "shorts.json").write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(photos, "fetch_flux", lambda *a, **k: None)
    seen = {}

    def fake_render(script, opts, progress=None):
        seen["charts"] = [s.get("chart", {}) and s["chart"]["type"] for s in script["scenes"]]
        seen["chart_fn"] = opts.chart_fn
        seen["medias"] = [s["media"] for s in script["scenes"]]
        vid = opts.output_dir / "v.mp4"
        opts.output_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=108x192:d=2",
                        "-pix_fmt", "yuv420p", str(vid)], check=True)
        return RenderResult(vid, vid, vid, 2.0, [], [], "곡: x", 0,
                            [{"media": m, "start": 0.0, "dur": 2.0} for m in seen["medias"]])

    monkeypatch.setattr(daily, "render", fake_render)
    out = tmp_path / "out"
    post = daily.make_from_content(src, out)
    assert seen["charts"] == ["stat", "gauge", "calendar", "impact", "flow"] and seen["chart_fn"] is charts.chart_card
    assert post["youtube_synthetic"] is True            # 모든 사진이 AI 가 그린 것
    assert post["topic"] == "미국 빅스텝설과 내 대출이자" and post["daily"] is True
    assert (out / "video.mp4").exists() and (out / "id.txt").exists() and (out / "summary.md").exists()
    blog = json.loads((out / "blog" / "blog.json").read_text(encoding="utf-8"))
    assert blog["titles"] and blog["video_id"] == (out / "id.txt").read_text(encoding="utf-8")
    assert "FLUX 서버에서 그림을 받지 못해" in (out / "summary.md").read_text(encoding="utf-8")


def test_make_from_content_rejects_bad_script(tmp_path):
    d = sample()
    d["scenes"] = d["scenes"][:2]
    (tmp_path / "shorts.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(daily.WriterError, match="원고 검사"):
        daily.make_from_content(tmp_path, tmp_path / "out")


# ---------------------------------------------------------------- 워크플로

def test_workflow_structure():
    wf = yaml.safe_load((REPO / ".github/workflows/shorts-daily.yml").read_text(encoding="utf-8"))
    assert set(wf["jobs"]) == {"intake", "daily"}
    assert wf[True]["push"]["branches"] == ["claude/**"]
    assert wf["jobs"]["intake"]["if"] == "github.event_name == 'push'"
    assert wf["jobs"]["daily"]["if"] == "github.event_name != 'push'"
    env = wf["jobs"]["daily"]["env"]
    assert "SHORTS_SOURCE" in env["ANTHROPIC_API_KEY"] and "SHORTS_SOURCE" in env["GEMINI_API_KEY"]
    assert "make-routine" in json.dumps(wf["jobs"]["daily"]["steps"], ensure_ascii=False)
