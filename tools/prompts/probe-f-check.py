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

# The failing case the header promises. It was described here from the start but never
# actually run - found 2026-08-04, when a generated file raised NameError inside check()
# and this script died with a traceback instead of a verdict. A checker that has only
# ever seen good input cannot be told apart from one that checks nothing, so it now
# proves on every invocation that CASES rejects a wrong implementation.
def self_test():
    def never_merges(intervals):
        return [list(p) for p in intervals]
    if check(never_merges) is None:
        print("RESULT: CHECKER BROKEN - a non-merging implementation passed CASES")
        sys.exit(3)

if __name__ == "__main__":
    self_test()

    path = sys.argv[1]
    try:
        fn = load(path)
    except Exception as e:
        print(f"RESULT: DOES NOT RUN - {type(e).__name__}: {e}")
        sys.exit(2)

    # Generated code fails at call time as often as at import time: truncated functions
    # reference names that were never bound. That is still "does not run", not a crash
    # of the harness - a traceback here would report the checker's failure, not the model's.
    try:
        err = check(fn)
    except Exception as e:
        print(f"RESULT: DOES NOT RUN - {type(e).__name__}: {e}")
        sys.exit(2)

    if err:
        print(f"RESULT: RUNS BUT WRONG - {err}")
        sys.exit(1)
    print(f"RESULT: RUNS AND CORRECT - {len(CASES)} cases")
    sys.exit(0)
