"""Tests for the Code API Checker (FR-C7). Owner: Role C.

These run against the REAL installed libraries (requests, pandas, numpy, fastapi …) — that is
the point of this detector — but never execute the AI's code, and never touch the network
(PyPI lookups are mocked).
"""

from __future__ import annotations

import httpx
import pytest

from app.detectors.base import http_client
from app.detectors.code_api_checker import (
    CodeApiChecker,
    analyze,
    compact_sig,
    parse_code,
    suggest,
)
from app.models import Claim, SessionContext


def claim(code: str) -> Claim:
    return Claim(
        claim_id="c",
        message_id="m",
        quote=code,
        normalized="Code uses real APIs.",
        type="code_api",
        risk="high",
        code=code,
    )


def pypi(existing: set[str]):
    def handler(req: httpx.Request) -> httpx.Response:
        name = req.url.path.split("/")[2]
        return httpx.Response(200 if name in existing else 404, json={})

    return lambda: http_client(transport=httpx.MockTransport(handler))


@pytest.fixture
def session() -> SessionContext:
    return SessionContext(session_id="t")


@pytest.fixture
def checker() -> CodeApiChecker:
    return CodeApiChecker(client_factory=pypi(set()))


# --------------------------------------------------------------------------- fixture scenario
async def test_code_api_scenario(load_scenario, checker, session):
    sc = load_scenario("code_api")
    raw = sc["expected"]["claims"][0]
    r = await checker.check(Claim(**raw), session)
    want = sc["expected"]["detector_results"][raw["claim_id"]][0]
    assert r.status == want["status"] == "nonexistent_api"
    assert "retries" in r.explanation
    assert r.evidence[0].source.startswith("requests ")  # library + installed version
    assert "Session.request(" in r.evidence[0].snippet  # the real signature


# --------------------------------------------------------------------------- what it catches
@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        ("import numpy as np\nnp.linalg.normalize([1, 2])", "no 'normalize'"),
        ("import pandas as pd\npd.read_csv('f.csv', seperator=',')", "no 'seperator'"),
        ("from requests import get\nget('u', retries=3)", "no 'retries'"),
        ("import requests\ns = requests.Session()\ns.get('u', max_retries=2)", "max_retries"),
        ("import os\nos.path.joinpath('a', 'b')", "no 'joinpath'"),
        (
            "from pathlib import Path\np = Path('a')\np.read_text(encodings='utf8')",
            "no 'encodings'",
        ),
        # no **kwargs on model_validate → a made-up keyword is caught
        (
            "from pydantic import BaseModel\nBaseModel.model_validate({}, anything_goes=1)",
            "no 'anything_goes'",
        ),
    ],
)
async def test_made_up_apis_are_flagged(checker, session, code, fragment):
    r = await checker.check(claim(code), session)
    assert r.status == "nonexistent_api", r.explanation
    assert fragment in r.explanation


async def test_did_you_mean_suggestions(checker, session):
    r = await checker.check(claim("import numpy as np\nnp.linalg.normalize([1])"), session)
    assert "did you mean norm" in r.evidence[0].snippet
    r = await checker.check(
        claim("import pandas as pd\npd.read_csv('f', seperator=',')"),
        SessionContext(session_id="t2"),
    )
    assert "sep" in r.evidence[0].snippet.split("did you mean")[1]


# --------------------------------------------------------------------------- no false alarms
@pytest.mark.parametrize(
    "code",
    [
        "import requests\nrequests.get('u', timeout=10, headers={}, verify=False)",
        (
            "import pandas as pd\nimport io\ndf = pd.read_csv(io.StringIO('a'), sep=',')\n"
            "pd.DataFrame.from_dict({})"
        ),
        "from fastapi import FastAPI\napp = FastAPI(title='x')",
        "import numpy as np\nnp.linalg.norm([1, 2], ord=2)",
        "import os.path\nos.path.join('a', 'b')",
        "from collections import Counter\nCounter('abc').most_common(1)",
        # **kwargs we can't follow → never flagged (route(rule, **options); loads(s, **kw))
        "from flask import Flask\napp = Flask(__name__)\napp.route('/', methods=['GET'])",
        "import json\njson.loads('{}', strict_mode=True)",
    ],
)
async def test_real_apis_pass(checker, session, code):
    r = await checker.check(claim(code), session)
    assert r.status == "supported", r.explanation


