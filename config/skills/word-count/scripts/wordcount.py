#!/usr/bin/env python3
"""Exact line/word/char counts for a text file (Agent Skills script example).

The idiom: deterministic, stdlib-only mechanical work the model shouldn't do by
eye. Read the file, count, print one line. Output-only — nothing is written.
"""

import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: wordcount.py <path-to-file>", file=sys.stderr)
        return 2
    with open(argv[1], "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    print(f"lines={len(text.splitlines())} words={len(text.split())} chars={len(text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
