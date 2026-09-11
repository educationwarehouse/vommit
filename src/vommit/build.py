"""
Whether this project's build configuration will produce the release we intend.

Two kinds of question, reported together:

- *will the configured command build this project at all?* This covers the
  module uv_build looks for and the maturin target it would silently narrow to;
- *is the project on the build backend vommit releases with?* This covers
  Hatchling instead of uv_build, or a uv_build pin outside the range vommit is
  tested against.

A release runs `clean` and `build` *after* the bump, so a build that cannot
work is found out once the version has been written, the changelog rewritten
and the commit tagged. That is recoverable with `vommit bump --undo`, but only
after the fact. These checks run at `setup` instead, where the answer costs
nothing, and again (report-only) at the head of a release.

They warn rather than refuse. The build command is a shell string and the ways
to make one work are open-ended; the only thing worth saying with confidence is
that a specific, verified misconfiguration is present. Each warning carries a
stable id, which is what a project puts in `ignore` to stop hearing it, and,
where the change is mechanical and cannot lose information, a `fix` that writes
it.
"""

import dataclasses as dc
import json
import shutil
import typing as t
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from .config import CommandConfig, Config
from .errors import VommitError
from .helpers import read_toml
from .migrate import normalise_name, project_name
from .shell import Runner
from .versioning import NPM

PYPROJECT = "pyproject.toml"
UV_BUILD = "uv build"
UV_PUBLISH = "uv publish"
NPM_BUILD = "npm run build"
NPM_PUBLISH = "npm publish"
BUN_BUILD = "bun run build"
BUN_PUBLISH = "bun publish"
HATCHLING = "hatchling.build"

#: Where the uv build backend looks when the project does not say otherwise.
DEFAULT_MODULE_ROOT = "src"

#: The uv_build range vommit's releases are built with. A project outside it is
#: not broken; it is on a backend version nothing here has been run against.
RECOMMENDED_UV_BUILD_MINIMUM = "0.12.4"
RECOMMENDED_UV_BUILD_MAXIMUM = "0.13"
RECOMMENDED_UV_BUILD_SPECIFIER = SpecifierSet(
    f">={RECOMMENDED_UV_BUILD_MINIMUM},<{RECOMMENDED_UV_BUILD_MAXIMUM}"
)
RECOMMENDED_UV_BUILD_REQUIREMENT = (
    f"uv_build>={RECOMMENDED_UV_BUILD_MINIMUM},<{RECOMMENDED_UV_BUILD_MAXIMUM}"
)

#: The build commands a `hatch build` project can be moved off mechanically:
#: anything else is a script someone wrote on purpose, and rewriting it would
#: throw away whatever it does besides building.
HATCH_BUILD_COMMANDS = {"hatch build", "hatch build -c", "hatch build --clean"}
HATCH_PUBLISH_COMMANDS = {"hatch publish"}

#: Every id a warning here can carry. What `ignore` silences and what `setup
#: --fix` names; kept in one place so both can say which ids exist rather than
#: accepting a typo as a request nothing matches.
WARNING_IDS = frozenset(
    (
        "hatchling-backend",
        "uv-build-pin",
        "hatch-build-command",
        "maturin-uv-build",
        "uv-build-module-missing",
    )
)

#: The subset carrying a `fix`, where the project's shape allows it. The other
#: ids describe something with no mechanical answer, so `--fix` cannot promise
#: them anything.
FIXABLE_WARNING_IDS = frozenset(
    ("hatchling-backend", "uv-build-pin", "hatch-build-command")
)

#: `--fix` shorthand for every id in `FIXABLE_WARNING_IDS`.
FIX_ALL = "all"


