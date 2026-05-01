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
    # so chains (`A1 = AuthError; A2 = A1`) propagate. Adding aliases
    # only EXPANDS the recognised set; it never narrows it -- a
    # `Local = Something_Unrelated` doesn't pull `Something_Unrelated`
    # into `names` (LHS is added only when RHS is already in `names`),
    # so the asymmetric expansion is safe even when scopes nest.
    changed = True
    while changed:
        changed = False
        for stmt in ast.walk(tree):
            if not isinstance(stmt, ast.Assign):
                continue
            if not (isinstance(stmt.value, ast.Name) and stmt.value.id in names):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return names


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


# Module specs that resolve to the data-layer source of the sanctioned
# getters. Both absolute and relative imports are checked against this
# set after relative paths are resolved against the importing file's
# own package (see `_resolve_import_module`). `app.models` is included
# to cover a re-export through the package `__init__`; the closed list
# keeps the trust surface small.
_DATA_LAYER_ABSOLUTE_MODULES = frozenset({"app.models", "app.models.users"})


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


def _stmt_reads_are_quarantined(
    stmt: ast.AST,
    trusted: set[str],
    sanctioned_aliases: set[str],
    models_shadowed: bool = False,
) -> bool:
    """Check every `totp_secret` read inside `stmt` against the
    `trusted` set as it stands BEFORE the statement runs. Returns
    False on the first untrusted receiver."""
    for node in _walk_local(stmt):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "totp_secret"
            and not _is_trusted_totp_receiver(
                node.value, trusted, sanctioned_aliases, models_shadowed
            )
        ):
            return False
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"pop", "get"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "totp_secret"
            and not _is_trusted_totp_receiver(
                node.func.value, trusted, sanctioned_aliases, models_shadowed
            )
        ):
            return False
    return True


def _apply_seq(
    stmts: list[ast.stmt], trusted: set[str], aliases: set[str], models_shadowed: bool
) -> tuple[set[str], set[str]]:
    """Thread `(trusted, aliases)` through `stmts` in source order via
    `_apply_stmt`. Used inside structural branch handlers to process
    one branch's body sequentially before joining with siblings."""
    t, a = trusted, aliases
    for stmt in stmts:
        t, a = _apply_stmt(stmt, t, a, models_shadowed)
    return t, a


def _apply_assign(
    stmt: ast.Assign, trusted: set[str], aliases: set[str], models_shadowed: bool
) -> tuple[set[str], set[str]]:
    """Sanctioned RHS adds each Name target to `trusted`; non-sanctioned
    RHS discards them. Discarding the target from `aliases` too revokes
    a function-local rebind of an imported sanctioned-getter name --
    `get_user_with_totp_by_id = other_thing` shadows the import in
    this scope so downstream calls of that name no longer count."""
    nt, na = set(trusted), set(aliases)
    sanctioned = _is_sanctioned_or_none_source(stmt.value, na, models_shadowed)
    for target in stmt.targets:
        if not isinstance(target, ast.Name):
            continue
        if sanctioned:
            nt.add(target.id)
        else:
            nt.discard(target.id)
            na.discard(target.id)
    return nt, na


def _apply_annassign(
    stmt: ast.AnnAssign, trusted: set[str], aliases: set[str], models_shadowed: bool
) -> tuple[set[str], set[str]]:
    """`user: dict = expr` -- behaves like Assign for trust purposes when
    `value` is set. A bare `user: dict` with no `value` doesn't bind at
    runtime, so we leave the sets unchanged."""
    if stmt.value is None or not isinstance(stmt.target, ast.Name):
        return set(trusted), set(aliases)
    nt, na = set(trusted), set(aliases)
    if _is_sanctioned_or_none_source(stmt.value, na, models_shadowed):
        nt.add(stmt.target.id)
    else:
        nt.discard(stmt.target.id)
        na.discard(stmt.target.id)
    return nt, na


