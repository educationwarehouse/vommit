"""
Standalone plugin-management tasks inspired by edwh's ``plugin`` namespace.
"""

import concurrent.futures
import datetime as dt
import json
import re
import shutil
import typing
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

import dateutil.parser
from packaging.requirements import Requirement
from packaging.version import Version, parse as parse_package_version

try:
    from ewok import Context, task
except ImportError:
    class Context:
        def run(self, *_: typing.Any, **__: typing.Any) -> typing.Any:
            raise RuntimeError("ewok is required to execute vommit tasks")

    def task(*decorator_args: typing.Any, **decorator_kwargs: typing.Any) -> typing.Callable[[typing.Callable[..., typing.Any]], typing.Callable[..., typing.Any]]:
        if decorator_args and callable(decorator_args[0]) and not decorator_kwargs:
            return decorator_args[0]

        def decorator(fn: typing.Callable[..., typing.Any]) -> typing.Callable[..., typing.Any]:
            return fn

        return decorator

try:
    import keyring
except ImportError:
    class _KeyringFallback:
        _store: dict[tuple[str, str], str] = {}

        @classmethod
        def set_password(cls, service: str, username: str, password: str) -> None:
            cls._store[(service, username)] = password

        @classmethod
        def get_password(cls, service: str, username: str) -> Optional[str]:
            return cls._store.get((service, username))

    keyring = _KeyringFallback()

try:
    from termcolor import colored, cprint
    from termcolor._types import Color
except ImportError:
    Color = str

    def colored(text: str, *_: typing.Any, **__: typing.Any) -> str:
        return text

    def cprint(text: str, *_: typing.Any, **__: typing.Any) -> None:
        print(text)


PYPI_URL = "https://pypi.org/pypi"
GITHUB_RAW_URL = "https://raw.githubusercontent.com"


def confirm(prompt: str, default: bool = False) -> bool:
    answer = input(prompt).strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes", "1", "true", "t"}


def kwargs_to_options(flags: dict[str, typing.Any]) -> str:
    options: list[str] = []
    for key, value in flags.items():
        option = f"--{key.replace('_', '-')}"
        if isinstance(value, bool):
            if value:
                options.append(option)
        elif value is not None:
            options.append(f"{option} {value}")
    return " ".join(options)


def _pip() -> str:
    if shutil.which("uv"):
        return "uv pip"
    return "python -m pip"


def is_installed(_: Context, binary: str) -> bool:
    return shutil.which(binary) is not None


def _fetch_pypi_json(package: str) -> dict[str, typing.Any]:
    with urllib.request.urlopen(f"{PYPI_URL}/{package}/json", timeout=10) as response:
        return typing.cast(dict[str, typing.Any], json.loads(response.read().decode("utf-8")))


def _get_latest_version_from_pypi(package: str) -> Version:
    return parse_package_version(_fetch_pypi_json(package)["info"]["version"])


def _get_available_plugins_from_pypi(package: str = "edwh", extra: str = "plugins") -> list[str]:
    metadata = _fetch_pypi_json(package)
    requires_dist = metadata.get("info", {}).get("requires_dist", []) or []
    result: list[str] = []

    for item in requires_dist:
        try:
            requirement = Requirement(item)
        except Exception:
            continue

        marker = str(requirement.marker or "")
        if f'extra == "{extra}"' in marker or f"extra == '{extra}'" in marker:
            result.append(requirement.name)

    return sorted(set(result))


def _parse_versions(packages: list[str]) -> dict[str, Version]:
    parsed: dict[str, Version] = {}

    for package in packages:
        package = package.strip()
        if not package or package.startswith("#"):
            continue

        if "==" in package:
            name, version = package.split("==", 1)
            parsed[name.strip()] = parse_package_version(version.strip())

    return parsed


def _gather_package_metadata_threaded(packages: list[str]) -> dict[str, dict[str, typing.Any]]:
    all_data: dict[str, dict[str, typing.Any]] = {}
    with concurrent.futures.ThreadPoolExecutor() as executor:
        for result, package in zip(executor.map(_fetch_pypi_json, packages), packages):
            all_data[package] = result
    return all_data


