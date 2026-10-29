import sys

from ghostapi.extractors.python import scan_source

SKIP = set(sys.stdlib_module_names) | {"myapp"}


def targets(source: str) -> set[str]:
    return {u.target for u in scan_source(source, "x.py", SKIP).usages}


def test_import_alias_and_attribute_chain():
    found = targets("import numpy as np\nnp.linalg.norm([1])\n")
    assert found == {"numpy", "numpy.linalg.norm"}


def test_from_import_and_call_with_kwargs():
    scan = scan_source(
        "from openai import OpenAI\n\ndef go():\n    OpenAI(api_key='k', timeout=3)\n", "x.py", SKIP
    )
    call = next(u for u in scan.usages if u.n_positional is not None)
    assert call.target == "openai.OpenAI"
    assert call.kwargs == ("api_key", "timeout")
    assert call.symbol == "go"
    assert call.line == 4
    assert scan.defined == {"go"}
    assert scan.imported_roots == {"openai"}


def test_dotted_import_binds_root():
    assert "google.genai.Client" in targets("import google.genai\ngoogle.genai.Client()\n")


def test_stdlib_local_and_relative_imports_are_ignored():
    found = targets("import os\nfrom myapp import x\nfrom . import y\nos.path.join('a')\n")
    assert found == set()


def test_star_args_flagged():
    scan = scan_source("import requests\nrequests.get(*a, **k)\n", "x.py", SKIP)
    call = next(u for u in scan.usages if u.n_positional is not None)
    assert call.has_star_args


def test_call_result_attributes_are_not_guessed():
    # `client.chat` lives on an instance; we can't know its type statically, so we don't guess.
    found = targets("import openai\nclient = openai.OpenAI()\nclient.chat.completions.create()\n")
    assert found == {"openai", "openai.OpenAI"}


def test_syntax_error_is_reported_not_raised():
    scan = scan_source("def broken(:\n", "x.py", SKIP)
    assert scan.error is not None
    assert scan.usages == []
