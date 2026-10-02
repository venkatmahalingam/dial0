#!/usr/bin/env python3
"""Statically extract the Click command tree from sonic-utilities' config/ and show/ directories.

Nothing is imported or executed: the Python source is parsed with `ast`, so it works at image-build time
without SONiC's dependencies.  Output: commands.json  {"meta": {...}, "nodes": {"config vlan add": {...}, ...}}

Handles the patterns SONiC uses:  @click.group/@click.command (+ parent.add_command(child[, name])),
@parent.group(...)/@parent.command(...), name= overrides, docstring help, click.option/argument,
cross-module references (vlan.vlan), plugins with register(cli).
Anything it cannot resolve is reported (unresolved/orphans) instead of guessed.
"""
import argparse, ast, inspect, json, os, sys, time

TREES = {"config": "config", "show": "cli"}  # tree dir -> root group function name in <tree>/main.py
PLAIN_OK = {"click.option", "click.argument", "click.pass_context", "click.pass_obj", "click.command", "click.group"}


def dotted(n):
    """The dotted name of an AST expression (e.g. click.option), or ''."""
    if isinstance(n, ast.Call):
        return dotted(n.func)
    if isinstance(n, ast.Attribute):
        b = dotted(n.value)
        return f"{b}.{n.attr}" if b else None
    if isinstance(n, ast.Name):
        return n.id
    return None


def sconst(n):
    return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None


def kwarg(call, key):
    for k in getattr(call, "keywords", []):
        if k.arg == key:
            return k.value
    return None


def bconst(n, default=False):
    return n.value if isinstance(n, ast.Constant) and isinstance(n.value, bool) else default


class Obj:
    def __init__(self, mod, func, line, kind, name, help_, options, args, complete, runnable=False):
        self.mod, self.func, self.line, self.kind, self.name = mod, func, line, kind, name
        self.help, self.options, self.args, self.complete = help_, options, args, complete
        self.runnable = runnable  # a group that also runs by itself (invoke_without_command=True)

    def source(self):
        return f"{self.mod.path}:{self.line}"


class Mod:
    def __init__(self, path, tree_name):
        self.path, self.tree, self.imports, self.objs, self.edges = path, tree_name, {}, [], []
        parts = path[:-3].split("/")
        self.short = parts[-2] if parts[-1] == "__init__" else parts[-1]
        self.is_plugin = "/plugins/" in path


def first_para(doc):
    if not doc:
        return ""
    d = inspect.cleandoc(doc).split("\n\n")[0]
    return " ".join(d.split())


def parse_option(call):
    """One @click.option(...) decorator -> {name, flags, flag, help}."""
    flags = [s for s in (sconst(a) for a in call.args) if s and s.startswith("-")]
    names = [s for s in (sconst(a) for a in call.args) if s and not s.startswith("-")]
    if not flags:
        return None
    name = names[0] if names else max(flags, key=len).lstrip("-").replace("-", "_")
    return {"flags": flags, "name": name, "flag": bconst(kwarg(call, "is_flag")) or bconst(kwarg(call, "count")),
            "metavar": sconst(kwarg(call, "metavar")) or "", "help": " ".join((sconst(kwarg(call, "help")) or "").split()),
            "required": bconst(kwarg(call, "required"))}


def parse_argument(call):
    """One @click.argument(...) decorator -> {name, required, variadic, metavar}."""
    names = [sconst(a) for a in call.args if sconst(a)]
    if not names:
        return None
    nargs = kwarg(call, "nargs")
    var = isinstance(nargs, ast.UnaryOp) or (isinstance(nargs, ast.Constant) and nargs.value == -1)
    req = kwarg(call, "required")
    return {"name": names[0], "metavar": sconst(kwarg(call, "metavar")) or "", "variadic": bool(var),
            "required": bconst(req, True) if req is not None else True}