def list_installed_plugins(c: Context, pip_command: Optional[str] = None) -> list[str]:
    """
    List installed edwh-related packages.
    """
    pip_command = pip_command or _pip()

    result = c.run(f"{pip_command} freeze | grep -E 'edwh|ewok'", hide=True, warn=True)
    packages = result.stdout.strip().split("\n") if result and result.stdout.strip() else []

    regular_installs = [_ for _ in packages if not (_.startswith("#") or _.startswith("-e"))]
    local_installs = [_.split("/")[-1] for _ in packages if _.startswith("-e")]

    return regular_installs + local_installs


@dataclass
class Plugin:
    raw_name: str
    installed_version: Optional[Version]
    latest_version: Optional[Version]
    metadata: dict[str, typing.Any]
    is_installed: bool
    clean_name: str = ""
    is_outdated: bool = False

    def __post_init__(self) -> None:
        if self.latest_version and self.installed_version:
            self.is_outdated = self.latest_version > self.installed_version

        self.clean_name = self.raw_name.removeprefix("edwh-").removesuffix("-plugin")
        project_urls = self.metadata.get("info", {}).get("project_urls", {})
        self.github_url = project_urls.get("Documentation") or project_urls.get("Source") or ""
        self.requires_python = self.metadata.get("info", {}).get("requires_python", "")

    def __repr__(self) -> str:
        version = (self.installed_version if self.is_installed else self.latest_version) or "?"
        state = "installed" if self.is_installed else "available"
        return f"<Vommit Plugin: {self.clean_name}-{version} {state}>"

    def __str__(self) -> str:
        return json.dumps(self.__dict__, default=str)

    def print_details(self, verbose: bool = False) -> None:
        version_text = ""
        if self.is_outdated:
            version_text = f"({self.installed_version} < {self.latest_version})"
            color: Color = "yellow"
        elif self.is_installed and self.installed_version:
            version_text = f"({self.installed_version})"
            color = "green"
        elif self.latest_version:
            version_text = f"({self.latest_version})"
            color = "red"
        else:
            color = "yellow"

        details = f"• {self.clean_name}"
        if version_text:
            details += f" {version_text}"
        if self.github_url:
            details += f" - {self.github_url}"
        if verbose and self.requires_python:
            details += f" - Python {self.requires_python}"

        cprint(details, color)


def _require_affixes(package: str, prefix: str = "edwh-", suffix: str = "-plugin") -> str:
    if package == "edwh":
        return package

    package = package.removeprefix(prefix).removesuffix(suffix)
    return f"{prefix}{package}{suffix}"


def _gather_plugin_info(c: Context, plugin_names: list[str]) -> list[Plugin]:
    installed_plugins = _parse_versions(list_installed_plugins(c))
    plugin_names = [_require_affixes(name) for name in plugin_names]
    plugin_infos = _gather_package_metadata_threaded(plugin_names)

    result: list[Plugin] = []
    for plugin_name in plugin_names:
        metadata = plugin_infos.get(plugin_name, {})
        info = metadata.get("info")
        if not info:
            continue

        result.append(
            Plugin(
                raw_name=plugin_name,
                is_installed=plugin_name in installed_plugins,
                installed_version=installed_plugins.get(plugin_name),
                latest_version=parse_package_version(info["version"]),
                metadata=metadata,
            )
        )

    return result


def gather_plugin_info(c: Context) -> list[Plugin]:
    available_plugins = ["edwh", *_get_available_plugins_from_pypi("edwh", "plugins")]
    return _gather_plugin_info(c, available_plugins)


@task(name="list")
def list_plugins(c: Context, verbose: bool = False) -> None:
    """
    List available plugins and their install state.
    """
    plugins = gather_plugin_info(c)

    outdated: list[Plugin] = []
    missing_plugin: Optional[str] = None
    for plugin in plugins:
        plugin.print_details(verbose=verbose)
        if plugin.is_outdated:
            outdated.append(plugin)
        if not plugin.is_installed and missing_plugin is None:
            missing_plugin = plugin.clean_name

    if outdated:
        print()
        suffix = "" if len(outdated) == 1 else "s"
        verb = "is" if len(outdated) == 1 else "are"
        cprint(
            f"{len(outdated)} plugin{suffix} {verb} out of date. "
            f"Try `vommit update all` or `vommit changelog --new`.",
            "yellow",
        )

    if missing_plugin:
        print()
        cprint(
            f"Tip: not all plugins are installed. Try `vommit add {missing_plugin}` or `vommit add all`.",
            "blue",
        )


@task()
def add_all(c: Context) -> None:
    pip = _pip()
    plugins = _get_available_plugins_from_pypi("edwh", "plugins")
    if plugins:
        c.run(f"{pip} install {' '.join(plugins)}")


