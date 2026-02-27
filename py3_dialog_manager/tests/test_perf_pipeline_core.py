from __future__ import annotations

from dialog.interfaces import CommandDecision, LLMResult, RouteDecision, UserInput
from dialog.pipeline import InputLLMOutputPipeline

import pytest
from tests.perf_utils import collect_latency_ms, default_perf_controls, perf_env_float, record_perf_metric


class _InputOnce:
    def __init__(self, text: str) -> None:
        self._text = text
        self._used = False

    def get_input(self) -> UserInput:
        if self._used:
            return UserInput(raw_text="", text="")
        self._used = True
        return UserInput(raw_text=self._text, text=self._text)


class _LLMSpy:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, messages):
        self.calls += 1
        return LLMResult(reply="ok", messages=list(messages))


class _NoOpOutput:
    def __init__(self) -> None:
        self.calls = 0

    def emit(self, _text: str) -> None:
        self.calls += 1


class _Recognizer:
    def __init__(self, decision: RouteDecision) -> None:
        self._decision = decision
        self.bundle_path = None

    def route(self, _text: str, _mode: str, _active_behavior):
        return self._decision


class _ExecutorSpy:
    def __init__(self) -> None:
        self.calls = 0
        self._on_finish = None

    def set_on_finish(self, callback) -> None:
        self._on_finish = callback

    def execute(self, _cmd: CommandDecision) -> None:
        self.calls += 1


def _run_perf_scenario(
    *,
    metric: str,
    budget_env: str,
    budget_default: float,
    one_call,
    extra: dict[str, str],
) -> None:
    warmup, iterations = default_perf_controls()
    p95_budget_ms = perf_env_float(key=budget_env, default=budget_default)
    samples = collect_latency_ms(one_call, warmup=warmup, iterations=iterations)
    stats = record_perf_metric(
        metric=metric,
        samples=samples,
        budget_ms=p95_budget_ms,
        extra=extra,
    )
    p95 = float(stats["p95_ms"])
    assert p95 <= p95_budget_ms, f"{metric} p95={p95:.2f}ms > {p95_budget_ms:.2f}ms"


@pytest.mark.perf
def test_pipeline_run_once_empty_input_latency_budget():
    def _one_call():
        llm = _LLMSpy()
        output = _NoOpOutput()
        pipeline = InputLLMOutputPipeline(
            input_backend=_InputOnce(""),
            llm=llm,
            output_backend=output,
            cmdrec_recognizer=None,
            behavior_executor=None,
            status_to_console=False,
        )
        turn = pipeline.run_once(history=[])
        assert llm.calls == 0
        assert output.calls == 0
        assert turn.llm.reply == ""

    _run_perf_scenario(
        metric="pipeline_run_once_empty_input",
        budget_env="DM_PERF_PIPELINE_EMPTY_P95_MS",
        budget_default=4.0,
        one_call=_one_call,
        extra={"layer": "pipeline", "path": "empty_input"},
    )


@pytest.mark.perf
def test_pipeline_run_once_dialog_path_latency_budget():
    def _one_call():
        llm = _LLMSpy()
        output = _NoOpOutput()
        pipeline = InputLLMOutputPipeline(
            input_backend=_InputOnce("hallo"),
            llm=llm,
            output_backend=output,
            cmdrec_recognizer=None,
            behavior_executor=None,
            status_to_console=False,
        )
        turn = pipeline.run_once(history=[])
        assert llm.calls == 1
        assert output.calls == 1
        assert turn.llm.reply == "ok"

    _run_perf_scenario(
        metric="pipeline_run_once_dialog_path",
        budget_env="DM_PERF_PIPELINE_DIALOG_P95_MS",
        budget_default=12.0,
        one_call=_one_call,
        extra={"layer": "pipeline", "path": "dialog"},
    )


@pytest.mark.perf
def test_pipeline_run_once_command_stop_latency_budget():
    decision = RouteDecision(
        is_command=True,
        command=CommandDecision(label="STOP", confidence=0.99, raw_text="stop"),
        reason="perf_test",
        top3=[("STOP", 0.99)],
    )

    def _one_call():
        llm = _LLMSpy()
        executor = _ExecutorSpy()
        pipeline = InputLLMOutputPipeline(
            input_backend=_InputOnce("stop"),
            llm=llm,
            output_backend=_NoOpOutput(),
            cmdrec_recognizer=_Recognizer(decision),
            behavior_executor=executor,
            status_to_console=False,
        )
        turn = pipeline.run_once(history=[])
        assert llm.calls == 0
        assert executor.calls == 1
        assert turn.llm.reply == "[CMD] STOP"

    _run_perf_scenario(
        metric="pipeline_run_once_command_stop",
        budget_env="DM_PERF_PIPELINE_CMD_STOP_P95_MS",
        budget_default=10.0,
        one_call=_one_call,
        extra={"layer": "pipeline", "path": "command_stop"},
    )


@pytest.mark.perf
def test_pipeline_run_once_command_non_stop_latency_budget():
    decision = RouteDecision(
        is_command=True,
        command=CommandDecision(label="STAND_UP", confidence=0.98, raw_text="standup"),
        reason="perf_test",
        top3=[("STAND_UP", 0.98)],
    )

    def _one_call():
        llm = _LLMSpy()
        executor = _ExecutorSpy()
        pipeline = InputLLMOutputPipeline(
            input_backend=_InputOnce("standup"),
            llm=llm,
            output_backend=_NoOpOutput(),
            cmdrec_recognizer=_Recognizer(decision),
            behavior_executor=executor,
            status_to_console=False,
        )
        turn = pipeline.run_once(history=[])
        assert llm.calls == 0
        assert executor.calls == 1
        assert turn.llm.reply == "[CMD] STAND_UP"

    _run_perf_scenario(
        metric="pipeline_run_once_command_non_stop",
        budget_env="DM_PERF_PIPELINE_CMD_NON_STOP_P95_MS",
        budget_default=10.0,
        one_call=_one_call,
        extra={"layer": "pipeline", "path": "command_non_stop"},
    )


@pytest.mark.perf
def test_pipeline_run_once_command_without_executor_latency_budget():
    decision = RouteDecision(
        is_command=True,
        command=CommandDecision(label="DANCE", confidence=0.97, raw_text="doe een dans"),
        reason="perf_test",
        top3=[("DANCE", 0.97)],
    )

    def _one_call():
        llm = _LLMSpy()
        pipeline = InputLLMOutputPipeline(
            input_backend=_InputOnce("doe een dans"),
            llm=llm,
            output_backend=_NoOpOutput(),
            cmdrec_recognizer=_Recognizer(decision),
            behavior_executor=None,
            status_to_console=False,
        )
        turn = pipeline.run_once(history=[])
        assert llm.calls == 0
        assert turn.llm.reply == "[CMD] DANCE"

    _run_perf_scenario(
        metric="pipeline_run_once_command_no_executor",
        budget_env="DM_PERF_PIPELINE_CMD_NO_EXECUTOR_P95_MS",
        budget_default=10.0,
        one_call=_one_call,
        extra={"layer": "pipeline", "path": "command_no_executor"},
    )
