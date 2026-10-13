"""Deterministic migration engine. Pure Python, stdlib only, no I/O, no clock, no randomness.

Faithful port of the prototype's engine.js. Hashes and fingerprints are byte-compatible with the
JS engine (verified in tests/test_engine.py against tests/parity_fixture.json).
"""
from __future__ import annotations

import json
import re
from typing import Any

import phonenumbers


class CountInvariantError(ValueError):
    """Raised when record count invariants fail (e.g. source != accepted + quarantined)."""
    pass


class CountsDict(dict):
    """Dictionary holding the 4 canonical dry-run count keys, aliasing quarantined and explicit counts."""
    _ALIASES = {
        "quarantined": "rejected",
        "source_count": "source",
        "transformed_count": "transformed",
        "accepted_count": "accepted",
        "quarantined_count": "rejected",
        "pending_count": None,
    }

    def __getitem__(self, key):
        if key in self._ALIASES:
            target = self._ALIASES[key]
            return 0 if target is None else dict.__getitem__(self, target)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        if key in self._ALIASES:
            target = self._ALIASES[key]
            return 0 if target is None else dict.get(self, target, default)
        return dict.get(self, key, default)

    def __contains__(self, key):
        return dict.__contains__(self, key) or key in self._ALIASES

    @property
    def source_count(self) -> int:
        return self["source"]

    @property
    def transformed_count(self) -> int:
        return self["transformed"]

    @property
    def accepted_count(self) -> int:
        return self["accepted"]

    @property
    def quarantined_count(self) -> int:
        return self["rejected"]

    @property
    def quarantined(self) -> int:
        return self["rejected"]

    @property
    def pending_count(self) -> int:
        return 0


MAX_RECORDS = 200
REFERENCE_DATE = "2026-10-12"
_A = re.ASCII


