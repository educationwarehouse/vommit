"""
Handing the changelog entry to the user's editor, and taking it back.

The generated entry is a starting point, not a verdict: a release sometimes
needs a sentence that no commit message contains. Editing happens before
anything is written, so the hand-written text lands in the same release commit
as the version bump, exactly as the generated one would have.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import typing as t
from pathlib import Path

from .errors import VommitError
from .shell import Runner

#: What the user is handed and what comes back; the callable `run_bump` takes.
EntryEditor = t.Callable[[str], str]

#: Editors that return immediately unless told to wait, leaving the entry as it
#: was generated without anyone noticing they were asked.
DETACHING = frozenset({"code", "code-insiders", "codium", "subl", "sublime_text"})
WAITING = frozenset({"--wait", "-w", "-nw"})

#: Last resort when neither git nor the environment names one.
FALLBACKS = ("sensible-editor", "nano", "vi")

INSTRUCTIONS = """<!--
Write the changelog entry above, then save and close.
Everything in an HTML comment is removed; an empty entry cancels the release.
-->
"""

#: Every HTML comment, the way `git commit` drops every `#` line rather than
#: only the ones it wrote itself: an instruction someone edited, reflowed or
#: half-deleted is still an instruction, and must not reach the changelog.
#: An unclosed comment runs to the end, exactly as a renderer would read it.
_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)\n?", re.DOTALL)


def resolve_editor(runner: Runner) -> str:
    """
    The editor command, in git's own order of precedence.

    `git var GIT_EDITOR` already resolves `$GIT_EDITOR`, `core.editor`,
    `$VISUAL`, `$EDITOR` and git's compiled default, so asking git is both
    shorter and more faithful than reimplementing the chain. It is asked
    first and its answer is only used when it is a non-empty one: git may be
    absent entirely, and `core.editor = ""` is a configured value that
    resolves to nothing, which is not an answer either.
    """
    for candidate in (
        _git_editor(runner),
        os.environ.get("VISUAL"),
        os.environ.get("EDITOR"),
    ):
        if candidate and candidate.strip():
            return candidate.strip()

    for fallback in FALLBACKS:
        if shutil.which(fallback):
            return fallback

    raise VommitError(
        "No editor to open the changelog entry in. Set $EDITOR, or configure "
        "one for git with `git config --global core.editor <editor>`."
    )


def _git_editor(runner: Runner) -> str:
    result = runner.run(shlex.join(["git", "var", "GIT_EDITOR"]))
    return result.out if result.ok else ""


def detaching(editor: str) -> bool:
    """
    Whether this editor hands control straight back instead of waiting.
    """
    parts = shlex.split(editor) or [editor]
    program = Path(parts[0]).name
    return program in DETACHING and not (set(parts[1:]) & WAITING)


def edit_text(
    text: str,
    runner: Runner,
    notify: t.Callable[[str], None] = lambda _: None,
    filename: str = "VOMMIT_ENTRY.md",
) -> str:
    """
    Open `text` in the user's editor and return what they saved.

    The file lives in a temporary directory rather than the project: a release
    refuses to build a tree with anything uncommitted in it, so a scratch file
    in the repository would hold up the very release it belongs to.
    """
    if not sys.stdin.isatty():
        raise VommitError(
            "Editing the changelog entry needs a terminal, and there is none "
            "here. Drop --edit to use the generated entry as it stands."
        )

    editor = resolve_editor(runner)
    if detaching(editor):
        notify(
            f"'{editor}' returns before the file is saved; add --wait to it if "
            "the entry comes back unchanged."
        )

    with tempfile.TemporaryDirectory(prefix="vommit-") as directory:
        path = Path(directory) / filename
        path.write_text(text)
        # deliberately not through `Runner`: both implementations capture
        # output, and an editor with no terminal of its own hangs or garbles.
        completed = subprocess.run(
            f"{editor} {shlex.quote(str(path))}", shell=True, check=False
        )
        if completed.returncode != 0:
            raise VommitError(
                f"'{editor}' exited with {completed.returncode}; nothing was written."
            )
        return path.read_text()


def strip_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text)


def edit_entry(
    entry: str,
    runner: Runner,
    notify: t.Callable[[str], None] = lambda _: None,
) -> str:
    """
    The generated entry, as the user rewrote it.

    The entry comes first and the instructions below it, as in `COMMIT_EDITMSG`:
    the cursor lands on the text being written, and a comment someone leaves
    unclosed swallows the instructions rather than the entry.

    An entry saved empty cancels the release, the way an empty message cancels
    a commit: it is the one edit that cannot have been meant literally.
    """
    edited = strip_comments(edit_text(f"{entry}\n\n{INSTRUCTIONS}", runner, notify))
    if not edited.strip():
        raise VommitError("The changelog entry was left empty; nothing was written.")
    return edited.strip("\n")
