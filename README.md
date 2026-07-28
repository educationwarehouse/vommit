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
