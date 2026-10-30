"""Create the `minicalc` fixture repository: a small package with one real bug.

    python fixtures/make_minicalc.py /path/to/new/repo

The committed state has a `median` that is wrong for even-length input and a
regression suite that passes. The fix and its target test are not committed;
`FIX` and `TARGET_TEST` below are what a session is expected to produce.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

FILES = {
    "pyproject.toml": """\
        [project]
        name = "minicalc"
        version = "0.1.0"
        requires-python = ">=3.10"

        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"
        """,
    "minicalc/__init__.py": """\
        from minicalc.stats import mean, median, spread

        __all__ = ["mean", "median", "spread"]
        """,
    "minicalc/stats.py": """\
        def mean(values):
            if not values:
                raise ValueError("mean of empty data")
            return sum(values) / len(values)


        def median(values):
            if not values:
                raise ValueError("median of empty data")
            ordered = sorted(values)
            return ordered[len(ordered) // 2]


        def spread(values):
            if not values:
                raise ValueError("spread of empty data")
            return max(values) - min(values)
        """,
    "tests/test_stats.py": """\
        import pytest

        from minicalc import mean, median, spread


        def test_mean():
            assert mean([1, 2, 3, 4]) == 2.5


        def test_mean_empty():
            with pytest.raises(ValueError):
                mean([])


        def test_median_odd():
            assert median([3, 1, 2]) == 2


        def test_spread():
            assert spread([4, 9, 1]) == 8
        """,
    "README.md": """\
        # minicalc

        Tiny statistics helpers.
        """,
}

FIX = (
    "minicalc/stats.py",
    "    return ordered[len(ordered) // 2]\n",
    "    mid = len(ordered) // 2\n"
    "    if len(ordered) % 2:\n"
    "        return ordered[mid]\n"
    "    return (ordered[mid - 1] + ordered[mid]) / 2\n",
)

TARGET_TEST = (
    "tests/test_median_even.py",
    "from minicalc import median\n\n\n"
    "def test_median_even():\n"
    "    assert median([4, 1, 3, 2]) == 2.5\n",
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def make(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=False)
    for rel, body in FILES.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "minicalc: initial")
    return dest


if __name__ == "__main__":
    print(make(Path(sys.argv[1])))
