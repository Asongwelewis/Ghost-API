"""The project's own HTTP API: routes and calls across frameworks, and the multi-agent rules."""

import json
import subprocess

import pytest
from typer.testing import CliRunner

from ghostapi.checker import run_check
from ghostapi.cli import app
from ghostapi.config import Config, load_config
from ghostapi.endpoints import call_segments, endpoint_matches, internal_path, route_segments
from ghostapi.endpoints.check import project_index

runner = CliRunner()


def write(root, files: dict[str, str]):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def routes(root) -> set[str]:
    files = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    return {e.display for e in project_index(root, files, load_config(root)).endpoints}


def calls(root) -> set[str]:
    files = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    return {c.display for c in project_index(root, files, load_config(root)).calls}


def rules(
    report, rule_prefix=("unknown-endpoint", "wrong-method", "endpoint-", "duplicate", "similar")
):
    return sorted((f.rule, f.file) for f in report.findings if f.rule.startswith(rule_prefix))


# path normalisation


@pytest.mark.parametrize(
    ("template", "segments"),
    [
        ("/orders/:id", ("orders", "{id}")),
        ("/orders/{order_id}", ("orders", "{order_id}")),
        ("/orders/<int:pk>/", ("orders", "{pk}")),
        ("app/orders/[id]", ("app", "orders", "{id}")),
        ("/files/{path:path}", ("files", "**")),
        ("/docs/[...slug]", ("docs", "**")),
        ("/*", ("**",)),
    ],
)
def test_route_templates_normalise(template, segments):
    assert route_segments(template) == segments


def test_matching_and_internal_urls():
    hosts = ("localhost",)
    assert internal_path("http://localhost:8000/api/x?y=1", hosts) == ("api", "x")
    assert internal_path("https://api.stripe.com/v1/charges", hosts) is None
    assert internal_path("//cdn.example.com/x", hosts) is None
    from ghostapi.endpoints import Endpoint

    e = Endpoint("GET", ("orders", "{id}"), "a.py")
    assert endpoint_matches(e, call_segments("/orders/{}"))
    assert endpoint_matches(e, call_segments("/orders/42"))
    assert not endpoint_matches(e, call_segments("/orders"))
    mounted = Endpoint("GET", ("orders",), "a.py", prefix_known=False)
    assert endpoint_matches(mounted, call_segments("/v2/api/orders"))


# Python frameworks


def test_fastapi_routers_mounted_from_other_files(tmp_path):
    write(tmp_path, {
        "app/__init__.py": "",
        "app/main.py": "from fastapi import FastAPI\nfrom app.routers import orders\n"
                       "from .routers.users import router as users_router\n"
                       "app = FastAPI()\napp.include_router(orders.router, prefix='/api')\n"
                       "app.include_router(users_router)\n",
        "app/routers/__init__.py": "",
        "app/routers/orders.py": "from fastapi import APIRouter\nrouter = APIRouter(prefix='/orders')\n"
                                 "@router.get('/{oid}')\ndef one(oid): ...\n"
                                 "@router.api_route('', methods=['POST', 'PUT'])\ndef save(): ...\n",
        "app/routers/users.py": "from fastapi import APIRouter\nrouter = APIRouter(prefix='/users')\n"
                                "@router.get('/me')\ndef me(): ...\n",
        "app/routers/orphan.py": "from fastapi import APIRouter\nrouter = APIRouter()\n"
                                 "@router.get('/lost')\ndef lost(): ...\n",
    })  # fmt: skip
    assert routes(tmp_path) == {
        "GET /api/orders/{oid}", "POST /api/orders", "PUT /api/orders", "GET /users/me",
        "GET .../lost",  # never mounted anywhere we can see: matched by suffix only
    }  # fmt: skip


def test_flask_blueprints(tmp_path):
    write(tmp_path, {
        "web.py": "from flask import Flask, Blueprint\napp = Flask(__name__)\n"
                  "bp = Blueprint('b', __name__, url_prefix='/shop')\n"
                  "@bp.route('/items/<int:item_id>', methods=['GET', 'DELETE'])\ndef item(item_id): ...\n"
                  "@app.get('/ping')\ndef ping(): ...\n"
                  "app.register_blueprint(bp, url_prefix='/v1')\n",
    })  # fmt: skip
    assert routes(tmp_path) == {
        "GET /v1/items/{item_id}",
        "DELETE /v1/items/{item_id}",
        "GET /ping",
    }


def test_django_urlconfs_and_drf_routers(tmp_path):
    write(tmp_path, {
        "proj/settings.py": "ROOT_URLCONF = 'proj.urls'\n",
        "proj/urls.py": "from django.urls import path, include\n"
                        "urlpatterns = [path('api/', include('shop.urls')), path('health/', health)]\n",
        "shop/urls.py": "from django.urls import path, include\nfrom rest_framework.routers import DefaultRouter\n"
                        "router = DefaultRouter()\nrouter.register(r'orders', OrderViewSet)\n"
                        "urlpatterns = [path('items/<int:pk>/', item), path('', include(router.urls))]\n",
    })  # fmt: skip
    assert routes(tmp_path) == {
        "* /health", "* /api/items/{pk}", "* /api/orders", "* /api/orders/**",
    }  # fmt: skip


