import pytest
from voice.wake_word import match_wake, parse_yes_no, overlaps_spoken, strip_wake


@pytest.mark.parametrize("text", [
    "Hey Mei Mei", "Hey, Mei Mei.", "hey may may", "Hey Mei-Mei!", "hey maymay", "Hey Mamay", "hay mei mei",
    "a mei mei", "Hey Mei Mei, open Firefox", "okay hey mei mei what time is it", "hey mei mai", "Mei Mei, clear your context",
])
def test_accepts(text):
    assert match_wake(text).matched, text


@pytest.mark.parametrize("text", [
    "hey mom", "hey there how are you", "open firefox", "I love this song", "hey siri", "hey google", "what is the weather",
    "my mom made me", "okay then", "hey jarvis", "hey alexa", "maybe tomorrow", "hey mickey",
])
def test_rejects(text):
    assert not match_wake(text).matched, text


def test_remainder():
    m = match_wake("Hey Mei Mei, open Firefox please.")
    assert m.matched and m.remainder == "open Firefox please."
    assert match_wake("Hey Mei Mei").remainder == ""


def test_strip_wake_fallback():
    assert strip_wake("Hey Mei Mei open firefox", 0.6, True) == "open firefox"
    assert strip_wake("Hey may me open firefox", 0.6, True, fallback_words=3) == "open firefox"


def test_sensitivity_monotonic():
    t = "hey mimi"
    assert match_wake(t, 1.0).score == match_wake(t, 0.0).score
    assert match_wake(t, 1.0).matched or not match_wake(t, 0.0).matched


def test_yes_no():
    assert parse_yes_no("yes please") is True and parse_yes_no("no") is False
    assert parse_yes_no("don't do it") is False and parse_yes_no("go ahead") is True
    assert parse_yes_no("the weather is nice today and so on and on") is None


def test_self_echo():
    spoken = "Mei Mei here, your CPU is at ten percent"
    assert overlaps_spoken("mei mei here your cpu", spoken)
    assert not overlaps_spoken("hey mei mei stop talking", spoken)


def test_find_wake_anywhere():
    from voice.wake_word import find_wake
    m = find_wake("Paris proper has about two million Hey Mei Mei stop")
    assert m.matched and m.remainder == "stop"
    assert not find_wake("Paris proper has about two million people").matched
    assert not match_wake("Paris proper has about two million Hey Mei Mei stop").matched   # anchored version rejects


def test_barge_requires_full_phrase():
    from voice.wake_word import find_wake
    assert not find_wake("Mei Mei is much bigger around 12 million", 0.4, allow_bare_name=False).matched
    assert find_wake("blah blah Hey Mei Mei stop", 0.4, allow_bare_name=False).matched
