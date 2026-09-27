"""Code API Checker (FR-C7) — catches made-up Python functions and parameters.

Owner: Role C. Hallucination type #6 in PRD §7.0 ("made-up code APIs"): a function, method or
keyword argument that doesn't exist in the library, e.g. `requests.get(url, retries=3)`.

How it works (Python only, PRD §3.2):
  1. Parse each code block with `ast`. The AI's code is NEVER executed (§14 rule 14).
  2. Track imports (`import pandas as pd`, `from requests import get`) and simple instances
     (`s = requests.Session()`), then collect every call like `pd.read_csv(..., sep=",")`.
  3. For each library:
       - allow-listed + installed → import the LIBRARY (never the AI's code), walk the attribute
         chain with getattr; missing attribute → nonexistent_api (+ "did you mean …")
       - for callables: inspect.signature; unknown keyword → nonexistent_api (+ real signature).
         Functions that take **kwargs are followed to where the kwargs go (requests.get →
         Session.request); otherwise **kwargs means "can't tell", never a false alarm.
       - not installed → PyPI existence only (FR-C1): missing → contradicted, else unverified.
Safety: only libraries on ALLOWED_THIRD_PARTY + the standard library are ever imported, so a
reply can't make the engine import arbitrary modules.
"""

from __future__ import annotations

import ast
import asyncio
import difflib
import importlib
import importlib.metadata
import inspect
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

from app.detectors.base import BaseDetector, http_client, snippet
from app.detectors.claim_gate import requested_untrue
from app.detectors.reference_auditor import package_exists
from app.models import Claim, DetectorResult, Evidence, SessionContext

# FR-C7: pre-installed in the engine venv (see engine/pyproject.toml).
ALLOWED_THIRD_PARTY = {
    "requests",
    "numpy",
    "pandas",
    "fastapi",
    "pydantic",
    "httpx",
    "flask",
    "anthropic",
    "openai",
}
# Standard-library modules that do something visible when imported (open a browser, print a
# poem, start a GUI) or are otherwise pointless to inspect. Everything else in the stdlib is fine.
STDLIB_DENY = {"antigravity", "this", "idlelib", "tkinter", "turtle", "turtledemo", "pydoc_data"}
STDLIB = set(sys.stdlib_module_names) - STDLIB_DENY

# Import name → PyPI project name, where they differ.
PYPI_NAMES = {
    "sklearn": "scikit-learn",
    "cv2": "opencv-python",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "jwt": "pyjwt",
    "Crypto": "pycryptodome",
    "google": "google-api-python-client",
}

# Functions whose **kwargs are simply passed on to another callable: check the keywords
# against that one instead (e.g. requests.get(url, **kwargs) → Session.request).
KWARGS_FORWARD = {
    **{
        f"requests.{m}": "requests.sessions.Session.request"
        for m in ("get", "post", "put", "patch", "delete", "head", "options", "request")
    },
    **{
        f"requests.sessions.Session.{m}": "requests.sessions.Session.request"
        for m in ("get", "post", "put", "patch", "delete", "head", "options")
    },
}

MAX_CALLS = 60  # bound the work per code block

log = logging.getLogger("reigns.detectors.code_api_checker")


# =============================================================================================
# Parsing (pure ast — nothing is executed)
# =============================================================================================
@dataclass
class Call:
    chain: list[str]  # ["requests", "get"] — first item is the real module path's root
    keywords: list[str]  # explicit keyword names (not **spread)
    line: int
    text: str  # "requests.get" as written, for messages


@dataclass
class ParsedCode:
    modules: dict[str, str] = field(default_factory=dict)  # local name → module path
    names: dict[str, str] = field(default_factory=dict)  # local name → "module.attr"
    calls: list[Call] = field(default_factory=list)
    imported_roots: set[str] = field(default_factory=set)


