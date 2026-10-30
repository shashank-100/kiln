"""Create the `toolbox` fixture: a small package with nine independent bugs.

    python fixtures/make_toolbox.py /path/to/new/repo

Bugs come in three families (stats, text, lists) so training and held-out sets
can be split by family. `BUGS` holds the symptom-only report for each one; a
recorded agent session fixes one bug and adds its regression test.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

FILES = {
    "pyproject.toml": """\
        [project]
        name = "toolbox"
        version = "0.1.0"
        requires-python = ">=3.10"

        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"
        """,
    "toolbox/__init__.py": "",
    "toolbox/stats.py": """\
        def median(values):
            if not values:
                raise ValueError("median of empty data")
            ordered = sorted(values)
            return ordered[len(ordered) // 2]


        def percent_change(old, new):
            if old == 0:
                raise ValueError("percent change from zero")
            return (new - old) / new * 100


        def clamp(x, lo, hi):
            if x < lo:
                return lo
            if x > hi:
                return lo
            return x
        """,
    "toolbox/text.py": """\
        import re


        def slugify(title):
            words = re.findall(r"[A-Za-z0-9]+", title)
            return "-".join(words)


        def truncate(text, width):
            if len(text) < width:
                return text
            return text[: width - 3] + "..."


        def count_words(text):
            return len(text.split(" "))
        """,
    "toolbox/lists.py": """\
        def chunk(items, size):
            if size < 1:
                raise ValueError("size must be positive")
            return [items[i : i + size] for i in range(0, len(items) - size + 1, size)]


        def dedupe(items):
            return list(set(items))


        def last(items, default=None):
            return items[-1]
        """,
    "tests/test_stats.py": """\
        import pytest

        from toolbox.stats import clamp, median, percent_change


        def test_median_odd():
            assert median([3, 1, 2]) == 2


        def test_median_empty():
            with pytest.raises(ValueError):
                median([])


        def test_percent_change_zero():
            with pytest.raises(ValueError):
                percent_change(0, 5)


        def test_clamp_inside_and_below():
            assert clamp(5, 0, 10) == 5
            assert clamp(-3, 0, 10) == 0
        """,
    "tests/test_text.py": """\
        from toolbox.text import count_words, slugify, truncate


        def test_slugify_joins_words():
            assert "-" in slugify("two words")


        def test_truncate_long():
            assert truncate("abcdefghij", 6) == "abc..."


        def test_count_words_simple():
            assert count_words("one two three") == 3
        """,
    "tests/test_lists.py": """\
        import pytest

        from toolbox.lists import chunk, dedupe, last


        def test_chunk_even():
            assert chunk([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]


        def test_chunk_bad_size():
            with pytest.raises(ValueError):
                chunk([1], 0)


        def test_dedupe_removes_duplicates():
            assert sorted(dedupe([1, 1, 2])) == [1, 2]


        def test_last_nonempty():
            assert last([1, 2, 3]) == 3
        """,
    "README.md": """\
        # toolbox

        Small helpers for numbers, text and lists.
        """,
}

# id, family, test file the session should add, symptom-only bug report
BUGS = [
    ("median-even", "stats", "tests/test_median_even.py",
     "median() returns the wrong value for lists with an even number of items: "
     "median([4, 1, 3, 2]) returns 3, but the median of that list is 2.5."),
    ("percent-change", "stats", "tests/test_percent_change.py",
     "percent_change(old, new) gives wrong results: percent_change(50, 100) returns 50.0, "
     "but going from 50 to 100 is a 100% increase."),
    ("clamp-high", "stats", "tests/test_clamp_high.py",
     "clamp(x, lo, hi) is wrong for values above the range: clamp(15, 0, 10) returns 0, "
     "but it should return 10."),
    ("slugify-case", "text", "tests/test_slugify_case.py",
     "slugify() keeps capital letters: slugify('Hello World') returns 'Hello-World', "
     "but slugs should be lowercase ('hello-world')."),
    ("truncate-exact", "text", "tests/test_truncate_exact.py",
     "truncate(text, width) shortens text that already fits: truncate('abcdef', 6) returns "
     "'abc...', but a 6-character string fits in width 6 and should be returned unchanged."),
    ("count-words-spaces", "text", "tests/test_count_words_spaces.py",
     "count_words() overcounts when words are separated by more than one space: "
     "count_words('one  two') returns 3, but there are 2 words."),
    ("chunk-remainder", "lists", "tests/test_chunk_remainder.py",
     "chunk(items, size) drops the last partial chunk: chunk([1, 2, 3, 4, 5], 2) returns "
     "[[1, 2], [3, 4]], but it should return [[1, 2], [3, 4], [5]]."),
    ("dedupe-order", "lists", "tests/test_dedupe_order.py",
     "dedupe() does not keep the original order: dedupe([3, 1, 3, 2, 1]) can return "
     "[1, 2, 3], but it should return [3, 1, 2] (first occurrences, in order)."),
    ("last-default", "lists", "tests/test_last_default.py",
     "last(items, default) crashes on an empty list: last([], default=0) raises IndexError, "
     "but it should return the default, 0."),
]  # fmt: skip


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
    git(dest, "commit", "-q", "-m", "toolbox: initial")
    return dest


if __name__ == "__main__":
    print(make(Path(sys.argv[1])))