def requested_fixes(fix: str | None) -> set[str]:
    """
    The warning ids a `--fix` flag asked for, refusing anything that is not one.

    A typo is an error rather than a silent no-op: the flag exists so that an
    unattended run changes something, and a run that changed nothing because of
    a misspelling looks exactly like a project with nothing to fix. An id that
    is real but carries no fix is refused for the same reason.
    """
    if fix is None:
        return set()

    asked = {part.strip() for part in fix.split(",") if part.strip()}
    if not asked:
        raise VommitError(f"--fix needs a warning id, or '{FIX_ALL}'.")
    if FIX_ALL in asked:
        return set(FIXABLE_WARNING_IDS)

    if unknown := sorted(asked - WARNING_IDS):
        raise VommitError(
            f"Not a build warning: {', '.join(unknown)}. "
            f"Fixable ids are {', '.join(sorted(FIXABLE_WARNING_IDS))}."
        )
    if unfixable := sorted(asked - FIXABLE_WARNING_IDS):
        raise VommitError(
            f"No fix to apply for {', '.join(unfixable)}: the warning names what "
            "is in the way instead of offering a change. "
            f"Fixable ids are {', '.join(sorted(FIXABLE_WARNING_IDS))}."
        )
    return asked


@dc.dataclass(frozen=True)
class Fix:
    """
    A change small enough to offer as a question, and to apply on a yes.

    `apply` re-reads the file it edits: `setup` writes the config, applies
    whichever fixes were accepted and may write the config again, so a document
    captured when the warning was produced would undo the write before it.
    """

    question: str
    done: str
    apply: t.Callable[[], None]


@dc.dataclass(frozen=True)
class ProjectWarning:
    id: str
    message: str
    fix: Fix | None = None


def _silence(warning_id: str) -> str:
    return f'Add "{warning_id}" to `ignore` under [tool.vommit] to stop reporting it.'


def project_warnings(root: Path, config: Config) -> list[ProjectWarning]:
    """
    Everything wrong with this project's build configuration, minus what it has
    chosen to stop hearing about.
    """
    ignored = set(config.ignore)
    found = [*backend_warnings(root), *command_warnings(root, config)]
    return [warning for warning in found if warning.id not in ignored]


def suggested_commands(root: Path) -> CommandConfig | None:
    """
    JS-flavored release commands to offer at `setup`, for a project whose
    version lives in `package.json` rather than `pyproject.toml` (see
    `NpmProject`). Only the caller knows that -- it has the resolved
    `VersionSource` already -- so this answers for a project it has been told
    is one, and returns None when `package.json` turns out not to be readable
    after all.

    `build` is suggested only when `package.json` itself declares a "build"
    script. Plenty of npm packages have no build step at all (a plain `.js`
    file, say), and suggesting one that fails on every fresh setup would be
    worse than suggesting none: `clean = ""`/`build = ""` is `CommandConfig`'s
    own way of saying a step does not apply here.

    Raises `VommitError` when neither `npm` nor `bun` is on PATH: a publish
    command that cannot possibly run is a worse default than none, and a
    silent one would go unnoticed until an actual release failed on it.
    """
    if not (root / NPM).exists():  # pragma: no cover - backstop, not a path
        return None

    build_command, publish_command = _npm_or_bun_commands()

    commands = CommandConfig()
    commands.build = build_command if _has_npm_build_script(root) else ""
    commands.publish = publish_command
    return commands


def _npm_or_bun_binary() -> str:
    """
    Which JS package manager to run commands with: `bun` or `npm`.

    bun is checked first: installing npm (meaning, in practice, Node) can add
    hundreds of megabytes a project might otherwise have no use for, so a
    machine that already has bun and nothing else should be offered `bun`
    commands rather than told to install npm for one it did not ask to run
    yet. Raises `VommitError` when neither is on PATH.
    """
    if shutil.which("bun"):
        return "bun"
    if shutil.which("npm"):
        return "npm"
    raise VommitError(
        "Neither `npm` nor `bun` is on PATH. Install one of them, set "
        "`commands.publish` (and `commands.build`, if needed) yourself, or "
        "authenticate later once one is installed."
    )


def _npm_or_bun_commands() -> tuple[str, str]:
    """
    (build, publish) for whichever JS package manager is actually installed.
    """
    if _npm_or_bun_binary() == "bun":
        return BUN_BUILD, BUN_PUBLISH
    return NPM_BUILD, NPM_PUBLISH


