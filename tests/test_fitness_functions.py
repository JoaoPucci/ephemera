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
    does NOT recurse into nested `FunctionDef` / `AsyncFunctionDef`
    bodies. Use this for per-function analysis: a nested function
    has its own scope, so its statements shouldn't count toward the
    enclosing function's coupling -- and the nested function itself
    will be walked independently when the outer test loop reaches it.

    `Lambda` bodies ARE descended into. A lambda is a single
    expression, never iterated by the outer test loop; reads inside
    a lambda would otherwise vanish from analysis entirely
    (`def f(): callback = lambda r: r["totp_secret"]; ...` would
    bypass the quarantine because the enclosing function's read
    detector skipped the lambda body and the lambda itself was
    never analysed). The lambda's parameters live in its own scope,
    so a read on a lambda parameter is correctly an untrusted
    Identifier read under the enclosing function's trust set."""
    from collections import deque

    queue = deque([(node, True)])
    while queue:
        current, is_root = queue.popleft()
        yield current
        if not is_root and isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.iter_child_nodes(current):
            queue.append((child, False))


def _string_text_outside_docstrings(tree: ast.AST):
    """Yield `(text, lineno)` for every string-shaped expression that is
    NOT a module / function / class docstring. Covers two AST shapes:

      - `ast.Constant` str values: regular `"..."` / `'...'` literals.
      - `ast.JoinedStr`: f-strings. The yielded text is the concatenation
        of the f-string's literal segments (`Constant` parts of the
        `JoinedStr.values` list); interpolated `{expr}` parts are
        skipped, since static checks like the SQL-keyword scan only
        need to see the literal SQL skeleton, not whatever a runtime
        expression might substitute.

    Without the JoinedStr arm, SQL written as
    `f"INSERT INTO analytics_events ({cols}) VALUES (...)"` would skip
    the analytics-table guard entirely (the table name lives inside
    the literal portion of an f-string, not in any plain ast.Constant
    string).

    Comments are already stripped by ast.parse, so the remaining set
    is the strings that appear in real expressions."""
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
            yield (node.value, node.lineno)
        elif isinstance(node, ast.JoinedStr):
            parts = [
                v.value
                for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            ]
            if parts:
                yield ("".join(parts), node.lineno)


# ---------------------------------------------------------------------------
# A. Generic-credentials error invariant (AGENTS.md §5)
# ---------------------------------------------------------------------------


def _autherror_local_names(tree: ast.AST) -> set[str]:
    """Set of names in this module that resolve to AuthError. Two
    alias channels are tracked:

      ImportFrom: `from ._core import AuthError as <X>` -- standard
                  import aliasing.
      Assign:     `<X> = AuthError` at module level -- in-module
                  rebinding. Iterated to fixed point so chains like
                  `A1 = AuthError; A2 = A1` propagate through.

    Without both channels, an in-module alias would silently bypass
    the canonical-message check: `LoginError = AuthError; raise
    LoginError("wrong password")` would not match
    `_is_autherror_construction` because `LoginError` wasn't in the
    set."""
    names = {"AuthError"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "AuthError":
                    names.add(alias.asname or "AuthError")
    # Alias assignments at any scope. Walks `ast.walk(tree)` instead
    # of `tree.body` so a function-local rebind like
    # `def login_handler(): Local = AuthError; raise Local("wrong
    # password")` also feeds the alias set. Iterated to fixed point
    # so chains (`A1 = AuthError; A2 = A1`) propagate. Two RHS shapes
    # propagate the alias:
    #
    #   Name:      `Local = AuthError`              -- bare reference
    #   Attribute: `Local = auth_core.AuthError`    -- module-qualified
    #              reference; recognise any `<X>.<attr>` whose final
    #              attr is already in `names`. Conservative direction:
    #              we don't verify what `<X>` resolves to, so a
    #              `Local = unrelated.AuthError` would also propagate
    #              (the canonical-message check would then over-flag
    #              an unrelated `Local("...")`). The realistic
    #              codebase doesn't have such a rebind, and over-
    #              flagging matches the rest of the test posture.
    #
    # Adding aliases only EXPANDS the recognised set; it never
    # narrows it -- `Local = Something_Unrelated` (Name with id not
    # in `names`) doesn't pull `Something_Unrelated` into the set.
    changed = True
    while changed:
        changed = False
        for stmt in ast.walk(tree):
            if not isinstance(stmt, ast.Assign):
                continue
            if not _value_resolves_to_known_name(stmt.value, names):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return names


def _value_resolves_to_known_name(value: ast.expr, names: set[str]) -> bool:
    """True iff `value` is a Name or Attribute whose terminal label is
    already in `names`. Used by the AuthError alias propagator: a
    `Local = AuthError` and a `Local = auth_core.AuthError` both
    bind `Local` to the AuthError class; either RHS shape should
    feed the alias set."""
    if isinstance(value, ast.Name):
        return value.id in names
    if isinstance(value, ast.Attribute):
        return value.attr in names
    return False


def _is_autherror_construction(call: ast.Call, autherror_names: set[str]) -> bool:
    """True iff `call` constructs an AuthError instance: `AuthError(...)`,
    `auth_core.AuthError(...)`, or an aliased `LoginError(...)` where
    `LoginError` was imported from the data layer (recorded in
    `autherror_names`).

    Checking constructions instead of the narrower `raise <Call>(...)`
    pattern catches two-step shapes the earlier helper missed:

        err = AuthError("wrong password")
        raise err

    The construction itself is the message-bearing site -- if the
    call's first arg isn't the canonical string, the eventual raise
    leaks the per-factor wording regardless of how many local
    variables sit between the two."""
    func = call.func
    name = None
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    if name is None:
        return False
    return name == "AuthError" or name in autherror_names


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
            if not isinstance(node, ast.Call):
                continue
            if not _is_autherror_construction(node, autherror_names):
                continue
            ok = (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == canonical
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


def _string_const_aliases(scope: ast.AST) -> dict[str, set[str]]:
    """Return a `Name -> set[string]` map collecting EVERY string
    constant a local name has been bound to anywhere in `scope`.
    Multiple bindings ACCUMULATE; the matcher treats a name as a
    totp_secret alias iff `"totp_secret"` is in its set, regardless
    of source order.

    Last-write-wins (the previous semantics) loses reads from
    earlier bindings:

        key = "totp_secret"
        row[key]                # reads totp_secret here
        key = "other"           # later rebind hid the earlier match
                                # under last-write-wins -- bypass

    Set accumulation conservatively flags both `row[key]` reads in
    the rebind-then-read shape `key = "totp_secret"; row[key];
    key = "x"; row[key]` (the second isn't actually a totp_secret
    read at runtime). Realistic codebases rarely rebind a totp-key
    variable, and over-flagging is the safe direction. `_walk_local`
    picks up bindings inside lambdas / conditionals but skips
    nested `FunctionDef` bodies (their own scope)."""
    aliases: dict[str, set[str]] = {}
    for node in _walk_local(scope):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    aliases.setdefault(target.id, set()).add(node.value.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and isinstance(node.target, ast.Name)
        ):
            aliases.setdefault(node.target.id, set()).add(node.value.value)
    return aliases


def _matches_totp_secret_key(node: ast.AST, string_consts: dict[str, set[str]]) -> bool:
    """True iff `node` is the literal `"totp_secret"` constant or a
    `Name` whose alias set in `string_consts` contains "totp_secret".
    Used by both the scope-level read detector and the per-statement
    read check inside the structural walker."""
    if isinstance(node, ast.Constant) and node.value == "totp_secret":
        return True
    if isinstance(node, ast.Name):
        return "totp_secret" in string_consts.get(node.id, set())
    return False


def _scope_reads_totp_secret(
    scope: ast.AST, string_consts: dict[str, set[str]] | None = None
) -> bool:
    """True iff `scope` performs a real READ of `totp_secret` in its
    own body -- a Load-context subscript (`x["totp_secret"]`) or a
    `.pop` / `.get` / `.setdefault` call that returns the value.
    Store/Del subscripts (`row["totp_secret"] = "[redacted]"`, `del
    row["totp_secret"]`) are NOT reads -- they don't expose the
    plaintext value.

    Indirect-key access via a local string-const alias
    (`key = "totp_secret"; row[key]`) is recognised when
    `string_consts` resolves `key` to the literal value. Pass the
    map (computed once per scope via `_string_const_aliases`); the
    default `None` handles callers that don't want the indirection
    coverage.

    Nested function bodies are skipped via `_walk_local`; lambda
    bodies are NOT skipped, so a `lambda r: r["totp_secret"]` inside
    the scope counts as a read of the enclosing function."""
    consts = string_consts or {}
    for node in _walk_local(scope):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and _matches_totp_secret_key(node.slice, consts)
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"pop", "get", "setdefault"}
            and node.args
            and _matches_totp_secret_key(node.args[0], consts)
        ):
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


# Module specs that resolve to the data-layer source of the sanctioned
# getters. Both absolute and relative imports are checked against this
# set after relative paths are resolved against the importing file's
# own package (see `_resolve_import_module`). `app.models` is included
# to cover a re-export through the package `__init__`; the closed list
# keeps the trust surface small.
_DATA_LAYER_ABSOLUTE_MODULES = frozenset({"app.models", "app.models.users"})

# Names whose canonical implementation lives at a specific module path.
# An `import` (or `from ... import`) that brings ANY of these names into
# scope from a NON-canonical source shadows the legit binding, and the
# rebound-names sweep treats the name as no longer resolving to the
# trusted symbol. Names not in this dict aren't tracked -- a stray
# `import urllib` doesn't make every downstream `urllib` reference
# shadowed, and the consumers (`_scope_totp_reads_are_quarantined`,
# `test_state_mutating_routes_all_carry_origin_gate`) only look up the
# specific names they care about.
#
# `verify_same_origin` is defined in `app/dependencies.py`. `models` is
# the package `app/models/`, exposed by both `from app import models`
# (re-export through `app/__init__.py`) and direct `from app.models
# import ...` for individual symbols.
_CANONICAL_IMPORT_SOURCES: dict[str, frozenset[str]] = {
    "models": frozenset({"app", "app.models"}),
    "verify_same_origin": frozenset({"app.dependencies"}),
    # FastAPI's `Depends` is the only callable that wires a
    # FastAPI dependency in our codebase. A rebind of `Depends`
    # (Assign / AnnAssign at module level) or an import from a
    # non-FastAPI source shadows the real one, so a literal
    # `Depends(verify_same_origin)` would no longer wire the gate.
    "Depends": frozenset({"fastapi"}),
}


