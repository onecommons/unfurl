# Types for the parts of the `unfurl._rust` extension module (`rust/py`) that
# Python code type-checks against. Most of the solver's are imported untyped
# (see `unfurl/solver.py`).

class Constraint:
    """A solver constraint (opaque here)."""

def make_pattern_constraint(pattern: str) -> Constraint: ...
def regex_match_exact(pattern: Constraint, text: str) -> bool: ...
