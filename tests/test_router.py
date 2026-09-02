from soloscale.models import Surface, TaskEnvelope
from soloscale.router import route_task


def task(**kwargs: object) -> TaskEnvelope:
    return TaskEnvelope.model_validate(
        {
            "title": "Representative task",
            "goal": "Produce a verifiable and useful outcome for the workflow.",
            **kwargs,
        }
    )


def test_reasoning_defaults_to_chat() -> None:
    decision = route_task(task())
    assert decision.primary is Surface.CHAT


def test_plugin_action_routes_to_plugin() -> None:
    decision = route_task(task(plugin_can_complete=True, plugin_name="Figma"))
    assert decision.primary is Surface.PLUGIN
    assert Surface.CHAT in decision.secondary


def test_local_engineering_routes_to_codex() -> None:
    decision = route_task(task(requires_local_files=True, requires_terminal=True))
    assert decision.primary is Surface.CODEX
    assert Surface.CHAT in decision.secondary


def test_realtime_routes_to_runtime() -> None:
    decision = route_task(task(requires_realtime=True))
    assert decision.primary is Surface.RUNTIME


def test_public_action_requires_human_gate() -> None:
    decision = route_task(task(plugin_can_complete=True, plugin_name="Vercel", public_action=True))
    assert decision.human_gate_required
    assert Surface.HUMAN in decision.secondary