@task()
def remove_all(c: Context) -> None:
    pip = _pip()
    plugins = _get_available_plugins_from_pypi("edwh", "plugins")
    if plugins:
        c.run(f"{pip} uninstall {' '.join(plugins)}")


@task(aliases=("install",))
def add(c: Context, plugin_names: str) -> None:
    if plugin_names == "all":
        add_all(c)
        return

    pip = _pip()
    names = [_require_affixes(plugin_name.strip()) for plugin_name in plugin_names.split(",")]
    c.run(f"{pip} install {' '.join(names)}")


@task(aliases=("upgrade",))
def update(c: Context, plugin_names: str, version: Optional[str] = None, verbose: bool = False, force: bool = False) -> None:
    if force:
        c.run("uv cache clean", hide=True, warn=True)

    if plugin_names == "all":
        installed = [plugin.raw_name for plugin in gather_plugin_info(c) if plugin.is_installed]
        plugin_names = ",".join(installed)

    pip = _pip()
    plugins_with_version: list[str] = []
    for plugin_name in plugin_names.split(","):
        plugin_name = _require_affixes(plugin_name.strip())
        plugin_version = version or str(_get_latest_version_from_pypi(plugin_name))
        plugins_with_version.append(f"{plugin_name}=={plugin_version}")

    if verbose:
        cprint(str(plugins_with_version), "blue")

    c.run(f"{pip} install {' '.join(plugins_with_version)}")


@task(aliases=("uninstall",))
def remove(c: Context, plugin_names: str) -> None:
    if plugin_names == "all":
        remove_all(c)
        return

    pip = _pip()
    names = [_require_affixes(plugin_name.strip()) for plugin_name in plugin_names.split(",")]
    c.run(f"{pip} uninstall {' '.join(names)}")


def get_changelog(github_repo: str) -> str:
    path = urllib.parse.urlparse(github_repo).path.removeprefix("/")
    changelog_url = f"{GITHUB_RAW_URL}/{path}/master/CHANGELOG.md"
    with urllib.request.urlopen(changelog_url, timeout=10) as response:
        return response.read().decode("utf-8")


def get_changelogs_threaded(github_repos: dict[str, str]) -> dict[str, str]:
    all_data: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor() as executor:
        repo_urls = list(github_repos.values())
        for result, package in zip(executor.map(get_changelog, repo_urls), github_repos.keys()):
            all_data[package] = result
    return all_data


def _filter_away_version(changelog_version: Version, _filter: str) -> bool:
    try:
        return changelog_version <= parse_package_version(_filter)
    except Exception:
        return False


def _filter_away_date(date: dt.datetime, _filter: str) -> bool:
    try:
        return date <= dateutil.parser.parse(_filter)
    except Exception:
        return False


def _filter_away(version: Version, date: dt.datetime, _filter: str) -> bool:
    return (not _filter.isnumeric()) and (_filter_away_version(version, _filter) or _filter_away_date(date, _filter))


def sort_versions(key_value: tuple[str, typing.Any]) -> Version:
    key, _value = key_value
    try:
        version, _date = key.split(" ")
        return parse_package_version(version)
    except Exception:
        return Version("0.0.0")


type T_Changelog = dict[str, dict[str, list[str]]]
type T_OrderedChangelog = OrderedDict[str, dict[str, list[str]]]
type T_Changelogs = dict[str, T_Changelog]


def parse_changelog(markdown: str) -> T_Changelog:
    changelog: T_Changelog = {}
    current_version: Optional[str] = None
    current_category: Optional[str] = None

    for line in markdown.split("\n"):
        if line.startswith("# Changelog"):
            continue

        if version_match := re.match(r"^## (.+)", line):
            current_version = version_match.group(1)
            changelog[current_version] = {}
            continue

        if category_match := re.match(r"^### (.+)", line):
            if current_version:
                current_category = category_match.group(1)
                changelog[current_version][current_category] = []
            continue

        if feature_match := re.match(r"^\* (.+)", line):
            if current_version and current_category:
                changelog[current_version][current_category].append(feature_match.group(1))

    return changelog


def to_date(key: str) -> dt.datetime:
    try:
        _, date = key.split(" ", 1)
        return dateutil.parser.parse(date.removeprefix("(").removesuffix(")"))
    except Exception:
        return dateutil.parser.parse("2000-01-01")