def _apply_if(
    stmt: ast.If, trusted: set[str], aliases: set[str], models_shadowed: bool
) -> tuple[set[str], set[str]]:
    """Thread the pre-If state through body and orelse separately, then
    intersect the per-branch results: trust survives the construct only
    if it survives on EVERY reachable path. Closes the bypass where one
    branch reassigns to an unsanctioned source -- the other branch's
    sanctioned reassignment can no longer "win" by visit order."""
    if_t, if_a = _apply_seq(stmt.body, set(trusted), set(aliases), models_shadowed)
    else_t, else_a = _apply_seq(
        stmt.orelse, set(trusted), set(aliases), models_shadowed
    )
    return if_t & else_t, if_a & else_a


def _apply_try(
    stmt: ast.Try, trusted: set[str], aliases: set[str], models_shadowed: bool
) -> tuple[set[str], set[str]]:
    """Three reachable post-Try paths to merge: try-body completes (and
    orelse runs); or any handler runs (from arbitrary point in try body
    -- conservative pre-state is the original pre-Try state, with any
    `as <name>` excluded from trust). `finally` always runs after the
    merged state."""
    try_t, try_a = _apply_seq(stmt.body, set(trusted), set(aliases), models_shadowed)
    else_t, else_a = _apply_seq(stmt.orelse, try_t, try_a, models_shadowed)
    merged_t, merged_a = else_t, else_a
    for handler in stmt.handlers:
        h_t, h_a = set(trusted), set(aliases)
        if handler.name is not None:
            h_t.discard(handler.name)
            h_a.discard(handler.name)
        h_t, h_a = _apply_seq(handler.body, h_t, h_a, models_shadowed)
        merged_t = merged_t & h_t
        merged_a = merged_a & h_a
    return _apply_seq(stmt.finalbody, merged_t, merged_a, models_shadowed)


def _apply_loop(
    stmt: ast.While | ast.For | ast.AsyncFor,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
) -> tuple[set[str], set[str]]:
    """Loop body might not execute at all (empty iter / initially false
    cond) -- so the post-loop state is the intersection of pre-state
    (body skipped) and the body-end state (body ran ≥ once and
    completed). The For target is bound to elements of `iter`, not
    sanctioned. The else clause runs only if the loop exits without a
    break; thread it through the body-end state."""
    body_t, body_a = set(trusted), set(aliases)
    if isinstance(stmt, (ast.For, ast.AsyncFor)) and isinstance(stmt.target, ast.Name):
        body_t.discard(stmt.target.id)
        body_a.discard(stmt.target.id)
    body_t, body_a = _apply_seq(stmt.body, body_t, body_a, models_shadowed)
    else_t, else_a = _apply_seq(stmt.orelse, body_t, body_a, models_shadowed)
    return set(trusted) & else_t, set(aliases) & else_a


def _apply_with(
    stmt: ast.With | ast.AsyncWith,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool,
) -> tuple[set[str], set[str]]:
    """`with ctx as name:` binds `name` to the context manager's
    `__enter__` return -- not sanctioned for our purposes. Body
    statements thread through sequentially."""
    nt, na = set(trusted), set(aliases)
    for item in stmt.items:
        if isinstance(item.optional_vars, ast.Name):
            nt.discard(item.optional_vars.id)
            na.discard(item.optional_vars.id)
    return _apply_seq(stmt.body, nt, na, models_shadowed)


def _apply_stmt(
    stmt: ast.AST,
    trusted: set[str],
    aliases: set[str],
    models_shadowed: bool = False,
) -> tuple[set[str], set[str]]:
    """Return the (trusted, aliases) state after `stmt` runs, computed
    structurally: branching constructs process each reachable branch
    with cloned state and intersect the per-branch results, so trust
    survives the construct only on every reachable path. Sequential
    within-branch threading happens by chaining state through the
    branch's statements in source order via `_apply_seq`.

    Returns fresh sets; the input sets are never mutated.

    All shapes not handled below (Return, Raise, Pass, Break, Continue,
    Expr, Import, ImportFrom, Global, Nonlocal, Delete, FunctionDef,
    ClassDef, AugAssign) leave (trusted, aliases) unchanged. Nested
    function / class bodies have their own scopes and are checked
    independently by the caller."""
    if isinstance(stmt, ast.Assign):
        return _apply_assign(stmt, trusted, aliases, models_shadowed)
    if isinstance(stmt, ast.AnnAssign):
        return _apply_annassign(stmt, trusted, aliases, models_shadowed)
    if isinstance(stmt, ast.If):
        return _apply_if(stmt, trusted, aliases, models_shadowed)
    if isinstance(stmt, ast.Try):
        return _apply_try(stmt, trusted, aliases, models_shadowed)
    if isinstance(stmt, (ast.While, ast.For, ast.AsyncFor)):
        return _apply_loop(stmt, trusted, aliases, models_shadowed)
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return _apply_with(stmt, trusted, aliases, models_shadowed)
    return set(trusted), set(aliases)


