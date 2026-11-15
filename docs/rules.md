# Rules

Every finding has a **severity** and a **confidence**:

- **Severity:** `error`, `warning` or `info`. By default `ghostapi check` exits 1 on errors;
  `--fail-on warning` includes warnings, and `--fail-on never` always exits 0.
- **Confidence:** `high` means the fact comes straight from the package's published files.
  `medium` means a static reading of something that is decided at runtime, such as a web
  router. `low` is a hint.

Findings are suppressed whenever Ghost can't see clearly: dynamic modules, overloaded
signatures, classes inheriting from other packages, and routers mounted in ways Ghost can't
trace.

## Library calls (PyPI and npm)

| Rule | Severity | Meaning |
|---|---|---|
| `unknown-symbol` | error | The function, class or attribute doesn't exist in any known version. Comes with "did you mean". |
| `removed-in` | error | It existed in older versions and is gone from the one you pin. |
| `too-new` | error | It was added after the version you pin. The fix is to upgrade, or use what your version has. |
| `import-path` | error | It moved; the fix gives the new path. |
| `bad-parameter` | error | A keyword argument the function doesn't accept in your version. |
| `arity` | error | Too many positional arguments, or required ones missing. |
| `deprecated` | warning | Still works in your version, but is marked deprecated. |

```text
app.py:6:12 error removed-in [high] `openai.ChatCompletion` was removed in openai 1.0.0; you have 1.40.0.
src/App.tsx:6:31 error unknown-symbol [high] `zod:z.strng` does not exist in any known version of zod.
    fix: Did you mean `zod:z.string`?
src/App.tsx:17:9 error arity [high] `axios:default.get` takes at most 2 positional argument(s) in axios 1.7.9; got 3.
```

npm symbols are written `<module>:<export>.<member>`: `react:useState`, `axios:default.get`,
`react-dom/client:createRoot`. When a package ships no types, the message names the `@types/*`
package and version that answered.

## Registry drift

| Rule | Severity | Meaning |
|---|---|---|
| `unregistered-api-call` | warning | The code calls an external API that isn't recorded in `.ghost/apis/`. The fix is the exact `ghostapi register` command. |
| `stale-entry` | warning | A registry entry points at a file, symbol or import that no longer exists. |
| `registry-version-mismatch` | warning | The registry records a different version than the lockfile pins. The lockfile wins. |

Ghost doesn't ask an agent to register a call it has also flagged as an error: fix it first.

## Your own endpoints

| Rule | Severity | Meaning |
|---|---|---|
| `unknown-endpoint` | error | A call to this project's API matches no route it serves. Comes with "did you mean". |
| `wrong-method` | error | The path exists but doesn't accept that HTTP method. |
| `endpoint-removed` | error | The current edit removed or changed a route that other code still calls. Reported at each caller, to the agent making the edit. |
| `duplicate-endpoint` | warning | The same method and path is defined in two places. |
| `similar-endpoint` | info | Near-identical paths, such as `/user/{id}` next to `/users/{id}`. |

```text
web/src/client.ts:7:9 error unknown-endpoint [medium] `GET /api/order/{}` matches no route this project serves.
    fix: Did you mean `/api/orders/{id}` (backend/routers/orders.py:6)?
web/src/client.ts:8:9 error wrong-method [medium] `/api/orders/{}` exists (backend/routers/orders.py:6) but accepts DELETE, GET, not PUT.
```

In call paths, `{}` marks a segment whose value is only known at runtime, such as
`` `/api/orders/${id}` ``.

## Other

| Rule | Severity | Meaning |
|---|---|---|
| `unparseable-file` | info | A file couldn't be parsed (for example, mid-edit) and was skipped. |
