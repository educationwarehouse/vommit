"""
The license identifiers `init` offers.

Identifiers only: the LICENSE file itself is the author's to choose and to
place. Vommit records what they picked in `[project].license` and says the file
is still theirs to write.
"""

NO_LICENSE = "none"
OTHER_LICENSE = "other"

# Common SPDX identifiers, offered so the usual answer is not a typing exercise.
COMMON: tuple[str, ...] = (
    "MIT",
    "Apache-2.0",
    "BSD-3-Clause",
    "BSD-2-Clause",
    "ISC",
    "MPL-2.0",
    "GPL-3.0-only",
    "AGPL-3.0-only",
    "LGPL-3.0-only",
    "Unlicense",
)

# What `init` offers, with the two answers that record nothing last.
CHOICES: list[str] = [*COMMON, OTHER_LICENSE, NO_LICENSE]

DEFAULT_LICENSE = "MIT"
