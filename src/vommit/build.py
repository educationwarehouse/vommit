"""
Whether the configured build command will actually build this project.

A release runs `clean` and `build` *after* the bump, so a build that cannot
work is found out once the version has been written, the changelog rewritten
and the commit tagged. That is recoverable with `vommit bump --undo`, but only
after the fact. These checks run at `setup` instead, where the answer costs nothing.

They warn rather than refuse. The build command is a shell string and the ways
to make one work are open-ended; the only thing worth saying with confidence is
that a specific, verified misconfiguration is present.
"""

import typing as t
from pathlib import Path

from .helpers import read_toml
from .migrate import normalise_name, project_name

PYPROJECT = "pyproject.toml"
UV_BUILD = "uv build"

#: Where the uv build backend looks when the project does not say otherwise.
DEFAULT_MODULE_ROOT = "src"


def build_warnings(root: Path, build_command: str) -> list[str]:
    """
    What is wrong with `build_command` for the project at `root`, if anything.

    Only commands that run `uv build` are examined: a project that builds itself
    some other way has already opted out of the assumptions checked here.
    """
    pyproject = root / PYPROJECT
    if UV_BUILD not in build_command or not pyproject.exists():
        return []

    document = read_toml(pyproject)
    backend = _backend(document)

    if backend == "uv_build":
        return _uv_build_warnings(root, pyproject, document)
    if backend.split(".")[0] == "maturin":
        return [
            (
                f"`{UV_BUILD}` builds a maturin project for this machine only, "
                "so the release would publish a single wheel for the platform "
                "it ran on. Projects that ship several targets set "
                "`commands.build` to their own `maturin build --target ...` "
                "script instead."
            )
        ]
    return []


def _uv_build_warnings(root: Path, pyproject: Path, document: t.Any) -> list[str]:
    """
    The one uv_build misconfiguration that is certain: no module where it looks.

    uv_build derives the module path from the distribution name, so renaming
    `[project].name` without renaming the directory (or keeping a flat layout,
    or a single-file module) breaks the build with nothing else changing.
    """
    settings = _table(document, "tool", "uv", "build-backend")

    # a namespace package is spelled as a dotted module name across directories
    # that deliberately have no `__init__.py`; the check below does not apply
    if settings.get("namespace"):
        return []

    module_name = str(settings.get("module-name") or _default_module_name(pyproject))
    if not module_name or "." in module_name:
        return []

    module_root = str(settings.get("module-root", DEFAULT_MODULE_ROOT))
    expected = Path(module_root) / module_name / "__init__.py"
    if (root / expected).is_file():
        return []

    return [
        (
            f"`{UV_BUILD}` will fail: the uv build backend expects a Python "
            f"module at {expected.as_posix()}, which does not exist. Create "
            "it, or point the backend at the real one with `module-name` / "
            "`module-root` under [tool.uv.build-backend]."
        )
    ]


def _default_module_name(pyproject: Path) -> str:
    name = project_name(pyproject)
    return normalise_name(name).replace("-", "_") if name else ""


def _backend(document: t.Any) -> str:
    build_system = document.get("build-system")
    if not isinstance(build_system, dict):
        return ""
    return str(build_system.get("build-backend", ""))


def _table(document: t.Any, *keys: str) -> dict[str, t.Any]:
    current: t.Any = document
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return {}
        current = current[key]
    return current if isinstance(current, dict) else {}
