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
            and node.func.attr in {"pop", "get", "setdefault"}
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


def _check_reads_in_node(
    node: ast.AST | None,
    trusted: set[str],
    sanctioned_aliases: set[str],
    models_shadowed: bool = False,
) -> bool:
    """Walk `node` (any AST sub-tree -- a single expression, a simple
    statement, or a nested compound that the structural walker has
    already chosen to treat as a leaf) and verify every `totp_secret`
    read is on a trusted receiver at the current state. Returns False
    on the first untrusted receiver. `_walk_local` skips nested
    `FunctionDef` / `AsyncFunctionDef` / `Lambda` bodies -- those
    have their own scopes and are checked independently."""
    if node is None:
        return True
    for sub in _walk_local(node):
        if (
            isinstance(sub, ast.Subscript)
            and isinstance(sub.ctx, ast.Load)
            and isinstance(sub.slice, ast.Constant)
            and sub.slice.value == "totp_secret"
            and not _is_trusted_totp_receiver(
                sub.value, trusted, sanctioned_aliases, models_shadowed
            )
        ):
            return False
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr in {"pop", "get", "setdefault"}
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and sub.args[0].value == "totp_secret"
            and not _is_trusted_totp_receiver(
                sub.func.value, trusted, sanctioned_aliases, models_shadowed
            )
        ):
            return False
    return True


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
    expr: ast.expr | None,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
) -> tuple[set[str], set[str]]:
    """Apply every walrus binding (`name := value`) reachable inside
    `expr` to (trusted, aliases), in source order. Returns fresh sets.

    Walrus assignments in `if` / `while` headers, `match` subjects, and
    For-loop iters bind in the enclosing scope and run BEFORE the
    branching body (PEP 572). Without this hook, a reassignment like
    `if (user := models.get_user_by_id(uid)):` would leave a trusted
    `user` from earlier in the function trusted through both branches.

    `ast.walk` descends through nested expressions, including list /
    set / dict / generator comprehensions (PEP 572 explicitly says
    walrus in those binds the enclosing scope). Skipping nested
    `Lambda` would be the precise move, but the realistic codebase
    doesn't ship lambdas with walrus side effects, and over-revoking
    matches the rest of the test posture."""
    if expr is None:
        return set(trusted), set(aliases)
    nt, na = set(trusted), set(aliases)
    for node in ast.walk(expr):
        if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            sanctioned = _is_sanctioned_or_none_source(node.value, na, models_shadowed)
            if sanctioned:
                nt.add(node.target.id)
            else:
                nt.discard(node.target.id)
                na.discard(node.target.id)
    return nt, na


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


def _walk_scope_seq(
    stmts: list[ast.stmt],
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
) -> tuple[bool, set[str], set[str]]:
    """Thread `(trusted, aliases)` through `stmts` in source order via
    `_check_and_apply_stmt`, returning False on the first read failure
    along with the post-failure state (which the caller discards
    anyway). Used inside structural branch handlers to process one
    branch's body sequentially before joining with siblings.

    Stops threading after an unconditional control-transfer at the
    body's top level -- `Return`, `Raise`, `Break`, `Continue`. The
    statements after such a transfer are unreachable in this scope,
    so applying their state changes would re-establish trust that
    isn't actually visible at any reachable point. Concrete bypass
    that motivated this:

        try:
            user = unsanctioned()       # state: revoke
            raise RuntimeError()        # control transfer
            user = sanctioned()         # UNREACHABLE
        except RuntimeError:
            user["totp_secret"]         # handler entry: pre & try_t

    Without the break, the unreachable re-add brought `user` back
    into `try_t`, leaking through the `pre & try_t` handler-entry
    intersection. With it, `try_t` reflects only the reachable
    prefix and the handler correctly sees `user` revoked."""
    t, a = set(trusted), set(aliases)
    for stmt in stmts:
        ok, t, a = _check_and_apply_stmt(stmt, t, a, models_shadowed)
        if not ok:
            return False, t, a
        if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            break
    return True, t, a


