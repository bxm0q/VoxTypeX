from concurrent.futures import ThreadPoolExecutor

import pytest

from voxtypex.state import AppController
from voxtypex.state import AppState as S


def test_lifecycle():
    c = AppController()
    assert not c.transition(S.IDLE, S.INSERTING)
    assert c.transition(S.IDLE, S.RECORDING)
    assert not c.transition(S.IDLE, S.RECORDING)
    assert c.transition(S.RECORDING, S.TRANSCRIBING)
    assert not c.transition(S.RECORDING, S.TRANSCRIBING)
    assert c.transition(S.TRANSCRIBING, S.INSERTING)
    assert c.transition(S.INSERTING, S.IDLE)


def test_concurrent_recording_claim():
    c = AppController()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: c.transition(S.IDLE, S.RECORDING), range(40)))
    assert sum(results) == 1


def test_error_empty_result_shutdown():
    c = AppController()
    assert c.transition(S.IDLE, S.RECORDING)
    assert c.transition(S.RECORDING, S.ERROR)
    assert not c.transition(S.ERROR, S.RECORDING)
    assert c.transition(S.ERROR, S.IDLE)
    assert c.transition(S.IDLE, S.RECORDING)
    assert c.transition(S.RECORDING, S.TRANSCRIBING)
    assert c.transition(S.TRANSCRIBING, S.IDLE)
    c.close()
    c.close()
    assert c.closed
    assert not c.transition(S.IDLE, S.RECORDING)


@pytest.mark.parametrize("source", list(S))
@pytest.mark.parametrize("target", list(S))
def test_transition_matrix_and_closed_gate(source, target):
    paths = {
        S.IDLE: [],
        S.RECORDING: [S.RECORDING],
        S.TRANSCRIBING: [S.RECORDING, S.TRANSCRIBING],
        S.INSERTING: [S.RECORDING, S.TRANSCRIBING, S.INSERTING],
        S.ERROR: [S.ERROR],
    }
    allowed = {
        (S.IDLE, S.RECORDING),
        (S.IDLE, S.ERROR),
        (S.RECORDING, S.IDLE),
        (S.RECORDING, S.TRANSCRIBING),
        (S.RECORDING, S.ERROR),
        (S.TRANSCRIBING, S.INSERTING),
        (S.TRANSCRIBING, S.IDLE),
        (S.TRANSCRIBING, S.ERROR),
        (S.INSERTING, S.IDLE),
        (S.INSERTING, S.ERROR),
        (S.ERROR, S.IDLE),
    }
    controller = AppController()
    for step in paths[source]:
        assert controller.transition(controller.state, step)
    expected = (source, target) in allowed
    assert controller.transition(source, target) is expected
    assert controller.state is (target if expected else source)
    controller.close()
    assert not controller.transition(controller.state, target)
