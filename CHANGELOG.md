# Changelog

<!-- next-version-placeholder -->

## v0.1.2 (2026-08-03)

### Fix
* **migration:** preserve config layout and bump defaults

## v0.1.1 (2026-08-03)

### Fix
* **migrate:** preserve publishing during v7 config migration

## v0.1.0 (2026-07-30)

### Features
* `vommit init` command to setup a new (uv + vommit) project
* **release:** add release subcommand
* migrate from PSR7 (#5)
* implemented `bump` command, with `--undo`  (#2)
* improvements in interactive `setup`
* better interactive that also looks at current values
* in progress interactive setup (not stored or anything yet)
* in progress vommit config classes
* **helper:** `canonical_version` helper
* stub cli
* initial version

### Fixes
* **migrate:** preserve authors and project tables
* **release:** support configurable commit authors
* don't crash on ignored `uv.lock`, hint at `--undo` when something like that does happen
* use `__version__ = version(__package__)`

### Documentation
* **readme:** shorten info about 'release' subcommand