def collect_module(path, src, tree_name):
    """Every Click group and command defined in one module, with their decorators, parents and help text."""
    mod = Mod(path, tree_name)
    tree = ast.parse(src)
    for n in ast.walk(tree):  # imports (also inside functions)
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    mod.imports.setdefault(a.asname, []).append((a.name.split(".")[-1], None))
        elif isinstance(n, ast.ImportFrom):
            m = (n.module or "").split(".")[-1]
            for a in n.names:
                al = a.asname or a.name
                cands = mod.imports.setdefault(al, [])
                if m:
                    cands.append((m, a.name))
                cands.append((a.name, None))
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        define, options, args, complete = None, [], [], True
        for d in n.decorator_list:
            dn = dotted(d)
            if dn is None:
                complete = False
                continue
            last = dn.rsplit(".", 1)[-1]
            if last in ("group", "command"):
                define = d
            elif dn == "click.option" and isinstance(d, ast.Call):
                o = parse_option(d)
                options.append(o) if o else None
                complete = complete and o is not None
            elif dn == "click.argument" and isinstance(d, ast.Call):
                a = parse_argument(d)
                args.append(a) if a else None
                complete = complete and a is not None
            elif last in ("pass_context", "pass_obj", "pass_db"):
                pass
            else:
                complete = False  # unknown decorator (e.g. multi_asic options): flags cannot be trusted
        if define is None:
            continue
        kind = "group" if dotted(define).endswith("group") else "command"
        nm = sconst(kwarg(define, "name")) if isinstance(define, ast.Call) else None
        if nm is None and isinstance(define, ast.Call) and define.args:
            nm = sconst(define.args[0])
        nm = nm or n.name.lower().replace("_", "-")
        h = (sconst(kwarg(define, "help")) if isinstance(define, ast.Call) else None) or \
            (sconst(kwarg(define, "short_help")) if isinstance(define, ast.Call) else None) or first_para(ast.get_docstring(n, clean=False))
        runnable = kind == "group" and isinstance(define, ast.Call) and bconst(kwarg(define, "invoke_without_command"))
        obj = Obj(mod, n.name, n.lineno, kind, nm, " ".join((h or "").split()), options, args, complete, runnable)
        mod.objs.append(obj)
        base = define.func.value if isinstance(define, ast.Call) and isinstance(define.func, ast.Attribute) else None
        if base is not None and dotted(base) != "click":
            mod.edges.append((base, obj, None))  # parent.group()/parent.command()
    for n in ast.walk(tree):  # parent.add_command(child[, name])
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_command" and n.args:
            nm = sconst(n.args[1]) if len(n.args) > 1 else sconst(kwarg(n, "name"))
            mod.edges.append((n.func.value, n.args[0], nm))
    return mod