def npm_whoami_command() -> str:
    """
    The read-only command that confirms a configured npm/bun credential
    authenticates at all, without publishing anything.

    There is no npm/bun equivalent of `auth.verify_token`'s trick (posting a
    deliberately incomplete upload so the index authenticates it before
    finding it wanting): `whoami` is the closest safe, read-only substitute.
    It can only confirm the credential is valid, never that it also carries
    the "bypass 2FA" exemption a publish specifically needs; see
    `npm_token_instructions` for why that is unknowable from here at all.
    """
    binary = _npm_or_bun_binary()
    return f"{binary} pm whoami" if binary == "bun" else f"{binary} whoami"


#: Where npm and bun both read (and, together with the registry line below,
#: write) a publish credential. A project-local `.npmrc` would also work, but
#: is more likely to end up committed with a live token in it by accident, so
#: `setup` only ever offers the one in the home directory.
NPMRC = Path.home() / ".npmrc"
NPM_REGISTRY = "registry.npmjs.org"


def has_npm_auth_token(npmrc: Path = NPMRC, registry: str = NPM_REGISTRY) -> bool:
    """
    Whether `npmrc` already configures a publish credential for `registry`.

    Only recognises the `_authToken` form, the one an Automation or a
    Granular Access Token uses and the one `setup` offers to write. A project
    authenticating some other way (`_auth`, a username/password pair, an
    environment variable set outside this file entirely) is not detected
    here and gets asked again, which is a repeated nag, not a correctness
    problem: nothing here is ever a precondition for publishing to succeed,
    only for whether `setup` mentions it.
    """
    if not npmrc.exists():
        return False
    needle = f"//{registry}/:_authToken="
    return any(
        line.strip().startswith(needle) for line in npmrc.read_text().splitlines()
    )


def write_npm_auth_token(
    token: str, npmrc: Path = NPMRC, registry: str = NPM_REGISTRY
) -> None:
    """
    Stores `token` as `npmrc`'s `_authToken` line for `registry`, replacing
    one already there rather than adding a second, conflicting line for the
    same registry (which is what a plain append would do on every
    re-authentication after the first).
    """
    needle = f"//{registry}/:_authToken="
    line = f"{needle}{token}\n"
    if not npmrc.exists():
        # created before it is written, so the token is never briefly readable
        # at whatever the umask allows; npm writes this file 0600 for the same
        # reason. An `npmrc` that already exists keeps the mode it has: its
        # owner may have widened it deliberately, and this only adds a line.
        npmrc.touch(mode=0o600)
        npmrc.write_text(line)
        return

    lines = npmrc.read_text().splitlines(keepends=True)
    for index, existing in enumerate(lines):
        if existing.strip().startswith(needle):
            lines[index] = line
            npmrc.write_text("".join(lines))
            return

    text = "".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    npmrc.write_text(text + line)


def clear_npm_auth_token(npmrc: Path = NPMRC, registry: str = NPM_REGISTRY) -> bool:
    """
    Removes `npmrc`'s `_authToken` line for `registry`, if there is one.

    Returns whether a line was actually removed, the way `TokenStore.forget`
    reports the same thing for the PyPI keyring, so `authenticate` can say
    truthfully whether there was anything to clear.
    """
    if not npmrc.exists():
        return False
    needle = f"//{registry}/:_authToken="
    lines = npmrc.read_text().splitlines(keepends=True)
    kept = [line for line in lines if not line.strip().startswith(needle)]
    if len(kept) == len(lines):
        return False
    npmrc.write_text("".join(kept))
    return True


