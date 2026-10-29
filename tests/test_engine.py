from ghostapi.engine import check_usage
from ghostapi.models import ApiHistory, ApiSymbol, Param, Usage

HISTORY = ApiHistory(
    package="openai",
    symbols={
        s.path: s
        for s in [
            ApiSymbol("openai", "module"),
            ApiSymbol("openai.OpenAI", "class", added_in="1.0.0"),
            ApiSymbol(
                "openai.ChatCompletion",
                "class",
                removed_in="1.0.0",
                replacement="openai.OpenAI().chat.completions.create",
            ),
            ApiSymbol("openai.old_helper", "function", deprecated_in="1.20.0", removed_in="2.0.0"),
            ApiSymbol("openai.legacy", "module", removed_in="1.0.0", moved_to="openai._legacy"),
            ApiSymbol(
                "openai.embed",
                "function",
                params=(
                    Param("text", required=True),
                    Param("model", "keyword_only"),
                ),
            ),
            ApiSymbol("openai.types", "module", dynamic=True),
        ]
    },
)


def run(target: str, version: str, **call):
    usage = Usage(target=target, module="openai", file="app.py", line=1, **call)
    return check_usage(usage, package="openai", version=version, history=HISTORY)


def rules(findings):
    return [f.rule for f in findings]


def test_removed_with_replacement():
    [f] = run("openai.ChatCompletion", "1.40.0")
    assert f.rule == "removed-in"
    assert "openai.OpenAI().chat.completions.create" in (f.fix or "")


def test_still_present_in_old_version():
    assert run("openai.ChatCompletion", "0.28.0") == []


def test_too_new():
    [f] = run("openai.OpenAI", "0.28.0")
    assert f.rule == "too-new"
    assert "1.0.0" in f.message


def test_deprecated_mentions_removal():
    [f] = run("openai.old_helper", "1.30.0")
    assert f.rule == "deprecated"
    assert "2.0.0" in f.message


def test_moved_module():
    [f] = run("openai.legacy", "1.1.0")
    assert f.rule == "import-path"
    assert "openai._legacy" in (f.fix or "")


def test_unknown_symbol_did_you_mean():
    [f] = run("openai.OpenAl", "1.40.0")
    assert f.rule == "unknown-symbol"
    assert f.fix == "Did you mean `openai.OpenAI`?"


def test_unknown_under_dynamic_module_is_silent():
    assert run("openai.types.Whatever", "1.40.0") == []


def test_unknown_below_a_function_is_silent():
    # Attributes of a function's return value aren't in the index; don't guess.
    assert run("openai.embed.something", "1.40.0") == []


def test_parameters():
    assert run("openai.embed", "1.40.0", n_positional=1, kwargs=("model",)) == []
    assert rules(run("openai.embed", "1.40.0", n_positional=1, kwargs=("modle",))) == [
        "bad-parameter"
    ]
    assert rules(run("openai.embed", "1.40.0", n_positional=0)) == ["arity"]
    assert rules(run("openai.embed", "1.40.0", n_positional=2)) == ["arity"]
    assert run("openai.embed", "1.40.0", n_positional=0, has_star_args=True) == []