def to_version(key: str) -> Version:
    try:
        version, _rest = key.split(" ", 1)
        return parse_package_version(version)
    except Exception:
        return Version("0.0.0")


def sort_and_filter_changelog(changelog: T_Changelog, since: Optional[str] = None) -> T_OrderedChangelog:
    filtered: dict[str, dict[str, list[str]]] = {}
    prev_major = prev_minor = prev_patch = 0

    for idx, (key, value) in enumerate(changelog.items()):
        version = to_version(key)
        date = to_date(key)

        if since and (
            (since == "major" and version.major < prev_major)
            or (since == "minor" and (version.minor < prev_minor or version.major < prev_major))
            or (since == "patch" and (version.micro < prev_patch or version.minor < prev_minor or version.major < prev_major))
            or (since.isnumeric() and idx >= int(since))
        ):
            break
        if since and _filter_away(version, date, since):
            continue

        prev_major = version.major
        prev_minor = version.minor
        prev_patch = version.micro
        filtered[key] = value

    return OrderedDict(sorted(filtered.items(), reverse=True, key=sort_versions))


COLORS: dict[str, Color] = {
    "fix": "yellow",
    "feature": "green",
    "documentation": "blue",
}

BOLD_RE = re.compile(r"((\*\*|__).+?(\*\*|__))")


def colored_markdown(text: str) -> str:
    final = ""
    for part in BOLD_RE.split(text):
        if part.startswith("**") and part.endswith("**"):
            part = colored(part.removeprefix("**").removesuffix("**"), attrs=["bold"])
        final += part
    return final


def display_changelogs(changelogs: dict[str, T_OrderedChangelog]) -> None:
    for package, history in changelogs.items():
        cprint(package, "red", attrs=["bold", "underline"])
        for version, changes in history.items():
            print("-", version)
            for change_type, change_descriptions in changes.items():
                print("--", colored(change_type, COLORS.get(change_type.lower(), "white")))
                for change in change_descriptions:
                    print("----", colored_markdown(change))


def _gather_and_display_changelogs(info: list[Plugin], since: dict[str, str]) -> None:
    changelogs_raw = get_changelogs_threaded(
        {plugin.clean_name: plugin.metadata["info"]["project_urls"]["Source"] for plugin in info if plugin.metadata["info"]["project_urls"].get("Source")}
    )
    changelogs_parsed = {
        name: sort_and_filter_changelog(parse_changelog(data), since[name])
        for name, data in changelogs_raw.items()
    }
    display_changelogs(changelogs_parsed)


def _changelog_new(ctx: Context, *_: typing.Any) -> None:
    info = [plugin for plugin in gather_plugin_info(ctx) if plugin.is_outdated and plugin.installed_version]
    since = {plugin.clean_name: str(plugin.installed_version) for plugin in info}
    _gather_and_display_changelogs(info, since)


def _changelog_specific(ctx: Context, plugin_names: list[str], since: str, *_: typing.Any) -> None:
    info = _gather_plugin_info(ctx, plugin_names)
    _gather_and_display_changelogs(info, {plugin.clean_name: since for plugin in info})


def _changelog_all(ctx: Context, _: list[str], since: str, *__: typing.Any) -> None:
    info = gather_plugin_info(ctx)
    _gather_and_display_changelogs(info, {plugin.clean_name: since for plugin in info})


@task(iterable=["plugin"])
def changelog(ctx: Context, plugin: list[str], since: str = "5", new: bool = False) -> None:
    if new:
        _changelog_new(ctx, plugin, since, new)
    elif plugin:
        _changelog_specific(ctx, plugin, since, new)
    else:
        _changelog_all(ctx, plugin, since, new)


def _semantic_release_publish(c: Context, flags: dict[str, typing.Any], **kw: typing.Any) -> Optional[str]:
    semver = c.run(f"semantic-release publish {kwargs_to_options(flags)}", **kw)
    stderr = semver.stderr if semver else ""
    matches = re.findall(r"to (\d+\.\d+\.\d+.*)", stderr)
    if matches:
        return matches[0]

    cprint("No new version found!", "yellow")
    return None


def uvenv(ctx: Context, specifier: str) -> typing.Any:
    return ctx.run(f"~/.local/bin/uvenv install '{specifier}'", warn=True)


@task()
def require_semantic_release(ctx: Context) -> None:
    if not is_installed(ctx, "semantic-release"):
        uvenv(ctx, "python-semantic-release<8")
        assert is_installed(ctx, "semantic-release"), "Tool 'semantic-release' still can't be found!"