def npm_token_env(token: str, registry: str = NPM_REGISTRY) -> dict[str, str]:
    """
    The environment that makes npm *or* bun authenticate as `token`, without
    either of them reading `npmrc` for it.

    Both are set because the two managers read exactly one each, and not the
    same one (measured against npm 10.9.7 and bun 1.2):

    - npm ignores `NPM_CONFIG_TOKEN` outright. Its env override is the
      registry-scoped config key, `npm_config_//<registry>/:_authToken`, odd as
      that looks as a variable name.
    - bun is the mirror image: it reads `NPM_CONFIG_TOKEN` and ignores the
      scoped key.

    Getting this wrong is worse than not checking at all, which is why it is
    spelled out here. Setting only `NPM_CONFIG_TOKEN` leaves npm falling back
    to `npmrc`, so a mistyped token "verifies" against the credential already
    on disk and then overwrites it -- the exact outcome `verify_npm_token`
    exists to prevent.
    """
    return {
        "NPM_CONFIG_TOKEN": token,
        f"npm_config_//{registry}/:_authToken": token,
    }


def verify_npm_token(token: str, runner: Runner) -> None:
    """
    Confirms `token` actually authenticates, before it is ever written
    anywhere: checked through the environment (see `npm_token_env`) rather
    than `npmrc`, the same shape `auth.verify_token` checks a PyPI token
    before `authenticate` stores it, so a rejected token leaves whatever was
    already in `npmrc` untouched rather than overwriting it with one that
    does not work. See `npm_whoami_command` for what this can and cannot
    actually confirm.
    """
    command = npm_whoami_command()
    result = runner.run(command, env=npm_token_env(token))
    if not result.ok:
        raise VommitError(
            f"`{command}` failed: {result.error}\nThis token does not seem "
            "to work; check it and try again."
        )


def npm_token_advice() -> str:
    """
    Where to get a non-interactive-safe npm/bun publish credential, and what
    to set on it when creating one. The part every context asking for a
    token can show, whether or not it also has to say where the token then
    goes (see `npm_token_instructions`, for a context that does).

    Most packages on the npm registry require two-factor authentication for
    publishing by default, which ordinarily means a live browser click on
    every single release; neither this nor any other automation can satisfy
    that, because it is specifically designed not to be satisfiable by a
    stored credential. An Automation Token, or a Granular Access Token with
    "Bypass two-factor authentication" enabled, is the documented exception:
    npm treats either as already having proven who is publishing, so it never
    asks for a one-time password from a release authenticating with one.

    Crucially, that exemption is a property of how the token was created, not
    of whether `npmrc` has an `_authToken` line at all: the credential
    `npm login`/`bunx npm login` produces is a regular one, and does *not*
    get the exemption, so pasting it here still gets asked for an OTP on
    every publish. `has_npm_auth_token` cannot tell the two apart, because
    npm does not expose a token's own bypass setting anywhere client-side;
    only the token's issuer (npmjs.com) knows that, so it can only ever
    detect "some token is here", not "the right kind is here". Worth stating
    plainly here rather than let that silence be read as "this is handled".
    """
    return (
        "`npm publish`/`bun publish` will ask for a live one-time password "
        "on every release if the package requires 2FA to publish, which is "
        "the default. An Automation Token, or a Granular Access Token with "
        '"Bypass two-factor authentication" enabled, is exempt from that. '
        "Generate one at https://www.npmjs.com/settings/~/tokens (checking "
        "that box explicitly). A token from `npm login`/`bunx npm login` "
        "does NOT count here: it does not carry that exemption, so it will "
        "still be asked for an OTP on every publish."
    )


def npm_token_instructions(npmrc: Path = NPMRC, registry: str = NPM_REGISTRY) -> str:
    """
    What to tell someone about configuring a non-interactive npm/bun publish
    credential, for a project whose version lives in `package.json`, when
    they also have to be told where the token itself then goes (`setup`'s
    context: it only ever suggests this, it does not write the file itself).
    See `npm_token_advice` for a context that writes it and only needs the
    first half of this message.
    """
    return (
        f"No npm publish credential found. {npm_token_advice()}\nAdd\n"
        f"    //{registry}/:_authToken=<token>\n"
        f"to {npmrc}."
    )


