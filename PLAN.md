# Plan: `vommit release`

`release` (`tasks.py:526`) is the last stub among vommit's base commands. It is not a large
feature in itself, but it is the seam where three already-built-yet-unwired pieces meet:

- `GitRepo` has no `push` at all (`git.py:17-282`), so `bump` commits and tags purely locally.
  The asymmetry is visible in `undo.py:72`, which refuses to delete a tag that was pushed to
  origin: vommit can detect a pushed tag, but can never create one.
- `PypiConfig` (`config.py:267`) has zero consumers. It is configured, prompted for
  interactively, and migrated from python-semantic-release, but no code path reads it.
- `CommandConfig.release_command` (`config.py:285`) likewise has no caller.

## Pipeline

Default path, four separately reported steps after the bump:

```
bump (local commit + tag) -> clean -> build -> push -> publish
```

Build runs before the push because it is the likeliest failure and the cheapest to recover
from: nothing has left the machine yet, so `run_undo` still works (`undo.py:69` sees no
remote tag). The push runs before the publish because the reverse order can leave a version
on PyPI that exists in no remote git repository, and PyPI never permits reuploading that
version. The only unrecoverable window is "tag pushed, publish failed", which retries
cleanly with `--no-bump`.

## Decisions

### 1. Shell execution: `bash -c`

Every configured command runs as `shlex.join(["bash", "-c", command])` through a `shell()`
helper.

`CommandConfig.release` (`config.py:277`) defaults to `"{clean} && {build} && {publish}"`,
but `LocalRunner.run` does `shlex.split` with no shell (`shell.py:48`), so `&&` becomes a
literal argv token handed to `rm`. `ContextRunner` goes through invoke, which *does* use a
shell, so the two `Runner` implementations disagree today, and the protocol docstring
(`shell.py:34-38`) explicitly promises single strings built with `shlex.join`.

`bash -c` survives `shlex.split` intact and nests harmlessly inside invoke's own shell. It
also fixes the same latent problem for single commands: a user writing `rm -rf dist/*` or
`uv build && ls dist` in `commands.clean` hits the identical wall today.

### 2. `commands.release` stays as a full override

When `commands.release` differs from its class default, it runs as one `bash -c` blob and
the individual steps are skipped. Users keep arbitrary pipelines.

Consequence, to be stated in the error message rather than worked around: the override path
cannot interleave, so the push happens *before* the blob. A build failure there therefore
leaves a pushed tag that `run_undo` refuses to touch (`undo.py:69-73`). That is the price of
an opaque pipeline.

### 3. `--no-bump`

When no commit warrants a version bump, `run_bump` returns `None` (`bump.py:157`). `release`
then asks "no version-worthy changes found; publish the current version anyway?" — but only
when no explicit level flag or `--version` was passed.

The prompt reuses `_asker` (`tasks.py:141-162`), which already encodes the wanted behaviour:
`--yes` passes straight through, and a missing TTY prints "No terminal to ask on; pass
--yes" and returns False. `--no-bump` skips the bump outright and publishes what is already
in `pyproject.toml`; it still runs the branch and freshness checks.

### 4. PyPI authentication

vommit reads the token itself and passes it as an environment variable, matching the
existing `edwh` implementation:

```python
token = keyring.get_password("vommit", "pypi")
runner.run(publish_command, env={"UV_PUBLISH_TOKEN": token})
```

- Keyring namespace is `("vommit", "pypi")` — vommit's own, not shared with edwh.
- `Runner.run` gains an `env` parameter (`shell.py:32-40`, `48`, `71`), as does `FakeRunner`
  (`conftest.py:57`).
- The env dict is deliberately **not** stored on `CommandResult` (`shell.py:10-29`), so a
  token can never surface in an error message, a traceback, or a test's `calls` list.
- `use_keyring = false` means "use whatever is already in the environment": vommit sets
  nothing and an ambient `UV_PUBLISH_TOKEN` applies.
- On a failed publish whose output contains `403`, print a hint pointing at
  `vommit authenticate` (adapted from the current edwh implementation).

### 5. `authenticate` and `ensure_authenticated`

- `vommit authenticate` always prompts and overwrites the stored token, so rotation works.
- `ensure_authenticated` only prompts when no token is stored. It is a real task, so it is
  usable as `pre=[...]` by other tasks and by plugins; `ewok`'s `TaskOptions` supports both
  `pre` and `post`.
- `release` nevertheless calls it *inline* rather than through `pre=`. Invoke-style
  pre-tasks receive the `Context` but not the calling task's flags, so a literal `pre=`
  would fire on `vommit release --noop` and demand a stored token to preview a version it
  is never going to publish. Called inline, it runs after the noop and `pypi.enabled`
  checks.

### 6. `PypiConfig.username` is removed

`UV_PUBLISH_TOKEN` implies `__token__`, so the field only ever mattered for a real
username/password pair. Removing it drops the setting, its interactive prompt, and requires
updating `test_pypi_defaults` (`test_config.py:170`). `use_keyring` stays.

### 7. Output stays captured

`ContextRunner.run` keeps `hide=True` (`shell.py:72`). Each pipeline step gets a rich
spinner so a slow upload does not look like a hang, and the captured output is forwarded
when a step fails. Keeping the output in hand is also what makes the 403 check in decision 4
possible.

### 8. Tags stay lightweight

`GitRepo.tag` (`git.py:279`) creates a lightweight tag — a bare pointer to a commit, with no
tagger, timestamp, or message of its own. This is unchanged.