def _resolve_import_module(node: ast.ImportFrom, file_path: pathlib.Path) -> str | None:
    """Return the absolute dotted module path for `node` (a `from ...
    import ...` statement) inside `file_path`. Resolves relative
    imports against the importing file's package: `from .users import
    X` in `app/auth/login.py` resolves to `app.auth.users`, NOT
    `app.models.users`. Returns None if `node.level` walks past the
    repo root, or if the import has no module name and no level (a
    syntactic impossibility for ImportFrom).

    Without proper resolution, the trust check would accept any
    relative `from .users ...` regardless of where the importing file
    lives, so an unrelated `app/auth/users.py` shipping a same-named
    helper could satisfy the quarantine."""
    mod = node.module or ""
    if node.level == 0:
        return mod or None
    rel = file_path.relative_to(REPO_ROOT) if file_path.is_absolute() else file_path
    package_parts = list(rel.with_suffix("").parts)[:-1]
    drop = node.level - 1
    if drop > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - drop]
    if not base_parts:
        return None
    base = ".".join(base_parts)
    return f"{base}.{mod}" if mod else base


def _sanctioned_totp_getter_aliases(tree: ast.AST, file_path: pathlib.Path) -> set[str]:
    """Module-scope names that resolve to a sanctioned getter via direct
    import FROM THE DATA LAYER. Walks ONLY the module's top-level
    `from <X> import <getter>` / `... as <alias>` statements --
    function-local imports are scope-bound and would polluteon other
    functions' alias sets if collected here.

    Resolves relative imports against `file_path`'s package and
    accepts only those whose absolute module is in
    `_DATA_LAYER_ABSOLUTE_MODULES`. Used so `<name>(uid)` calls are
    accepted only when `<name>` was actually imported at module scope
    from the data layer, not when an unrelated module (sibling
    `users.py`, third-party helper) happens to export the same name.

    Module-level `try: from ... import X except ImportError:` blocks
    are not specifically supported -- we only walk `tree.body` directly.
    Such blocks would need a small extension that descends through
    top-level `Try` / `If` containers; the codebase doesn't use them
    for the data-layer imports we care about."""
    names: set[str] = set()
    if not isinstance(tree, ast.Module):
        return names
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        resolved = _resolve_import_module(node, file_path)
        if resolved not in _DATA_LAYER_ABSOLUTE_MODULES:
            continue
        for alias in node.names:
            if alias.name in _WITH_TOTP_GETTERS:
                names.add(alias.asname or alias.name)
    return names


def _is_sanctioned_getter_call(
    call: ast.Call,
    sanctioned_aliases: set[str],
    models_shadowed: bool = False,
) -> bool:
    """True iff `call` is a sanctioned-getter invocation, in either
    convention shape:

      models.<getter>(...)  -- Attribute call on the bare Name `models`
                               (the receiver must be `models`, not
                               `svc.<getter>` or `obj.<getter>` on an
                               unrelated class). Rejected when
                               `models_shadowed` is True -- a function
                               parameter, a local Assign / AnnAssign,
                               or a module-level rebind has shadowed
                               the imported `models` name.
      <getter>(...)         -- Name call where `<getter>` was imported
                               directly into this module via
                               `from app.models[.users] import ...`,
                               recorded in `sanctioned_aliases`.
                               (Function-local rebinding of those
                               names is handled separately by the
                               flow-sensitive walker, which removes
                               them from `local_sanctioned`.)
    """
    func = call.func
    if isinstance(func, ast.Name) and func.id in sanctioned_aliases:
        return True
    return (
        not models_shadowed
        and isinstance(func, ast.Attribute)
        and func.attr in _WITH_TOTP_GETTERS
        and isinstance(func.value, ast.Name)
        and func.value.id == "models"
    )


def _is_sanctioned_or_none_source(
    expr: ast.AST | None,
    sanctioned_aliases: set[str],
    models_shadowed: bool = False,
) -> bool:
    """True iff every reachable value of `expr` is either the result
    of a sanctioned getter call OR `None`. Handles two real patterns:

      Call:    `sanctioned(...)`                    -> trusted
      None:    `None`                               -> trusted (any
                                                       subscript on
                                                       None raises at
                                                       runtime, so a
                                                       None branch
                                                       can't carry
                                                       a totp_secret
                                                       read)
      IfExp:   `sanctioned(...) if cond else None`  -> trusted
               (the canonical login.py shape: bind to None when no
               username, else fetch through the sanctioned getter;
               the None branch falls through to a raise before any
               read happens)

    Other shapes (BoolOp `or`/`and`, Subscript, comprehension,
    arbitrary Call) return False -- conservative direction matches
    the rest of the test."""
    if expr is None:
        return False
    if isinstance(expr, ast.Constant) and expr.value is None:
        return True
    if isinstance(expr, ast.Call):
        return _is_sanctioned_getter_call(expr, sanctioned_aliases, models_shadowed)
    if isinstance(expr, ast.IfExp):
        return _is_sanctioned_or_none_source(
            expr.body, sanctioned_aliases, models_shadowed
        ) and _is_sanctioned_or_none_source(
            expr.orelse, sanctioned_aliases, models_shadowed
        )
    return False


def _is_trusted_totp_receiver(
    recv: ast.AST,
    trusted_locals: set[str],
    sanctioned_aliases: set[str],
    models_shadowed: bool = False,
) -> bool:
    """True iff `recv` (the expression on which `["totp_secret"]` or
    `.pop("totp_secret", ...)` is performed) is sourced from a
    sanctioned getter:

      - A `Name` that's in `trusted_locals` (bound earlier in the
        scope from a sanctioned-getter call).
      - A direct sanctioned-getter `Call` (for the inline pattern
        `models.get_user_with_totp_by_id(uid)["totp_secret"]`),
        passing `models_shadowed` through so a shadowed-models
        receiver doesn't slip past.
    """
    if isinstance(recv, ast.Name):
        return recv.id in trusted_locals
    if isinstance(recv, ast.Call):
        return _is_sanctioned_getter_call(recv, sanctioned_aliases, models_shadowed)
    return False


_COMP_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _comprehension_target_names(node: ast.AST) -> set[str]:
    """Names bound by a comprehension's `for ... in ...` generators.
    A list/set/dict/generator comprehension introduces a NEW local
    scope where every generator target shadows enclosing names; a
    `[user["totp_secret"] for user in rows]` re-binds `user` to each
    element of `rows`, so the read inside is on the comprehension-
    scope `user`, not the outer trusted one."""
    out: set[str] = set()
    if isinstance(node, _COMP_NODES):
        for gen in node.generators:
            out.update(_names_bound_by_target(gen.target))
    return out


def _lambda_param_names(node: ast.Lambda) -> set[str]:
    """Lambda parameters: positional, kw-only, *args, **kwargs."""
    args = node.args
    out = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg is not None:
        out.add(args.vararg.arg)
    if args.kwarg is not None:
        out.add(args.kwarg.arg)
    return out


def _check_reads_in_node(
    node: ast.AST | None,
    trusted: set[str],
    sanctioned_aliases: set[str],
    models_shadowed: bool = False,
    string_consts: dict[str, set[str]] | None = None,
    shadowed: frozenset[str] = frozenset(),
) -> bool:
    """Recursively check `totp_secret` reads inside `node`. Returns
    False on the first untrusted receiver.

    `shadowed` carries names bound by an enclosing comprehension /
    lambda. A read on a Name in `shadowed` is treated as untrusted
    even when that Name is in the outer `trusted` set, because
    comprehension targets and lambda parameters live in their own
    inner scope:

        user = sanctioned()                          # outer trusted = {user}
        [user["totp_secret"] for user in rows]       # inner `user` is rows-element

    The list comprehension's `for user in rows` re-binds `user`
    locally; the read inside the comprehension body is on the
    rebound `user`, not the outer one.

    Nested `FunctionDef` / `AsyncFunctionDef` bodies are skipped
    (they have their own scope, analyzed independently). Lambda
    bodies are descended into with the lambda's parameters added
    to `shadowed`. Comprehensions add their generator targets to
    `shadowed` and recurse into both `elt` (the body) and the
    generators' iter / ifs / target subtrees."""
    if node is None:
        return True
    consts = string_consts or {}
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return True
    if isinstance(node, ast.Lambda):
        new_shadowed = shadowed | _lambda_param_names(node)
        return _check_reads_in_node(
            node.body,
            trusted,
            sanctioned_aliases,
            models_shadowed,
            consts,
            new_shadowed,
        )
    if isinstance(node, _COMP_NODES):
        return _check_comprehension_reads(
            node, trusted, sanctioned_aliases, models_shadowed, consts, shadowed
        )
    if _is_untrusted_totp_subscript_or_call(
        node, trusted, sanctioned_aliases, models_shadowed, consts, shadowed
    ):
        return False
    for child in ast.iter_child_nodes(node):
        if not _check_reads_in_node(
            child, trusted, sanctioned_aliases, models_shadowed, consts, shadowed
        ):
            return False
    return True