def _update_trust_from_stmt(
    stmt: ast.AST,
    trusted: set[str],
    sanctioned_aliases: set[str],
    models_shadowed: bool = False,
) -> None:
    """In-place wrapper around `_apply_stmt` for the legacy call shape.
    Mutates `trusted` and `sanctioned_aliases` to reflect the post-
    statement state."""
    nt, na = _apply_stmt(stmt, trusted, sanctioned_aliases, models_shadowed)
    trusted.clear()
    trusted.update(nt)
    sanctioned_aliases.clear()
    sanctioned_aliases.update(na)


def _name_is_bound_in_scope(name: str, scope: ast.AST) -> bool:
    """True iff `name` is bound somewhere in `scope` (function
    parameters or local Assign / AnnAssign in the body), and would
    therefore SHADOW any same-named module-level import for the
    duration of this function. Used to detect when `models` no longer
    resolves to the data-layer package because of a parameter named
    `models` or a local rebind."""
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
    for node in _walk_local(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return True
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return True
    return False


def _module_level_rebound_names(tree: ast.AST) -> set[str]:
    """Names that have been rebound at module scope via top-level
    Assign / AnnAssign. Treat any subsequent Name reference in this
    module as resolving to the rebound value, not to whatever was
    imported under the same name. Used so a `models = something` or
    `verify_same_origin = something` at module level shadows the
    import for every downstream use."""
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
    return names


def _scope_totp_reads_are_quarantined(
    scope: ast.AST,
    sanctioned_aliases: set[str],
    module_rebound: set[str] | None = None,
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
    local_sanctioned = set(sanctioned_aliases)
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
    ) or _name_is_bound_in_scope("models", scope)
    for stmt in scope.body:
        if not _stmt_reads_are_quarantined(
            stmt, trusted, local_sanctioned, models_shadowed
        ):
            return False
        _update_trust_from_stmt(stmt, trusted, local_sanctioned, models_shadowed)
    return True


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
        module_rebound = _module_level_rebound_names(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _scope_reads_totp_secret(fn):
                continue
            if not _scope_totp_reads_are_quarantined(fn, sanctioned, module_rebound):
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


def _is_origin_dependency(node: ast.AST, vso_shadowed: bool = False) -> bool:
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

    `vso_shadowed=True` rejects every match: a module-level rebind of
    `verify_same_origin` to a different callable shadows the imported
    symbol for every downstream `Depends(verify_same_origin)`
    reference, so the gate isn't actually wired even though the AST
    still contains the Name."""
    if vso_shadowed:
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
        vso_shadowed = "verify_same_origin" in _module_level_rebound_names(tree)
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
                _is_origin_dependency(inner, vso_shadowed)
                for inner in ast.walk(node.args)
            )
            for deco in mutating_decos:
                deco_has_dep = any(
                    _is_origin_dependency(inner, vso_shadowed)
                    for inner in ast.walk(deco)
                )
                if not (deco_has_dep or sig_has_dep):
                    rel = py.relative_to(REPO_ROOT)
                    offenders.append(
                        f"{rel}:{deco.lineno} {node.name} (.{deco.func.attr})"
                    )
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
            ok = any(
                _is_origin_dependency(inner, vso_shadowed) for inner in ast.walk(node)
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
