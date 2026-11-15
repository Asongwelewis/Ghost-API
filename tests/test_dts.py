"""The .d.ts surface builder, one declaration pattern per test (each seen in real packages)."""

import json

from ghostapi.providers.dts import build_dts_surface, specifier_name


def surface(tmp_path, files: dict[str, str], package: str = "pkg", manifest: dict | None = None):
    for name, source in {"package.json": json.dumps(manifest or {}), **files}.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    result = build_dts_surface(tmp_path, package)
    assert result is not None
    return result


def node(s, path):
    chain = s.walk(path)
    assert len(chain) == len(path.replace(":", ":.").split(".")) - (1 if path.endswith(":") else 0)
    return chain[-1]


def test_specifier_name_for_definitely_typed():
    assert specifier_name("@types/react") == "react"
    assert specifier_name("@types/babel__core") == "@babel/core"
    assert specifier_name("zod") == "zod"


def test_named_exports_signatures_and_overloads(tmp_path):
    s = surface(
        tmp_path,
        {
            "index.d.ts": "export declare function format(date: Date, fmt?: string): string;\n"
            "export declare function parse(a: string): Date;\n"
            "export declare function parse(a: number): Date;\n"
            "export declare const VERSION: string;\n"
        },
    )
    params = node(s, "pkg:format").params or ()
    assert [(p.name, p.required) for p in params] == [("date", True), ("fmt", False)]
    assert node(s, "pkg:parse").params is None  # overloaded: don't guess
    assert s.has("pkg:VERSION") and not s.has("pkg:formatt")


def test_export_equals_namespace_merged_with_function(tmp_path):
    # @types/express style
    s = surface(
        tmp_path,
        {
            "index.d.ts": 'import * as other from "other-pkg";\n'
            "declare function e(): e.Express;\n"
            "declare namespace e {\n"
            "  var json: typeof other.json;\n"
            "  export function Router(options?: RouterOptions): Router;\n"
            "  interface Express { listen(port: number): void; }\n"
            "}\n"
            "export = e;\n"
        },
        "express",
    )
    assert node(s, "express:default").kind == "class"  # callable and has members
    assert s.has("express:default.json") and s.has("express:Router") and s.has("express:json")
    assert not s.has("express:default.statik")


def test_export_equals_namespace_react_style(tmp_path):
    s = surface(
        tmp_path,
        {
            "index.d.ts": "export = React;\nexport as namespace React;\n"
            "declare namespace React {\n"
            "  function useState<S>(s: S): [S, (v: S) => void];\n"
            "  function useState<S = undefined>(): [S | undefined, (v: S) => void];\n"
            "  function useEffect(effect: () => void, deps?: unknown[]): void;\n"
            "  class Component<P> { setState(s: P): void; }\n"
            "  type FC<P = {}> = FunctionComponent<P>;\n"
            "  interface FunctionComponent<P> { (props: P): unknown; displayName?: string; }\n"
            "}\n"
        },
        "react",
    )
    assert s.has("react:useState") and s.has("react:default.useEffect")
    assert node(s, "react:useState").params is None
    assert [p.name for p in node(s, "react:useEffect").params or ()] == ["effect", "deps"]
    assert s.has("react:default.Component.setState") and s.has("react:FC.displayName")


def test_typed_constant_exposes_its_interface(tmp_path):
    # axios style: export default axios; declare const axios: AxiosStatic
    s = surface(
        tmp_path,
        {
            "index.d.ts": "export interface AxiosInstance {\n"
            "  get(url: string, config?: object): unknown;\n"
            "  defaults: Defaults;\n"
            "}\n"
            "interface Defaults { timeout: number; }\n"
            "export interface AxiosStatic extends AxiosInstance {\n"
            "  create(config?: object): AxiosInstance;\n"
            "}\n"
            "declare const axios: AxiosStatic;\nexport default axios;\n"
        },
        "axios",
    )
    assert [p.name for p in node(s, "axios:default.get").params or ()] == ["url", "config"]
    assert s.has("axios:default.create") and s.has("axios:default.defaults.timeout")
    assert not s.has("axios:default.fetchh")
    assert not node(s, "axios:default").dynamic


def test_reexports_and_star_exports(tmp_path):
    s = surface(
        tmp_path,
        {
            "index.d.ts": 'export * from "./a";\nexport { b as renamed } from "./b";\n'
            'export * as ns from "./b";\nexport { default as Main } from "./main";\n',
            "a.d.ts": "export declare function fromA(): void;\n",
            "b.d.ts": "export declare const b: number;\n",
            "main.d.ts": "declare class Main { run(): void }\nexport default Main;\n",
        },
    )
    assert s.has("pkg:fromA") and s.has("pkg:renamed") and s.has("pkg:ns.b")
    assert s.has("pkg:Main.run")


def test_things_we_cannot_see_into_are_dynamic(tmp_path):
    s = surface(
        tmp_path,
        {
            "index.d.ts": 'export * from "some-other-package";\nexport declare const x: number;\n',
            "sub.d.ts": 'import { Base } from "other";\n'
            "export declare class Child extends Base { own(): void; }\n"
            "export interface Bag { [key: string]: unknown; }\n",
        },
    )
    assert node(s, "pkg:").dynamic  # re-exports a package we don't index
    assert node(s, "pkg/sub:Child").dynamic  # inherits from outside the package
    assert node(s, "pkg/sub:Bag").dynamic  # index signature


def test_global_script_is_dynamic_not_empty(tmp_path):
    s = surface(tmp_path, {"index.d.ts": "declare var something: number;\n"})
    assert node(s, "pkg:").dynamic


def test_exports_map_with_conditions_and_wildcards(tmp_path):
    manifest = {
        "exports": {
            ".": {"import": {"types": "./dist/index.d.mts"}, "require": "./dist/index.cjs"},
            "./client": {"types": "./dist/client.d.ts", "default": "./dist/client.js"},
            "./locale/*": {"types": "./dist/locale/*.d.ts"},
            "./package.json": "./package.json",
        }
    }
    s = surface(
        tmp_path,
        {
            "dist/index.d.mts": "export declare const root: 1;\n",
            "dist/client.d.ts": "export declare function createRoot(el: unknown): void;\n",
            "dist/locale/fr.d.ts": "export declare const fr: object;\n",
        },
        manifest=manifest,
    )
    assert set(s.roots) == {"pkg:", "pkg/client:", "pkg/locale/fr:"}
    assert s.has("pkg:root") and s.has("pkg/client:createRoot") and s.has("pkg/locale/fr:fr")


def test_types_field_and_deep_imports_without_exports_map(tmp_path):
    s = surface(
        tmp_path,
        {
            "types/index.d.ts": "export declare function chunk<T>(a: T[], n?: number): T[][];\n",
            "fp.d.ts": "export declare const map: unknown;\n",
        },
        "lodash",
        manifest={"types": "types/index.d.ts"},
    )
    assert s.has("lodash:chunk") and s.has("lodash/fp:map")


def test_ambient_module_declarations(tmp_path):
    s = surface(
        tmp_path,
        {
            "index.d.ts": 'declare module "pkg" { export function a(): void; }\n'
            'declare module "pkg/extra" { function b(): void; }\n'
            'declare module "vue" { interface Augmented { x: 1 } }\n'
        },
    )
    assert s.has("pkg:a") and s.has("pkg/extra:b")
    assert "vue:" not in s.roots  # augmenting another package doesn't define it
