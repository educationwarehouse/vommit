"""
What an answer cleared down to nothing means in `setup`.

The prompt is pre-filled with the default, so an empty answer is someone
deleting it on purpose. "Empty to skip" has to come out empty, not as the
default it replaced.
"""

import typing as t

import pytest

from src.vommit import interactive
from src.vommit.config import CommandConfig, GitConfig


class Answer:
    def __init__(self, text: str) -> None:
        self.text = text

    def unsafe_ask(self) -> str:
        return self.text


@pytest.fixture
def answer(monkeypatch) -> t.Callable[[str], None]:
    def give(text: str) -> None:
        monkeypatch.setattr(
            interactive.questionary, "text", lambda *_, **__: Answer(text)
        )

    return give


def ask(field_type, default, allow_empty=False):
    return interactive._ask_for_value(
        "field", "Field", field_type, True, default, 1, 1, allow_empty=allow_empty
    )


@pytest.mark.parametrize("cleared", ["", "   "])
def test_a_command_cleared_to_nothing_is_skipped(answer, cleared):
    answer(cleared)
    assert ask(str, "uv build", allow_empty=True) == ""


def test_an_optional_format_cleared_to_nothing_is_switched_off(answer):
    # "", not None: None is never written, so the default would come back
    answer("")
    assert ask(str | None, "v{version}") == ""


def test_a_required_field_cleared_to_nothing_keeps_its_default(answer):
    answer("")
    assert ask(str, "origin") == "origin"


def test_a_required_field_without_a_default_is_refused(answer):
    answer("")
    with pytest.raises(ValueError, match="required"):
        interactive._ask_for_value("field", "Field", str, False, None, 1, 1)


def test_an_answer_is_taken_as_given(answer):
    answer(" cargo package ")
    assert ask(str, "uv build", allow_empty=True) == "cargo package"


@pytest.mark.parametrize("name", ["clean", "build", "publish", "post_publish"])
def test_every_command_can_be_left_empty(name):
    _, meta = interactive._unwrap_annotated(
        t.get_type_hints(CommandConfig, include_extras=True)[name]
    )
    assert meta.get("allow_empty") is True
    assert "empty to skip" in str(meta.get("label"))


@pytest.mark.parametrize("name", ["origin", "branch"])
def test_git_settings_that_cannot_be_empty_are_not_marked_so(name):
    _, meta = interactive._unwrap_annotated(
        t.get_type_hints(GitConfig, include_extras=True)[name]
    )
    assert not meta.get("allow_empty")
