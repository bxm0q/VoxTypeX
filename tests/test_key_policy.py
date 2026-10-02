import pytest

from voxtypex.key_policy import KEY_CODES, KeyCapture, modifiers_allowed


@pytest.mark.parametrize("name", ["right_alt", "right_ctrl", "f8", "f9", "f24", "mouse4", "mouse5"])
def test_supported_single_button_commits_after_release(name):
    capture = KeyCapture()
    vk = KEY_CODES[name]
    assert capture.event(vk, True) == ("release", name)
    for _ in range(5):
        assert capture.event(vk, True) == ("waiting", None)
    assert capture.event(vk, False) == ("accepted", name)
    assert capture.event(vk, False) == ("waiting", None)


@pytest.mark.parametrize("vk", [0x41, 0x0D, 0x20, 0x09, 0x2E, 0x5B, 0xA0, 0xA2, 0xA4, 0x73, 0x79])
def test_unsafe_key_not_assigned(vk):
    capture = KeyCapture()
    assert capture.event(vk, True)[0] == "unsupported"
    assert capture.event(vk, False)[0] != "accepted"


def test_chord_after_candidate_and_altgr():
    capture = KeyCapture()
    capture.event(KEY_CODES["f9"], True)
    assert capture.event(0xA2, True)[0] == "unsupported"
    assert capture.event(0xA2, False)[0] != "accepted"
    assert capture.event(KEY_CODES["f9"], False)[0] != "accepted"
    capture.event(0xA2, True)
    assert capture.event(0xA5, True, {0x11, 0x12})[0] == "release"
    assert capture.event(0xA5, False)[0] == "waiting"
    assert capture.event(0xA2, False) == ("accepted", "right_alt")
    assert not modifiers_allowed(KEY_CODES["f9"], {0xA2})
    assert not modifiers_allowed(0xA5, {0x5B})


def test_escape_is_cancellation():
    assert KeyCapture().event(0x1B, True) == ("cancel", None)