def _check_comprehension_reads(
    node: ast.expr,
    trusted: set[str],
    sanctioned_aliases: set[str],
    models_shadowed: bool,
    consts: dict[str, set[str]],
    shadowed: frozenset[str],
) -> bool:
    """Walk a list / set / dict / generator comprehension, adding
    the comprehension's generator-target names to `shadowed` for
    its `elt`, key/value (DictComp), iters, and ifs.

    Threads walrus (`:=`) rebinds from the iters and ifs into the
    trust state before checking the elt. PEP 572 says a walrus
    inside a comprehension binds in the *containing scope*, and
    Python evaluates a comprehension clause-by-clause -- iter[0]
    -> bind target[0] -> ifs[0] -> iter[1] -> bind target[1] ->
    ifs[1] -> ... -> elt -- so by the time the elt runs, every
    walrus in the iters / ifs that fired this iteration has
    already mutated the containing scope. Without threading those
    rebinds, the elt is checked against a stale snapshot:

        user = models.get_user_with_totp_by_id(uid)  # trusted
        [
            user["totp_secret"]
            for row in rows
            if (user := models.get_user_by_id(uid))   # unsanctioned
        ]

    The if's walrus rebinds `user` to an unsanctioned source on
    every matching iteration; the elt's read should be flagged.
    Threading state through iter and ifs in clause order makes the
    elt's check see the post-walrus trust set."""
    new_shadowed = shadowed | _comprehension_target_names(node)
    cur_t, cur_a = trusted, sanctioned_aliases
    for gen in node.generators:
        if not _check_reads_in_node(
            gen.iter, cur_t, cur_a, models_shadowed, consts, new_shadowed
        ):
            return False
        cur_t, cur_a = _apply_walrus_in_expr(gen.iter, cur_t, cur_a, models_shadowed)
        for if_clause in gen.ifs:
            if not _check_reads_in_node(
                if_clause, cur_t, cur_a, models_shadowed, consts, new_shadowed
            ):
                return False
            cur_t, cur_a = _apply_walrus_in_expr(
                if_clause, cur_t, cur_a, models_shadowed
            )
    elt_nodes = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
    for elt in elt_nodes:
        if not _check_reads_in_node(
            elt, cur_t, cur_a, models_shadowed, consts, new_shadowed
        ):
            return False
    return True


def _is_untrusted_totp_subscript_or_call(
    node: ast.AST,
    trusted: set[str],
    sanctioned_aliases: set[str],
    models_shadowed: bool,
    consts: dict[str, set[str]],
    shadowed: frozenset[str],
) -> bool:
    """True iff `node` is a totp_secret read shape (Subscript or
    `.get` / `.pop` / `.setdefault` call) AND the receiver is NOT
    trusted at this read site (accounting for `shadowed` from any
    enclosing comprehension / lambda scope)."""
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and _matches_totp_secret_key(node.slice, consts)
        and not _read_receiver_trusted(
            node.value, trusted, sanctioned_aliases, models_shadowed, shadowed
        )
    ):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"pop", "get", "setdefault"}
        and bool(node.args)
        and _matches_totp_secret_key(node.args[0], consts)
        and not _read_receiver_trusted(
            node.func.value, trusted, sanctioned_aliases, models_shadowed, shadowed
        )
    )


def _read_receiver_trusted(
    recv: ast.AST,
    trusted: set[str],
    sanctioned_aliases: set[str],
    models_shadowed: bool,
    shadowed: frozenset[str],
) -> bool:
    """Like `_is_trusted_totp_receiver`, but excludes Names that are
    `shadowed` by an enclosing comprehension / lambda scope. A Name
    in `shadowed` is bound to the comprehension target / lambda
    parameter at the read site, not to whatever the outer scope's
    trust set thinks."""
    if isinstance(recv, ast.Name) and recv.id in shadowed:
        return False
    return _is_trusted_totp_receiver(recv, trusted, sanctioned_aliases, models_shadowed)


