from infineq.simulator.scenarios import ScenarioFamily, scenario_catalog


def test_queue_variants_apply_their_distinct_onset_to_observed_arrivals() -> None:
    queue_cases = [
        case for case in scenario_catalog() if case.family is ScenarioFamily.QUEUE_SATURATION
    ]

    assert all(
        case.onset_s in {phase.start_s for phase in case.simulation_config.workload}
        for case in queue_cases
    )