def build(root):
    """Walk the config/ and show/ packages and build the command tree: path -> {kind, help, usage, options, args}."""
    mods = []
    for tree_name in TREES:
        base = os.path.join(root, tree_name)
        for dp, _, files in os.walk(base):
            for f in sorted(files):
                if f.endswith(".py"):
                    p = os.path.join(dp, f)
                    rel = os.path.relpath(p, root).replace(os.sep, "/")
                    try:
                        mods.append(collect_module(rel, open(p, encoding="utf-8").read(), tree_name))
                    except SyntaxError as e:
                        print(f"skip {rel}: {e}", file=sys.stderr)
    allobjs = [o for m in mods for o in m.objs]
    roots = {}
    for o in allobjs:
        if o.mod.path == f"{o.mod.tree}/main.py" and o.func == TREES[o.mod.tree]:
            roots[o.mod.tree] = o
    missing = [t for t in TREES if t not in roots]
    if missing:
        sys.exit(f"cannot find root group for {missing}: is --root the sonic-utilities checkout?")

    def find(modshort, func, tree):
        c = [o for o in allobjs if o.mod.short == modshort and o.func == func]
        c = [o for o in c if o.mod.tree == tree] or c
        return c[0] if c else None

    def resolve(expr, mod):
        """The full command path of a group or command, following parent groups across modules."""
        if isinstance(expr, ast.Name):
            loc = [o for o in mod.objs if o.func == expr.id]
            if loc:
                return loc[0]
            for m, sym in mod.imports.get(expr.id, []):
                if sym and (o := find(m, sym, mod.tree)):
                    return o
            if expr.id == TREES[mod.tree] or mod.is_plugin:
                return roots[mod.tree]
            g = [o for o in allobjs if o.mod.tree == mod.tree and o.func == expr.id]
            return g[0] if len(g) == 1 else None
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            for m, sym in mod.imports.get(expr.value.id, []):
                if sym is None and (o := find(m, expr.attr, mod.tree)):
                    return o
        return None

    children, unresolved, seen = {}, [], set()
    for m in mods:
        for parent_e, child_e, nm in m.edges:
            p = resolve(parent_e, m)
            c = child_e if isinstance(child_e, Obj) else resolve(child_e, m)
            if p is None or c is None:
                unresolved.append(f"{m.path}: {ast.unparse(parent_e)} <- {ast.unparse(child_e) if not isinstance(child_e, Obj) else child_e.func}")
                continue
            key = (id(p), id(c), nm or c.name)
            if key not in seen:
                seen.add(key)
                children.setdefault(id(p), []).append((c, nm or c.name))

    nodes, reached = {}, set()

    def usage(path, o):
        """A usage line like `config vlan member add [-u] <vid> <port>` from a command's arguments and options."""
        parts = [path]
        for op in o.options:
            body = "|".join(op["flags"]) + ("" if op["flag"] else f" <{op['metavar'] or op['name']}>")
            parts.append(f"[{body}]")
        for a in o.args:
            nm = a["metavar"] or a["name"].upper()
            nm += "..." if a["variadic"] else ""
            parts.append(nm if (a["required"] or nm.startswith("[")) else f"[{nm}]")
        return " ".join(parts)

    def walk(o, path, stack):
        """Visit every node below a group, depth first."""
        reached.add(id(o))
        kids = children.get(id(o), [])
        nodes[path] = {"kind": o.kind, "runnable": o.runnable, "help": o.help,
                       "usage": usage(path, o) if (o.kind == "command" or o.runnable) else path,
                       "options": o.options, "args": o.args, "opts_complete": o.complete, "source": o.source(),
                       "children": [n for _, n in kids]}
        for c, nm in kids:
            if id(c) not in stack:
                walk(c, f"{path} {nm}", stack | {id(c)})

    for tree_name, r in roots.items():
        walk(r, tree_name, {id(r)})
    orphans = [f"{o.mod.path}:{o.line} {o.func}" for o in allobjs if id(o) not in reached]
    return nodes, unresolved, orphans


def main():
    """Command-line entry: extract a source tree into a commands.json index."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="sonic-utilities checkout (contains config/ and show/)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ref", default="")
    ap.add_argument("--commit", default="")
    ap.add_argument("--min-commands", type=int, default=150, help="fail if fewer leaf commands are found")
    a = ap.parse_args()
    nodes, unresolved, orphans = build(a.root)
    leaves = sum(1 for n in nodes.values() if n["kind"] == "command")
    print(f"commands: {leaves}  groups: {len(nodes) - leaves}  unresolved edges: {len(unresolved)}  unreachable defs: {len(orphans)}")
    for u in unresolved[:15]:
        print("  unresolved:", u)
    for o in orphans[:15]:
        print("  unreachable:", o)
    if leaves < a.min_commands:
        sys.exit(f"only {leaves} commands found (< {a.min_commands}); the parser probably does not understand this checkout")
    meta = {"ref": a.ref, "commit": a.commit, "generated": int(time.time()), "commands": leaves,
            "unresolved": len(unresolved), "unreachable": len(orphans)}
    with open(a.out, "w") as f:
        json.dump({"meta": meta, "nodes": nodes}, f)


if __name__ == "__main__":
    main()