def _check_and_apply_assign(
    stmt: ast.Assign, trusted: set[str], aliases: set[str], models_shadowed: bool
) -> tuple[bool, set[str], set[str]]:
    """Read-check the RHS against pre-stmt state, then apply binding.
    Sanctioned RHS adds each top-level Name target to `trusted`;
    non-sanctioned RHS discards every name bound by the assignment.
    Tuple / list unpacking revokes every Name in the pattern --
    static analysis can't pair pattern slots to RHS-tuple elements."""
    if not _check_reads_in_node(stmt.value, trusted, aliases, models_shadowed):
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
    stmt: ast.AnnAssign, trusted: set[str], aliases: set[str], models_shadowed: bool
) -> tuple[bool, set[str], set[str]]:
    """`user: dict = expr` -- behaves like Assign for trust purposes when
    `value` is set. A bare `user: dict` with no `value` doesn't bind at
    runtime, so the sets are unchanged. The annotation expression
    itself isn't read-checked: any totp_secret subscript inside a
    type annotation would be a static type expression, not a runtime
    read."""
    if stmt.value is None or not isinstance(stmt.target, ast.Name):
        return True, set(trusted), set(aliases)
    if not _check_reads_in_node(stmt.value, trusted, aliases, models_shadowed):
        return False, set(trusted), set(aliases)
    nt, na = set(trusted), set(aliases)
    if _is_sanctioned_or_none_source(stmt.value, na, models_shadowed):
        nt.add(stmt.target.id)
    else:
        nt.discard(stmt.target.id)
        na.discard(stmt.target.id)
    return True, nt, na


def _check_and_apply_if(
    stmt: ast.If, trusted: set[str], aliases: set[str], models_shadowed: bool
) -> tuple[bool, set[str], set[str]]:
    """Read-check the test, apply walrus bindings, then thread the
    resulting state through body and orelse separately and intersect.
    Inner-branch reads that depend on inner-branch assignments now
    see the threaded state because each body statement is processed
    one-at-a-time via `_walk_scope_seq`."""
    if not _check_reads_in_node(stmt.test, trusted, aliases, models_shadowed):
        return False, set(trusted), set(aliases)
    pre_t, pre_a = _apply_walrus_in_expr(stmt.test, trusted, aliases, models_shadowed)
    if_ok, if_t, if_a = _walk_scope_seq(stmt.body, pre_t, pre_a, models_shadowed)
    if not if_ok:
        return False, if_t, if_a
    else_ok, else_t, else_a = _walk_scope_seq(
        stmt.orelse, pre_t, pre_a, models_shadowed
    )
    if not else_ok:
        return False, else_t, else_a
    return True, if_t & else_t, if_a & else_a


def _check_and_apply_try(
    stmt: ast.Try, trusted: set[str], aliases: set[str], models_shadowed: bool
) -> tuple[bool, set[str], set[str]]:
    """Three reachable post-Try paths to merge: try-body completes (and
    orelse runs); or any handler runs (from arbitrary point in try
    body); or `finally` ends the construct. Each handler's entry
    state is the INTERSECTION of the pre-Try state and the try-body-
    end state -- not just the pre-Try state. The handler can fire at
    any point during the try body, so a name that survives both
    sides is the conservative "trusted at handler entry" set:

      pre = {user}
      try:
          user = unsanctioned()    # try-body-end revokes user
          raise
      except:
          user["totp_secret"]      # handler entry: pre & try_t = {}

    Without the intersection, `user` would still be marked trusted
    at handler entry from the pre-Try copy and the read would slip
    through. (The opposite case -- name added in try body, handler
    fires before the add -- is also rejected by the intersection
    since the pre-Try set wouldn't have it.) `finally` always runs
    after the merged state; threaded through that."""
    body_ok, try_t, try_a = _walk_scope_seq(
        stmt.body, trusted, aliases, models_shadowed
    )
    if not body_ok:
        return False, try_t, try_a
    else_ok, else_t, else_a = _walk_scope_seq(
        stmt.orelse, try_t, try_a, models_shadowed
    )
    if not else_ok:
        return False, else_t, else_a
    merged_t, merged_a = else_t, else_a
    for handler in stmt.handlers:
        h_t = set(trusted) & try_t
        h_a = set(aliases) & try_a
        if handler.name is not None:
            h_t.discard(handler.name)
            h_a.discard(handler.name)
        if handler.type is not None and not _check_reads_in_node(
            handler.type, h_t, h_a, models_shadowed
        ):
            return False, h_t, h_a
        h_ok, h_t, h_a = _walk_scope_seq(handler.body, h_t, h_a, models_shadowed)
        if not h_ok:
            return False, h_t, h_a
        merged_t = merged_t & h_t
        merged_a = merged_a & h_a
    fin_ok, fin_t, fin_a = _walk_scope_seq(
        stmt.finalbody, merged_t, merged_a, models_shadowed
    )
    if not fin_ok:
        return False, fin_t, fin_a
    return True, fin_t, fin_a


