"""Load a script from a skill folder (the folder names have hyphens)."""

import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")


def load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


INDIA = os.path.join(FIX, "india")


def run(mod, args, cwd=None):
    """Run a tool's main(args) in cwd; returns (exit code, stdout, stderr)."""
    import io
    from contextlib import redirect_stderr, redirect_stdout
    out, err = io.StringIO(), io.StringIO()
    old = os.getcwd()
    if cwd:
        os.chdir(cwd)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = mod.main(args)
            except SystemExit as e:
                code = e.code
    finally:
        os.chdir(old)
    return code, out.getvalue(), err.getvalue()
