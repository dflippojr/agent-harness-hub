"""Temporary: a deliberately failing test to prove the CI gate fails (#139). Reverted before merge."""


def test_gate_fails_on_a_failing_test():
    raise AssertionError("deliberate failure to prove the gate job fails; reverted before merge")