def test_python_http_calls(tmp_path):
    write(tmp_path, {
        "client.py": "import requests, httpx\nAPI = 'http://localhost:8000'\n"
                     "def go(oid):\n"
                     "    requests.get(f'{API}/api/orders/{oid}')\n"
                     "    requests.request('PUT', API + '/api/orders/' + str(oid))\n"
                     "    requests.post('https://api.github.com/repos')\n"
                     "    with httpx.Client(base_url='http://localhost:8000/api') as c:\n"
                     "        c.delete('/orders/1')\n",
    })  # fmt: skip
    assert calls(tmp_path) == {"GET /api/orders/{}", "PUT /api/orders/{}", "DELETE /api/orders/1"}


# JavaScript / TypeScript frameworks


def test_express_router_mounted_from_another_file(tmp_path):
    write(tmp_path, {
        "server/app.ts": "import express from 'express';\nimport orders from './routes/orders';\n"
                         "const app = express();\napp.use('/api/orders', orders);\n"
                         "app.get('/health', (req, res) => res.send('ok'));\n",
        "server/routes/orders.ts": "import { Router } from 'express';\nconst router = Router();\n"
                                   "router.get('/:id', h);\nrouter.route('/').get(h).post(h);\n"
                                   "export default router;\n",
    })  # fmt: skip
    assert routes(tmp_path) == {
        "GET /api/orders/{id}", "GET /api/orders", "POST /api/orders", "GET /health",
    }  # fmt: skip


def test_nextjs_app_and_pages_routes(tmp_path):
    write(tmp_path, {
        "next.config.js": "module.exports = {}\n",
        "app/api/orders/[id]/route.ts": "export async function GET() {}\nexport const DELETE = () => {};\n",
        "app/(shop)/api/cart/route.ts": "export async function POST() {}\n",
        "pages/api/users/index.ts": "export default function handler(req, res) {}\n",
        "src/lib/app/route.ts": "export async function GET() {}\n",  # not a Next.js app dir
    })  # fmt: skip
    assert routes(tmp_path) == {
        "GET /api/orders/{id}", "DELETE /api/orders/{id}", "POST /api/cart", "* /api/users",
    }  # fmt: skip


def test_nestjs_controllers_with_global_prefix(tmp_path):
    write(tmp_path, {
        "src/main.ts": "const app = await NestFactory.create(AppModule);\napp.setGlobalPrefix('api');\n",
        "src/orders.controller.ts": "@Controller('orders')\nexport class OrdersController {\n"
                                    "  @Get(':id')\n  find() {}\n  @Post()\n  create() {}\n}\n",
    })  # fmt: skip
    assert routes(tmp_path) == {"GET /api/orders/{id}", "POST /api/orders"}


def test_js_http_calls(tmp_path):
    write(tmp_path, {
        ".ghost/config.yaml": "http:\n  base_url_names: [process.env.NEXT_PUBLIC_API]\n",
        "web/client.ts": "import axios from 'axios';\nconst api = axios.create({ baseURL: '/api' });\n"
                         "const BASE = 'http://localhost:3000';\n"
                         "export async function go(id) {\n"
                         "  await api.get(`/orders/${id}`);\n"
                         "  await fetch('/api/orders', { method: 'POST' });\n"
                         "  await fetch(`${BASE}/api/users`);\n"
                         "  await fetch(`${process.env.NEXT_PUBLIC_API}/api/cart`, { method: 'PATCH' });\n"
                         "  await fetch(url, opts);\n"
                         "  await axios({ url: '/api/x', method: 'delete' });\n"
                         "  await fetch('https://api.stripe.com/v1/charges');\n"
                         "}\n",
    })  # fmt: skip
    assert calls(tmp_path) == {
        "GET /api/orders/{}", "POST /api/orders", "GET /api/users", "PATCH /api/cart",
        "DELETE /api/x",
    }  # fmt: skip


def test_openapi_spec_counts_as_routes(tmp_path):
    write(tmp_path, {
        "openapi.yaml": "openapi: 3.1.0\nservers:\n- url: https://api.example.com/v2\n"
                        "paths:\n  /pets/{petId}:\n    get: {}\n    delete: {}\n",
    })  # fmt: skip
    assert routes(tmp_path) == {"GET /v2/pets/{petId}", "DELETE /v2/pets/{petId}"}


# rules


FULLSTACK = {
    "api/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"
                   "@app.get('/api/orders/{oid}')\ndef one(oid): ...\n"
                   "@app.post('/api/orders')\ndef create(): ...\n",
    "web/client.ts": "export async function go(id) {\n"
                     "  await fetch(`/api/orders/${id}`);\n"
                     "  await fetch('/api/orders', { method: 'POST' });\n"
                     "  await fetch('/api/order/1');\n"
                     "  await fetch(`/api/orders/${id}`, { method: 'PUT' });\n"
                     "  await fetch('/static/logo.png');\n"
                     "}\n",
}  # fmt: skip


