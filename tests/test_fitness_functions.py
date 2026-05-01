"""Architectural fitness functions.

Each test in this file pins an invariant the codebase already upholds, so
that a future change which silently weakens it breaks the pytest run
instead of slipping past review. Invariants here are derived from
explicit evidence -- AGENTS.md, in-source docstrings, or memory-of-past-
incident comments -- not from "what feels architectural."

Distinct from runtime tests: a fitness function makes no HTTP request and
spins up no app instance. It walks the source tree (text or AST) and
asserts a structural property holds. The point is to catch regressions
that runtime tests miss because the regression hides in a code path that
isn't (yet) exercised -- e.g., a new POST handler that forgot the origin
gate, or a new module reading user["totp_secret"] without coming through
the data-layer's `_with_totp` quarantine.

If one of these tests fails, prefer fixing the code over relaxing the
test. The invariants are spec, not implementation -- see AGENTS.md §3.
"""

import ast
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"


def _py_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _walk_local(node: ast.AST):
    """`ast.walk` variant that yields the input node + descendants but
    does NOT recurse into nested `FunctionDef` / `AsyncFunctionDef` /
    `Lambda` bodies. Use this for per-function analysis: a nested
    helper has its own scope, so its statements shouldn't count toward
    the enclosing function's coupling -- and the nested helper itself
    will be walked independently when the outer loop reaches it."""
    from collections import deque

    queue = deque([(node, True)])
    while queue:
        current, is_root = queue.popleft()
        yield current
        if not is_root and isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            continue
        for child in ast.iter_child_nodes(current):
            queue.append((child, False))


def _string_constants_outside_docstrings(tree: ast.AST):
    """Yield every ast.Constant string node that is NOT a module / function
    / class docstring. Comments are already stripped by ast.parse, so the
    remaining set is the strings that show up in real expressions -- SQL
    queries, error messages, format templates, etc."""
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            docstring_ids.add(id(node.body[0].value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
        ):
            yield node


# ---------------------------------------------------------------------------
# A. Generic-credentials error invariant (AGENTS.md §5)
# ---------------------------------------------------------------------------


def _autherror_local_names(tree: ast.AST) -> set[str]:
    """Set of names in this module that resolve to AuthError. Always
    includes the literal "AuthError" (covers the class definition in
    _core.py + every direct `from ._core import AuthError`), plus any
    local alias from `from ... import AuthError as <X>`. Without alias
    resolution, `from ._core import AuthError as LoginError` followed
    by `raise LoginError("wrong password")` would silently ship per-
    factor wording while the test stayed green."""
    names = {"AuthError"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "AuthError":
                    names.add(alias.asname or "AuthError")
    return names


def _raise_calls_autherror(
    node: ast.Raise, autherror_names: set[str]
) -> ast.Call | None:
    """If `node` is `raise <X>(...)` where `<X>` is a name resolving to
    AuthError (Name form against `autherror_names`, OR Attribute form
    with trailing `.AuthError` regardless of module alias), return the
    Call node; otherwise None."""
    if not isinstance(node.exc, ast.Call):
        return None
    func = node.exc.func
    name = None
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    if name is None:
        return None
    if name == "AuthError" or name in autherror_names:
        return node.exc
    return None


def test_authentication_only_raises_canonical_credential_error():
    """AGENTS.md §5: 'User-facing error copy on auth failures should not
    distinguish *why* a credential was rejected. "Invalid credentials" is
    the canonical surface; per-factor wording (wrong password vs. wrong
    TOTP vs. unknown user) gives an attacker a free oracle.'

    Static check: every direct `raise AuthError(...)` call inside app/auth/
    must use the literal canonical string. LockoutError is intentionally
    distinct (it carries `until_iso` for the route to translate) and is
    not covered by this rule -- the rule applies only to the unqualified
    AuthError surface that reaches "invalid credentials" copy at the
    route layer.
    """
    canonical = "invalid credentials"
    offenders: list[str] = []
    for py in _py_files(APP_DIR / "auth"):
        tree = ast.parse(py.read_text())
        autherror_names = _autherror_local_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            call = _raise_calls_autherror(node, autherror_names)
            if call is None:
                continue
            ok = (
                call.args
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value == canonical
            )
            if not ok:
                rel = py.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "AuthError must be raised exclusively with the canonical "
        f"{canonical!r} message (AGENTS.md §5). Per-factor wording leaks "
        "which credential was wrong.\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# B. TOTP-secret quarantine (app/models/users.py docstring + dev-sec rule)
# ---------------------------------------------------------------------------


def _scope_reads_totp_secret(scope: ast.AST) -> bool:
    """True iff `scope` performs a real READ of `totp_secret` in its
    own body -- a Load-context subscript (`x["totp_secret"]`) or a
    `.pop` / `.get` call that returns the value. Store/Del subscripts
    (`row["totp_secret"] = "[redacted]"`, `del row["totp_secret"]`)
    are NOT reads -- they don't expose the plaintext value.

    Nested function bodies are skipped: a helper `def` inside `scope`
    has its own scope and will be analyzed independently."""
    for node in _walk_local(scope):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "totp_secret"
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"pop", "get"}
            and node.args
        ):
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == "totp_secret":
                return True
    return False


