import shlex
import sys

import pytest

from src.vommit.editor import (
    INSTRUCTIONS,
    detaching,
    edit_entry,
    edit_text,
    resolve_editor,
    strip_comments,
)
from src.vommit.errors import VommitError

from .conftest import FakeRunner

ENTRY = "## v1.2.0 (2023-04-10)\n\n### Features\n* something new"


@pytest.fixture
def terminal(monkeypatch):
    """
    A terminal to ask on; pytest replaces stdin with something that is not one.
    """
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


@pytest.fixture
def no_environment_editor(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)


def git_says(editor: str) -> FakeRunner:
    return FakeRunner().reply("git var GIT_EDITOR", stdout=editor)


def script(body: str) -> str:
    """
    An 'editor' that runs `body` with $1 set to the file it is handed.
    """
    return f"bash -c {shlex.quote(body)} _"


def test_git_answers_first(monkeypatch):
    monkeypatch.setenv("EDITOR", "nano")

    assert resolve_editor(git_says("vim")) == "vim"


def test_an_empty_core_editor_is_not_an_answer(monkeypatch, no_environment_editor):
    monkeypatch.setenv("VISUAL", "kak")

    # `core.editor = ""` resolves to nothing, and so does git being absent
    assert resolve_editor(git_says("  \n")) == "kak"
    assert resolve_editor(FakeRunner().reply("git var", returncode=1)) == "kak"


def test_visual_outranks_editor(monkeypatch, no_environment_editor):
    monkeypatch.setenv("EDITOR", "nano")
    assert resolve_editor(git_says("")) == "nano"

    monkeypatch.setenv("VISUAL", "kak")
    assert resolve_editor(git_says("")) == "kak"


def test_falls_back_to_an_installed_editor(
    monkeypatch, no_environment_editor, tmp_path
):
    installed = tmp_path / "nano"
    installed.write_text("#!/bin/sh\nexit 0\n")
    installed.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert resolve_editor(git_says("")) == "nano"


def test_without_any_editor_it_says_how_to_set_one(
    monkeypatch, no_environment_editor, tmp_path
):
    # an empty PATH: nothing named, nothing installed to fall back on either
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(VommitError, match=r"\$EDITOR"):
        resolve_editor(git_says(""))


@pytest.mark.parametrize(
    "editor, expected",
    [
        ("code", True),
        ("/usr/bin/code", True),
        ("code --wait", False),
        ("subl -w", False),
        ("vim", False),
        ("nano", False),
    ],
)
def test_detaching(editor, expected):
    assert detaching(editor) is expected


def test_a_detaching_editor_is_warned_about(terminal, tmp_path):
    said: list[str] = []
    # named after the editor it stands in for: that name is the whole signal
    fake = tmp_path / "code"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)

    edit_text("text", git_says(script(":")), said.append)
    assert said == []

    edit_text("text", git_says(str(fake)), said.append)
    assert said and "--wait" in said[0]


def test_the_edited_text_comes_back(terminal):
    editor = git_says(script('echo "* by hand" >> "$1"'))

    assert edit_text("first\n", editor).splitlines() == ["first", "* by hand"]


def test_editing_needs_a_terminal(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with pytest.raises(VommitError, match="terminal"):
        edit_text("text", git_says(script(":")))


def test_an_editor_that_fails_writes_nothing(terminal):
    with pytest.raises(VommitError, match="exited with 1"):
        edit_text("text", git_says("false"))


def test_every_comment_is_stripped_not_only_the_one_we_wrote():
    assert strip_comments(f"{ENTRY}\n\n{INSTRUCTIONS}").strip() == ENTRY
    assert strip_comments(f"{INSTRUCTIONS}\n{ENTRY}\n").strip() == ENTRY

    # rewritten, reflowed, added to: an instruction is an instruction
    rewritten = "<!-- note to self:\nfinish this before Friday\n-->"
    assert strip_comments(f"{ENTRY}\n\n{rewritten}\n").strip() == ENTRY


def test_an_unclosed_comment_runs_to_the_end():
    # what a markdown renderer would do with it, and the entry is above it
    mangled = INSTRUCTIONS.replace("-->", "")
    assert strip_comments(f"{ENTRY}\n\n{mangled}").strip() == ENTRY


def test_the_entry_is_handed_over_above_the_instructions(terminal, tmp_path):
    seen = tmp_path / "seen.md"
    editor = git_says(script(f'cp "$1" {shlex.quote(str(seen))}'))

    assert edit_entry(ENTRY, editor) == ENTRY

    handed = seen.read_text()
    # the entry first, as in COMMIT_EDITMSG: that is what is being written
    assert handed.startswith(ENTRY)
    assert handed.rstrip().endswith("-->")


def test_an_empty_entry_cancels_the_release(terminal):
    with pytest.raises(VommitError, match="left empty"):
        edit_entry(ENTRY, git_says(script(': > "$1"')))


def test_an_entry_of_comments_only_counts_as_empty(terminal):
    # saving without writing anything is not a hand-written entry
    editor = git_says(script(f'printf %s {shlex.quote(INSTRUCTIONS)} > "$1"'))

    with pytest.raises(VommitError, match="left empty"):
        edit_entry(ENTRY, editor)