# ---------------------------------------------------------------- utilities
def stable(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, (int, float, str)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(stable(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + stable(v[k]) for k in sorted(v)) + "}"
    raise TypeError(f"cannot fingerprint {type(v)}")


_M = 0xFFFFFFFF


def _imul(a: int, b: int) -> int:
    return (a * b) & _M


def hash_str(s: str) -> str:
    """cyrb53 over UTF-16 code units (matches the JS implementation)."""
    h1, h2 = 0xDEADBEEF, 0x41C6CE57
    raw = s.encode("utf-16-le")
    for i in range(0, len(raw), 2):
        ch = raw[i] | (raw[i + 1] << 8)
        h1 = _imul(h1 ^ ch, 2654435761)
        h2 = _imul(h2 ^ ch, 1597334677)
    h1 = _imul(h1 ^ (h1 >> 16), 2246822507) ^ _imul(h2 ^ (h2 >> 13), 3266489909)
    h2 = _imul(h2 ^ (h2 >> 16), 2246822507) ^ _imul(h1 ^ (h1 >> 13), 3266489909)
    return format(4294967296 * (2097151 & h2) + (h1 & _M), "x").rjust(14, "0")


def is_blank(v: Any) -> bool:
    return v is None or str(v).strip() == ""


def clip(s: Any, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _ok(value):
    return (True, value, None, None)


def _bad(code, message):
    return (False, None, code, message)


# ---------------------------------------------------------------- schemas
SOURCE_SCHEMA: dict = {
    "name": "legacy_customers",
    "description": "Legacy CRM customer export. Every value arrives as a string.",
    "fields": [
        {"name": "cust_id", "type": "string", "nullable": False, "description": "Customer number such as CUST-1001; some rows are lowercase"},
        {"name": "full_name", "type": "string", "nullable": False, "description": 'Either "Last, First" or "First Last"'},
        {"name": "email_addr", "type": "string", "nullable": True, "description": "Email address; mixed case, sometimes padded with spaces"},
        {"name": "phone_no", "type": "string", "nullable": True, "description": "Free-form phone number, national or international"},
        {"name": "dob", "type": "string", "nullable": True, "description": "Date of birth written DD/MM/YYYY"},
        {"name": "country", "type": "string", "nullable": False, "description": "Country name or short code"},
        {"name": "status_cd", "type": "string", "nullable": False, "description": "One-letter status code"},
        {"name": "balance", "type": "string", "nullable": False, "description": "Account balance as a decimal string, possibly with thousands separators"},
        {"name": "signup_date", "type": "string", "nullable": False, "description": "Signup time written DD/MM/YYYY HH:MM"},
        {"name": "notes", "type": "string", "nullable": True, "description": "Free-text agent notes"},
        {"name": "region_code", "type": "string", "nullable": True, "description": "Legacy sales region (N, S, E, W)"},
    ],
}

_COUNTRY_DEFS = [
    ("IN", "India", "IND"), ("US", "United States", "USA", "United States of America"),
    ("GB", "United Kingdom", "UK", "Great Britain", "England"), ("CA", "Canada", "CAN"), ("AU", "Australia", "AUS"),
    ("DE", "Germany", "DEU"), ("FR", "France", "FRA"), ("SG", "Singapore", "SGP"), ("AE", "United Arab Emirates", "UAE"),
    ("JP", "Japan", "JPN"), ("BR", "Brazil", "BRA"), ("NL", "Netherlands", "NLD"), ("ES", "Spain", "ESP"),
    ("IT", "Italy", "ITA"), ("ZA", "South Africa", "ZAF"), ("NZ", "New Zealand", "NZL"), ("IE", "Ireland", "IRL"),
    ("SE", "Sweden", "SWE"), ("MX", "Mexico", "MEX"), ("CH", "Switzerland", "CHE"),
]
_COUNTRY_LOOKUP: dict[str, str] = {}
for _d in _COUNTRY_DEFS:
    _COUNTRY_LOOKUP[_d[0].lower()] = _d[0]
    for _a in _d[1:]:
        _COUNTRY_LOOKUP[_a.lower()] = _d[0]
COUNTRY_CODES = [d[0] for d in _COUNTRY_DEFS]

TARGET_SCHEMA: dict = {
    "name": "accounts",
    "description": "Billing platform account table (staging copy).",
    "primary_key": "account_id",
    "fields": [
        {"name": "account_id", "type": "string", "required": True, "pk": True, "pattern": r"^[A-Z0-9][A-Z0-9-]{2,19}$", "hint": "Uppercase letters, digits and hyphens, 3 to 20 characters", "description": "Primary key"},
        {"name": "first_name", "type": "string", "required": True, "max_length": 60, "description": "Given name"},
        {"name": "last_name", "type": "string", "required": True, "max_length": 60, "description": "Family name"},
        {"name": "email", "type": "string", "required": True, "format": "email", "max_length": 254, "pattern": r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,}$", "hint": "Lowercase, valid address", "description": "Login and billing email"},
        {"name": "phone_e164", "type": "string", "required": False, "format": "e164", "pattern": r"^\+[1-9]\d{7,14}$", "hint": "E.164, for example +919876543210", "description": "Optional phone in E.164"},
        {"name": "birth_date", "type": "date", "required": False, "min": "1900-01-01", "max": REFERENCE_DATE, "description": "Optional ISO date"},
        {"name": "country_code", "type": "string", "required": True, "format": "iso2", "enum": COUNTRY_CODES, "description": "ISO 3166-1 alpha-2"},
        {"name": "status", "type": "string", "required": True, "enum": ["active", "inactive", "suspended"], "description": "Account state"},
        {"name": "balance_minor", "type": "integer", "required": True, "format": "minor_units", "min": -1e12, "max": 1e12, "description": "Balance in minor currency units (cents)"},
        {"name": "created_at", "type": "datetime", "required": True, "description": "UTC ISO timestamp"},
        {"name": "tier", "type": "string", "required": True, "enum": ["standard", "premium", "enterprise"], "description": "Service tier. The legacy export has no equivalent."},
        {"name": "marketing_opt_in", "type": "boolean", "required": True, "description": "Marketing consent flag. The legacy export has no equivalent."},
    ],
}
TF = {f["name"]: f for f in TARGET_SCHEMA["fields"]}
SF = {f["name"]: f for f in SOURCE_SCHEMA["fields"]}
TARGET_ORDER = [f["name"] for f in TARGET_SCHEMA["fields"]]
PK = TARGET_SCHEMA["primary_key"]


# ---------------------------------------------------------------- supported transformation rules
def _dim(y: int, m: int) -> int:
    leap = (y % 4 == 0 and y % 100 != 0) or y % 400 == 0
    return [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]


def _date_parts(s: Any, fmt: str):
    m = re.fullmatch(r"(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})", str(s).strip(), _A)
    if not m:
        return None
    if fmt == "DMY":
        d, mo, y, ys = int(m[1]), int(m[2]), int(m[3]), m[3]
        if len(m[1]) > 2:
            return None
    elif fmt == "MDY":
        mo, d, y, ys = int(m[1]), int(m[2]), int(m[3]), m[3]
        if len(m[1]) > 2:
            return None
    else:
        y, mo, d, ys = int(m[1]), int(m[2]), int(m[3]), m[1]
        if len(m[3]) > 2:
            return None
    if len(ys) != 4 or y < 1900 or y > 2100 or mo < 1 or mo > 12 or d < 1 or d > _dim(y, mo):
        return None
    return y, mo, d


def _iso_date(p) -> str:
    return f"{p[0]}-{p[1]:02d}-{p[2]:02d}"


FORMATS = ["DMY", "MDY", "YMD"]


def _r_copy(v, p):
    return _ok(None if is_blank(v) else str(v))


def _r_trim(v, p):
    return _ok(None if is_blank(v) else str(v).strip())


def _r_lower(v, p):
    return _ok(None if is_blank(v) else str(v).strip().lower())


def _r_upper(v, p):
    return _ok(None if is_blank(v) else str(v).strip().upper())


def _r_split_name(v, p):
    if is_blank(v):
        return _ok(None)
    s = re.sub(r"\s+", " ", str(v).strip())
    if "," in s:
        i = s.index(",")
        last, first = s[:i].strip(), s[i + 1:].strip()
    else:
        t = s.split(" ")
        if len(t) < 2:
            return _bad("name_part_missing", f'Cannot split the single-word name "{s}" into first and last')
        last = t.pop()
        first = " ".join(t)
    out = last if p.get("part") == "last" else first
    return _ok(out) if out else _bad("name_part_missing", f'No {p.get("part")} name found in "{s}"')


def _r_parse_date(v, p):
    if is_blank(v):
        return _ok(None)
    d = _date_parts(v, p.get("format"))
    return _ok(_iso_date(d)) if d else _bad("invalid_date", f'"{v}" is not a valid {p.get("format")} calendar date')


def _r_parse_datetime(v, p):
    if is_blank(v):
        return _ok(None)
    m = re.fullmatch(r"(\S+?)(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?", str(v).strip(), _A)
    d = _date_parts(m[1], p.get("format")) if m else None
    if not d:
        return _bad("invalid_date", f'"{v}" is not a valid {p.get("format")} date/time')
    h = int(m[2]) if m[2] is not None else 0
    mi = int(m[3]) if m[3] is not None else 0
    s = int(m[4]) if m[4] is not None else 0
    if h > 23 or mi > 59 or s > 59:
        return _bad("invalid_time", f'"{v}" has an impossible time of day')
    return _ok(f"{_iso_date(d)}T{h:02d}:{mi:02d}:{s:02d}Z")


def _r_country(v, p):
    if is_blank(v):
        return _ok(None)
    c = _COUNTRY_LOOKUP.get(str(v).strip().lower())
    return _ok(c) if c else _bad("unknown_country", f'"{v}" is not in the supported country table')


def _r_map_enum(v, p):
    if is_blank(v):
        return _ok(None)
    mp = p.get("map") or {}
    k = str(v).strip().lower()
    if k in mp:
        return _ok(mp[k])
    return _bad("unmapped_value", f'Value "{str(v).strip()}" has no entry in the mapping table')


def _r_decimal(v, p):
    if is_blank(v):
        return _ok(None)
    dec = 2 if p.get("decimals") is None else p["decimals"]
    s = str(v).strip()
    if "," in s:
        if not re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s, _A):
            return _bad("ambiguous_separator", f'"{s}" uses commas in a way that is not a clear thousands separator')
        s = s.replace(",", "")
    m = re.fullmatch(r"(-)?(\d+)(?:\.(\d+))?", s, _A)
    if not m:
        return _bad("invalid_decimal", f'"{s}" is not a decimal number')
    frac = m[3] or ""
    if len(frac) > dec:
        return _bad("precision_loss", f'"{s}" has more than {dec} decimal places')
    frac = frac.ljust(dec, "0")
    n = int(m[2] + frac)
    if n > 2**53 - 1:
        return _bad("invalid_decimal", f'"{s}" is too large')
    return _ok(-n if m[1] and n != 0 else n)


def _resolve_phone_region(p: dict) -> tuple[str | None, str | None]:
    """Resolves (calling_code_digits, iso2_country_code) from rule params."""
    p = p or {}
    reg = p.get("default_region")
    cc = p.get("default_cc")
    if reg:
        s = str(reg).strip().upper()
        if re.fullmatch(r"\d{1,3}", s, _A):
            region = phonenumbers.region_code_for_country_code(int(s))
            return s, region
        c_code = phonenumbers.country_code_for_region(s)
        return (str(c_code) if c_code else None), s
    if cc:
        c = str(cc).lstrip("+").strip()
        if re.fullmatch(r"\d{1,3}", c, _A):
            region = phonenumbers.region_code_for_country_code(int(c))
            return c, region
    return None, None


def _r_phone(v, p):
    if is_blank(v):
        return _ok(None)
    s = re.sub(r"[\s().\-]", "", str(v).strip())
    if s.startswith("00"):
        s = "+" + s[2:]
    
    cc, reg = _resolve_phone_region(p)
    
    if s.startswith("+"):
        # Explicit international numbers retain their existing country code
        try:
            pn = phonenumbers.parse(s, None)
            formatted = phonenumbers.format_number(pn, phonenumbers.PhoneNumberFormat.E164)
            if re.fullmatch(r"\+[1-9]\d{7,14}", formatted, _A):
                return _ok(formatted)
        except Exception:
            pass
        if re.fullmatch(r"\+[1-9]\d{7,14}", s, _A):
            return _ok(s)
        return _bad("invalid_phone", f'"{v}" is not a valid international E.164 number')
    
    # National number without country code
    if not reg and not cc:
        return _bad("invalid_phone", f'"{v}" has no country code and no default phone region is configured')
    
    # Try parsing with phonenumbers using the configured region
    try:
        pn = phonenumbers.parse(s, reg)
        if phonenumbers.is_possible_number(pn):
            formatted = phonenumbers.format_number(pn, phonenumbers.PhoneNumberFormat.E164)
            if re.fullmatch(r"\+[1-9]\d{7,14}", formatted, _A):
                return _ok(formatted)
    except Exception:
        pass
    
    # Fallback for 10-digit national number with calling code
    if cc and re.fullmatch(r"\d{10}", s, _A):
        candidate = f"+{cc}{s}"
        if re.fullmatch(r"\+[1-9]\d{7,14}", candidate, _A):
            return _ok(candidate)
            
    return _bad("invalid_phone", f'"{v}" is not a valid phone number')


def _r_default(v, p):
    policy = p.get("policy")
    target = p.get("_target") or "business field"
    if policy == "quarantine":
        return _bad("quarantined_by_policy", f'Field "{target}" is held in quarantine pending business decision')
    if policy == "unresolved":
        return _bad("unresolved_business_policy", f'Field "{target}" requires an explicit reviewer-approved business policy')
    if policy == "preserve_unknown":
        return _ok(None)
    return _ok(p.get("value"))


RULES: dict[str, dict] = {
    "copy": {"label": "Copy as-is", "uses_source": True, "params": {}, "description": "Copies the source value unchanged (blank becomes null).", "apply": _r_copy},
    "trim": {"label": "Trim whitespace", "uses_source": True, "params": {}, "description": "Removes leading and trailing whitespace.", "apply": _r_trim},
    "lowercase": {"label": "Trim and lowercase", "uses_source": True, "params": {}, "description": "Trims and lowercases text.", "apply": _r_lower},
    "uppercase": {"label": "Trim and uppercase", "uses_source": True, "params": {}, "description": "Trims and uppercases text.", "apply": _r_upper},
    "split_name": {"label": "Split full name", "uses_source": True, "params": {"part": ["first", "last"]}, "description": 'Splits "Last, First" or "First Last" into a first or last name. With no comma, the final word is the last name.', "apply": _r_split_name},
    "parse_date": {"label": "Parse date to ISO date", "uses_source": True, "params": {"format": FORMATS}, "description": "Parses a calendar date in DMY, MDY or YMD order (separators / - .) into YYYY-MM-DD. Impossible dates are errors.", "apply": _r_parse_date},
    "parse_datetime": {"label": "Parse date and time to ISO UTC", "uses_source": True, "params": {"format": FORMATS}, "description": 'Parses "date" or "date HH:MM" into YYYY-MM-DDTHH:MM:SSZ. The source time is assumed to be UTC.', "apply": _r_parse_datetime},
    "country_to_iso2": {"label": "Country to ISO alpha-2", "uses_source": True, "params": {}, "description": "Looks up a country name, alpha-2 or alpha-3 code in a fixed table of 20 countries.", "apply": _r_country},
    "map_enum": {"label": "Map values (lookup)", "uses_source": True, "params": {"map": "object of source value to target value"}, "description": "Maps each source value (case-insensitive) to a target value. Values missing from the map are errors.", "apply": _r_map_enum},
    "decimal_to_minor": {"label": "Decimal string to minor units", "uses_source": True, "params": {"decimals": "integer 0 to 6, default 2"}, "description": 'Converts "1,234.50" to 123450 using exact string arithmetic. Rejects ambiguous separators and extra precision.', "apply": _r_decimal},
    "normalize_phone": {"label": "Normalize phone to E.164", "uses_source": True, "params": {"default_region": "ISO-2 country code (e.g. IN, US) or calling code digits", "default_cc": "country calling code digits (legacy)"}, "description": 'Normalizes phone numbers to E.164. Existing "+" or "00" prefixes retain their international country code. Unqualified numbers use the configured default region.', "apply": _r_phone},
    "default_value": {"label": "Constant value / business policy", "uses_source": False, "params": {"value": "any string, number or boolean", "policy": "approved_default | quarantine | unresolved | preserve_unknown", "rationale": "decision rationale", "approved_by": "reviewer identifier", "semantic_consequences": "documented semantic impact"}, "description": "Sets a constant value or enforces a reviewer business policy for fields without a direct source equivalent.", "apply": _r_default},
}


def describe_rules() -> list[dict]:
    return [{"id": k, "label": r["label"], "uses_source": r["uses_source"], "params": r["params"], "description": r["description"]} for k, r in RULES.items()]


def _param_error(rule_id: str, p: dict | None):
    p = p or {}
    if rule_id == "split_name":
        return None if p.get("part") in ("first", "last") else 'param "part" must be "first" or "last"'
    if rule_id in ("parse_date", "parse_datetime"):
        return None if p.get("format") in FORMATS else 'param "format" must be DMY, MDY or YMD'
    if rule_id == "map_enum":
        m = p.get("map")
        return None if isinstance(m, dict) and m else 'param "map" must be a non-empty object'
    if rule_id == "decimal_to_minor":
        d = p.get("decimals")
        return None if d is None or (isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6) else 'param "decimals" must be an integer 0 to 6'
    if rule_id == "normalize_phone":
        reg = p.get("default_region")
        cc = p.get("default_cc")
        if reg is not None and reg != "":
            s = str(reg).strip().upper()
            if not (re.fullmatch(r"[A-Z]{2}", s) or re.fullmatch(r"\d{1,3}", s)):
                return 'param "default_region" must be an ISO 2-letter country code (e.g. IN, US) or 1 to 3 digits'
        if cc is not None and cc != "":
            c = str(cc).lstrip("+").strip()
            if not re.fullmatch(r"\d{1,3}", c, _A):
                return 'param "default_cc" must be 1 to 3 digits'
        return None
    if rule_id == "default_value":
        if p.get("policy") in ("quarantine", "unresolved", "preserve_unknown"):
            return None
        if "value" not in p:
            return 'param "value" is required (string, number or boolean)'
        v = p["value"]
        return None if v is None or isinstance(v, (str, int, float, bool)) else 'param "value" is required (string, number or boolean)'
    return None


PARAM_KEYS = {
    "split_name": ["part"],
    "parse_date": ["format"],
    "parse_datetime": ["format"],
    "map_enum": ["map"],
    "decimal_to_minor": ["decimals"],
    "normalize_phone": ["default_region", "default_cc"],
    "default_value": ["value", "policy", "rationale", "approved_by", "approved_at", "semantic_consequences"],
}


# ---------------------------------------------------------------- mappings and plans
def canon(mappings) -> list[dict]:
    out = [{"target": m["target"], "source": m.get("source"), "rule": m["rule"], "params": m.get("params") or {}} for m in (mappings or []) if m and m.get("target") and m.get("rule")]
    out.sort(key=lambda m: TARGET_ORDER.index(m["target"]) if m["target"] in TARGET_ORDER else -1)
    return out


def plan_hash(mappings) -> str:
    return hash_str(stable(canon(mappings)))


def source_hash(records) -> str:
    return hash_str(stable(records))


def mapping_label(m) -> str:
    if not m or not m.get("rule"):
        return "not mapped"
    p = m.get("params") or {}
    if m["rule"] == "map_enum":
        bits = [", ".join(f"{k}→{v}" for k, v in (p.get("map") or {}).items())]
    else:
        bits = [f"{k}={json.dumps(v)}" for k, v in p.items()]
    return f"{m['rule']}({m.get('source') or '—'}{'; ' + '; '.join(bits) if bits else ''})"


def diff_mappings(a, b) -> list[dict]:
    ma = {m["target"]: m for m in canon(a)}
    mb = {m["target"]: m for m in canon(b)}
    return [{"target": t, "from": mapping_label(ma.get(t)), "to": mapping_label(mb.get(t))} for t in TARGET_ORDER if stable(ma.get(t)) != stable(mb.get(t))]


def validate_plan(mappings) -> dict:
    issues: list[dict] = []
    seen: set = set()
    used: set = set()
    for m in mappings or []:
        t = m.get("target")
        if t not in TF:
            issues.append({"level": "error", "code": "unknown_target", "target": t, "message": f'Target field "{t}" does not exist'})
            continue
        if t in seen:
            issues.append({"level": "error", "code": "duplicate_target", "target": t, "message": f'Target field "{t}" is mapped more than once'})
        seen.add(t)
        r = RULES.get(m.get("rule"))
        if not r:
            issues.append({"level": "error", "code": "unsupported_rule", "target": t, "message": f'Rule "{m.get("rule")}" is not in the supported catalogue'})
            continue
        pe = _param_error(m["rule"], m.get("params"))
        if pe:
            issues.append({"level": "error", "code": "bad_params", "target": t, "message": f"{t}: {pe}"})
        if r["uses_source"]:
            s = m.get("source")
            if not s:
                issues.append({"level": "error", "code": "missing_source", "target": t, "message": f'{t}: rule "{m["rule"]}" needs a source field'})
            elif s not in SF:
                issues.append({"level": "error", "code": "unknown_source", "target": t, "message": f'{t}: source field "{s}" does not exist'})
            else:
                used.add(s)
    for f in TARGET_SCHEMA["fields"]:
        if f["required"] and f["name"] not in seen:
            issues.append({"level": "error", "code": "required_unmapped", "target": f["name"], "message": f'Required target field "{f["name"]}" has no mapping'})
    for f in SOURCE_SCHEMA["fields"]:
        if f["name"] not in used:
            issues.append({"level": "warn", "code": "source_unused", "field": f["name"], "message": f'Source field "{f["name"]}" is not carried into the target'})
    for m in mappings or []:
        if m.get("rule") == "default_value" and m.get("target") in TF:
            issues.append({"level": "warn", "code": "assumed_default", "target": m["target"], "message": f'"{m["target"]}" is set to {json.dumps((m.get("params") or {}).get("value"))} for every record'})
    return {"ok": not any(i["level"] == "error" for i in issues), "issues": issues}


# ---------------------------------------------------------------- target validation / transformation
def _validate_value(f: dict, v):
    if v is None:
        return {"code": "required_missing", "message": f'"{f["name"]}" is required but the value is empty'} if f["required"] else None
    t = f["type"]
    if t == "string":
        if not isinstance(v, str):
            return {"code": "wrong_type", "message": f'"{f["name"]}" must be text'}
        if f.get("max_length") and len(v) > f["max_length"]:
            return {"code": "too_long", "message": f'"{f["name"]}" is longer than {f["max_length"]} characters'}
        if f.get("enum") and v not in f["enum"]:
            return {"code": "not_in_enum", "message": f'"{v}" is not an allowed value for {f["name"]}'}
        if f.get("pattern") and not re.search(f["pattern"].replace("$", r"\Z"), v, _A):
            return {"code": "pattern_mismatch", "message": f'"{clip(v, 60)}" does not match the required format: {f.get("hint") or f["pattern"]}'}
    elif t == "integer":
        if not isinstance(v, int) or isinstance(v, bool):
            return {"code": "wrong_type", "message": f'"{f["name"]}" must be a whole number'}
        if v < f["min"] or v > f["max"]:
            return {"code": "out_of_range", "message": f'{v} is outside the allowed range for {f["name"]}'}
    elif t == "boolean":
        if not isinstance(v, bool):
            return {"code": "wrong_type", "message": f'"{f["name"]}" must be true or false'}
    elif t == "date":
        if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v, _A):
            return {"code": "pattern_mismatch", "message": f'"{v}" is not an ISO date'}
        if (f.get("min") and v < f["min"]) or (f.get("max") and v > f["max"]):
            return {"code": "out_of_range", "message": f'{v} is outside {f.get("min")} to {f.get("max")}'}
    elif t == "datetime":
        if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", v, _A):
            return {"code": "pattern_mismatch", "message": f'"{v}" is not an ISO UTC timestamp'}
    return None


def transform_record(mappings, rec: dict) -> dict:
    by = {m["target"]: m for m in canon(mappings)}
    out: dict = {}
    errors: list[dict] = []
    for f in TARGET_SCHEMA["fields"]:
        m = by.get(f["name"])
        if not m:
            if f["required"]:
                errors.append({"field": f["name"], "stage": "transform", "code": "unmapped_required", "message": f'Required field "{f["name"]}" has no mapping'})
            continue
        rule = RULES.get(m["rule"])
        if not rule:
            errors.append({"field": f["name"], "source": m.get("source"), "rule": m["rule"], "stage": "transform", "code": "unsupported_rule", "message": f'Rule "{m["rule"]}" is not supported'})
            continue
        raw = rec.get(m.get("source")) if rule["uses_source"] else None
        sv = None if raw is None else clip(raw, 120)
        try:
            params = dict(m.get("params") or {})
            params["_target"] = f["name"]
            ok, val, code, msg = rule["apply"](raw, params)
        except Exception as e:  # a rule must never crash a dry run
            ok, val, code, msg = _bad("rule_exception", str(e))
        if not ok:
            stage = "policy" if code in ("quarantined_by_policy", "unresolved_business_policy") else "transform"
            errors.append({"field": f["name"], "source": m.get("source"), "rule": m["rule"], "stage": stage, "code": code, "message": msg, "source_value": sv})
            continue
        out[f["name"]] = val
        ve = _validate_value(f, val)
        if ve:
            errors.append({"field": f["name"], "source": m.get("source"), "rule": m["rule"], "stage": "validate", "code": ve["code"], "message": ve["message"], "source_value": sv, "value": clip(val, 120) if isinstance(val, str) else val})
    return {"record": out, "errors": errors}


# ---------------------------------------------------------------- deterministic dry run
def dry_run(mappings, records) -> dict:
    if not isinstance(records, list):
        return {"error": "invalid_source", "message": "Source records must be an array"}
    if len(records) > MAX_RECORDS:
        return {"error": "too_many_records", "message": f"Sample size is limited to {MAX_RECORDS} records"}
    v = validate_plan(mappings)
    if not v["ok"]:
        return {"error": "invalid_plan", "message": "The mapping has errors that must be fixed first", "issues": [i for i in v["issues"] if i["level"] == "error"]}
    by = {m["target"]: m for m in canon(mappings)}
    pk_map = by.get(PK)
    seen: dict = {}
    accepted: list[dict] = []
    rejected: list[dict] = []
    record_dispositions: list[dict] = []
    
    for i, rec in enumerate(records):
        t = transform_record(mappings, rec)
        errors = list(t["errors"])
        transform_ok = not any(e["stage"] == "transform" for e in errors)
        key = t["record"].get(PK)
        if key is not None:
            if key in seen:
                errors.append({"field": PK, "source": pk_map and pk_map.get("source"), "rule": pk_map and pk_map["rule"], "stage": "validate", "code": "duplicate_key", "message": f"Key {key} already appeared in row {seen[key]}", "source_value": clip(rec.get(pk_map["source"]), 120) if pk_map and pk_map.get("source") and rec.get(pk_map["source"]) is not None else None, "value": key})
            else:
                seen[key] = i + 1
        
        disposition = "quarantined" if errors else "accepted"
        record_dispositions.append({
            "row": i + 1,
            "transformed": transform_ok,
            "disposition": disposition,
            "key": key if not errors else None,
        })
        
        if errors:
            stages = {e.get("stage") for e in errors}
            primary_stage = "transform" if "transform" in stages else ("validate" if "validate" in stages else "policy")
            sk = clip(rec.get(pk_map["source"]), 60) if pk_map and pk_map.get("source") and rec.get(pk_map["source"]) is not None else None
            rejected.append({"row": i + 1, "source_key": sk, "errors": errors, "stage": primary_stage})
        else:
            accepted.append({"row": i + 1, "key": key, "record": t["record"], "hash": hash_str(stable(t["record"]))})

    # Derive all counts directly from record-level results
    source_count = len(records)
    transformed_count = sum(1 for r in record_dispositions if r["transformed"])
    accepted_count = sum(1 for r in record_dispositions if r["disposition"] == "accepted")
    quarantined_count = sum(1 for r in record_dispositions if r["disposition"] == "quarantined")

    # Invariant: source == accepted + quarantined (no record counted in multiple terminal states)
    if source_count != accepted_count + quarantined_count:
        raise CountInvariantError(
            f"Record count invariant failed: source ({source_count}) != accepted ({accepted_count}) + quarantined ({quarantined_count})"
        )

    counts = CountsDict({
        "source": source_count,
        "transformed": transformed_count,
        "accepted": accepted_count,
        "rejected": quarantined_count,
    })

    summ: dict = {}
    for r in rejected:
        for e in r["errors"]:
            k = (e["field"], e["code"])
            summ[k] = summ.get(k, 0) + 1
    error_summary = sorted(({"field": f, "code": c, "count": n} for (f, c), n in summ.items()), key=lambda e: (-e["count"], e["field"] + e["code"]))
    ph, sh = plan_hash(mappings), source_hash(records)
    fp = hash_str(stable({"planHash": ph, "sourceHash": sh, "a": [[a["row"], a["hash"]] for a in accepted], "r": [[r["row"], [e["field"] + ":" + e["code"] for e in r["errors"]]] for r in rejected]}))
    return {"counts": counts, "accepted": accepted, "rejected": rejected, "error_summary": error_summary, "plan_hash": ph, "source_hash": sh, "hash": fp}


def preview_against_target(accepted, target_records: dict) -> dict:
    out = {"insert": 0, "skip": 0, "conflict": 0}
    for a in accepted:
        ex = target_records.get(a["key"])
        if not ex:
            out["insert"] += 1
        elif ex["hash"] == a["hash"]:
            out["skip"] += 1
        else:
            out["conflict"] += 1
    return out


# ---------------------------------------------------------------- reference semantics for the mock target
def classify_existing(existing_hash: str, existing_run: str | None, new_hash: str, run_id: str) -> str:
    """What happens when a key already exists. Used by both apply_batch and the SQL executor."""
    if existing_hash != new_hash:
        return "conflict"
    return "skipped_own" if existing_run == run_id else "skipped_present"


def apply_batch(target: dict, accepted, run_id: str, fail_after: int | None = None) -> dict:
    records = dict(target["records"])
    seq = target.get("seq", 0)
    out = {"inserted": [], "skipped_present": [], "skipped_own": [], "conflicts": [], "not_attempted": [], "error": None}
    n = 0
    for i, a in enumerate(accepted):
        ex = records.get(a["key"])
        if ex:
            c = classify_existing(ex["hash"], ex["run_ref"], a["hash"], run_id)
            if c == "conflict":
                out["conflicts"].append({"row": a["row"], "key": a["key"], "existing_run": ex["run_ref"], "diff_fields": [k for k in a["record"] if stable(ex["record"].get(k)) != stable(a["record"].get(k))]})
            else:
                out[c].append(a["key"])
            continue
        if fail_after is not None and n >= fail_after:
            out["error"] = {"code": "target_unavailable", "message": f"Simulated target outage after {n} inserts"}
            out["not_attempted"] = [x["key"] for x in accepted[i:]]
            break
        seq += 1
        records[a["key"]] = {"record": a["record"], "hash": a["hash"], "run_ref": run_id, "seq": seq}
        out["inserted"].append(a["key"])
        n += 1
    return {"target": {"records": records, "seq": seq}, "outcome": out}


def rollback_run(target: dict, run_id: str) -> dict:
    keep: dict = {}
    removed: list = []
    for k, v in target["records"].items():
        if v["run_ref"] == run_id:
            removed.append(k)
        else:
            keep[k] = v
    return {"target": {"records": keep, "seq": target.get("seq", 0)}, "removed": removed}


def snapshot_hashes(target: dict) -> dict:
    return {k: v["hash"] for k, v in target["records"].items()}


def reconcile(run: dict, dry: dict, target_records: dict) -> dict:
    """Reconciliation formulas (all explicit):
      F1 source = accepted + quarantined                         (dry-run counts)
      F2 every accepted key is, in the target, exactly one of: inserted-by-run | present-identical | conflict | missing
      F3 ledger(keys inserted by the run) == target rows tagged with the run
      F4 control total: sum(balance_minor) over accepted rows that are inserted/present == same sum read from the target
      F5 quarantine evidence kept for every rejected row
      F6 pre-existing rows (hash snapshot taken before the run) are unchanged
    After rollback, F2-F4 are replaced by: no rows tagged to the run remain, and rollback removed == ledger.
    """
    recs = target_records
    total = len(recs)
    cls = {"inserted": [], "present": [], "conflict": [], "missing": []}
    for a in dry["accepted"]:
        t = recs.get(a["key"])
        if not t:
            cls["missing"].append(a["key"])
        elif t["hash"] != a["hash"]:
            cls["conflict"].append(a["key"])
        elif t["run_ref"] == run["id"]:
            cls["inserted"].append(a["key"])
        else:
            cls["present"].append(a["key"])
    own = [k for k, v in recs.items() if v["run_ref"] == run["id"]]
    ledger = set(run.get("inserted_keys") or [])
    checks: list[dict] = []

    def add(id_, label, ok, detail):
        checks.append({"id": id_, "label": label, "ok": bool(ok), "detail": detail})

    c = dry["counts"]
    add("source_balance", "Source rows = accepted + quarantined", c["source"] == c["accepted"] + c["rejected"], f'{c["source"]} = {c["accepted"]} + {c["rejected"]}')
    q = run.get("quarantine") or []
    add("quarantine_evidence", "Every quarantined record kept field-level evidence", len(q) == c["rejected"] and all(x.get("errors") and all(e.get("field") and e.get("code") for e in x["errors"]) for x in q), f'{len(q)} evidence records for {c["rejected"]} rejected rows')
    pre = run.get("pre") or {}
    add("pre_existing_intact", "Records that existed before the run are untouched", all(k in recs and recs[k]["hash"] == h for k, h in pre.items()), f"{len(pre)} pre-existing records checked")
    sums = None
    if run["status"] == "rolled_back":
        add("rollback_removed_all", "No record tagged to this run remains in the target", len(own) == 0, f"{len(own)} remaining")
        rb = run.get("rollback") or {}
        add("rollback_matches_ledger", "Rollback removed exactly what the run inserted", rb.get("removed") == len(ledger), f'{rb.get("removed", 0)} removed, {len(ledger)} inserted')
    else:
        add("all_accounted", "Every accepted record is in the target or quarantined as a conflict", not cls["missing"], f'{len(cls["missing"])} accepted records are not in the target yet' if cls["missing"] else f'{len(cls["inserted"]) + len(cls["present"])} in target, {len(cls["conflict"])} conflicts')
        add("ledger_matches_target", "Run ledger matches records tagged to this run", len(ledger) == len(own) and all(k in ledger for k in own), f"ledger {len(ledger)}, target {len(own)}")
        ok_keys = set(cls["inserted"] + cls["present"])
        a_sum = sum(x["record"]["balance_minor"] for x in dry["accepted"] if x["key"] in ok_keys)
        t_sum = sum(recs[x["key"]]["record"]["balance_minor"] for x in dry["accepted"] if x["key"] in ok_keys)
        sums = {"accepted": a_sum, "target": t_sum}
        add("control_total", "Balance control total matches (minor units)", a_sum == t_sum, f"accepted {a_sum}, target {t_sum}")
        add("conflicts_not_overwritten", "Conflicting records were not overwritten", all(recs[k]["run_ref"] != run["id"] for k in cls["conflict"]), f'{len(cls["conflict"])} conflicting keys left as found')
    rolled = run["status"] == "rolled_back"
    return {"checks": checks, "passed": all(x["ok"] for x in checks), "sums": sums, "totals": {
        "source_rows": c["source"], "transformed": c["transformed"], "accepted": c["accepted"], "quarantined": c["rejected"],
        "conflicts": len(cls["conflict"]), "inserted": len(cls["inserted"]), "already_present": len(cls["present"]),
        "not_applied": c["accepted"] if rolled else len(cls["missing"]), "target_before": run.get("pre_count"), "target_now": total}}


# ---------------------------------------------------------------- server-side gates
def approval_gate(plan_hash_: str, mappings, stored_dry: dict | None, current_source_hash: str, open_questions: int = 0) -> dict:
    v = validate_plan(mappings)

    # Check phone default region configuration
    phone_map = next((m for m in (mappings or []) if m.get("target") == "phone_e164"), None)
    phone_region_ok = True
    if phone_map and phone_map.get("rule") == "normalize_phone":
        p_params = phone_map.get("params") or {}
        cc, reg = _resolve_phone_region(p_params)
        if not cc and not reg:
            phone_region_ok = False

    # Check required business policies
    unresolved_policies = []
    for m in (mappings or []):
        t = m.get("target")
        f = TF.get(t)
        if f and f.get("required") and m.get("rule") == "default_value":
            p_params = m.get("params") or {}
            pol = p_params.get("policy")
            if pol == "unresolved":
                unresolved_policies.append(t)
    policies_ok = len(unresolved_policies) == 0

    # Invariants check on dry run counts
    invariants_ok = True
    if stored_dry and "counts" in stored_dry:
        c = stored_dry["counts"]
        invariants_ok = (c.get("source") == c.get("accepted", 0) + c.get("quarantined", c.get("rejected", 0)))

    checks = [
        {"id": "valid", "label": "Mapping has no errors", "ok": v["ok"]},
        {"id": "dry", "label": "A dry run exists for this exact version", "ok": bool(stored_dry) and stored_dry["plan_hash"] == plan_hash_},
        {"id": "fresh", "label": "Dry run used the current source data", "ok": bool(stored_dry) and stored_dry["source_hash"] == current_source_hash},
        {"id": "accepts", "label": "The dry run accepts at least one record", "ok": bool(stored_dry) and stored_dry["counts"]["accepted"] > 0},
        {"id": "phone_region", "label": "Default phone region is configured", "ok": phone_region_ok},
        {"id": "business_policies", "label": "Required business policies are resolved and approved", "ok": policies_ok},
        {"id": "invariants", "label": "Record count invariants verified", "ok": invariants_ok},
    ]
    return {"can_approve": all(c["ok"] for c in checks), "checks": checks, "open_questions": open_questions}


# ---------------------------------------------------------------- profiling
def _shape(v) -> str:
    s = clip(v, 24)
    s = re.sub(r"[A-Z]", "A", s, flags=_A)
    s = re.sub(r"[a-z]", "a", s, flags=_A)
    return re.sub(r"\d", "9", s, flags=_A)


def profile_field(records, field: str) -> dict:
    if field not in SF:
        return {"error": f'Unknown source field "{field}"'}
    counts: dict = {}
    shapes: dict = {}
    blank = 0
    for r in records:
        v = r.get(field)
        if is_blank(v):
            blank += 1
            continue
        s = str(v)
        counts[s] = counts.get(s, 0) + 1
        sh = _shape(s)
        shapes[sh] = shapes.get(sh, 0) + 1

    def top(d):
        return sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:8]

    return {"field": field, "rows": len(records), "blank": blank, "distinct": len(counts),
            "top_values": [{"value": clip(k, 60), "count": n} for k, n in top(counts)],
            "top_shapes": [{"shape": k, "count": n} for k, n in top(shapes)]}


