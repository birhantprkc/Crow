# Fixed assert block for probe-f. Imports the required signature from a generated
# file and checks its behaviour on three named inputs.
#
# Its own failing case is run first, every time, against a deliberately wrong
# implementation. A check that has only ever seen good input cannot be told apart
# from one that checks nothing.
import sys, importlib.util

def load(path):
    spec = importlib.util.spec_from_file_location("gen", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.merge_intervals

CASES = [
    ("empty",     [],                              []),
    ("touching",  [[1, 3], [3, 5]],                [[1, 5]]),
    ("unsorted overlap", [[5, 8], [1, 4], [2, 6]], [[1, 8]]),
]

def check(fn):
    for name, given, want in CASES:
        arg = [list(p) for p in given]
        got = fn(arg)
        if got != want:
            return f"{name}: got {got}, want {want}"
        if arg != given:
            return f"{name}: input was modified"
    return None

if __name__ == "__main__":
    path = sys.argv[1]
    try:
        fn = load(path)
    except Exception as e:
        print(f"RESULT: DOES NOT RUN - {type(e).__name__}: {e}")
        sys.exit(2)
    err = check(fn)
    if err:
        print(f"RESULT: RUNS BUT WRONG - {err}")
        sys.exit(1)
    print(f"RESULT: RUNS AND CORRECT - {len(CASES)} cases")
    sys.exit(0)
