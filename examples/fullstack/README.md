# Example: two agents, one product

A FastAPI backend (`backend/`) and a Next.js frontend (`web/`), the kind of split you get when
one agent builds the API and another builds the UI. `web/src/client.ts` contains the mistakes
Ghost exists to catch.

## Run it

From the repository root:

```sh
ghostapi check --root examples/fullstack
```

```text
web/src/client.ts:15:10 error unknown-endpoint [medium] `GET /api/order/{}` matches no route this project serves.
    fix: Did you mean `/api/orders/{order_id}` (backend/routers/orders.py:6)?
web/src/client.ts:19:10 error wrong-method [medium] `/api/orders/{}` exists (backend/routers/orders.py:6) but accepts DELETE, GET, not PUT.
    fix: Use DELETE, GET, or add a PUT handler for this path.
web/src/client.ts:23:10 error unknown-symbol [high] `axios:default.fetchh` does not exist in any known version of axios.
```

The output also has three `unregistered-api-call` warnings, because nothing in this example
is recorded in `.ghost/` yet.

Ghost worked out that `/api/orders/{order_id}` exists even though the route says only
`/{order_id}`. The router has `prefix="/orders"`, and `backend/main.py` mounts it under
`/api`. The frontend's `axios.create({ baseURL: "/api" })` is followed the same way.

## The multi-agent part

Suppose the backend agent deletes `create_order` from `backend/routers/orders.py`. Its hook
checks only the file it edited, yet reports the caller in the frontend:

```text
web/src/client.ts:11:10 error endpoint-removed [medium] This call broke: the edit to
    backend/routers/orders.py removed or changed `POST /api/orders`, and `POST /api/orders`
    no longer matches any route.
```

To see this, commit the example in a scratch git repository, delete the route, and run
`ghostapi check --root . backend/routers/orders.py`. The comparison is with the last commit.

Before touching the route, the agent could have asked:

```sh
ghostapi show "POST /api/orders" --root examples/fullstack
```