def date_order_evidence(records, field: str) -> dict:
    dmy = mdy = total = 0
    for r in records:
        m = re.match(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", str(r.get(field) if r.get(field) is not None else "").strip(), _A)
        if not m:
            continue
        total += 1
        dmy += int(m[1]) > 12
        mdy += int(m[2]) > 12
    return {"total": total, "dmy": dmy, "mdy": mdy}


# ---------------------------------------------------------------- proposals (agent or baseline)
def normalize_proposal(raw) -> dict:
    """Sanitise a proposal from ANY source (model or baseline). Unsupported content is dropped and the reason recorded."""
    raw = raw if isinstance(raw, dict) else {}
    dropped: list[str] = []
    mappings: list[dict] = []
    seen: set = set()
    for m in raw.get("mappings") if isinstance(raw.get("mappings"), list) else []:
        if not isinstance(m, dict):
            continue
        t = m.get("target")
        if t not in TF:
            dropped.append(f'Dropped mapping for unknown target field "{t}"')
            continue
        if m.get("rule") not in RULES:
            dropped.append(f'Dropped {t}: "{m.get("rule")}" is not a supported rule')
            continue
        if t in seen:
            dropped.append(f"Dropped duplicate mapping for {t}")
            continue
        r = RULES[m["rule"]]
        src = None if m.get("source") in (None, "") else str(m["source"])
        if r["uses_source"] and src and src not in SF:
            dropped.append(f'Dropped {t}: source field "{src}" does not exist')
            continue
        mp = m.get("params") if isinstance(m.get("params"), dict) else {}
        params = {k: mp[k] for k in PARAM_KEYS.get(m["rule"], []) if k in mp}
        if m["rule"] == "map_enum" and isinstance(params.get("map"), dict):
            params["map"] = {str(k).strip().lower(): str(v) for k, v in params["map"].items()}
        if m["rule"] == "normalize_phone" and params.get("default_cc") is not None:
            params["default_cc"] = str(params["default_cc"]).lstrip("+")
        try:
            conf = max(0.0, min(1.0, float(m.get("confidence") or 0)))
        except (TypeError, ValueError):
            conf = 0.0
        seen.add(t)
        mappings.append({"target": t, "source": src if r["uses_source"] else None, "rule": m["rule"], "params": params, "confidence": conf, "rationale": clip(m.get("rationale") or "", 300)})
    mappings.sort(key=lambda m: TARGET_ORDER.index(m["target"]))

    def arr(x):
        return x if isinstance(x, list) else []

    risks = [{"level": x["level"] if x.get("level") in ("high", "medium", "low") else "medium", "field": x.get("field"), "text": clip(x["text"], 400)} for x in arr(raw.get("risks")) if isinstance(x, dict) and x.get("text")][:20]
    qsrc = [x for x in arr(raw.get("questions")) if isinstance(x, dict) and x.get("text")][:12]
    questions = [{"id": f"q{i + 1}", "field": x.get("field"), "text": clip(x["text"], 400), "assumption": clip(x["assumption"], 300) if x.get("assumption") else None} for i, x in enumerate(qsrc)]
    incompat = [{"kind": clip(x.get("kind") or "note", 40), "field": x.get("field"), "text": clip(x["text"], 400)} for x in arr(raw.get("incompatibilities")) if isinstance(x, dict) and x.get("text")][:20]
    return {"summary": clip(raw.get("summary") or "", 800), "mappings": mappings, "risks": risks, "questions": questions, "incompatibilities": incompat, "dropped": dropped}


_HINTS = {
    "account_id": ["cust_id", "account_id", "customer_id"], "first_name": ["full_name", "first_name", "name"], "last_name": ["full_name", "last_name", "name"],
    "email": ["email"], "phone_e164": ["phone", "tel", "mobile"], "birth_date": ["dob", "birth"], "country_code": ["country"], "status": ["status"],
    "balance_minor": ["balance", "amount"], "created_at": ["signup", "created", "registered"], "tier": ["tier", "plan", "level"], "marketing_opt_in": ["opt", "consent", "newsletter", "marketing"],
}


def heuristic_propose(records) -> dict:
    mappings, risks, questions, incompat = [], [], [], []
    used: set = set()

    def q(field, text, assumption):
        questions.append({"field": field, "text": text, "assumption": assumption})

    for tf in TARGET_SCHEMA["fields"]:
        hints = _HINTS.get(tf["name"], [tf["name"]])
        best, best_score = None, 0
        for sf in SOURCE_SCHEMA["fields"]:
            s = 0
            for h in hints:
                if sf["name"] == h:
                    s = max(s, 3)
                elif h in sf["name"]:
                    s = max(s, 2)
            if s > best_score:
                best, best_score = sf, s
        if not best:
            value = tf["enum"][0] if tf.get("enum") else (False if tf["type"] == "boolean" else None)
            mappings.append({"target": tf["name"], "source": None, "rule": "default_value", "params": {"value": value}, "confidence": 0.4, "rationale": f"The source has no field for {tf['name']}. Requires a human decision."})
            incompat.append({"kind": "missing_in_source", "field": tf["name"], "text": f'Required target field "{tf["name"]}" has no source equivalent.'})
            risks.append({"level": "high", "field": tf["name"], "text": f'Required field {tf["name"]} has no source equivalent and is defaulted to {json.dumps(value)}. A reviewer must verify or change this policy.'})
            q(tf["name"], f'The legacy export has no {tf["name"]}. Should every account be set to {json.dumps(value)}, or should a different policy be approved?', json.dumps(value))
            continue
        used.add(best["name"])
        m = {"target": tf["name"], "source": best["name"], "rule": "trim", "params": {}, "confidence": 0.9, "rationale": ""}
        if tf.get("pk"):
            m["rule"] = "uppercase"
            m["rationale"] = "Target keys are uppercase; some legacy ids are lowercase."
        elif tf["name"].endswith("_name") and best["name"] == "full_name":
            m.update(rule="split_name", params={"part": "first" if tf["name"].startswith("first") else "last"}, confidence=0.75, rationale='The source holds one full-name field in two orders. "First Last" is split on the final space.')
            if tf["name"] == "first_name":
                risks.append({"level": "medium", "field": "full_name", "text": "Names without a comma are split at the last space, so multi-word surnames land in first_name. Single-word names cannot be split and are rejected."})
        elif tf.get("format") == "email":
            m.update(rule="lowercase", rationale="Target emails must be lowercase and unpadded.")
        elif tf.get("format") == "e164":
            m.update(rule="normalize_phone", params={"default_cc": "91"}, confidence=0.75, rationale="Numbers with + or 00 retain their country code. Local numbers without country code use default calling code 91 (India) established by the source dataset.")
            local = sum(1 for r in records if not is_blank(r.get(best["name"])) and not re.match(r"\s*(\+|00)", str(r[best["name"]])))
            if local:
                risks.append({"level": "medium", "field": tf["name"], "text": f"{local} phone numbers have no international prefix. Default calling code 91 (India) applied from source data profile."})
                q(tf["name"], "Default country calling code for local numbers without a country code prefix (source dataset is predominantly India):", "91")
        elif tf["type"] in ("date", "datetime"):
            ev = date_order_evidence(records, best["name"])
            fmt = "DMY" if (ev["dmy"] > 0 and ev["mdy"] == 0) else "MDY" if (ev["mdy"] > 0 and ev["dmy"] == 0) else "DMY"
            decisive = ev["dmy"] + ev["mdy"] > 0 and not (ev["dmy"] and ev["mdy"])
            m.update(rule="parse_date" if tf["type"] == "date" else "parse_datetime", params={"format": fmt}, confidence=0.9 if decisive else 0.5)
            m["rationale"] = (f'{ev["dmy"]} of {ev["total"]} dates have a first part above 12 and {ev["mdy"]} have a second part above 12, which points to {fmt}.' if ev["dmy"] + ev["mdy"] > 0 else f"No value disambiguates day from month; assuming {fmt}.")
            if ev["dmy"] + ev["mdy"] == 0 or (ev["dmy"] and ev["mdy"]):
                q(tf["name"], f'Dates in {best["name"]} are ambiguous between day-first and month-first. Which order do they use?', fmt)
            else:
                risks.append({"level": "low", "field": tf["name"], "text": f"Date order ({fmt}) was inferred from the sample. Ambiguous values like 04/05/2020 would be read the same way."})
            if tf["type"] == "datetime":
                risks.append({"level": "medium", "field": tf["name"], "text": "The source has no timezone. Times are assumed to be UTC."})
        elif tf.get("format") == "iso2":
            m.update(rule="country_to_iso2", rationale="Source mixes country names and codes; a fixed lookup normalizes both.")
        elif tf.get("enum"):
            distinct = {str(r[best["name"]]).strip() for r in records if not is_blank(r.get(best["name"]))}
            mp = {}
            for v in distinct:
                hit = next((o for o in tf["enum"] if o == v.lower() or (o[0] == v[0].lower() and len(v) == 1)), None)
                if hit:
                    mp[v.lower()] = hit
            m.update(rule="map_enum", params={"map": mp}, confidence=0.8, rationale="One-letter codes matched to the allowed values by first letter.")
            risks.append({"level": "medium", "field": tf["name"], "text": "The code-to-status meaning is inferred from the first letter, not from documentation."})
            q(tf["name"], "Please confirm what the status codes mean: " + ", ".join(f"{k.upper()} = {v}" for k, v in mp.items()) + ".", "Letter matches the first letter of the status")
        elif tf.get("format") == "minor_units":
            m.update(rule="decimal_to_minor", params={"decimals": 2}, rationale="Decimal strings become integer cents using exact string arithmetic.")
            risks.append({"level": "medium", "field": tf["name"], "text": "Assumes two decimal places and a single currency. Values with commas that are not clear thousands separators are rejected, not guessed."})
        m["rationale"] = m["rationale"] or "Direct name match; trimmed."
        mappings.append(m)
    for sf in SOURCE_SCHEMA["fields"]:
        if sf["name"] not in used:
            incompat.append({"kind": "dropped_source", "field": sf["name"], "text": f'Source field "{sf["name"]}" has no target field and will not be migrated.'})
    dropped = [i["field"] for i in incompat if i["kind"] == "dropped_source"]
    if dropped:
        q(None, f'These source fields have no home in the target and will be dropped: {", ".join(dropped)}. Is that acceptable?', "Dropped")
    for m in mappings:
        if m["confidence"] < 0.6:
            incompat.append({"kind": "uncertain_classification", "field": m["target"], "text": f'Low confidence ({round(m["confidence"] * 100)}%): {m["rationale"]}'})
    draft = normalize_proposal({"mappings": mappings, "risks": risks, "questions": questions, "incompatibilities": incompat})
    dr = dry_run(draft["mappings"], records)
    if "error" not in dr:
        for e in dr["error_summary"][:8]:
            ex = next((r for r in dr["rejected"] if any(x["field"] == e["field"] and x["code"] == e["code"] for x in r["errors"])), None)
            err = next((x for x in ex["errors"] if x["field"] == e["field"] and x["code"] == e["code"]), None) if ex else None
            sv = f', for example {json.dumps(err["source_value"])}' if err and err.get("source_value") is not None else ""
            draft["incompatibilities"].append({"kind": "value_not_covered", "field": e["field"], "text": f'{e["count"]} record(s) fail "{e["code"]}" on {e["field"]}{sv}. They will be quarantined.'})
    missing = sum(1 for i in incompat if i["kind"] in ("missing_in_source", "missing_business_data"))
    draft["summary"] = f"Baseline proposal from field-name hints and value profiles (no language model). {len(draft['mappings'])} of {len(TARGET_SCHEMA['fields'])} target fields mapped; {missing} required fields have no source and require explicit business policy decisions."
    return draft


# ---------------------------------------------------------------- reference mapping and seed data
REFERENCE_MAPPING = [
    {"target": "account_id", "source": "cust_id", "rule": "uppercase", "params": {}},
    {"target": "first_name", "source": "full_name", "rule": "split_name", "params": {"part": "first"}},
    {"target": "last_name", "source": "full_name", "rule": "split_name", "params": {"part": "last"}},
    {"target": "email", "source": "email_addr", "rule": "lowercase", "params": {}},
    {"target": "phone_e164", "source": "phone_no", "rule": "normalize_phone", "params": {"default_cc": "91"}},
    {"target": "birth_date", "source": "dob", "rule": "parse_date", "params": {"format": "DMY"}},
    {"target": "country_code", "source": "country", "rule": "country_to_iso2", "params": {}},
    {"target": "status", "source": "status_cd", "rule": "map_enum", "params": {"map": {"a": "active", "i": "inactive", "s": "suspended"}}},
    {"target": "balance_minor", "source": "balance", "rule": "decimal_to_minor", "params": {"decimals": 2}},
    {"target": "created_at", "source": "signup_date", "rule": "parse_datetime", "params": {"format": "DMY"}},
    {"target": "tier", "source": None, "rule": "default_value", "params": {"value": "standard"}},
    {"target": "marketing_opt_in", "source": None, "rule": "default_value", "params": {"value": False}},
]


def build_seed_target(records) -> list[dict]:
    """Rows pre-loaded in the mock target: one identical to source row 10, one conflicting with source row 7, one unrelated."""
    def mk(rec):
        return {"account_id": rec["account_id"], "record": rec, "hash": hash_str(stable(rec)), "run_ref": "seed"}
    same = transform_record(REFERENCE_MAPPING, records[9])["record"]
    clash = transform_record(REFERENCE_MAPPING, records[6])["record"]
    clash["email"] = "legacy.hold@example.com"
    other = {"account_id": "ACC-9001", "first_name": "Seed", "last_name": "Account", "email": "seed.account@example.com", "phone_e164": "+442071234567", "birth_date": None, "country_code": "GB", "status": "active", "balance_minor": 0, "created_at": "2020-01-01T00:00:00Z", "tier": "premium", "marketing_opt_in": True}
    return [mk(same), mk(clash), mk(other)]
