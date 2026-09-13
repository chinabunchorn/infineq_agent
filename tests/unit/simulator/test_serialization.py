from infineq.simulator.serialization import stable_json_bytes


def test_stable_json_bytes_sorts_keys_and_uses_one_terminal_newline() -> None:
    assert stable_json_bytes({"z": 1, "a": [2, 1]}) == b'{"a":[2,1],"z":1}\n'


def test_stable_json_bytes_rejects_non_finite_numbers() -> None:
    import math

    try:
        stable_json_bytes({"value": math.nan})
    except ValueError as exc:
        assert "non-finite" in str(exc)
    else:
        raise AssertionError("non-finite JSON should be rejected")
