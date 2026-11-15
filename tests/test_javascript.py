from ghostapi.extractors.javascript import is_source, package_of, scan_source


def scan(source: str, file: str = "app.tsx", external=lambda _: True):
    return scan_source(source, file, external)


def targets(source: str, file: str = "app.tsx") -> set[str]:
    return {u.target for u in scan(source, file).usages}


def test_package_of():
    assert package_of("react") == "react"
    assert package_of("react-dom/client") == "react-dom"
    assert package_of("@tanstack/react-query/build") == "@tanstack/react-query"
    for local in ("./x", "../x", "/abs", "@/components/x", "~/x", "#internal", "node:fs", "fs"):
        assert package_of(local) is None


def test_is_source_skips_declarations_and_minified_files():
    assert is_source("a.ts") and is_source("a.jsx") and is_source("a.mjs")
    assert not is_source("a.d.ts") and not is_source("a.min.js") and not is_source("a.py")


def test_import_forms():
    found = targets(
        'import React, { useState as uS } from "react";\n'
        'import * as path from "pathe";\n'
        'import type { AxiosInstance } from "axios";\n'
        'import "side-effect";\n'
        'import { x } from "./local";\n'
    )
    assert found == {
        "react:default", "react:useState", "pathe:", "axios:AxiosInstance", "side-effect:",
    }  # fmt: skip


def test_require_forms():
    found = targets(
        'const fs = require("fs-extra");\nconst { get, post: send } = require("axios");\n',
        "app.js",
    )
    assert found == {"fs-extra:", "axios:get", "axios:post"}


def test_member_chains_calls_and_arguments():
    result = scan(
        'import axios from "axios";\n'
        "export async function load() {\n"
        '  await axios.get("/u", ...rest);\n'
        "  return axios.defaults.headers;\n"
        "}\n"
    )
    call = next(u for u in result.usages if u.target == "axios:default.get")
    assert (call.n_positional, call.has_star_args, call.symbol, call.line) == (2, True, "load", 3)
    assert "axios:default.defaults.headers" in {u.target for u in result.usages}
    assert result.defined == {"load"}
    assert result.imported_roots == {"axios"}


def test_jsx_new_and_type_positions():
    found = targets(
        'import React from "react";\nimport { QueryClient } from "@tanstack/react-query";\n'
        "const c = new QueryClient();\n"
        "let f: React.FC<Props>;\n"
        "const el = <React.Fragment />;\n"
    )
    assert {
        "@tanstack/react-query:QueryClient",
        "react:default.FC",
        "react:default.Fragment",
    } <= found


def test_reexports_from_packages():
    found = targets('export { default as Thing } from "lodash.debounce";\nexport * from "zod";\n')
    assert found == {"lodash.debounce:default", "zod:"}


def test_undeclared_packages_are_not_external():
    result = scan('import { a } from "components/a";\n', external=lambda p: p == "react")
    assert result.usages == []


def test_broken_source_still_yields_imports():
    assert "react:useState" in targets('import { useState } from "react";\nfunction (( {\n')