def _dotted(node: ast.AST) -> list[str] | None:
    """`a.b.c` → ["a", "b", "c"]; anything else (calls, subscripts…) → None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return [node.id, *reversed(parts)]
    return None


def parse_code(code: str) -> ParsedCode:
    """Raises SyntaxError for code that isn't valid Python."""
    tree = ast.parse(code)
    out = ParsedCode()
    instances: dict[str, list[str]] = {}  # var → resolved class path, from `x = mod.Class()`

    def resolve(chain: list[str]) -> list[str] | None:
        head, rest = chain[0], chain[1:]
        if head in out.modules:
            return out.modules[head].split(".") + rest
        if head in out.names:
            return out.names[head].split(".") + rest
        if head in instances:
            return [*instances[head], *rest]
        return None

    # Pass 1: imports (anywhere in the file, including inside functions).
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.imported_roots.add(a.name.split(".")[0])
                if a.asname:
                    out.modules[a.asname] = a.name
                else:  # `import os.path` binds `os`
                    root = a.name.split(".")[0]
                    out.modules[root] = root
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.imported_roots.add(node.module.split(".")[0])
            for a in node.names:
                if a.name != "*":
                    out.names[a.asname or a.name] = f"{node.module}.{a.name}"

    # Pass 2: simple instances, in source order: `s = requests.Session()`, `with X() as s`.
    for node in ast.walk(tree):
        target, value = None, None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.withitem):
            target, value = node.optional_vars, node.context_expr
        if isinstance(target, ast.Name) and isinstance(value, ast.Call):
            chain = _dotted(value.func)
            resolved = resolve(chain) if chain else None
            if resolved and resolved[-1][:1].isupper():  # CamelCase → probably a class
                instances[target.id] = resolved

    # Pass 3: calls.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        chain = _dotted(node.func)
        resolved = resolve(chain) if chain else None
        if not resolved:
            continue
        # Messages use the name as written (pd.read_csv), except for instances, where the
        # variable name means nothing to the user (s.get → Session.get).
        shown = chain if chain[0] not in instances else [instances[chain[0]][-1], *chain[1:]]
        out.calls.append(
            Call(
                chain=resolved,
                keywords=[k.arg for k in node.keywords if k.arg],
                line=node.lineno,
                text=".".join(shown),
            )
        )
        if len(out.calls) >= MAX_CALLS:
            break
    return out


# =============================================================================================
# Inspection (imports allow-listed LIBRARIES only)
# =============================================================================================
@dataclass
class Issue:
    kind: str  # "attribute" | "keyword"
    call: str  # as written, e.g. "requests.get"
    detail: str  # plain-English problem
    evidence: str  # real signature / suggestion
    library: str


def _allowed(root: str) -> bool:
    return root in ALLOWED_THIRD_PARTY or root in STDLIB


def _installed(root: str) -> bool:
    return importlib.util.find_spec(root) is not None


def library_version(root: str) -> str:
    if root in STDLIB:
        return f"Python {sys.version_info.major}.{sys.version_info.minor}"
    try:
        return f"{root} {importlib.metadata.version(PYPI_NAMES.get(root, root))}"
    except importlib.metadata.PackageNotFoundError:
        return root


def _walk(chain: list[str]) -> tuple[Any, int]:
    """Follow chain from its root module. Returns (object, index of first missing part or -1).

    Submodules are imported on demand (numpy.linalg, os.path, matplotlib.pyplot) — but only
    below an allow-listed root, so this can't reach anything outside the allow-list.
    """
    obj: Any = importlib.import_module(chain[0])
    path = chain[0]
    for i, part in enumerate(chain[1:], start=1):
        path = f"{path}.{part}"
        if hasattr(obj, part):
            obj = getattr(obj, part)
            continue
        if inspect.ismodule(obj):
            try:
                obj = importlib.import_module(path)
                continue
            except ImportError:
                pass
        return obj, i
    return obj, -1


def _qualpath(chain: list[str]) -> str:
    return ".".join(chain)


def _dynamic(obj: Any) -> bool:
    """Objects that invent attributes at runtime (so 'missing' proves nothing)."""
    cls = obj if inspect.isclass(obj) else type(obj)
    return any("__getattr__" in vars(k) for k in cls.__mro__ if k is not object) and not (
        inspect.ismodule(obj)
    )