def _has_npm_build_script(root: Path) -> bool:
    package_json = root / NPM
    if not package_json.exists():  # pragma: no cover - backstop, not a path
        # unreachable through `suggested_commands`, its only caller: that
        # checks for this exact file before it gets here
        return False
    try:
        data = json.loads(package_json.read_text())
    except json.JSONDecodeError:  # pragma: no cover - backstop, not a path
        # a manifest nothing can read states no build script either
        return False
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return isinstance(scripts, dict) and bool(str(scripts.get("build", "")).strip())


def command_warnings(root: Path, config: Config) -> list[ProjectWarning]:
    """
    What the configured release commands say about this project.
    """
    commands = config.commands
    if commands is None:
        return []

    warnings = build_warnings(root, commands.build)
    if hatch := _hatch_command_warning(root, config):
        warnings.append(hatch)
    return warnings


def build_warnings(root: Path, build_command: str) -> list[ProjectWarning]:
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
            ProjectWarning(
                id="maturin-uv-build",
                message=(
                    f"`{UV_BUILD}` builds a maturin project for this machine only, "
                    "so the release would publish a single wheel for the platform "
                    "it ran on. Projects that ship several targets set "
                    "`commands.build` to their own `maturin build --target ...` "
                    f"script instead. {_silence('maturin-uv-build')}"
                ),
            )
        ]
    return []


def backend_warnings(root: Path) -> list[ProjectWarning]:
    """
    What the declared build backend says, regardless of how the build is run.

    Unlike `build_warnings` these are about the shape of the project rather than
    a command that would fail: nothing here stops a release, so a project that
    disagrees silences the id and moves on.
    """
    pyproject = root / PYPROJECT
    if not pyproject.exists():
        return []

    document = read_toml(pyproject)
    backend = _backend(document)

    if backend == HATCHLING:
        return [_hatchling_warning(root, pyproject, document)]
    if backend == "uv_build":
        return [
            warning
            for warning in [_uv_build_pin_warning(pyproject, document)]
            if warning
        ]
    return []


def _hatchling_warning(
    root: Path,
    pyproject: Path,
    document: t.Any,
) -> ProjectWarning:
    """
    Hatchling instead of uv_build, with an offer to swap when nothing is lost.

    The swap is two keys, but only for a project whose artifact is described by
    nothing but those keys. Anything Hatchling-specific, such as a build target,
    a dynamic version, or an extra build requirement, has no uv_build equivalent
    to translate into, so the blockers are named instead of guessed at.
    """
    warning_id = "hatchling-backend"
    head = (
        "This project builds with the Hatchling backend, while vommit builds and "
        "publishes with uv. Migrating to uv_build puts the release on the backend "
        "vommit is tested against."
    )

    blockers = _hatchling_swap_blockers(root, pyproject, document)
    if blockers:
        listed = "; ".join(blockers)
        return ProjectWarning(
            id=warning_id,
            message=(
                f"{head} That cannot be done mechanically here: {listed}. "
                f"{_silence(warning_id)}"
            ),
        )

    return ProjectWarning(
        id=warning_id,
        message=f"{head} {_silence(warning_id)}",
        fix=Fix(
            question=(
                f"Switch [build-system] to `{RECOMMENDED_UV_BUILD_REQUIREMENT}` "
                "with build-backend uv_build?"
            ),
            done=f"[build-system] now builds with uv_build ({pyproject.name}).",
            apply=lambda: _swap_to_uv_build(pyproject),
        ),
    )


def _hatchling_swap_blockers(
    root: Path,
    pyproject: Path,
    document: t.Any,
) -> list[str]:
    """
    Why this Hatchling project cannot be moved to uv_build by rewriting two keys.
    """
    blockers: list[str] = []

    for table in ("build", "version", "metadata"):
        if _table(document, "tool", "hatch", table):
            blockers.append(f"[tool.hatch.{table}] has no uv_build equivalent")

    project = _table(document, "project")
    if "version" in [str(entry) for entry in project.get("dynamic", [])]:
        blockers.append(
            "the version is dynamic, and uv_build has never read a dynamic version"
        )

    extra = [
        requirement
        for requirement in _requirements(document)
        if canonicalize_name(requirement.name) != "hatchling"
    ]
    if extra:
        listed = ", ".join(sorted(str(requirement) for requirement in extra))
        blockers.append(f"[build-system].requires also carries {listed}")

    expected = _expected_module(pyproject, document)
    if expected and not (root / expected).is_file():
        blockers.append(
            f"uv_build would look for a module at {expected.as_posix()}, "
            "which does not exist"
        )

    return blockers


