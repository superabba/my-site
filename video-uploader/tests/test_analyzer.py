"""analyzer: 가짜 Anthropic 클라이언트로 요청 모양과 결과 정리만 확인한다 (실제 호출 없음)."""

import json
from types import SimpleNamespace

from core import analyzer


def text_block(text):
    return SimpleNamespace(type="text", text=text)


class FakeMessages:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def create(self, **kw):
        self.requests.append(kw)
        return self.responses.pop(0)


def fake_client(monkeypatch, responses):
    messages = FakeMessages(responses)
    monkeypatch.setattr(analyzer.anthropic, "Anthropic",
                        lambda api_key: SimpleNamespace(beta=SimpleNamespace(messages=messages)))
    return messages


def test_blog_photo_range_by_length():
    assert analyzer.blog_photo_range(15) == (2, 3)
    assert analyzer.blog_photo_range(60) == (2, 4)
    assert analyzer.blog_photo_range(120) == (4, 6)
    assert analyzer.blog_photo_range(600) == (5, 10)


def test_tidy_blog_keeps_photos_in_video_and_cleans_body():
    raw = {
        "title": "제목" * 60,
        "body": "도입\n[영상]\n[사진 1]\n[사진 1]\n본문\n[사진 2]\n[사진 3]\n[영상]\n끝",
        "tags": ["#가족 나들이", "축제", ""],
        "photos": [{"time": -3, "caption": "처음"}, {"time": 99, "caption": "끝"}, {"time": 5, "caption": "넘침"}],
    }
    out = analyzer.tidy_blog(raw, duration=30.0, photo_max=2, include_video=True)
    assert len(out["title"]) == 100
    assert out["photos"] == [{"time": 0.0, "caption": "처음"}, {"time": 29.8, "caption": "끝"}]
    # 같은 사진 자리 중복, 없는 [사진 3], 두 번째 [영상]은 빠진다
    assert out["body"] == "도입\n[영상]\n[사진 1]\n본문\n[사진 2]\n끝"
    assert out["tags"] == ["가족나들이", "축제"]
    assert "[영상]" not in analyzer.tidy_blog(raw, 30.0, 2, include_video=False)["body"]


def test_research_continues_after_pause_turn(monkeypatch):
    messages = fake_client(monkeypatch, [
        SimpleNamespace(stop_reason="pause_turn", content=[text_block("찾는 중")]),
        SimpleNamespace(stop_reason="end_turn", content=[text_block("주제: 축제\n- 사실 (https://a.b)")]),
    ])
    notes = analyzer.research_topic("k", "claude-opus-5-5", "유튜브 제목: 축제")
    assert notes.startswith("주제: 축제")
    assert len(messages.requests) == 2
    assert messages.requests[0]["tools"][0]["type"] == "web_search_20260209"
    assert messages.requests[1]["messages"][-1]["role"] == "assistant"  # 끊긴 곳부터 이어서


def test_research_returns_none_on_refusal(monkeypatch):
    fake_client(monkeypatch, [SimpleNamespace(stop_reason="refusal", content=[])])
    assert analyzer.research_topic("k", "claude-opus-5-5", "x") is None


def test_write_blog_asks_photo_range_and_tidies(monkeypatch, tmp_path):
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"\xff\xd8\xff")
    result = {"title": "축제 나들이", "body": "도입\n[사진 1]\n[사진 2]", "tags": ["축제"],
              "photos": [{"time": 2, "caption": "a"}, {"time": 4, "caption": "b"}]}
    messages = fake_client(monkeypatch, [
        SimpleNamespace(stop_reason="end_turn", content=[text_block(json.dumps(result, ensure_ascii=False))])])
    out = analyzer.write_blog("k", "claude-opus-5-5", [(2.0, frame), (4.0, frame)], 20.0, "유튜브 제목: 축제",
                              research="주제: 축제", include_video=False)
    assert out["photos"] == [{"time": 2.0, "caption": "a"}, {"time": 4.0, "caption": "b"}]
    req = messages.requests[0]
    texts = [c["text"] for c in req["messages"][0]["content"] if c["type"] == "text"]
    assert any("사진은 2~3장" in t for t in texts) and any("주제: 축제" in t for t in texts)
    assert req["output_config"]["format"]["schema"]["required"] == ["title", "body", "tags", "photos"]
    assert req["fallbacks"] == "default"


