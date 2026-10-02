#!/usr/bin/env python3
"""Checks that dial0/*.py stays compatible with Python 3.10 (the prebuilt base image uses the distro Python).

Needs Python >= 3.12 to run (it uses the f-string tokens introduced there). Flags what 3.10/3.11 reject:
  - the f-string's own quote character reused inside a {...} field   f"{d["k"]}"
  - a backslash inside a {...} field                                 f"{'\\n'.join(x)}"
Also parses every file with feature_version=(3, 10) for other newer syntax.
"""
import ast, glob, io, os, sys, tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check_file(path):
    problems = []
    src = open(path, encoding="utf-8").read()
    try:
        ast.parse(src, feature_version=(3, 10))
    except SyntaxError as e:
        problems.append(f"{path}:{e.lineno}: not valid Python 3.10 syntax: {e.msg}")
    if sys.version_info < (3, 12):
        return problems
    stack = []  # (quote, depth of {} inside this f-string)
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.FSTRING_START:
            if stack and stack[-1][1] > 0:
                q = tok.string.lstrip("fFrRbB")[0]
                if any(q == outer[0] for outer in stack):
                    problems.append(f"{path}:{tok.start[0]}: nested f-string reuses the outer quote")
            stack.append([tok.string.lstrip("fFrRbB")[:3] if tok.string.lstrip("fFrRbB")[:3] in ('"""', "'''")
                          else tok.string.lstrip("fFrRbB")[0], 0])
        elif tok.type == tokenize.FSTRING_END:
            stack.pop()
        elif stack and tok.type == tokenize.OP and tok.string == "{":
            stack[-1][1] += 1
        elif stack and tok.type == tokenize.OP and tok.string == "}":
            stack[-1][1] = max(0, stack[-1][1] - 1)
        elif stack and stack[-1][1] > 0 and tok.type == tokenize.STRING:
            body = tok.string.lstrip("fFrRbBuU")
            if any(body.startswith(outer[0]) or (len(outer[0]) == 1 and body[0] == outer[0]) for outer in stack):
                problems.append(f"{path}:{tok.start[0]}: string {tok.string} reuses the f-string's quote inside {{}}")
            if "\\" in tok.string:
                problems.append(f"{path}:{tok.start[0]}: backslash inside an f-string {{}} field")
    return problems


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "dial0", "*.py")))
    problems = [p for f in files for p in check_file(f)]
    for p in problems:
        print(p.replace(ROOT + "/", ""))
    print(f"python 3.10 compatibility: {len(files)} files, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