def _signature(obj: Any) -> inspect.Signature | None:
    try:
        return inspect.signature(obj)
    except (TypeError, ValueError):  # many C builtins have no signature
        return None


def compact_sig(name: str, sig: inspect.Signature, limit: int = 12) -> str:
    """read_csv(filepath_or_buffer, *, sep, delimiter, …) — names only, no type annotations."""
    parts, star_done = [], False
    for p in sig.parameters.values():
        if p.name == "self":
            continue
        if p.kind is p.VAR_POSITIONAL:
            parts.append(f"*{p.name}")
            star_done = True
        elif p.kind is p.VAR_KEYWORD:
            parts.append(f"**{p.name}")
        else:
            if p.kind is p.KEYWORD_ONLY and not star_done:
                parts.append("*")
                star_done = True
            parts.append(p.name)
    shown = parts if len(parts) <= limit else [*parts[:limit], "…"]
    return f"{name}({', '.join(shown)})"


def suggest(bad: str, options: set[str] | list[str]) -> list[str]:
    """'seperator' → ['sep'], 'normalize' → ['norm']: abbreviations first, then typos."""
    opts = sorted(options)
    low = bad.lower()
    prefix = [
        o for o in opts if len(o) >= 3 and (low.startswith(o.lower()) or o.lower().startswith(low))
    ]
    close = difflib.get_close_matches(bad, opts, n=3, cutoff=0.6)
    return list(dict.fromkeys(prefix + close))[:3]


def check_call(call: Call) -> Issue | None:
    root = call.chain[0]
    lib = library_version(root)
    parent, missing = _walk(call.chain)
    if missing != -1:
        if _dynamic(parent):
            return None
        name = call.chain[missing]
        close = suggest(name, [n for n in dir(parent) if not n.startswith("_")])
        where = _qualpath(call.chain[:missing])
        return Issue(
            kind="attribute",
            call=call.text,
            detail=f"{where} has no '{name}'",
            evidence=f"{lib}: {where} has no attribute '{name}'"
            + (f" — did you mean {', '.join(close)}?" if close else ""),
            library=root,
        )

    target, _ = _walk(call.chain)
    if not callable(target) or not call.keywords:
        return None
    sig = _signature(target)
    if sig is None:
        return None
    params = sig.parameters
    has_var_kw = any(p.kind is p.VAR_KEYWORD for p in params.values())
    accepted = {n for n, p in params.items() if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)}
    shown = compact_sig(call.text, sig)

    if has_var_kw:
        forward = KWARGS_FORWARD.get(_qualpath(call.chain)) or KWARGS_FORWARD.get(
            f"{getattr(target, '__module__', '')}.{getattr(target, '__qualname__', '')}"
        )
        if not forward:
            return None  # takes **kwargs we can't follow → can't tell, never a false alarm
        fwd_obj, fwd_missing = _walk(forward.split("."))
        fwd_sig = _signature(fwd_obj) if fwd_missing == -1 else None
        if fwd_sig is None:
            return None
        accepted |= {n for n in fwd_sig.parameters if n not in ("self", "method")}
        short = ".".join(forward.split(".")[-2:])
        fwd_params = [n for n in fwd_sig.parameters if n not in ("self", "method")]
        shown = f"{compact_sig(call.text, sig)} passes **kwargs to {short}({', '.join(fwd_params)})"

    unknown = [k for k in call.keywords if k not in accepted]
    if not unknown:
        return None
    bad = unknown[0]
    close = suggest(bad, accepted)
    return Issue(
        kind="keyword",
        call=call.text,
        detail=f"{call.text}() has no '{bad}' parameter",
        evidence=f"{lib}: {shown} — no '{bad}'"
        + (f" (did you mean {', '.join(close)}?)" if close else ""),
        library=root,
    )


