import itertools

import pytest

from negotiator.brain.fsm import ForbiddenTransition, NegotiationFSM
from negotiator.core.contracts import NegotiationPhase

PHASES = list(NegotiationPhase)
NEXT = dict(zip(PHASES, PHASES[1:]))


@pytest.mark.parametrize("source,target", itertools.product(PHASES, PHASES))
def test_transition_table(source, target):
    machine = NegotiationFSM(source)
    allowed = target is source or (NEXT.get(source) is target and target is not NegotiationPhase.LEVERAGE)
    if allowed:
        assert machine.transition(target) is target
    else:
        with pytest.raises(ForbiddenTransition):
            machine.transition(target)


def test_leverage_requires_complete_estimate():
    machine = NegotiationFSM(NegotiationPhase.PRESSURE_TEST)
    with pytest.raises(ForbiddenTransition):
        machine.transition(NegotiationPhase.LEVERAGE)
    assert machine.transition(NegotiationPhase.LEVERAGE, full_estimate=True) is NegotiationPhase.LEVERAGE


def test_call_can_only_finish_from_wrap():
    with pytest.raises(ForbiddenTransition):
        NegotiationFSM(NegotiationPhase.COMMIT).finish()
    assert NegotiationFSM(NegotiationPhase.WRAP).finish() is None
