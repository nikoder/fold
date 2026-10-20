#!/usr/bin/env python3
"""Parse self-test for foldctl's dispatch CLI (invoked by .fold/check).

Drives main() with hand-built argvs and asserts what the parser fed to
cmd_dispatch. Hermetic on purpose: FOLD_CREDS points at a nonexistent
path BEFORE the module import (neutralizing the creds-repo guard) and
cmd_dispatch is stubbed, so nothing here touches incus, the forge, or
a real credentials file.
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["FOLD_CREDS"] = os.path.join(ROOT, ".fold", "no-such-creds")

# bin/foldctl has no .py extension and cannot be imported the normal
# way; exec its source directly so the test reads exactly the bytes on
# disk — no __pycache__ can shadow the code under test.
_path = os.path.join(ROOT, "bin", "foldctl")
foldctl = {"__name__": "foldctl", "__file__": _path}
with open(_path) as f:
    exec(compile(f.read(), _path, "exec"), foldctl)


def parse(argv):
    """Run foldctl main() with argv -> ("dispatch", kwargs-dict from
    the stubbed cmd_dispatch) on a clean parse, ("exit", code) when
    the parser rejects the argv."""
    seen = {}
    foldctl["cmd_dispatch"] = (
        lambda template, prompt, **kw:
        seen.update(template=template, prompt=prompt, **kw))
    sys.argv = argv
    try:
        foldctl["main"]()
    except SystemExit as e:
        return "exit", e.code
    return "dispatch", seen


def want(label, actual, expected):
    if actual != expected:
        sys.exit(f"parse self-test FAIL {label}: got {actual!r}, "
                 f"want {expected!r}")


# (a) flags before and after the template; flag-like words inside a
#     quoted prompt word must survive verbatim
kind, a = parse(["foldctl", "dispatch", "--ctx", "X", "agent",
                 "--mem", "2", "write --model docs and -h advice"])
want("(a) parses as dispatch", kind, "dispatch")
want("(a) template", a["template"], "agent")
want("(a) ctx", a["ctx"], "X")
want("(a) mem", a["mem_gib"], 2.0)
want("(a) prompt verbatim", a["prompt"],
     "write --model docs and -h advice")

# (b) a flag-like word once the prompt has begun is prose, not a flag
kind, b = parse(["foldctl", "dispatch", "agent", "fix", "the",
                 "--bogus", "flag", "handling"])
want("(b) parses as dispatch", kind, "dispatch")
want("(b) template", b["template"], "agent")
want("(b) prompt is all five words", b["prompt"],
     "fix the --bogus flag handling")

# (c) an unknown flag before the template is a parse error; argparse
# prints its usage to stderr on the way out — capture that noise and
# surface it only if the case unexpectedly fails.
kind, c = None, None
_noise = io.StringIO()
_stderr, sys.stderr = sys.stderr, _noise
try:
    kind, c = parse(["foldctl", "dispatch", "--bogus", "agent", "hi"])
finally:
    sys.stderr = _stderr
if kind != "exit" or not c:
    sys.exit(f"parse self-test FAIL (c): unknown dispatch flag must "
             f"exit nonzero, got {kind} {c!r}\n{_noise.getvalue()}")

# (d) the full option set, prompt trailing after the template
kind, d = parse(["foldctl", "dispatch", "--repo", "fold", "--llm",
                 "llamaswap", "--model", "glm53-flash", "agent",
                 "do", "a", "thing"])
want("(d) parses as dispatch", kind, "dispatch")
want("(d) repo", d["repo"], "fold")
want("(d) llm", d["llm"], "llamaswap")
want("(d) model", d["model"], "glm53-flash")
want("(d) template", d["template"], "agent")
want("(d) prompt", d["prompt"], "do a thing")

print("parse self-test: dispatch CLI contract OK (4 argvs)")
