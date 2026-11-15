from ghostapi.paths import child, contains, display, join, leaf, parent, prefix, related, segments


def test_python_paths_are_dotted():
    assert segments("openai.OpenAI.chat") == ["openai", "OpenAI", "chat"]
    assert join(["openai", "OpenAI"]) == "openai.OpenAI"
    assert parent("openai.OpenAI") == "openai"
    assert parent("openai") is None


def test_npm_paths_keep_dots_and_slashes_in_the_module():
    assert segments("chart.js:Chart.register") == ["chart.js:", "Chart", "register"]
    assert segments("react-dom/client:") == ["react-dom/client:"]
    assert child("react:", "useState") == "react:useState"
    assert prefix("axios:default.get", 2) == "axios:default"
    assert parent("react:useState") == "react:"
    assert leaf("react:default.useState") == "useState"
    assert display("react-dom/client:") == "react-dom/client"


def test_containment():
    assert contains("react:", "react:useState")
    assert contains("openai", "openai.OpenAI")
    assert not contains("openai.OpenAI", "openai.OpenAIError")
    assert related("react:useState", "react:")
    assert not related("react:useState", "react:useEffect")