def _swap_to_uv_build(pyproject: Path) -> None:
    """
    Put the project on uv_build at the range vommit recommends.

    Replaces `requires` outright rather than editing it: the offer is only made
    for a project whose sole build requirement is Hatchling, so there is nothing
    else in there to keep.
    """
    document = read_toml(pyproject)
    build_system = document.get("build-system")
    if not isinstance(build_system, dict):
        return

    build_system["requires"] = [RECOMMENDED_UV_BUILD_REQUIREMENT]
    build_system["build-backend"] = "uv_build"
    pyproject.write_text(document.as_string())


def _uv_build_pin_warning(
    pyproject: Path,
    document: t.Any,
) -> ProjectWarning | None:
    """
    A uv_build requirement that is not the range vommit is built against.

    Also covers the requirement being absent: `build-backend = "uv_build"` with
    nothing in `requires` builds off whatever uv the machine happens to have.
    """
    warning_id = "uv-build-pin"
    declared = _uv_build_requirement(document)
    if declared and declared.specifier == RECOMMENDED_UV_BUILD_SPECIFIER:
        return None

    have = (
        f"is `{declared}`"
        if declared
        else "is missing, so the backend version is whatever the build machine has"
    )
    return ProjectWarning(
        id=warning_id,
        message=(
            f"This project's uv_build requirement {have}; vommit builds against "
            f"`{RECOMMENDED_UV_BUILD_REQUIREMENT}`. {_silence(warning_id)}"
        ),
        fix=Fix(
            question=(
                f"Set [build-system].requires to `{RECOMMENDED_UV_BUILD_REQUIREMENT}`?"
            ),
            done=f"[build-system].requires now pins {RECOMMENDED_UV_BUILD_REQUIREMENT}.",
            apply=lambda: _pin_uv_build(pyproject),
        ),
    )


def _pin_uv_build(pyproject: Path) -> None:
    """
    Rewrite the uv-build entry in `requires`, leaving every other entry alone.
    """
    document = read_toml(pyproject)
    build_system = document.get("build-system")
    if not isinstance(build_system, dict):
        return

    requires = build_system.get("requires")
    if not isinstance(requires, list):
        build_system["requires"] = [RECOMMENDED_UV_BUILD_REQUIREMENT]
        pyproject.write_text(document.as_string())
        return

    replaced = False
    for index, entry in enumerate(list(requires)):
        try:
            requirement = Requirement(str(entry))
        except InvalidRequirement:
            continue
        if canonicalize_name(requirement.name) == "uv-build":
            requires[index] = RECOMMENDED_UV_BUILD_REQUIREMENT
            replaced = True

    if not replaced:
        requires.append(RECOMMENDED_UV_BUILD_REQUIREMENT)

    pyproject.write_text(document.as_string())