The one thing to avoid is `git push --follow-tags`, which by design refuses to push
lightweight tags and would silently push the release commit while leaving the tag behind.
`git push --tags` handles both kinds, but pushes *every* local tag in the repository,
including stale or experimental ones. So the tag is pushed by explicit ref:

```
git push <origin> <branch>
git push <origin> refs/tags/<tag>
```

Two calls, so a failure names which of the two failed.

### 9. Prereleases publish

Nothing extra to configure. A prerelease exists to be released; PyPI accepts them and pip
skips them without `--pre`. `changelog.include_prereleases` continues to govern only the
changelog.

### 10. `--noop` stops after the bump preview

It prints the exact commands it would have run. Building under a dry run writes `./dist` and
surprises people, so nothing after the preview executes.

### 11. Flag parity with `bump`

`release` currently accepts only `major/minor/patch` (`tasks.py:527-531`). It gains
`--prerelease`, `--version`, `--allow-dirty`, `--noop`, `--yes` and `--no-bump`, and builds
a `BumpRequest` through the same `select_level` + `__post_init__` validation, so the
flag-conflict errors (`bump.py:38-75`) are stated in one place rather than two.

### Already settled by existing code

The original docstring lists "git pull" as the first step (`tasks.py:534`). That is already
handled: `ensure_up_to_date` (`git.py:113`) fetches and *refuses* when the branch is behind,
rather than pulling. The refusal stays — auto-pulling before a release is how you publish
someone else's unreviewed commit.

## Todo

### Foundations

- [x] Add `keyring` to `[project].dependencies` in `pyproject.toml`
- [x] Add `env: dict[str, str] | None = None` to the `Runner` protocol (`shell.py:32-40`)
- [x] Implement `env` in `LocalRunner.run` (`shell.py:48`) and `ContextRunner.run`
      (`shell.py:71`); keep it off `CommandResult`
- [x] Add a `shell()` helper that wraps a command in `bash -c`
- [x] Add `env` support to `FakeRunner` (`conftest.py:57`) and assert the token never lands
      in `calls`

### Git

- [x] `GitRepo.push(origin, branch)` (`git.py`)
- [x] `GitRepo.push_tag(origin, tag)`, pushing `refs/tags/<tag>` explicitly
- [x] Tests for both, including the failure messages

### Config

- [x] Remove `PypiConfig.username` (`config.py:269`)
- [x] Update `test_pypi_defaults` (`test_config.py:170`) and the interactive `PypiConfig`
      prompts
- [x] Confirm `migrate.py` needs no change (it maps only `pypi.enabled`, `migrate.py:651`)
- [x] Add a `CommandConfig` helper reporting whether `release` was customised away from its
      default

### Authentication

- [x] `keyring` read/write helpers on the `("vommit", "pypi")` namespace
- [x] `authenticate` task: always prompt, always overwrite
- [x] `ensure_authenticated` task: prompt only when the token is missing
- [x] Tests, including the non-TTY path

### Release

- [x] `release.py` with `ReleaseRequest` / `ReleaseResult` / `run_release`, mirroring
      `bump.py`'s shape
- [x] Default pipeline: bump -> clean -> build -> push -> publish
- [x] Override pipeline: push -> `bash -c "{release}"`
- [x] `--no-bump`, plus the "publish the current version anyway?" prompt
- [x] `--noop`: stop after the bump preview, print the commands
- [x] Per-step failure messages naming the step and the state it left behind
- [x] 403 hint pointing at `vommit authenticate`
- [x] Spinner per step, forwarding captured output on failure

### Task layer

- [x] Rewrite the `release` task (`tasks.py:526`) with full flag parity, sharing
      `BumpRequest` validation
- [x] `_report_release`, following `_report` (`tasks.py:72`) and `_report_undo`
      (`tasks.py:93`)

### Verification

- [x] `tests/test_release.py`
- [x] 100% coverage (`[tool.su6] coverage = 100`)
- [x] `ruff` and `ty` clean
- [x] Document `release` and `authenticate` in `README.md`
- [x] Remove the `# todo:` lines at the bottom of `tasks.py` that this work resolves

## Found while implementing

Four things surfaced that the plan did not anticipate. All are fixed; they are recorded
here because each one changes something a user can see.

### `clean` failed on every first release

The default `clean` was `rm -r ./dist`, which exits 1 when `./dist` does not exist — that is,
on every project that has not built yet. Nothing ever ran the command before, so it had never
failed. It is now `rm -rf ./dist`, in `CommandConfig` (`config.py`), in this project's own
`pyproject.toml`, and in the command that `migrate` generates from a v7 `dist_path`
(`migrate.py:628`).

### `$VAR` in a setting is expanded before the shell sees it

configuraptor interpolates environment variables while loading, so `publish = "twine upload
-p $TWINE_PASSWORD"` is expanded from *vommit's* environment at config-load time rather than
by the shell that runs the command. That is pre-existing behaviour affecting every string
setting, not something this branch introduced, but the release pipeline is the first place
where it matters. Documented in the README; anything needing the shell's own expansion
belongs in a script.

### Configured commands run from the project root

`_from_root` prefixes each command with `cd <root>`. Without it a command would run in
whatever directory the CLI was invoked from, and `uv build` would build the wrong project.
The task layer always passes `Path.cwd()`, so this only shows up when `run_release` is called
directly, but the pipeline is meaningless without it.

### Two reporting bugs, caught end-to-end rather than by tests

- `@task(flags={"no_bump": ["--no-bump"]})` produced `----no-bump`: ewok adds the dashes
  itself. Dropping the `flags` option gives the wanted `--no-bump`.
- A step that failed was still printed in green, because the error was raised after the
  reporting context manager had already closed. The raise now happens inside it.