def _check_and_apply_loop(
    stmt: ast.While | ast.For | ast.AsyncFor,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
) -> tuple[bool, set[str], set[str]]:
    """Loop body might not execute at all -- the post-loop state is the
    intersection of pre-state (body skipped) and the body-end state
    (body ran ≥ once and completed). Walrus bindings in the header
    apply to the pre-state."""
    header = stmt.test if isinstance(stmt, ast.While) else stmt.iter
    if not _check_reads_in_node(header, trusted, aliases, models_shadowed):
        return False, set(trusted), set(aliases)
    pre_t, pre_a = _apply_walrus_in_expr(header, trusted, aliases, models_shadowed)
    body_t, body_a = set(pre_t), set(pre_a)
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        for bound in _names_bound_by_target(stmt.target):
            body_t.discard(bound)
            body_a.discard(bound)
    body_ok, body_t, body_a = _walk_scope_seq(
        stmt.body, body_t, body_a, models_shadowed
    )
    if not body_ok:
        return False, body_t, body_a
    else_ok, else_t, else_a = _walk_scope_seq(
        stmt.orelse, body_t, body_a, models_shadowed
    )
    if not else_ok:
        return False, else_t, else_a
    return True, set(pre_t) & else_t, set(pre_a) & else_a


def _check_and_apply_with(
    stmt: ast.With | ast.AsyncWith,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
) -> tuple[bool, set[str], set[str]]:
    """`with ctx as name:` binds `name` to the context manager's
    `__enter__` return; tuple/list-unpacking forms (`with ctx as
    (a, b):`) bind every captured Name. Read-check each context_expr
    first, then revoke the captured names, then thread the body."""
    for item in stmt.items:
        if not _check_reads_in_node(
            item.context_expr, trusted, aliases, models_shadowed
        ):
            return False, set(trusted), set(aliases)
    nt, na = set(trusted), set(aliases)
    for item in stmt.items:
        if item.optional_vars is None:
            continue
        for bound in _names_bound_by_target(item.optional_vars):
            nt.discard(bound)
            na.discard(bound)
    return _walk_scope_seq(stmt.body, nt, na, models_shadowed)