def analyze(code: str) -> dict[str, Any]:
    """Sync + CPU/import-bound → run in a thread. Returns cacheable plain data."""
    parsed = parse_code(code)
    issues: list[Issue] = []
    checked: list[str] = []
    for call in parsed.calls:
        root = call.chain[0]
        if not (_allowed(root) and _installed(root)):
            continue
        try:
            issue = check_call(call)
        except Exception as exc:  # noqa: BLE001 — a library misbehaving on import/inspect
            log.warning("skipping %s: %r", call.text, exc)  # must only skip that one call
            continue
        checked.append(call.text)
        if issue and all(i.detail != issue.detail for i in issues):
            issues.append(issue)
    third_party = sorted(
        r for r in parsed.imported_roots if r not in STDLIB and not (_allowed(r) and _installed(r))
    )
    return {
        "issues": [vars(i) for i in issues],
        "checked": checked,
        "not_installed": third_party,
    }


# =============================================================================================
# The detector
# =============================================================================================
class CodeApiChecker(BaseDetector):
    name = "code_api_checker"

    def __init__(self, client_factory=http_client) -> None:
        self.client_factory = client_factory  # tests inject a mock for the PyPI lookups

    async def _check(self, claim: Claim, session: SessionContext) -> DetectorResult | None:
        if requested_untrue(session, claim):
            return None  # "write code with a wrong API call": broken on purpose
        code = claim.code or claim.quote
        try:
            data = await self.cached(
                session, f"analysis:{hash(code)}", lambda: asyncio.to_thread(analyze, code)
            )
        except SyntaxError as exc:
            return self.result("unverified", 0.3, f"Couldn't parse this as Python ({exc.msg}).")

        if data["issues"]:
            issues = data["issues"]
            first = issues[0]
            extra = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
            return self.result(
                "nonexistent_api",
                0.97 if first["kind"] == "attribute" else 0.95,
                f"{first['detail']}.{extra}",
                [
                    Evidence(
                        source=library_version(i["library"]),
                        url=(
                            f"https://pypi.org/project/{PYPI_NAMES.get(i['library'], i['library'])}/"
                            if i["library"] not in STDLIB
                            else "https://docs.python.org/3/library/"
                        ),
                        snippet=snippet(i["evidence"]),
                    )
                    for i in issues[:3]
                ],
            )

        # Libraries we can't inspect here: does the package even exist? (FR-C1 check)
        missing, unknown = [], []
        for root in data["not_installed"][:5]:
            project = PYPI_NAMES.get(root, root)
            try:
                exists = await package_exists(
                    project, "pypi", session, self.client_factory, self.name
                )
            except Exception as exc:  # noqa: BLE001 — PyPI down must not fail the whole check
                log.warning("PyPI lookup for %s failed: %r", project, exc)
                unknown.append(root)
                continue
            if not exists:
                missing.append(project)
        if missing:
            return self.result(
                "contradicted",
                0.95,
                f"The code imports {', '.join(missing)}, which "
                f"{'is' if len(missing) == 1 else 'are'} not on PyPI.",
                [
                    Evidence(
                        source="PyPI",
                        url=f"https://pypi.org/project/{m}/",
                        snippet=f"No package named '{m}'",
                    )
                    for m in missing
                ],
            )

        if data["checked"]:
            not_checked = data["not_installed"]
            return self.result(
                "supported" if not not_checked else "unverified",
                0.9 if not not_checked else 0.5,
                f"Checked {len(data['checked'])} call(s) against the real libraries; all exist."
                + (f" Couldn't inspect {', '.join(not_checked)}." if not_checked else ""),
                [
                    Evidence(
                        source="Code API Checker",
                        url=None,
                        snippet=snippet("Checked: " + ", ".join(dict.fromkeys(data["checked"]))),
                    )
                ],
            )
        if data["not_installed"]:  # e.g. PyPI was down, so we couldn't check those imports
            return self.result(
                "unverified",
                0.3,
                f"Couldn't check {', '.join(data['not_installed'])} (lookup unavailable).",
            )
        # Nothing this detector can inspect (e.g. only `import sys` + builtins): say nothing,
        # so a filler note can't crowd out a real problem in the bubble.
        return None


detector = CodeApiChecker()
