from dialog.interfaces import CommandDecision, LLMResult, RouteDecision, UserInput
from dialog.pipeline import InputLLMOutputPipeline


class StubInputBackend:
    def __init__(self, text: str) -> None:
        self._text = text
        self._used = False

    def get_input(self) -> UserInput:
        if self._used:
            return UserInput(raw_text="", text="")
        self._used = True
        return UserInput(raw_text=self._text, text=self._text)


class StubLLMBackend:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, messages):
        self.calls += 1
        return LLMResult(reply="ok", messages=list(messages))


class StaticReplyLLMBackend:
    def __init__(self, reply: str) -> None:
        self.reply = str(reply)
        self.calls = 0

    def generate(self, messages):
        self.calls += 1
        return LLMResult(reply=self.reply, messages=list(messages))


class StubOutputBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def emit(self, text: str) -> None:
        self.calls.append(text)


class StubRecognizer:
    def __init__(self, decision: RouteDecision) -> None:
        self._decision = decision
        self.bundle_path = None

    def route(self, text: str, mode: str, active_behavior):
        return self._decision


class StubExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.last = None

    def execute(self, cmd: CommandDecision) -> None:
        self.calls += 1
        self.last = cmd


def test_cmdrec_disabled_uses_llm_path() -> None:
    llm = StubLLMBackend()
    pipeline = InputLLMOutputPipeline(
        input_backend=StubInputBackend("hello"),
        llm=llm,
        output_backend=StubOutputBackend(),
        cmdrec_recognizer=None,
        behavior_executor=None,
    )

    turn = pipeline.run_once(history=[])
    assert turn.llm.reply == "ok"
    assert llm.calls == 1


def test_cmdrec_command_skips_llm_and_executes() -> None:
    llm = StubLLMBackend()
    executor = StubExecutor()
    decision = RouteDecision(
        is_command=True,
        command=CommandDecision(label="DANCE", confidence=0.9, raw_text="dans"),
        reason=None,
        top3=[("DANCE", 0.9)],
    )
    pipeline = InputLLMOutputPipeline(
        input_backend=StubInputBackend("dans"),
        llm=llm,
        output_backend=StubOutputBackend(),
        cmdrec_recognizer=StubRecognizer(decision),
        behavior_executor=executor,
    )

    turn = pipeline.run_once(history=[])
    assert llm.calls == 0
    assert executor.calls == 1
    assert turn.llm.reply == "[CMD] DANCE"


def test_pipeline_normalizes_markdown_and_status_before_emit() -> None:
    raw_reply = "Uitgevoerd: STAND\\_UP\n\nIk kan de *robotdans* of *disco* doen."
    llm = StaticReplyLLMBackend(raw_reply)
    output = StubOutputBackend()
    pipeline = InputLLMOutputPipeline(
        input_backend=StubInputBackend("wat kan je"),
        llm=llm,
        output_backend=output,
        cmdrec_recognizer=None,
        behavior_executor=None,
    )

    turn = pipeline.run_once(history=[])
    assert turn.llm.reply == raw_reply
    assert output.calls == ["Ik kan de robotdans of disco doen."]


def test_pipeline_skips_emit_for_status_only_reply() -> None:
    llm = StaticReplyLLMBackend("Uitgevoerd: STAND\\_UP")
    output = StubOutputBackend()
    pipeline = InputLLMOutputPipeline(
        input_backend=StubInputBackend("ok"),
        llm=llm,
        output_backend=output,
        cmdrec_recognizer=None,
        behavior_executor=None,
    )

    turn = pipeline.run_once(history=[])
    assert turn.llm.reply == "Uitgevoerd: STAND\\_UP"
    assert output.calls == []


def test_pipeline_strips_emojis_before_emit_only() -> None:
    raw_reply = "Top 😄! Ik doe een *happy dance* 🤖."
    llm = StaticReplyLLMBackend(raw_reply)
    output = StubOutputBackend()
    pipeline = InputLLMOutputPipeline(
        input_backend=StubInputBackend("doe iets leuks"),
        llm=llm,
        output_backend=output,
        cmdrec_recognizer=None,
        behavior_executor=None,
    )

    turn = pipeline.run_once(history=[])
    assert turn.llm.reply == raw_reply
    assert output.calls == ["Top! Ik doe een happy dance."]
