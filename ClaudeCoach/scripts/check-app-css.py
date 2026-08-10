#!/usr/bin/env python3
"""check-app-css.py - catch markup and CSS drift in coach/app.{js,html}.
Run: python3 ClaudeCoach/scripts/check-app-css.py

WHY THIS EXISTS
Three separate rounds of the Food page shipped broken on 10 Aug 2026, all the same shape
and none of them raising anything:

  1. markup written before its CSS, so the log and the position panel fell back to raw
     inline flow: "Breakfast758 kcal"
  2. `.figrow` invented and never defined (the app's grid is `.figures`), and `.pos`
     colliding with an existing colour class, so the panel inherited the accent colour
  3. `var(--green)` and `var(--secondary)`, neither of which exists in this palette, plus
     state classes `.good`/`.bad` in the JS against `.low`/`.over` in the CSS - so no zone
     bar ever got a fill

An undefined CSS variable resolves to nothing and an unmatched class does nothing. Both
fail silently and look like a design choice.

WHY THE FIRST VERSION OF THIS CHECK WAS USELESS
It regex-scanned `class="..."` and treated everything inside as a class name, so it flagged
JS variable names from interpolations - `tone`, `pm`, `fk` - as missing classes. A check
that cries wolf gets ignored, which is worse than no check. This version only inspects
LITERAL class tokens, splitting on the interpolation boundaries first.
"""

import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
JS = BASE / "coach" / "app.js"
HTML = BASE / "coach" / "app.html"

# Tokens that are never CSS classes even when they appear inside a class attribute.
NOT_CLASSES = {"esc", "var", "true", "false", "null", "undefined"}


def literal_class_tokens(js: str) -> set:
    """Class names actually written as literals.

    The value of a class attribute in this codebase is a concatenation of string literals
    and JS expressions. Splitting on the expression boundaries first is what stops a
    variable name being read as a class: `class="' + tone + '"` contributes NOTHING, while
    `class="zrow"` and `class="mi ' + x + '"` contribute `zrow` and `mi`.
    """
    out = set()
    for raw in re.findall(r'class="((?:[^"\\]|\\.)*)"', js):
        # Everything between a quote-close and the next quote-open is an expression.
        for literal in re.split(r"'\s*\+.*?\+\s*'", raw):
            literal = re.sub(r"'\s*\+.*$", "", literal)       # trailing open expression
            literal = re.sub(r"^.*?\+\s*'", "", literal)      # leading open expression
            for tok in literal.split():
                if re.fullmatch(r"[a-z][a-z0-9\-]*", tok) and tok not in NOT_CLASSES:
                    out.add(tok)
    return out


def state_classes_from_js(js: str) -> dict:
    """Variables assigned bare class-name strings, e.g. `tone = 'good'`.

    These reach the DOM through an interpolation, so the literal scan cannot see them, but
    they still have to exist in the CSS. This is the exact gap that let .good/.bad go
    undefined while the CSS defined .low/.over."""
    out = {}
    for var, val in re.findall(r"(\w+)\s*=\s*'([a-z][a-z0-9\-]*)'\s*[;,)]", js):
        if val not in NOT_CLASSES:
            out.setdefault(var, set()).add(val)
    return out


def strip_css_comments(css: str) -> str:
    """Blank out /* ... */ so a variable NAMED in a comment is not read as a usage.

    The first cut only skipped lines starting with a comment marker, so a `var(--green)`
    written mid-comment - explaining that --green no longer exists - was reported as a
    live usage of it. A check that flags its own documentation is a check nobody runs."""
    return re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), css, flags=re.S)


def main() -> int:
    js, html = JS.read_text(), HTML.read_text()
    css = strip_css_comments(html)
    problems = []

    # 1. every var() must be defined in :root
    root_block = css[css.index(":root{"):css.index("}", css.index(":root{"))]
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", root_block))
    for name in sorted(set(re.findall(r"var\((--[a-z0-9-]+)\)", css)) - defined):
        line_no = css[:css.index(f"var({name})")].count("\n") + 1
        problems.append(f"{name} is used at app.html:{line_no} and not defined in :root")

    # 2. every literal class in the JS must have a rule
    for cls in sorted(literal_class_tokens(js)):
        # A literal ending in - is a PREFIX completed by an expression, e.g.
        # class="sp sp-' + sport + '". The prefix itself is never a class.
        if cls.endswith("-"):
            continue
        if not re.search(r"\.%s[\s{,:.\[)]" % re.escape(cls), css):
            problems.append(f"class .{cls} is emitted by app.js and has no CSS rule")

    # 3. state strings assigned to variables must have rules too
    interpolated = set(re.findall(r"\+\s*(\w+)\s*\+", js))
    for var, values in sorted(state_classes_from_js(js).items()):
        if var not in interpolated:
            continue
        for val in sorted(values):
            if not re.search(r"\.%s[\s{,:.\[)]" % re.escape(val), css):
                problems.append(f"state class .{val} (via `{var}`) has no CSS rule")

    # 4. the script cache-buster must move when either file changes
    if not re.search(r'app\.js\?v=\d+', html):
        problems.append("app.html has no ?v= on the app.js tag; a stale worker will "
                        "pair new HTML with old script")

    print(f"checked {len(literal_class_tokens(js))} literal classes, "
          f"{len(defined)} palette variables")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print("  -", p)
        return 1
    print("no drift found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
