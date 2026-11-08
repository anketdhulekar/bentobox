"""Checks for values that come from outside: a board, Toggl, or a file on disk. Each gives back a usable value or the
stated default, and never raises."""
import math

DEFAULT_COLOR = "#8E8E94"   # a project with no usable color
LIMIT = 9e15                # beyond this, a JSON number is no count or time anyone sends


def whole(value):
    """An int that isn't a bool, or None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def number(value, default=0):
    """An int or a float, as an int, or the default: not a bool, not NaN or infinite, and within ±9e15."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    return int(value) if -LIMIT <= value <= LIMIT else default


def clamped(value, low, high, default=0):
    """number(), held between low and high, or the default if it isn't one."""
    n = number(value, None)
    return default if n is None else max(low, min(high, n))


def color(value):
    """A color as "#RRGGBB" in capitals, from "#rrggbb" or "rrggbb" (anything after six digits is ignored), or
    DEFAULT_COLOR."""
    text = value[1:] if isinstance(value, str) and value.startswith("#") else value
    if not isinstance(text, str) or len(text) < 6:
        return DEFAULT_COLOR
    try:
        int(text[:6], 16)
    except ValueError:
        return DEFAULT_COLOR
    return "#" + text[:6].upper()
