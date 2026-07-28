# Vommit

> Vommit Oversees Making & Managing Installable Things

## Getting started

Install Vommit into the active environment:

```bash
uv pip install vommit
```

For a global installation, you can use something like [uvenv](https://pypi.org/project/uvenv/), `pipx`, or `uv tool`:

```bash
uvenv install vommit
# alternatively: pipx install vommit
# alternatively: uv tool install vommit
```

From a Python project directory, create or complete the Vommit configuration interactively:

```bash
vommit setup
```

To see all available commands and options:

```bash
vommit --help
```

## Releasing

```bash
vommit release
```

Bumps the version, runs `clean` and `build`, pushes the commit and its tag, then publishes.

The order matters. A build that fails has pushed nothing, so `vommit bump --undo` still works.
Publishing last means PyPI never gets a version the remote does not have, which cannot be
repaired: PyPI does not allow reuploading a version.

Flags are the same as `bump` (`--major`, `--minor`, `--patch`, `--prerelease`, `--version`,
`--allow-dirty`, `--yes`), plus:

| flag | |
|---|---|
| `--noop` | show the plan, run nothing |
| `--no-bump` | publish the current version; also how you retry a failed upload |

When nothing warrants a bump, vommit asks whether to publish the current version anyway.

### Commands

`[tool.vommit.commands]` holds four, run in order. Leave one empty to skip it.

```toml
[tool.vommit.commands]
clean = "rm -rf ./dist"
build = "uv build"
publish = "uv publish"
post_publish = ""          # runs after publishing, including an empty publish command
```

Values are read with environment-variable interpolation, so a `$VAR` here is expanded when the
config loads, not by the shell that runs the command. Put anything needing shell expansion in a
script and call that.

### PyPI credentials

```bash
vommit authenticate
```

Stores a token in your keyring under `vommit`/`pypi`, replacing any token already there. It is
checked first: it has to look like a PyPI token, and PyPI has to accept it. An unreachable index
is not held against the token. `--no-verify` skips both checks.

A rejected token is never stored, so a failed rotation leaves the one you had (unless using
`--clear`). The error says so, and names it.

```bash
vommit ensure-authenticated          # asks only when there is no token yet; works as a `pre` task
vommit ensure-authenticated --show   # PyPI token from the keyring: pypi-AgEI...LWFi
vommit authenticate --clear          # drop the stored token, then ask for a new one
```

A release looks in three places, in order:

1. `UV_PUBLISH_TOKEN` in the environment
2. the keyring, when `pypi.use_keyring = true`
3. you

Set `pypi.use_keyring = false` to be asked every release instead of storing anything, or
`pypi.enabled = false` to build without publishing.

The default keyring backend needs a desktop session, so it fails over SSH. Install
`vommit[ssh]` there, which adds an `ssh-agent` backed keyring.

## Migrating from python-semantic-release v7

```bash
vommit migrate
```

Reads `[tool.semantic_release]`, reports what survives the translation, and asks what to do with the
result. Choose to review the translated settings, write them as-is, or stop; nothing is written
before that choice, so a plain run doubles as a dry run. Use `--yes` to accept the translation and
follow-up changes, `--on-unsupported=error` to refuse unsupported settings, or `=skip` to shorten the
report.

Supported settings are mapped to `[tool.vommit]`; values that have no equivalent are reported rather
than silently discarded. Migration refuses non-angular commit parsers, non-`[project].version`
`version_toml` targets, and `version_pattern`, because those would produce an unusable release setup.

Only `pyproject.toml` is read. `setup.cfg` is not supported.

If the project has a dynamic version, hatchling and setuptools projects can be fixed during migration:
the current version is frozen into `[project].version`, the backend hook is removed, and configured
version files are changed to read installed metadata:

```python
from importlib.metadata import version

__version__ = version(__package__)
```

The migration can then remove `[tool.semantic_release]` and the `python-semantic-release` dependency.
