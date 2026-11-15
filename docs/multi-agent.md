# Multi-agent development

When several agents work on one product at the same time, git worktrees keep them from
overwriting each other's files. But nothing stops them from disagreeing about an API:

- Agent A changes `POST /api/orders`; Agent B's frontend still sends the old request.
- Agent B calls `GET /api/orders/summary`, which nobody built.
- Two agents each build their own `/users/{id}`.
- An agent writes code against how an API worked when its context was loaded, not how it works
  now.

Ghost treats the code as the source of truth for the project's own APIs and keeps every agent's
view of them current.

## What Ghost does

| Situation | What happens |
|---|---|
| A call matches no route | `unknown-endpoint` at the call, with the closest real route |
| Wrong HTTP method | `wrong-method` |
| An agent removes or changes a route that other code calls | `endpoint-removed`, reported **to that agent** at every broken caller |
| The same route is defined twice | `duplicate-endpoint`; near-identical paths get `similar-endpoint` |
| Two agents register calls at the same moment | Both are kept: registry writes are locked and atomic |

`endpoint-removed` compares the edited files with their last committed version. That catches
the moment an agent breaks a contract, rather than at merge time.

## The workflow for agents

`ghostapi init` writes this into `AGENTS.md`. The gist:

1. **Before calling an endpoint**, look it up:

   ```sh
   ghostapi show "POST /api/orders"
   ```

   ```text
   POST /api/orders
     defined at: backend/routers/orders.py:10 (fastapi)
     handler:    create_order
     owner:      orders-agent
     callers:    web/src/client.ts:6
   ```

2. **When creating an endpoint other code will call**, claim it:

   ```sh
   ghostapi provide --method POST --path api/orders --owner orders-agent --description "Create an order"
   ```

3. **Before changing or removing an endpoint**, run `ghostapi show` on it. If it has callers,
   update them in the same change, or keep the old route until they move.
4. **Before finishing**, run `ghostapi check`.

`ghostapi list` gives the whole picture: every endpoint with its owner and caller count, and
every external API in the registry.

## Worktrees and merging

The registry is stored one file per item:

```text
.ghost/apis/<api>/api.yaml            # external API: kind, version, owner
.ghost/apis/<api>/calls/<target>.yaml # one file per called target
.ghost/provides/<endpoint>.yaml       # one file per declared endpoint
```

Agents in separate worktrees therefore touch different files, unless they record the same
target. After merging, run `ghostapi check`: `stale-entry` and `unregistered-api-call` point out
anything the merge left inconsistent. In CI, the [GitHub Action](ci.md) runs the same check on
every pull request.

## Limits

- **Static view of routers.** Routes are read from code and from OpenAPI files. A route built
  at runtime from data is invisible: declare it with `ghostapi provide`. A router mounted in a
  way Ghost can't trace is still matched by its path suffix, so it never causes a false
  `unknown-endpoint`.
- **Paths and methods only.** Request and response bodies aren't compared yet.
- **Which calls count.** A call is checked when it uses a relative URL, an internal host
  (localhost by default), or a configured base-URL variable, and its first path segment matches
  one of your routes. Calls to other services are ignored. See
  [configuration.md](configuration.md).