def test_unknown_endpoint_and_wrong_method(tmp_path):
    root = write(tmp_path, FULLSTACK)
    report = run_check(root, providers={})
    findings = {f.rule: f for f in report.findings if f.api == "http"}
    assert set(findings) == {"unknown-endpoint", "wrong-method"}  # /static/... isn't our API
    assert findings["unknown-endpoint"].line == 4
    assert findings["unknown-endpoint"].fix == "Did you mean `/api/orders` (api/main.py:5)?"
    assert "accepts GET, not PUT" in findings["wrong-method"].message


def test_duplicate_and_similar_endpoints(tmp_path):
    root = write(tmp_path, {
        "a.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/users/{id}')\ndef a(id): ...\n",
        "b.py": "from flask import Flask\nweb = Flask(__name__)\n@web.get('/users/<id>')\ndef b(id): ...\n"
                "@web.get('/user/<id>/avatar')\ndef c(id): ...\n@web.get('/users/<id>/avatar')\ndef d(id): ...\n",
    })  # fmt: skip
    assert rules(run_check(root, providers={})) == [
        ("duplicate-endpoint", "b.py"), ("similar-endpoint", "b.py"),
    ]  # fmt: skip


def git(root, *args):
    base = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run([*base, *args], cwd=root, check=True, capture_output=True)


def test_removing_a_route_tells_the_editing_agent_which_callers_break(tmp_path):
    root = write(tmp_path, FULLSTACK)
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    # Agent A deletes the POST route; Agent B's frontend still calls it.
    main = root / "api/main.py"
    main.write_text(main.read_text().replace("@app.post('/api/orders')\ndef create(): ...\n", ""))
    report = run_check(root, files=[main], providers={})
    [removed] = [f for f in report.findings if f.rule == "endpoint-removed"]
    assert (removed.file, removed.line) == ("web/client.ts", 3)
    assert "the edit to api/main.py removed or changed `POST /api/orders`" in removed.message


def test_unrelated_edits_skip_the_endpoint_scan(tmp_path):
    root = write(tmp_path, {**FULLSTACK, "util.py": "def f():\n    return 1\n"})
    report = run_check(root, files=[root / "util.py"], providers={})
    assert not [f for f in report.findings if f.api == "http"]


def test_owner_from_provide_shows_in_list_and_show(tmp_path):
    root = write(tmp_path, FULLSTACK)
    result = runner.invoke(
        app, ["provide", "--root", str(root), "--method", "POST", "--path", "api/orders",
              "--owner", "orders-agent"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    data = json.loads(runner.invoke(app, ["list", "--root", str(root), "-f", "json"]).output)
    [post] = [e for e in data["endpoints"] if e["method"] == "POST"]
    assert post["owner"] == "orders-agent" and post["callers"] == ["web/client.ts:3"]
    shown = runner.invoke(app, ["show", "POST /api/orders", "--root", str(root)])
    assert "owner:      orders-agent" in shown.output and shown.exit_code == 0
    assert runner.invoke(app, ["show", "/api/nothing", "--root", str(root)]).exit_code == 1


def test_shell_mangled_paths_get_a_clear_error(tmp_path):
    result = runner.invoke(
        app, ["provide", "--root", str(tmp_path), "--method", "GET",
              "--path", "C:/Program Files/Git/api/orders"],
    )  # fmt: skip
    assert result.exit_code == 2 and "Git Bash" in result.output


def test_config_is_optional(tmp_path):
    assert load_config(tmp_path) == Config()


def test_show_symbol_reports_signature_and_problems(tmp_path):
    from ghostapi.inventory import describe_symbol
    from ghostapi.models import ApiHistory, ApiKind, ApiSymbol, Param
    from ghostapi.providers import Provider, StaticProvider

    (tmp_path / "requirements.txt").write_text("fakeai==1.0.0\n", encoding="utf-8")
    history = ApiHistory(
        "fakeai",
        {
            "fakeai": ApiSymbol("fakeai", "module"),
            "fakeai.Client": ApiSymbol(
                "fakeai.Client", "class", params=(Param("api_key", "keyword_only"),)
            ),
            "fakeai.Old": ApiSymbol("fakeai.Old", "class", removed_in="1.0.0"),
        },
        version="1.0.0",
    )
    providers: dict[ApiKind, Provider] = {"python": StaticProvider([history])}
    ok = describe_symbol(tmp_path, "fakeai.Client", providers)
    assert (ok["status"], ok["signature"]) == ("ok", "fakeai.Client(api_key?)")
    assert describe_symbol(tmp_path, "fakeai.Old", providers)["status"] == "removed-in"
    typo = describe_symbol(tmp_path, "fakeai.Clinet", providers)
    assert typo["status"] == "unknown-symbol" and "fakeai.Client" in typo["problems"][0]["fix"]
