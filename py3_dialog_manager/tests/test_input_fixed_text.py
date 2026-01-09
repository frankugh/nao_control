from dialog.backends.input_fixed_text import FixedTextInputBackend


def test_fixed_text_backend_once():
    b = FixedTextInputBackend("hello")

    first = b.get_input()
    assert first.raw_text == "hello"
    assert first.text == "hello"

    second = b.get_input()
    assert second.raw_text == ""
    assert second.text == ""