def _names_bound_by_target(target: ast.expr) -> list[str]:
    """Yield every Name id bound by `target`. Walks Tuple / List
    unpacking patterns and Starred elements (`*rest`) recursively;
    returns Identifier-bound names only. Subscript and Attribute
    targets (`obj[key] = ...`, `obj.attr = ...`) bind no local
    names and are skipped."""
    out: list[str] = []
    if isinstance(target, ast.Name):
        out.append(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            out.extend(_names_bound_by_target(element))
    elif isinstance(target, ast.Starred):
        out.extend(_names_bound_by_target(target.value))
    return out


def _apply_walrus_in_expr(
    expr: ast.AST | None,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
) -> tuple[set[str], set[str]]:
    """Apply every walrus binding (`name := value`) reachable inside
    `expr` to (trusted, aliases), in source order. Returns fresh sets.

    Walrus assignments in `if` / `while` headers, `match` subjects /
    case guards, For-loop iters, and as expression statements
    (`(name := value)`) bind in the enclosing scope and run BEFORE
    the branching body (PEP 572). Without this hook, a reassignment
    like `if (user := models.get_user_by_id(uid)):` would leave a
    trusted `user` from earlier in the function trusted through both
    branches. The helper accepts any AST node (statements too) so
    fallback `_check_and_apply_stmt` paths can apply walrus from
    Expr / Return / Raise statements that contain `:=` expressions.

    The walk MUST stop at nested scope boundaries. Per PEP 572, walrus
    inside a comprehension binds in the containing scope (so we *do*
    descend into list / set / dict / generator comprehensions), but
    walrus inside a `def` / `async def` / `class` body or a `Lambda`
    binds in that nested scope -- not the enclosing function. A naive
    `ast.walk` would let

        def outer():
            user = models.get_user_by_id(uid)   # outer trust = unsanctioned
            def helper():
                if (user := models.get_user_with_totp_by_id(...)):
                    ...
            return user["totp_secret"]          # outer `user` still
                                                # unsanctioned, but the
                                                # nested walrus would have
                                                # blessed it

    silently bless the outer read. `_walk_walrus_scope_local` skips
    nested FunctionDef / AsyncFunctionDef / ClassDef / Lambda bodies
    so only walruses that actually bind in the enclosing scope are
    applied."""
    if expr is None:
        return set(trusted), set(aliases)
    nt, na = set(trusted), set(aliases)
    for node in _walk_walrus_scope_local(expr):
        if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            sanctioned = _is_sanctioned_or_none_source(node.value, na, models_shadowed)
            if sanctioned:
                nt.add(node.target.id)
            else:
                nt.discard(node.target.id)
                na.discard(node.target.id)
    return nt, na


def _walk_walrus_scope_local(node: ast.AST):
    """`ast.walk` variant for collecting walrus bindings that bind in
    the enclosing scope. Yields the input node + descendants but does
    NOT descend into nested `FunctionDef` / `AsyncFunctionDef` /
    `ClassDef` / `Lambda` bodies -- those are separate scopes per PEP
    572, so a walrus inside one of them does not mutate the caller's
    locals. Comprehensions ARE descended into (their walrus targets
    explicitly bind the containing scope, by PEP 572)."""
    from collections import deque

    queue = deque([(node, True)])
    while queue:
        current, is_root = queue.popleft()
        yield current
        if not is_root and isinstance(
            current,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            continue
        for child in ast.iter_child_nodes(current):
            queue.append((child, False))


# ---------------------------------------------------------------------------
# Flow-aware structural walker
#
# Each `_check_and_apply_*` returns `(ok, trusted, aliases)`. Reads
# are checked against the threaded state -- so an inner-branch read
# that depends on the same-branch reassignment sees the post-rebind
# trust set, not the pre-stmt outer state. The previous design's
# read-check-then-apply pair walked the whole stmt against entry-
# state and missed these cases.
# ---------------------------------------------------------------------------


def _stmts_terminate(stmts: list[ast.stmt]) -> bool:
    """True iff `stmts` (as a sequential block) reaches an always-
    terminating statement -- i.e., control unconditionally transfers
    out of the block before falling through. Returns False for an
    empty list (no terminator)."""
    return any(_always_terminates(s) for s in stmts)


def _always_terminates(stmt: ast.stmt) -> bool:
    """True iff `stmt` ALWAYS transfers control out of the enclosing
    block -- every reachable path through it returns / raises / breaks /
    continues. Conservative: returns False whenever static analysis
    can't prove termination on every path.

    Recognised compound shapes:

      `If`         -- both `body` and `orelse` always terminate, AND
                      orelse is non-empty (a missing-else branch
                      falls through, so the if doesn't always
                      terminate).
      `Try`        -- finalbody always terminates (overrides), OR
                      every handler always terminates AND
                      (body always terminates OR orelse always
                      terminates).
      `With` /     -- body always terminates. The `__exit__` method
      `AsyncWith`     could in theory swallow an exception (return
                      truthy), but our codebase never relies on that
                      shape; treating with-statements as transparent
                      to control flow matches the rest of the test
                      posture.

    For / While / Match are NOT recognised: a loop with an empty iter
    skips the body entirely (no termination), and a match with no
    matching case falls through. Both are reachable post-statement
    paths the static analysis can't statically rule out."""
    if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
        return True
    if isinstance(stmt, ast.If):
        if not stmt.orelse:
            return False
        return _stmts_terminate(stmt.body) and _stmts_terminate(stmt.orelse)
    if isinstance(stmt, (ast.Try, ast.TryStar)):
        if stmt.finalbody and _stmts_terminate(stmt.finalbody):
            return True
        if not all(_stmts_terminate(h.body) for h in stmt.handlers):
            return False
        # Body terminating means orelse never runs (orelse runs after a
        # successful body completion, which doesn't happen if body
        # terminates). Otherwise the body's normal-completion path goes
        # through orelse, which must terminate.
        return _stmts_terminate(stmt.body) or (
            bool(stmt.orelse) and _stmts_terminate(stmt.orelse)
        )
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return _stmts_terminate(stmt.body)
    return False


def _walk_scope_seq(
    stmts: list[ast.stmt],
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
    string_consts: dict[str, set[str]] | None = None,
) -> tuple[bool, set[str], set[str]]:
    """Thread `(trusted, aliases)` through `stmts` in source order via
    `_check_and_apply_stmt`, returning False on the first read failure
    along with the post-failure state (which the caller discards
    anyway). Used inside structural branch handlers to process one
    branch's body sequentially before joining with siblings.

    Stops threading after an always-terminating statement (literal
    `Return` / `Raise` / `Break` / `Continue`, or a compound stmt
    where every reachable path through it transfers control out --
    e.g. `if cond: raise else: raise`, or a try with always-raising
    body + every handler raising). The statements after such a
    terminator are unreachable in this scope, so applying their state
    changes would re-establish trust that isn't visible at any
    reachable point. Concrete bypass that motivated this:

        try:
            user = unsanctioned()
            if cond:
                raise RuntimeError()
            else:
                raise RuntimeError()
            user = sanctioned()         # UNREACHABLE
        except RuntimeError:
            user["totp_secret"]         # handler entry: pre & try_t

    Without the recursive terminator check, the unreachable re-add
    brought `user` back into `try_t`, leaking through the `pre &
    try_t` handler-entry intersection. `_always_terminates` recurses
    through If / Try / With shapes so it catches both the literal
    `raise` and the every-branch-raises forms."""
    t, a = set(trusted), set(aliases)
    for stmt in stmts:
        ok, t, a = _check_and_apply_stmt(stmt, t, a, models_shadowed, string_consts)
        if not ok:
            return False, t, a
        if _always_terminates(stmt):
            break
    return True, t, a


def _check_and_apply_assign(
    stmt: ast.Assign,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
    string_consts: dict[str, set[str]] | None = None,
) -> tuple[bool, set[str], set[str]]:
    """Read-check the RHS against pre-stmt state, then apply binding.
    Sanctioned RHS adds each top-level Name target to `trusted`;
    non-sanctioned RHS discards every name bound by the assignment.
    Tuple / list unpacking revokes every Name in the pattern --
    static analysis can't pair pattern slots to RHS-tuple elements."""
    if not _check_reads_in_node(
        stmt.value, trusted, aliases, models_shadowed, string_consts
    ):
        return False, set(trusted), set(aliases)
    nt, na = set(trusted), set(aliases)
    sanctioned = _is_sanctioned_or_none_source(stmt.value, na, models_shadowed)
    for target in stmt.targets:
        if isinstance(target, ast.Name) and sanctioned:
            nt.add(target.id)
            continue
        for name in _names_bound_by_target(target):
            nt.discard(name)
            na.discard(name)
    return True, nt, na


def _check_and_apply_annassign(
    stmt: ast.AnnAssign,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
    string_consts: dict[str, set[str]] | None = None,
) -> tuple[bool, set[str], set[str]]:
    """`user: dict = expr` -- behaves like Assign for trust purposes when
    `value` is set. A bare `user: dict` with no `value` doesn't bind at
    runtime, so the sets are unchanged. The annotation expression
    itself isn't read-checked: any totp_secret subscript inside a
    type annotation would be a static type expression, not a runtime
    read."""
    if stmt.value is None or not isinstance(stmt.target, ast.Name):
        return True, set(trusted), set(aliases)
    if not _check_reads_in_node(
        stmt.value, trusted, aliases, models_shadowed, string_consts
    ):
        return False, set(trusted), set(aliases)
    nt, na = set(trusted), set(aliases)
    if _is_sanctioned_or_none_source(stmt.value, na, models_shadowed):
        nt.add(stmt.target.id)
    else:
        nt.discard(stmt.target.id)
        na.discard(stmt.target.id)
    return True, nt, na


def _check_and_apply_if(
    stmt: ast.If,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
    string_consts: dict[str, set[str]] | None = None,
) -> tuple[bool, set[str], set[str]]:
    """Read-check the test, apply walrus bindings, then thread the
    resulting state through body and orelse separately and intersect.
    Inner-branch reads that depend on inner-branch assignments now
    see the threaded state because each body statement is processed
    one-at-a-time via `_walk_scope_seq`."""
    if not _check_reads_in_node(
        stmt.test, trusted, aliases, models_shadowed, string_consts
    ):
        return False, set(trusted), set(aliases)
    pre_t, pre_a = _apply_walrus_in_expr(stmt.test, trusted, aliases, models_shadowed)
    if_ok, if_t, if_a = _walk_scope_seq(
        stmt.body, pre_t, pre_a, models_shadowed, string_consts
    )
    if not if_ok:
        return False, if_t, if_a
    else_ok, else_t, else_a = _walk_scope_seq(
        stmt.orelse, pre_t, pre_a, models_shadowed, string_consts
    )
    if not else_ok:
        return False, else_t, else_a
    return True, if_t & else_t, if_a & else_a


def _check_and_apply_try(
    stmt: ast.Try | ast.TryStar,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
    string_consts: dict[str, set[str]] | None = None,
) -> tuple[bool, set[str], set[str]]:
    """Three reachable post-Try paths to merge: try-body completes (and
    orelse runs); or any handler runs (from arbitrary point in try
    body); or `finally` ends the construct.

    Each handler's entry state is the INTERSECTION of the pre-Try
    state with the state observed at EVERY point inside the try
    body, not just the try-body-end state. A handler can fire after
    any statement in the body (any statement can throw), so a name
    that's revoked mid-body must be considered untrusted at handler
    entry even if a later statement in the body would re-bless it:

      pre = {user}
      try:
          user = unsanctioned()    # interim revokes user
          risky()                  # could throw HERE -- handler
                                   # would enter with user revoked
          user = sanctioned()      # late re-bless; only reaches
                                   # handler if we never threw
      except:
          user["totp_secret"]      # entry: pre ∩ all-interim = {}

    Walking the body step-by-step and accumulating the running
    intersection captures this correctly. The previous shape used
    the post-body `try_t` only, which over-trusted any name that
    happened to be re-blessed before the try body completed.

    The walk stops at the first statement that unconditionally
    transfers control (`raise` / `return` / `break` / `continue`,
    or any compound that always terminates). Everything after such
    a statement is dead code: an unreachable reassignment must not
    revoke trust in the interim accumulator, otherwise an honest
    pattern like

        user = sanctioned()
        try:
            raise SpecificError()
            user = unsanctioned()   # dead, never executes
        except SpecificError:
            return user["totp_secret"]

    would over-revoke at handler entry and false-positive on a
    legitimate read.

    `finally` always runs after the merged state; threaded through
    that."""
    running_t, running_a = set(trusted), set(aliases)
    interim_t, interim_a = set(trusted), set(aliases)
    for s in stmt.body:
        body_ok, running_t, running_a = _check_and_apply_stmt(
            s, running_t, running_a, models_shadowed, string_consts
        )
        if not body_ok:
            return False, running_t, running_a
        interim_t &= running_t
        interim_a &= running_a
        if _always_terminates(s):
            break
    try_t, try_a = running_t, running_a
    else_ok, else_t, else_a = _walk_scope_seq(
        stmt.orelse, try_t, try_a, models_shadowed, string_consts
    )
    if not else_ok:
        return False, else_t, else_a
    merged_t, merged_a = else_t, else_a
    for handler in stmt.handlers:
        h_t = set(interim_t)
        h_a = set(interim_a)
        if handler.name is not None:
            h_t.discard(handler.name)
            h_a.discard(handler.name)
        if handler.type is not None and not _check_reads_in_node(
            handler.type, h_t, h_a, models_shadowed, string_consts
        ):
            return False, h_t, h_a
        h_ok, h_t, h_a = _walk_scope_seq(
            handler.body, h_t, h_a, models_shadowed, string_consts
        )
        if not h_ok:
            return False, h_t, h_a
        merged_t = merged_t & h_t
        merged_a = merged_a & h_a
    fin_ok, fin_t, fin_a = _walk_scope_seq(
        stmt.finalbody, merged_t, merged_a, models_shadowed, string_consts
    )
    if not fin_ok:
        return False, fin_t, fin_a
    return True, fin_t, fin_a


def _check_and_apply_loop(
    stmt: ast.While | ast.For | ast.AsyncFor,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
    string_consts: dict[str, set[str]] | None = None,
) -> tuple[bool, set[str], set[str]]:
    """Loop body might not execute at all -- the post-loop state is the
    intersection of pre-state (body skipped) and the body-end state
    (body ran ≥ once and completed). Walrus bindings in the header
    apply to the pre-state."""
    header = stmt.test if isinstance(stmt, ast.While) else stmt.iter
    if not _check_reads_in_node(
        header, trusted, aliases, models_shadowed, string_consts
    ):
        return False, set(trusted), set(aliases)
    pre_t, pre_a = _apply_walrus_in_expr(header, trusted, aliases, models_shadowed)
    body_t, body_a = set(pre_t), set(pre_a)
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        for bound in _names_bound_by_target(stmt.target):
            body_t.discard(bound)
            body_a.discard(bound)
    body_ok, body_t, body_a = _walk_scope_seq(
        stmt.body, body_t, body_a, models_shadowed, string_consts
    )
    if not body_ok:
        return False, body_t, body_a
    else_ok, else_t, else_a = _walk_scope_seq(
        stmt.orelse, body_t, body_a, models_shadowed, string_consts
    )
    if not else_ok:
        return False, else_t, else_a
    return True, set(pre_t) & else_t, set(pre_a) & else_a


def _check_and_apply_with(
    stmt: ast.With | ast.AsyncWith,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
    string_consts: dict[str, set[str]] | None = None,
) -> tuple[bool, set[str], set[str]]:
    """`with ctx as name:` binds `name` to the context manager's
    `__enter__` return; tuple/list-unpacking forms (`with ctx as
    (a, b):`) bind every captured Name. Read-check each context_expr
    first, then revoke the captured names, then thread the body."""
    for item in stmt.items:
        if not _check_reads_in_node(
            item.context_expr, trusted, aliases, models_shadowed, string_consts
        ):
            return False, set(trusted), set(aliases)
    nt, na = set(trusted), set(aliases)
    for item in stmt.items:
        if item.optional_vars is None:
            continue
        for bound in _names_bound_by_target(item.optional_vars):
            nt.discard(bound)
            na.discard(bound)
    return _walk_scope_seq(stmt.body, nt, na, models_shadowed, string_consts)


def _check_and_apply_match(
    stmt: ast.Match,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
    string_consts: dict[str, set[str]] | None = None,
) -> tuple[bool, set[str], set[str]]:
    """`match X: case <pat>: <body>` (PEP 634, Python 3.10+). The
    subject is read-checked once before any case runs; walrus bindings
    inside it apply to every case's pre-state. Each case starts from
    that subject-evaluated state minus the names bound by its pattern.
    Pattern guards (`case X if cond:`) are read-checked against the
    pattern-bound state. Post-Match state is the intersection of every
    case's end state PLUS the subject-evaluated state for the
    fall-through path (no case matched at runtime)."""
    if not _check_reads_in_node(
        stmt.subject, trusted, aliases, models_shadowed, string_consts
    ):
        return False, set(trusted), set(aliases)
    pre_t, pre_a = _apply_walrus_in_expr(
        stmt.subject, trusted, aliases, models_shadowed
    )
    case_states: list[tuple[set[str], set[str]]] = [(set(pre_t), set(pre_a))]
    for case in stmt.cases:
        case_t, case_a = set(pre_t), set(pre_a)
        for bound in _pattern_bound_names(case.pattern):
            case_t.discard(bound)
            case_a.discard(bound)
        if case.guard is not None:
            if not _check_reads_in_node(
                case.guard, case_t, case_a, models_shadowed, string_consts
            ):
                return False, case_t, case_a
            # Walrus bindings in `case ... if (x := ...):` bind in the
            # enclosing scope per PEP 572, and they run BEFORE the
            # case body. Apply them to the per-case state so a
            # `(user := unsanctioned())` guard revokes trust before
            # the body's reads.
            case_t, case_a = _apply_walrus_in_expr(
                case.guard, case_t, case_a, models_shadowed
            )
        case_ok, case_t, case_a = _walk_scope_seq(
            case.body, case_t, case_a, models_shadowed, string_consts
        )
        if not case_ok:
            return False, case_t, case_a
        case_states.append((case_t, case_a))
    final_t, final_a = case_states[0]
    for ct, ca in case_states[1:]:
        final_t = final_t & ct
        final_a = final_a & ca
    return True, final_t, final_a


def _check_and_apply_import(
    stmt: ast.Import | ast.ImportFrom,
    trusted: set[str],
    aliases: set[str],
) -> tuple[bool, set[str], set[str]]:
    """A function-local `import` / `from ... import` rebinds the
    imported names in this scope, shadowing any module-level binding
    of the same name. Revoke each bound name from both `trusted` and
    `aliases`. Conservative direction: even a local re-import from
    the canonical data layer is treated as a fresh local binding and
    revoked. Imports themselves contain no totp_secret reads, so
    there's no read check.

    `from X import *` is the case to be careful about. A naive
    discard-by-`alias.name` would revoke only the literal `"*"`
    binding (which doesn't exist as a tracked name), leaving every
    sanctioned getter alias intact -- a wildcard import that
    *actually* shadows a tracked alias would slip past:

        from attacker import *  # could rebind get_user_with_totp_by_id
        user = get_user_with_totp_by_id(...)
        user["totp_secret"]  # silently treated as trusted

    Since we can't statically know which names `X` exposes, the
    conservative response is to drop every tracked binding -- the
    wildcard could rebind any of them, so treat them all as
    untrusted from the wildcard onwards."""
    nt, na = set(trusted), set(aliases)
    for alias in stmt.names:
        if isinstance(stmt, ast.ImportFrom) and alias.name == "*":
            return True, set(), set()
        if isinstance(stmt, ast.ImportFrom):
            bound = alias.asname or alias.name
        else:
            bound = alias.asname or alias.name.partition(".")[0]
        nt.discard(bound)
        na.discard(bound)
    return True, nt, na


def _check_and_apply_stmt(
    stmt: ast.AST,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool = False,
    string_consts: dict[str, set[str]] | None = None,
) -> tuple[bool, set[str], set[str]]:
    """Return `(ok, new_trusted, new_aliases)` for one statement.
    `ok` is False iff a `totp_secret` read inside the (recursively-
    threaded) statement was on an untrusted receiver. The walker
    threads state INTO compound bodies so inner-branch reads see
    inner-branch reassignments -- closing the bypass where `if cond:
    user = unsanctioned(); user["totp_secret"]` was checked against
    the pre-If trusted set instead of the in-branch state.

    Returns fresh sets; the input sets are never mutated. Statements
    not handled below (Return, Raise, Pass, Break, Continue, Expr,
    Global, Nonlocal, Delete, FunctionDef, ClassDef, AugAssign) have
    no compound bodies, so the entire stmt is read-checked against
    pre-state and trust is unchanged. Nested function / class bodies
    are skipped (separate scopes; checked independently)."""
    if isinstance(stmt, ast.Assign):
        return _check_and_apply_assign(
            stmt, trusted, aliases, models_shadowed, string_consts
        )
    if isinstance(stmt, ast.AnnAssign):
        return _check_and_apply_annassign(
            stmt, trusted, aliases, models_shadowed, string_consts
        )
    if isinstance(stmt, ast.If):
        return _check_and_apply_if(
            stmt, trusted, aliases, models_shadowed, string_consts
        )
    if isinstance(stmt, (ast.Try, ast.TryStar)):
        return _check_and_apply_try(
            stmt, trusted, aliases, models_shadowed, string_consts
        )
    if isinstance(stmt, ast.Match):
        return _check_and_apply_match(
            stmt, trusted, aliases, models_shadowed, string_consts
        )
    if isinstance(stmt, (ast.While, ast.For, ast.AsyncFor)):
        return _check_and_apply_loop(
            stmt, trusted, aliases, models_shadowed, string_consts
        )
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return _check_and_apply_with(
            stmt, trusted, aliases, models_shadowed, string_consts
        )
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return _check_and_apply_import(stmt, trusted, aliases)
    if not _check_reads_in_node(stmt, trusted, aliases, models_shadowed, string_consts):
        return False, set(trusted), set(aliases)
    # Fallback shapes (Expr, Return, Raise, Pass, Break, Continue,
    # Global, Nonlocal, Delete, AugAssign, FunctionDef, ClassDef)
    # don't have their own state-update rule, but they CAN contain
    # walrus expressions that bind in the enclosing scope -- e.g.,
    # an Expr stmt `(user := models.get_user_by_id(uid))` runs the
    # walrus and revokes prior trust. Apply walrus from the entire
    # stmt subtree before returning.
    new_t, new_a = _apply_walrus_in_expr(stmt, trusted, aliases, models_shadowed)
    return True, new_t, new_a


def _import_binds_name_from_non_canonical_source(
    stmt: ast.Import | ast.ImportFrom,
    name: str,
    file_path: pathlib.Path | None,
) -> bool:
    """True iff `stmt` brings `name` into scope from a source module
    that is NOT one of the canonical sources for that name (per
    `_CANONICAL_IMPORT_SOURCES`). Names not tracked in that dict
    return False unconditionally -- importing `urllib` shouldn't make
    `urllib` a shadowed name. Returns False if `name` is in the dict
    but the import comes from a canonical source -- that's the legit
    binding.

    `from X import *` is treated conservatively: since we can't
    statically know which names X exposes, the wildcard could pull
    `name` in. Return True iff X is NOT a canonical source for
    `name`. Wildcard from a canonical source is fine (the legit
    binding could still be reached this way), wildcard from
    elsewhere shadows."""
    allowed = _CANONICAL_IMPORT_SOURCES.get(name)
    if allowed is None:
        return False
    if isinstance(stmt, ast.ImportFrom):
        resolved = (
            _resolve_import_module(stmt, file_path) if file_path is not None else None
        )
        for alias in stmt.names:
            if alias.name == "*":
                if resolved not in allowed:
                    return True
                continue
            bound = alias.asname or alias.name
            if bound == name and resolved not in allowed:
                return True
        return False
    # ast.Import: `import X [as Y]` -- the source module is `alias.name`.
    # The name brought into scope is `alias.asname` if present, else the
    # top-level component of `alias.name` (e.g. `import a.b.c` binds `a`).
    for alias in stmt.names:
        bound = alias.asname or alias.name.partition(".")[0]
        if bound == name and alias.name not in allowed:
            return True
    return False


def _pattern_bound_names(pattern: ast.pattern | None) -> list[str]:
    """Every name bound by a `match` pattern (PEP 634, Python 3.10+).

    Pattern shapes:

      MatchAs(pattern=p, name=n) -- `<p> as n`, `_` (n=None, p=None),
                                    `n` (capture pattern when p=None
                                    and n is set)
      MatchStar(name=n)          -- `*n` in MatchSequence; n=None for
                                    `*_`, captures the rest
      MatchSequence              -- recurse on inner patterns
      MatchMapping               -- recurse on inner patterns; `rest`
                                    is a str if `**rest` is in pattern
      MatchClass                 -- recurse on positional + keyword
                                    nested patterns
      MatchOr                    -- recurse on each branch (Python
                                    requires every branch to bind the
                                    same names)

    MatchValue / MatchSingleton bind nothing. The body delegates to
    per-shape helpers to keep each function under the complexity cap."""
    if pattern is None:
        return []
    if isinstance(pattern, ast.MatchAs):
        out: list[str] = []
        if pattern.name is not None:
            out.append(pattern.name)
        out.extend(_pattern_bound_names(pattern.pattern))
        return out
    if isinstance(pattern, ast.MatchStar):
        return [pattern.name] if pattern.name is not None else []
    if isinstance(pattern, ast.MatchSequence):
        return _names_in_pattern_list(pattern.patterns)
    if isinstance(pattern, ast.MatchMapping):
        names = _names_in_pattern_list(pattern.patterns)
        if pattern.rest is not None:
            names.append(pattern.rest)
        return names
    if isinstance(pattern, ast.MatchClass):
        return _names_in_pattern_list(pattern.patterns) + _names_in_pattern_list(
            pattern.kwd_patterns
        )
    if isinstance(pattern, ast.MatchOr):
        return _names_in_pattern_list(pattern.patterns)
    return []


def _names_in_pattern_list(patterns: list[ast.pattern]) -> list[str]:
    """Concatenate `_pattern_bound_names` across an iterable of patterns."""
    out: list[str] = []
    for p in patterns:
        out.extend(_pattern_bound_names(p))
    return out


def _node_binds_name(node: ast.AST, name: str, file_path: pathlib.Path | None) -> bool:
    """True iff `node` is a single AST node (statement or expression
    inside one) that introduces a new local binding for `name`.
    Covers every name-binding shape Python supports inside a function
    body that we care about for shadowing checks:

      Assign          `<name> = expr`           (also tuple/list unpack)
      AnnAssign       `<name>: T = expr`
      For / AsyncFor  `for <name> in iter:`     (loop variable; tuple
                                                  unpacking too)
      With / AsyncWith `with cm as <name>:`     (context-manager binding,
                                                  including unpacking)
      ExceptHandler   `except E as <name>:`     (handler exception
                                                  binding)
      FunctionDef /   `def <name>(...):`        (nested function rebinds
      AsyncFunctionDef `async def <name>(...)`   the name in this scope;
                                                 the body is a separate
                                                 scope, but the *name*
                                                 binding lands here)
      ClassDef        `class <name>: ...`       (same pattern as def)
      Match           `match X: case <pat>:`    (every case's pattern can
                                                  bind names via MatchAs,
                                                  MatchStar, MatchMapping
                                                  rest, etc.; recurse via
                                                  `_pattern_bound_names`)
      NamedExpr       `(<name> := expr)`        (walrus; PEP 572 binds
                                                  the target in the
                                                  enclosing scope, so a
                                                  walrus in an `if`
                                                  header / `while`
                                                  guard / expression
                                                  statement counts as a
                                                  fresh local binding)
      Import / ImportFrom from a NON-canonical source (delegates to
                      `_import_binds_name_from_non_canonical_source`).
    """
    if isinstance(node, ast.Assign):
        return any(name in _names_bound_by_target(t) for t in node.targets)
    if isinstance(node, ast.AnnAssign):
        return name in _names_bound_by_target(node.target)
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return name in _names_bound_by_target(node.target)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None and name in _names_bound_by_target(
                item.optional_vars
            ):
                return True
        return False
    if isinstance(node, ast.ExceptHandler):
        # `except E as <name>:` -- `node.name` is a plain str, not a
        # Name node.
        return node.name == name
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    if isinstance(node, ast.Match):
        return any(name in _pattern_bound_names(case.pattern) for case in node.cases)
    if isinstance(node, ast.NamedExpr):
        return isinstance(node.target, ast.Name) and node.target.id == name
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return _import_binds_name_from_non_canonical_source(node, name, file_path)
    return False


def _name_is_bound_in_scope(
    name: str,
    scope: ast.AST,
    file_path: pathlib.Path | None = None,
) -> bool:
    """True iff `name` is bound somewhere in `scope` (function
    parameters, local Assign / AnnAssign, For-loop / With-as /
    Except-as bindings, or a non-canonical function-local `import` /
    `from ... import`) and would therefore SHADOW the module-level
    binding for the duration of this function.

    Function-local `import attacker as models` rebinds `models` to a
    non-data-layer package; subsequent `models.<getter>(...)` calls
    inside this function must NOT count as sanctioned. Loop / with /
    except bindings are equally rebinding -- `for models in ...`
    captures each iter element, `with cm() as models` captures the
    context manager's `__enter__` return, `except E as models` binds
    the caught exception. None of these resolve to the data-layer
    package. Imports from a canonical source for `name` (per
    `_CANONICAL_IMPORT_SOURCES`) don't shadow."""
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    args = scope.args
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        if arg.arg == name:
            return True
    if args.vararg is not None and args.vararg.arg == name:
        return True
    if args.kwarg is not None and args.kwarg.arg == name:
        return True
    return any(_node_binds_name(node, name, file_path) for node in _walk_local(scope))


def _module_level_rebound_names(
    tree: ast.AST,
    file_path: pathlib.Path | None = None,
) -> set[str]:
    """Names that have been rebound at module scope so they no longer
    resolve to their canonical implementation. Four shadowing channels:

      Assign:           `<name> = <something>`             -- module-level
      AnnAssign:        `<name>: T = <something>`          -- module-level
      FunctionDef /
      AsyncFunctionDef: `def <name>(...): ...`             -- module-level
      ClassDef:         `class <name>: ...`                -- module-level
      Import:           `import <name>` / `from <m> import <name>`
                        from a source module that is NOT one of the
                        canonical sources for that name (per
                        `_CANONICAL_IMPORT_SOURCES`).

    Imports of names NOT tracked in `_CANONICAL_IMPORT_SOURCES`
    aren't added -- a stray `from urllib import parse` at module
    scope doesn't shadow anything we care about.

    Assign targets are walked through `_names_bound_by_target` so
    tuple / list destructuring rebinds count too -- a module-level

        (verify_same_origin, _) = (attacker_fn, None)
        (Depends, _) = (other_attacker, None)

    rebinds both names just as much as the direct-Name form, so
    later `Depends(verify_same_origin)` must not pass the
    origin-gate check.

    Module-scope walk descends into top-level compound containers
    (`if` / `try` / `with` / `for` / `while` / `match`) so a rebind
    nested inside a conditional or guarded import still counts:

        if flag:
            verify_same_origin = fake_origin_gate
        try:
            from attacker import models
        except ImportError:
            ...

    These are still module-scope bindings -- the `if` / `try` / etc.
    only conditionalises whether the rebind happens, not where the
    name lands -- so they shadow just like a plain top-level
    `verify_same_origin = ...` would. The walk stops at nested
    `FunctionDef` / `AsyncFunctionDef` / `ClassDef` / `Lambda`
    bodies, since names bound inside those are local to the nested
    scope and don't shadow at module level.

    Without the import channel, a malicious-or-mistaken
    `from attacker import models` / `from attacker import
    verify_same_origin` would still satisfy the TOTP-quarantine and
    origin-gate checks even though the symbol resolves to a
    non-canonical implementation. Without the def/class channel, a
    file that defines `def verify_same_origin(...)` locally and
    then writes `Depends(verify_same_origin)` would pass the
    origin-gate check even though no FastAPI dependency is actually
    being installed."""
    names: set[str] = set()
    if not isinstance(tree, ast.Module):
        return names
    for node in _walk_module_scope(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_names_bound_by_target(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_names_bound_by_target(node.target))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for tracked_name in _CANONICAL_IMPORT_SOURCES:
                if _import_binds_name_from_non_canonical_source(
                    node, tracked_name, file_path
                ):
                    names.add(tracked_name)
    return names


def _walk_module_scope(node: ast.AST):
    """`ast.walk` variant for module-scope rebound detection. Yields
    the input node + descendants but does NOT descend into nested
    `FunctionDef` / `AsyncFunctionDef` / `ClassDef` / `Lambda`
    bodies -- those are separate scopes; their internal Assigns /
    AnnAssigns / Imports don't shadow at module level. The walker
    DOES descend into module-level compound containers (`if`,
    `try`, `with`, `for`, `while`, `match`), so a rebind nested
    inside one of those is still picked up as module-scope."""
    from collections import deque

    queue = deque([(node, True)])
    while queue:
        current, is_root = queue.popleft()
        yield current
        if not is_root and isinstance(
            current,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            continue
        for child in ast.iter_child_nodes(current):
            queue.append((child, False))


def _scope_totp_reads_are_quarantined(
    scope: ast.AST,
    sanctioned_aliases: set[str],
    module_rebound: set[str] | None = None,
    file_path: pathlib.Path | None = None,
) -> bool:
    """True iff every `totp_secret` read in `scope` is on a receiver
    that's trusted at the point of the read. Walks the function body
    statement-by-statement in source order, maintaining a `trusted`
    set that's MUTATED as Assigns happen:

      stmt 1:  user = sanctioned()    -- adds `user` to trusted
      stmt 2:  user = other()         -- removes `user` from trusted
      stmt 3:  return user[\"totp_secret\"]  -- user not trusted -> FAIL

    The flow-sensitive shape closes the basic reassignment-bypass
    that a static set would miss: trust must be invalidated on
    reassignment, not just established once.

    Limitation: branch-specific reassignment (`if cond: user =
    other()`) revokes trust for the rest of the function body
    unconditionally -- the conservative direction. A future full
    reaching-definitions pass could distinguish "trust survives if
    branch didn't run" from "trust revoked everywhere," but the
    branched bypass is rare and the conservative posture matches
    the rest of the test."""
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return True
    trusted: set[str] = set()
    # Per-function copy of the sanctioned-alias set. Function-local
    # rebinding of an imported getter name shadows the import in this
    # scope only; the module-level set the caller maintains stays
    # untouched. Without this, `get_user_with_totp_by_id = other_thing;
    # user = get_user_with_totp_by_id(uid); user["totp_secret"]` would
    # bypass -- the Name still appears in the (immutable) module-level
    # alias set even though the local name resolves elsewhere.
    # Module-level rebinds revoke the sanctioned-alias trust before
    # this function even runs. A `from app.models import
    # get_user_with_totp_by_id` followed by a top-level
    # `get_user_with_totp_by_id = attacker_fn` shadows the canonical
    # name for every function in the file. `_module_level_rebound_names`
    # already records that, so subtract it from the per-function
    # alias set up front.
    local_sanctioned = set(sanctioned_aliases) - (module_rebound or set())
    # Function parameters shadow module-level imports of the same name
    # for the body of this function. `def f(get_user_with_totp_by_id):
    # ...` makes the parameter resolve to whatever the caller passed,
    # not to the data-layer getter, so calls like
    # `get_user_with_totp_by_id(uid)` inside `f` are NOT sanctioned.
    # Revoke every parameter name from the local copy upfront. Covers
    # positional-only, regular, kw-only, *args, and **kwargs.
    args = scope.args
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        local_sanctioned.discard(arg.arg)
    if args.vararg is not None:
        local_sanctioned.discard(args.vararg.arg)
    if args.kwarg is not None:
        local_sanctioned.discard(args.kwarg.arg)
    # Compute models-shadowing once per scope. The `models.<getter>`
    # Attribute path is trusted only when `models` resolves to the
    # imported package -- i.e., NOT shadowed by a parameter, local
    # rebind, or module-level reassignment.
    models_shadowed = (
        "models" in (module_rebound or set())
    ) or _name_is_bound_in_scope("models", scope, file_path)
    # String-const aliases for indirect totp_secret key access: a
    # `key = "totp_secret"; row[key]` in this function body resolves
    # `key` through this map and is treated as a literal-key read.
    # Computed once per scope (constant for the whole walker).
    string_consts = _string_const_aliases(scope)
    ok, _t, _a = _walk_scope_seq(
        scope.body, trusted, local_sanctioned, models_shadowed, string_consts
    )
    return ok


def test_totp_secret_reads_only_in_functions_that_use_with_totp_getters():
    """app/models/users.py module docstring pins the convention: the
    plaintext TOTP seed is exposed only via `get_user_with_totp_*`
    getters; every other read path returns a dict that omits the column.

    Static check: every `["totp_secret"]` read (or `.pop` / `.get` on
    the same key) inside a function under app/ must operate on a
    receiver that was bound from a sanctioned getter call -- either
    a Name set to `models.<getter>(...)` / `<imported_getter>(...)`
    earlier in the same scope, or the getter call itself used inline
    as the subscripted expression.

    The check is at function scope and dataflow-bound: a sibling
    function elsewhere in the module doesn't satisfy a separate
    function's read, AND a sanctioned-getter call elsewhere in the
    same function doesn't cover a read on a different (un-trusted)
    receiver. Catches the bypass shape where a function calls a
    sanctioned getter for one variable and then reads `totp_secret`
    from a different one (e.g., from a non-decrypting `get_user_by_id`
    result).

    Files in the data layer itself (app/models/users.py,
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
        sanctioned = _sanctioned_totp_getter_aliases(tree, py)
        module_rebound = _module_level_rebound_names(tree, py)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            string_consts = _string_const_aliases(fn)
            if not _scope_reads_totp_secret(fn, string_consts):
                continue
            if not _scope_totp_reads_are_quarantined(
                fn, sanctioned, module_rebound, py
            ):
                offenders.append(f"{rel}:{fn.lineno} {fn.name}")
    assert not offenders, (
        "Every `totp_secret` read must be on a receiver sourced from a "
        "sanctioned `get_user_with_totp_*` call in the same function -- "
        "either a name bound from one, or the call inline. A sanctioned "
        "call elsewhere in the function doesn't cover a read on a "
        "different (un-trusted) variable.\n  " + "\n  ".join(offenders)
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
    # SQL keyword (or comma in a multi-table FROM list) + optional
    # schema prefix + (optionally quoted) `analytics_events`. Covers
    # every realistic SQLite/SQL form:
    #   FROM analytics_events
    #   JOIN analytics_events                  (any join family --
    #                                           INNER / LEFT / OUTER /
    #                                           CROSS all reduce to
    #                                           the JOIN keyword)
    #   FROM users, analytics_events           (comma-join: ANSI implicit
    #                                           join, second table after
    #                                           the comma in a FROM
    #                                           multi-table list)
    #   FROM main.analytics_events             (schema-qualified)
    #   FROM "analytics_events"                (standard double quotes)
    #   FROM `analytics_events`                (MySQL-style backticks)
    #   FROM [analytics_events]                (T-SQL-style brackets)
    #   FROM _analytics_events_v4              (renamed-during-migration)
    # The bare-identifier version still has the trailing `\b` so partial
    # matches like `analytics_events_archive` don't trigger; the quoted
    # versions are anchored by their closing delimiter.
    #
    # Limitation: `RENAME TO ...` is a separate keyword we don't list
    # here -- the only RENAME sites are inside the migration allowlist.
    sql_ref = re.compile(
        r"(?:\b(?:FROM|INTO|UPDATE|TABLE|ON|EXISTS|JOIN)\s+|,\s*)"
        r"(?:\w+\.)?"
        r"(?:"
        r'"_?analytics_events"'
        r"|`_?analytics_events`"
        r"|\[_?analytics_events\]"
        r"|_?analytics_events\b"
        r")",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for py in _py_files(APP_DIR):
        rel = str(py.relative_to(REPO_ROOT))
        if rel in allowlist:
            continue
        tree = ast.parse(py.read_text())
        offender_lineno: int | None = None
        for text, lineno in _string_text_outside_docstrings(tree):
            if sql_ref.search(text):
                offender_lineno = lineno
                break
        if offender_lineno is None:
            # Pass 2: dynamic table-name assembly. Catches the
            # f-string and concat bypasses
            #
            #     table = "analytics_events"
            #     sql = f"INSERT INTO {table} ..."
            #     sql = "INSERT INTO " + table + " ..."
            #
            # which the literal-text scan misses because the SQL
            # keyword and the table token live in different fragments.
            # Build a file-scope alias map of `name = "literal"`
            # bindings, then walk every JoinedStr (f-string) and
            # BinOp(Add, ...) (concat chain) and reconstruct each
            # with Name placeholders substituted by their bound
            # values. Run the regex on the reconstructed string.
            # Doesn't over-flag the legit documentation references
            # in app/admin/cli.py or app/routes/prefs.py -- those
            # are bare string literals without an f-string or
            # concat shape.
            file_aliases = _file_level_string_aliases(tree)
            for node in ast.walk(tree):
                if not _is_string_assembly_node(node):
                    continue
                segments = _string_assembly_segments(node, file_aliases)
                candidates = _candidates_from_segments(segments)
                if any(sql_ref.search(c) for c in candidates):
                    offender_lineno = node.lineno
                    break
        if offender_lineno is not None:
            offenders.append(f"{rel}:{offender_lineno}")
    assert not offenders, (
        "SQL operations on `analytics_events` must live inside the data-"
        "layer allowlist (analytics.py, models/_core.py, "
        "models/migrations/v4.py + v5.py). Any other writer would bypass "
        "the two-gate emit + presence-only invariants.\n  " + "\n  ".join(offenders)
    )


def _file_level_string_aliases(tree: ast.AST) -> dict[str, set[str]]:
    """Walk the entire file's AST and return a `Name -> {string,...}`
    map for every `<name> = "literal"` Assign / AnnAssign at any
    scope. Used by the analytics-table-writer scan to substitute
    FormattedValue placeholders inside f-strings with their bound
    values, catching the dynamic-assembly bypass

        table = "analytics_events"
        sql = f"INSERT INTO {table} ..."

    which the literal-text scan misses (the SQL keyword and the
    table token live in different string fragments).

    Multi-valued (set) rather than single-string. A flat last-write-
    wins map would let an *unrelated* later binding in another scope
    overwrite the value used by an earlier offending f-string -- e.g.

        def offender():
            table = "analytics_events"
            sql = f"INSERT INTO {table} (...) VALUES (...)"

        def something_else():
            table = "users"  # later, different scope

    Last-write-wins resolves `{table: "users"}` and the regex misses.
    Tracking every observed binding for a name and trying each of
    them at reconstruction time avoids the silent miss without
    needing per-scope analysis."""
    aliases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    aliases.setdefault(target.id, set()).add(node.value.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and isinstance(node.target, ast.Name)
        ):
            aliases.setdefault(node.target.id, set()).add(node.value.value)
    return aliases


_STRING_ASSEMBLY_CARTESIAN_CAP = 32


def _string_assembly_segments(
    node: ast.AST, aliases: dict[str, set[str]]
) -> list[list[str]]:
    """Flatten `node` into a list of concat segments. Each segment is
    a list of candidate strings; the caller takes the cartesian
    product to enumerate the possible assembled strings. Handles
    four shapes recursively:

      - `Constant` str:  one segment with one literal candidate.
      - `Name`:          one segment with every known string binding
                         (or a single `?` if the name isn't in
                         `aliases`).
      - `JoinedStr`:     each `value` becomes a sub-segment list,
                         expanded recursively (so a `FormattedValue`
                         wrapping a Name is resolved through
                         `aliases`).
      - `BinOp` `Add`:   `segments(left) ++ segments(right)`. Catches
                         non-f-string concat assembly:

                             table = "analytics_events"
                             sql = "INSERT INTO " + table + " VALUES ..."

                         Each `+` flattens left-to-right, so the
                         reconstructed candidate has the keyword and
                         table token adjacent and the regex matches.

    Anything else collapses to a single `?` placeholder, which keeps
    us from accidentally completing a SQL-keyword-plus-table-name
    match across an unresolved interpolation."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [[node.value]]
    if isinstance(node, ast.Name):
        if node.id in aliases:
            return [sorted(aliases[node.id])]
        return [["?"]]
    if isinstance(node, ast.JoinedStr):
        out: list[list[str]] = []
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                out.extend(_string_assembly_segments(value.value, aliases))
            elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                out.append([value.value])
            else:
                out.append(["?"])
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _string_assembly_segments(
            node.left, aliases
        ) + _string_assembly_segments(node.right, aliases)
    return [["?"]]


def _candidates_from_segments(segments: list[list[str]]) -> list[str]:
    """Cartesian product of `segments` capped at
    `_STRING_ASSEMBLY_CARTESIAN_CAP`. Above the cap, every segment
    collapses to its first candidate -- preserves the literal-
    fragment scan but loses the substitution boost. Realistic
    table-name assemblies have one or two interpolations bound to a
    small number of string literals, so the cap is comfortably above
    any honest case."""
    total = 1
    for seg in segments:
        total *= len(seg)
        if total > _STRING_ASSEMBLY_CARTESIAN_CAP:
            return ["".join(seg[0] for seg in segments)]
    candidates = [""]
    for seg in segments:
        candidates = [prev + s for prev in candidates for s in seg]
    return candidates


def _is_string_assembly_node(node: ast.AST) -> bool:
    """True for the AST shapes whose reconstruction the analytics SQL
    scan walks: `JoinedStr` (f-string) or `BinOp` with an `Add`
    operator. Other expression shapes don't compose strings out of
    multiple fragments and so can't carry the keyword + table-name
    bypass on their own."""
    if isinstance(node, ast.JoinedStr):
        return True
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)


# ---------------------------------------------------------------------------
# D. Origin gate on every state-mutating route (CSRF defense)
# ---------------------------------------------------------------------------


_MUTATING_VERBS = {"post", "put", "patch", "delete"}


def _kwargs_contain_mutating_method(keywords: list[ast.keyword]) -> bool:
    """True iff `keywords` (a Call's `.keywords` list) carries
    `methods=...` that we can't statically prove is purely read-only.

    FastAPI accepts `methods` as any `Sequence[str]`. The conservative
    posture is "if I can't see all the verbs, treat it as mutating":

      methods=["GET"]                  -- pure-literal read-only -> False
      methods=["POST"]                 -- literal mutating verb -> True
      methods=("PATCH",)               -- True (literal mutating)
      methods=MUTATING_METHODS         -- non-literal container -> True
      methods=[*MUTATING_METHODS]      -- literal container, non-literal
                                          element (Starred) -> True
      methods=[method_name]            -- Name element -> True
      methods=["GET", *MUTATING]       -- partially literal -> True
                                          (the spread could contain POST)

    A future literal-only `methods=[READ_ONLY_LIST]` constant that
    legitimately hides a read-only set can be allowlisted explicitly;
    silently skipping any unresolvable element leaves a CSRF bypass.

    Used by both the `api_route(...)` decorator detector and the
    imperative `add_api_route(...)` registration scan."""
    for kw in keywords:
        if kw.arg != "methods":
            continue
        if not isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
            return True
        for elt in kw.value.elts:
            if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
                # Starred, Name, computed expression -- can't introspect.
                return True
            if elt.value.lower() in _MUTATING_VERBS:
                return True
    return False


def _module_level_callable_aliases(tree: ast.AST) -> dict[str, list[ast.expr]]:
    """Return a `Name -> [expr,...]` map for module-scope assignments
    where the RHS is one of the shapes we statically follow when
    resolving aliased decorator references:

      Name      `post = router_post_alias`
      Attribute `post = router.post`
      Call      `register = router.post("/x")` -- decorator-factory
                  application that's later invoked on a handler

    Used by `_resolve_callable` to follow `Name` decorators back to
    their original Attribute/Call before the route-mutating
    detectors check the shape.

    Multi-valued (list of bindings) rather than last-write-wins: a
    later, unrelated reassignment in another branch must not silently
    overwrite an earlier binding that was used as a decorator. For
    example,

        post = router.post
        @post('/x')
        def handler(): ...
        post = something_else_unrelated   # last-write would clobber

    The earlier `post = router.post` is what `@post('/x')` actually
    references. Tracking every observed RHS keeps the resolver from
    losing it.

    Walk via `_walk_module_scope` instead of just `tree.body` so a
    rebind nested in a top-level `if` / `try` / etc. still counts:

        if True:
            post = router.post
        @post('/x')
        def handler(): ...

    `if True` is still a module-scope binding -- the conditional
    only gates whether the rebind happens, not the scope it lands
    in."""
    aliases: dict[str, list[ast.expr]] = {}
    if not isinstance(tree, ast.Module):
        return aliases
    for node in _walk_module_scope(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(
                    node.value, (ast.Name, ast.Attribute, ast.Call)
                ):
                    aliases.setdefault(target.id, []).append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
            and isinstance(node.value, (ast.Name, ast.Attribute, ast.Call))
        ):
            aliases.setdefault(node.target.id, []).append(node.value)
    return aliases


def _resolve_callable(
    node: ast.expr,
    aliases: dict[str, list[ast.expr]] | None,
    seen: frozenset[str] = frozenset(),
) -> list[ast.expr]:
    """Resolve `node` through `aliases`, returning a list of every
    underlying expression it can reach. If `node` is a `Name`
    recorded in `aliases`, follow each binding (multi-valued, since
    the same Name can be reassigned at module scope) and recurse
    until the binding isn't itself a tracked Name.

    Returns `[node]` unchanged when the input isn't a tracked Name,
    so callers can iterate the result and check each candidate
    shape in turn. Conservative direction: a name with multiple
    bindings flags as state-mutating if ANY binding resolves to a
    mutating decorator -- a later reassignment doesn't get to
    silently disarm an earlier one.

    Cycle protection comes from `seen` (the set of Name ids
    already on this resolution path), not from a depth cap. A
    fixed-depth cutoff would falsely bottom out a long but honest
    alias chain (`a = router.post; b = a; c = b; d = c; e = d;
    @e('/x')`) as the unresolved Name, which the mutating-route
    detectors then ignore -- letting a POST/PUT/PATCH/DELETE
    handler skip the `Depends(verify_same_origin)` check. Using a
    name-tracking seen set instead, the resolver follows a chain
    of arbitrary length and only bottoms out on a true cycle
    (`a = b; b = a`), where the final Name is returned and the
    caller continues to ignore non-Attribute candidates from that
    branch."""
    if aliases is None:
        return [node]
    if isinstance(node, ast.Name) and node.id in aliases and node.id not in seen:
        out: list[ast.expr] = []
        next_seen = seen | {node.id}
        for sub in aliases[node.id]:
            out.extend(_resolve_callable(sub, aliases, next_seen))
        return out or [node]
    return [node]


def _is_state_mutating_route_decorator(
    deco: ast.expr,
    callable_aliases: dict[str, list[ast.expr]] | None = None,
) -> bool:
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

    `callable_aliases` resolves a Name-callee decorator back to its
    underlying Attribute. Without it, `post = router.post` followed
    by `@post("/x")` would slip past since the Call's `func` is a
    bare Name rather than an Attribute. Returns True if ANY resolved
    binding for that Name is a mutating shape."""
    if not isinstance(deco, ast.Call):
        return False
    for func in _resolve_callable(deco.func, callable_aliases):
        if not isinstance(func, ast.Attribute):
            continue
        attr = func.attr
        if attr in _MUTATING_VERBS:
            return True
        if attr == "api_route" and _kwargs_contain_mutating_method(deco.keywords):
            return True
    return False


def _is_imperative_mutating_registration(
    node: ast.AST,
    callable_aliases: dict[str, list[ast.expr]] | None = None,
) -> bool:
    """True iff `node` is an imperative state-mutating route registration.
    FastAPI exposes three function-call equivalents of the decorator API:

    - **add_api_route**: `router.add_api_route(path, endpoint,
      methods=[...], ...)` -- the explicit imperative shape. Counts
      when `methods=` contains a state-mutating verb.

    - **Call-style decorator (one-step)**: `router.post("/x")(handler)`,
      `router.delete("/x")(handler)`, `router.api_route("/x",
      methods=["POST"])(handler)`. Equivalent to writing
      `@router.post("/x") def handler():` -- a Call whose function
      is itself a Call that the decorator detector recognises.

    - **Call-style decorator (two-step)**: `register =
      router.post("/x"); register(handler)`. The factory call is
      stored under a Name and applied later. We resolve `node.func`
      (a Name) through `callable_aliases` to recover the
      decorator-factory Call and check the same shape. Conservative
      direction: True if ANY binding for the Name resolves to a
      mutating Call.
    """
    if not isinstance(node, ast.Call):
        return False
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_api_route"
        and _kwargs_contain_mutating_method(node.keywords)
    ):
        return True
    for resolved in _resolve_callable(node.func, callable_aliases):
        if isinstance(resolved, ast.Call) and _is_state_mutating_route_decorator(
            resolved, callable_aliases
        ):
            return True
    return False


def _is_origin_dependency(
    node: ast.AST,
    vso_shadowed: bool = False,
    depends_shadowed: bool = False,
) -> bool:
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
    docstring/comment containing the token are all not the dependency.

    Two shadowing flags reject every match independently:

      `vso_shadowed=True`     -- `verify_same_origin` rebound at
                                 module scope or imported from a
                                 non-canonical source. The Name in
                                 the call resolves to something
                                 other than the real CSRF check.
      `depends_shadowed=True` -- `Depends` itself rebound or imported
                                 from a non-FastAPI source. A literal
                                 `Depends(...)` call no longer wires
                                 a FastAPI dependency, so the gate
                                 isn't actually installed even
                                 though the AST still spells the
                                 token."""
    if vso_shadowed or depends_shadowed:
        return False
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
        # `verify_same_origin` rebound at module scope shadows the
        # imported symbol for every downstream Depends(...) reference
        # in this file -- the decorator runs at definition time using
        # the lexical binding then-in-scope. Compute once per file.
        # `verify_same_origin` and `Depends` must each resolve to their
        # canonical FastAPI binding for the gate to actually wire. A
        # rebind or a non-canonical import of either shadows the literal
        # `Depends(verify_same_origin)` shape (the decorator runs at
        # definition time using the lexical binding then-in-scope).
        # Compute once per file.
        rebound = _module_level_rebound_names(tree, py)
        vso_shadowed = "verify_same_origin" in rebound
        depends_shadowed = "Depends" in rebound
        # `post = router.post; @post(...)` and `register = router.post(...);
        # register(handler)` are both equivalent to direct decorator /
        # call-style registrations once the alias is followed. Compute
        # the alias map once per file; both detectors thread it.
        callable_aliases = _module_level_callable_aliases(tree)
        # Pass 1: decorator-style registrations on FunctionDef /
        # AsyncFunctionDef. Both sync `def` and `async def` shapes
        # are valid FastAPI handlers (e.g. app/routes/sender.py::
        # create_secret is async); walk both node kinds.
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            mutating_decos = [
                d
                for d in node.decorator_list
                if _is_state_mutating_route_decorator(d, callable_aliases)
            ]
            if not mutating_decos:
                continue
            # Each mutating decorator must be individually gated. Stacked
            # registrations like `@router.post(...,
            # dependencies=[Depends(verify_same_origin)])` +
            # `@router.delete(...)` -- where one decorator wires the gate
            # and another doesn't -- previously passed because we OR'd
            # all decorators together. The post route's dependency would
            # mask the delete route's missing gate. Now check every
            # decorator separately; the function-signature dependency
            # (the parameter-default form) inherits to every decorator
            # since it applies to the handler's own call.
            sig_has_dep = any(
                _is_origin_dependency(inner, vso_shadowed, depends_shadowed)
                for inner in ast.walk(node.args)
            )
            for deco in mutating_decos:
                deco_has_dep = any(
                    _is_origin_dependency(inner, vso_shadowed, depends_shadowed)
                    for inner in ast.walk(deco)
                )
                if not (deco_has_dep or sig_has_dep):
                    rel = py.relative_to(REPO_ROOT)
                    # Resolve through the alias map for the printout so
                    # an aliased `@post(...)` reports the underlying
                    # `.post` rather than crashing on `deco.func.attr`
                    # (which doesn't exist when `deco.func` is a Name).
                    resolved = _resolve_callable(deco.func, callable_aliases)
                    attr = (
                        resolved.attr
                        if isinstance(resolved, ast.Attribute)
                        else getattr(deco.func, "id", "?")
                    )
                    offenders.append(f"{rel}:{deco.lineno} {node.name} (.{attr})")
        # Pass 2: imperative `<expr>.add_api_route(path, endpoint,
        # methods=[...])` calls anywhere in the module, plus call-style
        # decorator applications (`router.post("/x")(handler)`,
        # `register = router.post("/x"); register(handler)`). The endpoint
        # reference can't be followed to its signature statically, so the
        # gate must be wired in the call's own keyword args -- a
        # registration that sneaks the dependency only into the endpoint's
        # signature is an accepted miss.
        for node in ast.walk(tree):
            if not _is_imperative_mutating_registration(node, callable_aliases):
                continue
            ok = any(
                _is_origin_dependency(inner, vso_shadowed, depends_shadowed)
                for inner in ast.walk(node)
            )
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
