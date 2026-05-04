"""Module 11."""
from old_lib import legacy_call

def compute_legacy_total(x, y):
    return legacy_call(x) + y

def use_total():
    return compute_legacy_total(1, 2)