@task()
def require_hatch(ctx: Context) -> None:
    if not is_installed(ctx, "hatch"):
        uvenv(ctx, "hatch")
        assert is_installed(ctx, "hatch"), "Tool 'hatch' still can't be found!"


@dataclass
class GitException(Exception):
    reason: str


@task()
def git_pull(c: Context, yes: bool = False) -> None:
    cprint("pulling latest version from git", "blue")

    git_status = c.run("git status --porcelain", hide=True)
    if git_status.stdout.strip():
        cprint("Warning: You have unstaged changes in your working directory:", "yellow")
        c.run("git status", hide=False)
        if not yes and not confirm("Continue with git pull despite unstaged changes? [yN] ", default=False):
            cprint("Operation cancelled. Please commit or stash your changes first.", "red")
            raise GitException("unstaged changes")

    git_pull_result = c.run("git pull", warn=True)
    stderr = git_pull_result.stderr.lower() if git_pull_result else ""
    if "merge" in stderr or "conflict" in stderr:
        cprint("Git merge conflict detected! Please resolve the conflicts manually and try again.", "red")
        c.run("git status", hide=False)
        raise GitException("merge required")

    if git_pull_result.ok:
        cprint("Git pull completed successfully", "green")
    else:
        cprint(f"Git pull failed: {git_pull_result.stderr}", "red")
        if not yes and not confirm("Continue despite git pull failure? [yN] ", default=False):
            raise GitException(git_pull_result.stderr)


def build(c: Context, hatch: bool = False) -> list[str]:
    if hatch:
        result = c.run("hatch build -c")
    else:
        c.run("rm -r dist/ || true", hide=True, warn=True)
        result = c.run("uv build")
    return re.findall(r"dist/(.+)-\d+\.\d+\.\d+.+tar\.gz", result.stderr if result else "")


@task()
def authenticate(_: Context) -> str:
    if token := input("Enter your token (starting with pypi-): ").strip():
        keyring.set_password("vommit", "pypi", token)
        return token

    cprint("No token specified, exiting", "red")
    raise SystemExit(1)


def publish(c: Context, hatch: bool = False) -> None:
    if hatch:
        c.run("hatch publish")
        return

    pypi_token = keyring.get_password("vommit", "pypi")
    if not pypi_token:
        pypi_token = authenticate(c)

    result = c.run("uv publish", env=dict(UV_PUBLISH_TOKEN=pypi_token), pty=True, warn=True)
    if not result.ok and "403" in (result.stdout + result.stderr):
        cprint("Hint: you may want to enter a new token via `vommit authenticate`", "blue")


@task(pre=[require_semantic_release])
def bump(
    c: Context,
    major: bool = False,
    minor: bool = False,
    patch: bool = False,
    prerelease: bool = False,
    noop: bool = False,
    hide: bool = False,
) -> Optional[str]:
    return _semantic_release_publish(
        c,
        {
            "noop": noop,
            "major": major,
            "minor": minor,
            "patch": patch,
            "prerelease": prerelease,
        },
        hide=hide,
    )


@task(pre=[require_semantic_release], aliases=("publish",))
def release(
    c: Context,
    noop: bool = False,
    major: bool = False,
    minor: bool = False,
    patch: bool = False,
    prerelease: bool = False,
    yes: bool = False,
    pull: bool = True,
    hatch: bool = False,
) -> None:
    if hatch:
        require_hatch(c)

    if pull:
        try:
            git_pull(c, yes=yes)
        except GitException:
            return

    cprint("bumping version", "blue")
    if not (yes or noop):
        new_version = bump(
            c,
            major=major,
            minor=minor,
            patch=patch,
            prerelease=prerelease,
            noop=True,
            hide=True,
        )
        if not new_version or not confirm(
            f"Are you sure you would like to release version {new_version}? [yN] ",
            default=False,
        ):
            print("bye!")
            return

    new_version = bump(
        c,
        major=major,
        minor=minor,
        patch=patch,
        prerelease=prerelease,
        noop=noop,
        hide=False,
    )
    if not new_version:
        return

    cprint("Starting build", "blue")
    pkg = build(c, hatch=hatch)

    if noop:
        cprint(f"Not publishing {pkg} {new_version} due to --noop", "yellow")
        return

    cprint("Starting release", "blue")
    publish(c, hatch=hatch)
    cprint(f"{pkg} {new_version} released!", "green")
