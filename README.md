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

Bumps the version, runs the configured `clean` and `build` commands, pushes the release commit and
its tag, and publishes last. That order is deliberate: a build that fails has pushed nothing, so
`vommit bump --undo` can still take the release back, and PyPI never receives a version that the
remote does not have. Use `--noop` to see the plan without running it, `--yes` to skip the
confirmation, and the same `--major`/`--minor`/`--patch`/`--prerelease`/`--version` flags as `bump`.

When no commit warrants a bump, vommit asks whether to publish the current version as it stands.
`--no-bump` answers that up front, which is also how you retry after a publish that failed: the
commit and tag are already in place, so only the upload is repeated.

Set `commands.release` to something other than its default to take over the pipeline entirely. It
then runs as a single command, and because nothing can be slotted into it, the push happens before
it rather than in the middle.

### PyPI credentials

```bash
vommit authenticate
```

Stores a PyPI token in your keyring under `vommit`/`pypi`, replacing any token already there.
`vommit ensure-authenticated` asks only when nothing is stored yet, and works as a `pre` task.
During a release the token is passed to the publish command as `UV_PUBLISH_TOKEN` and to nothing
else, so `clean` and `build` never see it.

Set `pypi.use_keyring = false` to leave credentials entirely to the environment, or
`pypi.enabled = false` to build without publishing.

> Note: values in `[tool.vommit]` are read with environment-variable interpolation, so a `$VAR`
> written in a command is expanded when the config loads rather than by the shell that runs it. Put
> anything that needs the shell's own expansion in a script and call that.

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
