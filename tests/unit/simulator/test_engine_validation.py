import pytest

from infineq.simulator.engine import RatePhase, SimulationConfig


def test_simulation_config_rejects_unsorted_or_overlapping_workload_phases() -> None:
    with pytest.raises(ValueError):
        SimulationConfig(
            duration_s=10.0,
            workload=(RatePhase(5.0, 10.0, 1.0), RatePhase(0.0, 6.0, 1.0)),
        )


def test_simulation_config_rejects_phase_after_episode_end() -> None:
    with pytest.raises(ValueError):
        SimulationConfig(
            duration_s=10.0,
            workload=(RatePhase(0.0, 11.0, 1.0),),
        )


def test_simulation_config_requires_one_or_more_initial_replicas() -> None:
    with pytest.raises(ValueError):
        SimulationConfig(
            duration_s=10.0,
            workload=(RatePhase(0.0, 10.0, 1.0),),
            initial_replicas=0,
        )