def test_sns_brief_uses_titles_and_hashtags():
    brief = analyzer.sns_brief({"title": "앗 벌레다!", "hashtags": ["나주영산강축제", "형제일상"]}, "7세·4세 형제", "축제장")
    assert "앗 벌레다!" in brief and "#나주영산강축제 #형제일상" in brief
    assert "사용자 메모: 7세·4세 형제" in brief and "영상 요약: 축제장" in brief


def api_error(cls, status, message, org="org-test"):
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, headers={"anthropic-organization-id": org})
    return cls(message, response=response, body={"type": "error", "error": {"message": message}})


class RaisingMessages:
    def __init__(self, error):
        self.error, self.calls = error, 0

    def create(self, **kw):
        self.calls += 1
        raise self.error


def raising_client(monkeypatch, error):
    messages = RaisingMessages(error)
    monkeypatch.setattr(analyzer.anthropic, "Anthropic",
                        lambda api_key: SimpleNamespace(beta=SimpleNamespace(messages=messages), messages=messages))
    return messages


def test_web_search_tool_matches_model():
    assert analyzer.web_search_tool("claude-opus-5-5")["type"] == "web_search_20260209"
    assert analyzer.web_search_tool("claude-sonnet-5-5")["type"] == "web_search_20260209"
    assert analyzer.web_search_tool("claude-haiku-5-5")["type"] == "web_search_20250305"


def test_haiku_has_no_fallbacks_and_uses_basic_search(monkeypatch):
    messages = fake_client(monkeypatch, [SimpleNamespace(stop_reason="end_turn", content=[text_block("주제: x")])])
    analyzer.research_topic("k", "claude-haiku-5-5", "brief")
    req = messages.requests[0]
    assert "fallbacks" not in req and req["tools"][0]["type"] == "web_search_20250305"


def test_credit_error_is_fatal_and_names_org(monkeypatch):
    import anthropic
    import pytest

    err = api_error(anthropic.BadRequestError, 400, "Your credit balance is too low to access the Anthropic API.",
                    org="3ce1f981")
    raising_client(monkeypatch, err)
    with pytest.raises(analyzer.AnalysisError) as e:
        analyzer.research_topic("k", "claude-haiku-5-5", "brief")  # '자료 없음'으로 넘기지 않는다
    assert e.value.fatal and "크레딧이 부족" in str(e.value) and "3ce1f981" in str(e.value)


def test_non_fatal_research_error_is_skipped(monkeypatch):
    import anthropic

    raising_client(monkeypatch, api_error(anthropic.RateLimitError, 429, "slow down"))
    assert analyzer.research_topic("k", "claude-haiku-5-5", "brief") is None


def test_check_key_reports_org_or_credit_problem(monkeypatch):
    import anthropic
    import pytest

    ok = SimpleNamespace(headers={"anthropic-organization-id": "org-ok"})
    monkeypatch.setattr(analyzer.anthropic, "Anthropic", lambda api_key: SimpleNamespace(
        messages=SimpleNamespace(with_raw_response=SimpleNamespace(create=lambda **kw: ok))))
    assert analyzer.check_key("k") == "org-ok"

    err = api_error(anthropic.BadRequestError, 400, "Your credit balance is too low", org="org-empty")

    def boom(**kw):
        raise err

    monkeypatch.setattr(analyzer.anthropic, "Anthropic", lambda api_key: SimpleNamespace(
        messages=SimpleNamespace(with_raw_response=SimpleNamespace(create=boom))))
    with pytest.raises(analyzer.AnalysisError, match="org-empty"):
        analyzer.check_key("k")


def test_default_model_is_cheapest():
    from core import config

    assert config.DEFAULT_SETTINGS["claude_model"] == "claude-haiku-5-5"
    assert list(config.CLAUDE_MODELS)[0] == "claude-haiku-5-5"