async def test_spread_kwargs_are_not_guessed(checker, session):
    r = await checker.check(claim("import requests\nopts = {}\nrequests.get('u', **opts)"), session)
    assert r.status == "supported"


# --------------------------------------------------------------------------- libraries we can't inspect
async def test_fake_package_is_contradicted(session):
    c = CodeApiChecker(client_factory=pypi({"requests"}))
    r = await c.check(claim("import torchlightning_pro\ntorchlightning_pro.fit()"), session)
    assert r.status == "contradicted" and "torchlightning_pro" in r.explanation


async def test_real_but_uninstalled_package_is_unverified(session):
    c = CodeApiChecker(client_factory=pypi({"scikit-learn"}))
    r = await c.check(
        claim(
            "from sklearn.linear_model import LinearRegression\n"
            "LinearRegression().fit([[1]], [1])"
        ),
        session,
    )
    assert r.status == "unverified"  # exists on PyPI (import name → project name mapped)


async def test_pypi_down_is_not_a_false_alarm(session):
    def down():
        def handler(req):
            raise httpx.ConnectError("offline", request=req)

        return http_client(transport=httpx.MockTransport(handler))

    r = await CodeApiChecker(client_factory=down).check(claim("import obscurelib\n"), session)
    assert r.status == "unverified"


# --------------------------------------------------------------------------- safety
def test_ai_code_is_never_executed(tmp_path):
    marker = tmp_path / "ran"
    code = f"import os\nopen({str(marker)!r}, 'w').write('x')\nos.system('echo hacked')"
    analyze(code)
    assert not marker.exists()


def test_non_allowlisted_modules_are_never_imported(monkeypatch):
    import importlib

    imported = []
    real = importlib.import_module

    def spy(name, *a, **k):
        imported.append(name)
        return real(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", spy)
    analyze("import antigravity\nimport this\nimport some_random_pkg\nantigravity.fly()")
    assert not any(m.split(".")[0] in {"antigravity", "this", "some_random_pkg"} for m in imported)


async def test_syntax_error_is_unverified(checker, session):
    r = await checker.check(claim("import requests\nrequests.get(("), session)
    assert r.status == "unverified" and "parse" in r.explanation


# --------------------------------------------------------------------------- helpers
def test_parse_code_resolves_aliases_and_instances():
    p = parse_code(
        "import pandas as pd\nfrom requests import Session as S\ns = S()\n"
        "s.get('u', timeout=1)\npd.read_csv('f', sep=',')"
    )
    chains = {".".join(c.chain): c for c in p.calls}
    assert "requests.Session.get" in chains and chains["requests.Session.get"].text == "Session.get"
    assert chains["pandas.read_csv"].keywords == ["sep"]


def test_compact_sig_and_suggest():
    import inspect

    def f(a, b=1, *, c, **kw): ...

    assert compact_sig("f", inspect.signature(f)) == "f(a, b, *, c, **kw)"
    assert suggest("seperator", {"sep", "delimiter", "iterator"})[0] == "sep"
    assert suggest("normalize", ["norm", "solve"]) == ["norm"]


async def test_nothing_to_inspect_stays_silent(checker, session):
    """Only stdlib builtins/`import sys` → no result at all (was a filler 'unverified')."""
    code = "import sys\nmatch sys.argv[1]:\n    case 'run':\n        print('ok')\n"
    assert await checker.check(claim(code), session) is None