# The exact set of sanctioned data-layer getters that decrypt + return
# `totp_secret`. Defined in app/models/users.py; any other call whose
# name happens to start with `get_user_with_totp_` (a future
# `get_user_with_totp_redacted_for_logs` helper, say) does NOT satisfy
# the quarantine -- only these two do. New sanctioned getters land here
# explicitly, in the same PR that adds them in app/models/.
_WITH_TOTP_GETTERS = frozenset(
    {"get_user_with_totp_by_id", "get_user_with_totp_by_username"}
)


def _scope_calls_with_totp_getter(scope: ast.AST) -> bool:
    """True iff `scope` actually CALLS one of the sanctioned data-layer
    `get_user_with_totp_*` getters in its own body -- a real `Call`
    node, name matched against the closed `_WITH_TOTP_GETTERS` set
    (not a prefix). Nested function bodies are skipped so a helper's
    getter call can't satisfy the enclosing function's read; the
    helper is analyzed in its own right when the outer loop reaches
    it."""
    for node in _walk_local(scope):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name in _WITH_TOTP_GETTERS:
            return True
    return False


def test_totp_secret_reads_only_in_functions_that_use_with_totp_getters():
    """app/models/users.py module docstring pins the convention: the
    plaintext TOTP seed is exposed only via `get_user_with_totp_*`
    getters; every other read path returns a dict that omits the column.

    Static check: any FUNCTION that performs a real `["totp_secret"]`
    Load-context read (or `.pop`/`.get` on the same key) must also CALL
    one of the `get_user_with_totp_*` getters in its own body. The
    coupling is at function scope, not file scope -- a sibling function
    elsewhere in the same module that happens to call the getter does
    NOT satisfy a separate function's read.

    Both halves are AST-grounded (not text/substring) so a comment,
    import, or unrelated string mentioning either name can't satisfy
    the guard. Files in the data layer itself (app/models/users.py,
    app/models/_core.py for the plaintext-encryption migration) are
    exempt -- they are the source of the secret, not consumers of it.
    """
    allowlist = {"app/models/users.py", "app/models/_core.py"}
    offenders: list[str] = []
    for py in _py_files(APP_DIR):
        rel = str(py.relative_to(REPO_ROOT))
        if rel in allowlist:
            continue
        tree = ast.parse(py.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _scope_reads_totp_secret(fn):
                continue
            if not _scope_calls_with_totp_getter(fn):
                offenders.append(f"{rel}:{fn.lineno} {fn.name}")
    assert not offenders, (
        "Functions reading `totp_secret` must obtain it via a real "
        "`get_user_with_totp_*` call in their own body, not lean on a "
        "sibling function elsewhere in the module "
        "(see app/models/users.py module docstring).\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# C. Single-writer to analytics_events (app/analytics.py module docstring)
# ---------------------------------------------------------------------------


def test_analytics_events_table_has_a_single_writer():
    """app/analytics.py docstring: 'The gate is checked inside record_event*,
    not at the call site -- a future emitter that forgets the gate is a
    class of bug we want the audit-internal contract to make impossible.'

    The two-gate emit (operator env + per-user opt-in) and the presence-
    only invariant only hold if there is exactly one runtime writer to
    `analytics_events`. Static check: SQL operations against the table
    (`FROM`, `INTO`, `UPDATE`, `TABLE`, `INDEX ... ON`, `IF EXISTS`)
    appear only in the data-layer surface -- analytics.py + models/_core.py
    + the migration files that created or evolved the table. Free-form
    mentions in docstrings or help text (e.g., `analytics_events table`
    in `python -m app.admin --help` output) are intentional documentation
    and don't carry write capability, so the regex requires a SQL
    keyword adjacent to the table name.
    """
    allowlist = {
        "app/analytics.py",
        "app/models/_core.py",
        "app/models/migrations/v4.py",
        "app/models/migrations/v5.py",
    }
    sql_ref = re.compile(
        r"\b(?:FROM|INTO|UPDATE|TABLE|ON|EXISTS)\s+_?analytics_events\b",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for py in _py_files(APP_DIR):
        rel = str(py.relative_to(REPO_ROOT))
        if rel in allowlist:
            continue
        tree = ast.parse(py.read_text())
        for node in _string_constants_outside_docstrings(tree):
            if sql_ref.search(node.value):
                offenders.append(f"{rel}:{node.lineno}")
                break
    assert not offenders, (
        "SQL operations on `analytics_events` must live inside the data-"
        "layer allowlist (analytics.py, models/_core.py, "
        "models/migrations/v4.py + v5.py). Any other writer would bypass "
        "the two-gate emit + presence-only invariants.\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# D. Origin gate on every state-mutating route (CSRF defense)
# ---------------------------------------------------------------------------


_MUTATING_VERBS = {"post", "put", "patch", "delete"}


def _kwargs_contain_mutating_method(keywords: list[ast.keyword]) -> bool:
    """True iff `keywords` (a Call's `.keywords` list) carries
    `methods=[...]` containing a state-mutating verb literal. Matches
    `methods=["POST"]`, `methods=("PUT",)`, `methods={"PATCH"}` -- any
    iterable literal of string constants. Used by both the
    `api_route(...)` decorator detector and the imperative
    `add_api_route(...)` registration scan."""
    for kw in keywords:
        if kw.arg != "methods":
            continue
        if not isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
            continue
        for elt in kw.value.elts:
            if (
                isinstance(elt, ast.Constant)
                and isinstance(elt.value, str)
                and elt.value.lower() in _MUTATING_VERBS
            ):
                return True
    return False


def _is_state_mutating_route_decorator(deco: ast.expr) -> bool:
    """True iff `deco` registers a state-mutating HTTP handler. Two
    FastAPI shapes count:

    - **Verb-named decorator**: `@<expr>.post(...)`, `@<expr>.put(...)`,
      `@<expr>.patch(...)`, `@<expr>.delete(...)`. `<expr>` may be a
      Name (`@router.post`, `@app.post`) or an attribute chain
      (`@api.router.post`, `@app.state.router.post`).

    - **Catch-all `api_route`** with methods= containing a mutating
      verb: `@<expr>.api_route(..., methods=["POST", "PUT"])` is just
      as state-changing as `@<expr>.post`, but its decorator name is
      neutral. The earlier verb-only detector skipped this shape, so
      a write endpoint registered through `api_route` could ship
      without the CSRF gate while the test still passed.
    """
    if not isinstance(deco, ast.Call):
        return False
    if not isinstance(deco.func, ast.Attribute):
        return False
    attr = deco.func.attr
    if attr in _MUTATING_VERBS:
        return True
    if attr == "api_route":
        return _kwargs_contain_mutating_method(deco.keywords)
    return False


def _is_imperative_mutating_registration(node: ast.AST) -> bool:
    """True iff `node` is an imperative `<expr>.add_api_route(...)` call
    that registers a state-mutating handler. FastAPI exposes
    `router.add_api_route(path, endpoint, methods=[...], ...)` as the
    function-call equivalent of the decorator API; the decorator-only
    detector skipped this shape entirely, so a write endpoint added
    imperatively could miss the CSRF gate while the test stayed
    green."""
    if not isinstance(node, ast.Call):
        return False
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "add_api_route":
        return False
    return _kwargs_contain_mutating_method(node.keywords)


def _is_origin_dependency(node: ast.AST) -> bool:
    """True iff `node` is a real `Depends(verify_same_origin)` call --
    a Call whose function is the bare name `Depends` and whose
    `verify_same_origin` argument is supplied either as the first
    positional arg OR via FastAPI's documented `dependency=` keyword.
    Catches every shape used (or potentially used) in the codebase:

        dependencies=[Depends(verify_same_origin)]             # positional
        _origin = Depends(verify_same_origin)                  # positional
        Depends(dependency=verify_same_origin)                 # keyword
        dependencies=[Depends(dependency=verify_same_origin)]  # keyword

    Rejects substring-only matches: a parameter literally NAMED
    `verify_same_origin`, an annotation referencing the symbol, or a
    docstring/comment containing the token are all not the dependency."""
    if not isinstance(node, ast.Call):
        return False
    if not isinstance(node.func, ast.Name) or node.func.id != "Depends":
        return False
    if node.args:
        first = node.args[0]
        if isinstance(first, ast.Name) and first.id == "verify_same_origin":
            return True
    for kw in node.keywords:
        if (
            kw.arg == "dependency"
            and isinstance(kw.value, ast.Name)
            and kw.value.id == "verify_same_origin"
        ):
            return True
    return False


def test_state_mutating_routes_all_carry_origin_gate():
    """Every POST/PUT/PATCH/DELETE handler under app/ must depend on
    `Depends(verify_same_origin)`, either inside the decorator's
    `dependencies=[...]` keyword or as a parameter default
    (`_origin = Depends(verify_same_origin)`). Both shapes are in
    active use -- see app/routes/sender.py (decorator form) and
    app/routes/prefs.py::patch_language (parameter form).

    Structural check: walks the decorator subtree and the function
    signature for the actual `Depends(verify_same_origin)` Call shape,
    not a substring of `ast.dump`. A parameter merely named
    `verify_same_origin` (no `Depends(...)`) does NOT satisfy the gate
    and is correctly flagged.

    Why static: a runtime test that misses 'this new POST has no test
    yet' silently lets the origin gate slip. AST walk catches the
    handler regardless of whether anyone wrote a request-level test.
    """
    offenders: list[str] = []
    for py in _py_files(APP_DIR):
        tree = ast.parse(py.read_text())
        # Pass 1: decorator-style registrations on FunctionDef /
        # AsyncFunctionDef. Both sync `def` and `async def` shapes
        # are valid FastAPI handlers (e.g. app/routes/sender.py::
        # create_secret is async); walk both node kinds.
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            mutating_decos = [
                d for d in node.decorator_list if _is_state_mutating_route_decorator(d)
            ]
            if not mutating_decos:
                continue
            roots: list[ast.AST] = list(mutating_decos)
            roots.append(node.args)
            ok = any(
                _is_origin_dependency(inner)
                for root in roots
                for inner in ast.walk(root)
            )
            if not ok:
                rel = py.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{node.lineno} {node.name}")
        # Pass 2: imperative `<expr>.add_api_route(path, endpoint,
        # methods=[...])` calls anywhere in the module. We can't
        # follow the endpoint reference to its signature statically,
        # so the gate must be wired in the call's own keyword args
        # (the standard `dependencies=[Depends(verify_same_origin)]`
        # placement). A call that sneaks the dependency only into
        # the endpoint's signature is an accepted miss -- a rare
        # enough shape that we'd catch it in review rather than
        # carry import-resolution machinery here.
        for node in ast.walk(tree):
            if not _is_imperative_mutating_registration(node):
                continue
            ok = any(_is_origin_dependency(inner) for inner in ast.walk(node))
            if not ok:
                rel = py.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{node.lineno} add_api_route(...)")
    assert not offenders, (
        "State-mutating routes (POST/PUT/PATCH/DELETE), whether registered "
        "via a decorator or `<router>.add_api_route(...)`, must carry "
        "`Depends(verify_same_origin)` -- either inline in the "
        "registration's `dependencies=` or as a function-parameter "
        "default.\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# F. No print() in app/ (use security_log.emit / structured logging instead)
# ---------------------------------------------------------------------------


def test_no_print_calls_in_request_path_or_data_layer():
    """`print()` inside the request-serving / data-layer surface lands
    in journalctl as unstructured text and bypasses the structured
    `app/security_log.py` conduit the codebase otherwise enforces. The
    one legitimate stdout-writer in this repo is `app/admin/`, the
    operator-facing CLI, where stdout IS the user interface
    (provisioning output, diagnostic dumps, etc.) -- exempt by design.

    Anywhere else, a stray `print()` is debug noise that escaped a
    commit. If a future module genuinely needs stdout (a new CLI
    surface, a release script invoked at boot), add a narrow
    file-level exemption here with a one-line reason.
    """
    exempt_prefixes = ("app/admin/",)
    offenders: list[str] = []
    for py in _py_files(APP_DIR):
        rel = str(py.relative_to(REPO_ROOT))
        if any(rel.startswith(prefix) for prefix in exempt_prefixes):
            continue
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Match both bare `print(...)` and qualified shapes like
            # `builtins.print(...)`. The qualified form was previously
            # skipped because the detector required `func` to be ast.Name;
            # `from builtins import print` aliases (renaming `print` to
            # something else) are NOT detected -- they're exotic enough
            # that we'd rather let them stand out in review than carry
            # the import-resolution machinery here. Same posture the
            # AuthError test already takes.
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "print":
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "`print()` calls outside `app/admin/` (the operator CLI) are not "
        "allowed -- use security_log.emit or the standard logging module "
        "so output is structured and audit-trail-visible.\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# G. Pinned auth/crypto tuning constants
# ---------------------------------------------------------------------------


def _read_module_int_constants(path: pathlib.Path) -> dict[str, int]:
    """Parse `path` as Python and return module-level `NAME = <int_literal>`
    bindings. Pure source read -- never imports the module, so this stays
    a true static check that doesn't run app/__init__.py or pull in
    FastAPI / auth submodules just to read four integers."""
    tree = ast.parse(path.read_text())
    out: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
            out[target.id] = node.value.value
    return out


def test_security_constants_are_not_silently_weakened():
    """Pin the deliberate tuning knobs in app/auth/_core.py so a future
    "tests are too slow, drop bcrypt cost" or "let's accept 4-digit TOTP"
    diff fails in source rather than at release.

    Source-level read (no import): the source-of-truth is the assignment
    in `_core.py`, not the runtime-resolved value. Importing the module
    would also execute `app/__init__.py` and the auth package, so
    unrelated wiring failures could trip a check whose contract is
    "are these four integer constants still at their pinned values."

    - BCRYPT_ROUNDS = 12: ~250ms/hash, deliberate (see cosmic-ray.toml
      timeout note + app/auth/password.py). Lower rounds ship a weaker
      hash to every existing user; higher rounds slow login + the
      timing-equalization loops in app/auth/login.py.
    - TOTP_DIGITS = 6, TOTP_INTERVAL = 30: RFC 6238 defaults; widening
      either silently breaks every provisioned authenticator app and
      needs a coordinated re-provisioning.
    - RECOVERY_CODE_COUNT = 10: drives the dummy-bcrypt loop count in
      authenticate()'s timing-equalization; changing it without updating
      that loop reintroduces the unknown-user timing oracle.
    """
    constants = _read_module_int_constants(APP_DIR / "auth" / "_core.py")

    bcrypt_rounds = constants.get("BCRYPT_ROUNDS")
    assert bcrypt_rounds is not None and bcrypt_rounds >= 12, (
        f"BCRYPT_ROUNDS dropped to {bcrypt_rounds}; "
        "12 is the floor -- weakening this ships a worse hash to every user"
    )
    assert constants.get("TOTP_DIGITS") == 6, (
        f"TOTP_DIGITS changed to {constants.get('TOTP_DIGITS')}; RFC 6238 "
        "default is 6 and every provisioned authenticator app expects it"
    )
    assert constants.get("TOTP_INTERVAL") == 30, (
        f"TOTP_INTERVAL changed to {constants.get('TOTP_INTERVAL')}s; RFC 6238 "
        "default is 30s and every provisioned authenticator app expects it"
    )
    assert constants.get("RECOVERY_CODE_COUNT") == 10, (
        f"RECOVERY_CODE_COUNT changed to {constants.get('RECOVERY_CODE_COUNT')}; "
        "the timing-equalization loop in app/auth/login.py is sized to 10. "
        "Changing one without the other reintroduces a username-existence "
        "timing oracle."
    )