def _check_and_apply_match(
    stmt: ast.Match,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
) -> tuple[bool, set[str], set[str]]:
    """`match X: case <pat>: <body>` (PEP 634, Python 3.10+). The
    subject is read-checked once before any case runs; walrus bindings
    inside it apply to every case's pre-state. Each case starts from
    that subject-evaluated state minus the names bound by its pattern.
    Pattern guards (`case X if cond:`) are read-checked against the
    pattern-bound state. Post-Match state is the intersection of every
    case's end state PLUS the subject-evaluated state for the
    fall-through path (no case matched at runtime)."""
    if not _check_reads_in_node(stmt.subject, trusted, aliases, models_shadowed):
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
        if case.guard is not None and not _check_reads_in_node(
            case.guard, case_t, case_a, models_shadowed
        ):
            return False, case_t, case_a
        case_ok, case_t, case_a = _walk_scope_seq(
            case.body, case_t, case_a, models_shadowed
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
    there's no read check."""
    nt, na = set(trusted), set(aliases)
    for alias in stmt.names:
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
        return _check_and_apply_assign(stmt, trusted, aliases, models_shadowed)
    if isinstance(stmt, ast.AnnAssign):
        return _check_and_apply_annassign(stmt, trusted, aliases, models_shadowed)
    if isinstance(stmt, ast.If):
        return _check_and_apply_if(stmt, trusted, aliases, models_shadowed)
    if isinstance(stmt, ast.Try):
        return _check_and_apply_try(stmt, trusted, aliases, models_shadowed)
    if isinstance(stmt, ast.Match):
        return _check_and_apply_match(stmt, trusted, aliases, models_shadowed)
    if isinstance(stmt, (ast.While, ast.For, ast.AsyncFor)):
        return _check_and_apply_loop(stmt, trusted, aliases, models_shadowed)
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return _check_and_apply_with(stmt, trusted, aliases, models_shadowed)
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return _check_and_apply_import(stmt, trusted, aliases)
    if not _check_reads_in_node(stmt, trusted, aliases, models_shadowed):
        return False, set(trusted), set(aliases)
    return True, set(trusted), set(aliases)


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
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for tracked_name in _CANONICAL_IMPORT_SOURCES:
                if _import_binds_name_from_non_canonical_source(
                    stmt, tracked_name, file_path
                ):
                    names.add(tracked_name)
    return names


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
    ok, _t, _a = _walk_scope_seq(scope.body, trusted, local_sanctioned, models_shadowed)
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
            if not _scope_reads_totp_secret(fn):
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
        for text, lineno in _string_text_outside_docstrings(tree):
            if sql_ref.search(text):
                offenders.append(f"{rel}:{lineno}")
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


def _module_level_callable_aliases(tree: ast.AST) -> dict[str, ast.expr]:
    """Return a `Name -> expr` map for module-scope `<name> = <expr>`
    assignments where `<expr>` is one of the shapes we statically
    follow when resolving aliased decorator references:

      Name      `post = router_post_alias`
      Attribute `post = router.post`
      Call      `register = router.post("/x")` -- decorator-factory
                  application that's later invoked on a handler

    Used by `_resolve_callable` to follow `Name` decorators back to
    their original Attribute/Call before the route-mutating
    detectors check the shape. Module scope only (function-local
    `def f(): post = router.post; @post(...)` is rare; the realistic
    bypass is a top-level rebind)."""
    aliases: dict[str, ast.expr] = {}
    if not isinstance(tree, ast.Module):
        return aliases
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and isinstance(
                    stmt.value, (ast.Name, ast.Attribute, ast.Call)
                ):
                    aliases[target.id] = stmt.value
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.value is not None
            and isinstance(stmt.value, (ast.Name, ast.Attribute, ast.Call))
        ):
            aliases[stmt.target.id] = stmt.value
    return aliases


def _resolve_callable(
    node: ast.expr,
    aliases: dict[str, ast.expr] | None,
    depth: int = 0,
) -> ast.expr:
    """If `node` is a `Name` recorded in `aliases`, follow the
    chain to its underlying expression. Caps at depth 4 to bail on
    cycles (`a = b; b = a`). Returns `node` unchanged when it isn't
    a Name or no alias applies, so callers use the result as a
    drop-in replacement."""
    if depth > 4 or aliases is None:
        return node
    if isinstance(node, ast.Name) and node.id in aliases:
        return _resolve_callable(aliases[node.id], aliases, depth + 1)
    return node


def _is_state_mutating_route_decorator(
    deco: ast.expr,
    callable_aliases: dict[str, ast.expr] | None = None,
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
    bare Name rather than an Attribute.
    """
    if not isinstance(deco, ast.Call):
        return False
    func = _resolve_callable(deco.func, callable_aliases)
    if not isinstance(func, ast.Attribute):
        return False
    attr = func.attr
    if attr in _MUTATING_VERBS:
        return True
    if attr == "api_route":
        return _kwargs_contain_mutating_method(deco.keywords)
    return False


def _is_imperative_mutating_registration(
    node: ast.AST,
    callable_aliases: dict[str, ast.expr] | None = None,
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
      decorator-factory Call and check the same shape.
    """
    if not isinstance(node, ast.Call):
        return False
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_api_route"
        and _kwargs_contain_mutating_method(node.keywords)
    ):
        return True
    resolved = _resolve_callable(node.func, callable_aliases)
    return isinstance(resolved, ast.Call) and _is_state_mutating_route_decorator(
        resolved, callable_aliases
    )


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
