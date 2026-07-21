import re

def parse_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

def parse_money_to_cents(s: str) -> int:
    if s is None: return 0
    s = s.strip()
    if s == "": return 0
    # Accept $1,234.56 or -1234.56 etc.
    m = re.match(r'^\s*([+-]?)\$?\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)?(?:\.([0-9]{1,2}))?\s*$', s)
    if not m:
        return 0
    sign = -1 if m.group(1) == '-' else 1
    whole = m.group(2) or "0"
    cents = m.group(3) or "0"
    cents = (cents + "0")[:2]
    return sign*(int(whole.replace(',', ''))*100 + int(cents))

def parse_money_to_cents_strict(s: str, *, field_name: str = "Amount", allow_blank: bool = False) -> int:
    """Parse a money input for write paths, raising ValueError on bad input."""
    if s is None:
        if allow_blank:
            return 0
        raise ValueError(f"{field_name} is required.")
    raw = str(s).strip()
    if raw == "":
        if allow_blank:
            return 0
        raise ValueError(f"{field_name} is required.")
    m = re.match(r'^\s*([+-]?)\$?\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)?(?:\.([0-9]{1,2}))?\s*$', raw)
    if not m:
        raise ValueError(f"{field_name} must be a valid dollar amount.")
    sign = -1 if m.group(1) == '-' else 1
    whole = m.group(2) or "0"
    cents = m.group(3) or "0"
    cents = (cents + "0")[:2]
    return sign*(int(whole.replace(',', ''))*100 + int(cents))

def cents_to_money(cents: int) -> str:
    sign = '-' if cents < 0 else ''
    cents = abs(int(cents))
    return f"{sign}${cents//100:,}.{cents%100:02d}"

def cents_to_dollars(value) -> str:
    """
    Convert integer cents -> string dollars WITHOUT a currency symbol,
    e.g. -12345 -> "-123.45". Safe for form <input value="...">.
    """
    try:
        cents = int(value)
    except (TypeError, ValueError):
        cents = 0
    sign = '-' if cents < 0 else ''
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"

def register_jinja(app):
    from flask import request
    # Filters
    app.jinja_env.filters["money"] = cents_to_money
    app.jinja_env.filters["cents_to_dollars"] = cents_to_dollars
    app.jinja_env.filters["dollars_to_cents"] = parse_money_to_cents
    
    @app.context_processor
    def _inject_helpers():
        def current_path():
            # request.full_path includes a trailing "?" when no query string
            fp = getattr(request, "full_path", None) or request.path
            return fp[:-1] if fp.endswith("?") else fp
        return {
            "money": cents_to_money,   
            "current_path": current_path,
        }