def _hatch_command_warning(root: Path, config: Config) -> ProjectWarning | None:
    """
    A release that builds with `hatch build` while publishing with uv's index.

    Reported on the command rather than the backend: a project can keep
    Hatchling in `[build-system]` and still have vommit call `uv build`, and it
    is the command that decides what actually gets uploaded.
    """
    warning_id = "hatch-build-command"
    commands = config.commands
    if not commands or "hatch build" not in commands.build:
        return None

    pyproject = root / PYPROJECT
    build = commands.build.strip()
    publish = commands.publish.strip()
    rewrites = {}
    if build in HATCH_BUILD_COMMANDS:
        rewrites["build"] = UV_BUILD
    if publish in HATCH_PUBLISH_COMMANDS:
        rewrites["publish"] = UV_PUBLISH

    head = (
        f"This release builds with `{build}`. "
        "`uv build` is what vommit's other defaults assume."
    )

    if "build" not in rewrites or not pyproject.exists():
        # a command that only mentions `hatch build` does more than build, and
        # what the rest of it is for is not ours to decide
        return ProjectWarning(
            id=warning_id,
            message=(
                f"{head} Change `commands.build` when the project can, or "
                f"{_silence(warning_id).lower()}"
            ),
        )

    listed = ", ".join(f'{key} = "{value}"' for key, value in rewrites.items())
    return ProjectWarning(
        id=warning_id,
        message=f"{head} {_silence(warning_id)}",
        fix=Fix(
            question=f"Set [tool.vommit.commands] {listed}?",
            done=f"[tool.vommit.commands] now runs {listed}.",
            apply=lambda: _use_uv_commands(config, pyproject, rewrites),
        ),
    )


def _use_uv_commands(
    config: Config,
    pyproject: Path,
    rewrites: dict[str, str],
) -> None:
    for key, value in rewrites.items():
        setattr(config.commands, key, value)
    config.write_to_pyproject(pyproject)


def _uv_build_warnings(
    root: Path,
    pyproject: Path,
    document: t.Any,
) -> list[ProjectWarning]:
    """
    The one uv_build misconfiguration that is certain: no module where it looks.

    uv_build derives the module path from the distribution name, so renaming
    `[project].name` without renaming the directory (or keeping a flat layout,
    or a single-file module) breaks the build with nothing else changing.
    """
    expected = _expected_module(pyproject, document)
    if expected is None or (root / expected).is_file():
        return []

    return [
        ProjectWarning(
            id="uv-build-module-missing",
            message=(
                f"`{UV_BUILD}` will fail: the uv build backend expects a Python "
                f"module at {expected.as_posix()}, which does not exist. Create "
                "it, or point the backend at the real one with `module-name` / "
                f"`module-root` under [tool.uv.build-backend]. "
                f"{_silence('uv-build-module-missing')}"
            ),
        )
    ]


def _expected_module(pyproject: Path, document: t.Any) -> Path | None:
    """
    The `__init__.py` uv_build will go looking for, or None when it cannot be
    derived (a namespace package, or a dotted module spread over directories).
    """
    settings = _table(document, "tool", "uv", "build-backend")

    # a namespace package is spelled as a dotted module name across directories
    # that deliberately have no `__init__.py`; the check below does not apply
    if settings.get("namespace"):
        return None

    module_name = str(settings.get("module-name") or _default_module_name(pyproject))
    if not module_name or "." in module_name:
        return None

    module_root = str(settings.get("module-root", DEFAULT_MODULE_ROOT))
    return Path(module_root) / module_name / "__init__.py"


def _default_module_name(pyproject: Path) -> str:
    name = project_name(pyproject)
    return normalise_name(name).replace("-", "_") if name else ""


def _backend(document: t.Any) -> str:
    build_system = document.get("build-system")
    if not isinstance(build_system, dict):
        return ""
    return str(build_system.get("build-backend", ""))


def _requirements(document: t.Any) -> list[Requirement]:
    """
    `[build-system].requires`, parsed, skipping anything that will not parse.
    """
    declared = _table(document, "build-system").get("requires")
    if not isinstance(declared, list):
        return []

    requirements = []
    for entry in declared:
        try:
            requirements.append(Requirement(str(entry)))
        except InvalidRequirement:
            continue
    return requirements


def _uv_build_requirement(document: t.Any) -> Requirement | None:
    for requirement in _requirements(document):
        if canonicalize_name(requirement.name) == "uv-build":
            return requirement
    return None


def _table(document: t.Any, *keys: str) -> dict[str, t.Any]:
    current: t.Any = document
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return {}
        current = current[key]
    return current if isinstance(current, dict) else {}
