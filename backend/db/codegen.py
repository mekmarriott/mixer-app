"""sqlc-style code generator: annotated .sql in, typed Python bindings out.

    python -m backend.db.codegen           # regenerate models.py and queries.py
    python -m backend.db.codegen --check   # fail if they are stale (CI / tests)

sqlc itself targets Go, and its Python plugin needs a Go toolchain plus a
protobuf plugin — too much machinery for this backend. This module keeps the
part that matters: **SQL is written as SQL, in files, and the Python surface is
derived from it rather than hand-maintained.** Same annotation vocabulary,
same guarantees:

  * every query is named and typed (``:one`` / ``:many`` / ``:exec`` /
    ``:scalar``), so the call sites are ordinary typed methods;
  * result rows map onto dataclasses derived from the schema, so a renamed
    column breaks generation instead of silently returning ``None`` at runtime;
  * parameters are checked against the columns of the tables the statement
    touches, so ``:track_id`` cannot be misspelled into a NULL;
  * the generated file is committed and verified in the test suite, so the
    checked-in bindings can never drift from the .sql sources.

Result rows are decoded positionally, which both sqlite3 and psycopg support
without a per-row dict — for ``SELECT *`` the order is the schema's, and for an
explicit select list it is the order given in the ``-- columns:`` annotation.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from . import dialect

SQL_DIR = Path(__file__).parent / "sql"
QUERY_DIR = SQL_DIR / "queries"
MODELS_PY = Path(__file__).parent / "models.py"
QUERIES_PY = Path(__file__).parent / "queries.py"

KINDS = (":one", ":many", ":exec", ":scalar")


class CodegenError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\n\);",
    re.IGNORECASE | re.DOTALL)
_NON_COLUMN = re.compile(
    r"^\s*(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT)\b", re.IGNORECASE)


def parse_schema(schema_sql):
    """-> {table: [(column, canonical_type), ...]} in declaration order."""
    tables = {}
    for name, body in _TABLE_RE.findall(schema_sql):
        columns = []
        for line in body.splitlines():
            line = line.split("--")[0].strip().rstrip(",")
            if not line or _NON_COLUMN.match(line):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            column, type_token = parts[0], parts[1].upper()
            if type_token not in dialect.TYPES:
                raise CodegenError(
                    "%s.%s uses %r, which is not a canonical type (%s)"
                    % (name, column, parts[1], ", ".join(sorted(dialect.TYPES))))
            columns.append((column, type_token))
        tables[name] = columns
    if not tables:
        raise CodegenError("no CREATE TABLE statements found in schema.sql")
    return tables


_NAME_RE = re.compile(r"^--\s*name:\s*(\w+)\s+(:\w+)\s*$", re.IGNORECASE)
_COLUMNS_RE = re.compile(r"^--\s*columns:\s*(.+)$", re.IGNORECASE)


def parse_queries(text, source):
    """Split an annotated .sql file into query descriptors.

    A ``-- columns:`` annotation may wrap onto following comment lines; the
    continuation ends at the first non-comment line or the next annotation.
    """
    queries, current, columns_spec = [], None, None

    def close_columns(lineno):
        nonlocal columns_spec
        if columns_spec is not None:
            current["columns"] = _parse_columns(columns_spec, source, lineno)
            columns_spec = None

    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        name_match = _NAME_RE.match(stripped)
        if name_match:
            if current:
                close_columns(lineno)
                queries.append(current)
            name, kind = name_match.group(1), name_match.group(2).lower()
            if kind not in KINDS:
                raise CodegenError("%s:%d: unknown query kind %r (expected %s)"
                                   % (source, lineno, kind, " ".join(KINDS)))
            current = {"name": name, "kind": kind, "columns": None,
                       "body": [], "source": source, "line": lineno}
            continue
        if current is None:
            continue
        columns_match = _COLUMNS_RE.match(stripped)
        if columns_match:
            close_columns(lineno)
            columns_spec = columns_match.group(1)
            continue
        if stripped.startswith("--"):
            if columns_spec is not None:
                columns_spec += " " + stripped[2:]
            continue
        close_columns(lineno)
        current["body"].append(line)
    if current:
        close_columns(len(text.splitlines()))
        queries.append(current)

    for q in queries:
        body = "\n".join(q["body"]).strip()
        if not body:
            raise CodegenError("%s: query %s has no statement" % (source, q["name"]))
        q["sql"] = body.rstrip(";").strip()
    return queries


def _parse_columns(spec, source, lineno):
    columns = []
    for item in spec.split(","):
        parts = item.split()
        if len(parts) != 2:
            raise CodegenError("%s:%d: expected '<name> <TYPE>', got %r"
                               % (source, lineno, item.strip()))
        column, type_token = parts[0], parts[1].upper()
        if type_token not in dialect.TYPES:
            raise CodegenError("%s:%d: %r is not a canonical type"
                               % (source, lineno, parts[1]))
        columns.append((column, type_token))
    return columns


# ---------------------------------------------------------------------------
# Type inference
# ---------------------------------------------------------------------------

_REFERENCED_RE = re.compile(r"\b(?:FROM|INTO|JOIN|UPDATE)\s+(\w+)", re.IGNORECASE)


def referenced_tables(sql, tables):
    """Tables the statement touches, in appearance order. Non-table matches
    (``DO UPDATE SET``) fall out because they are not in the schema."""
    seen = []
    for name in _REFERENCED_RE.findall(sql):
        if name in tables and name not in seen:
            seen.append(name)
    return seen


def infer_param_types(query, tables):
    """Map each ``:param`` to a canonical type via the column of the same name."""
    referenced = referenced_tables(query["sql"], tables)
    if not referenced:
        raise CodegenError("%s: query %s references no known table"
                           % (query["source"], query["name"]))
    types = {}
    for param in dialect.param_names(query["sql"]):
        found = {t: dict(tables[t])[param] for t in referenced
                 if param in dict(tables[t])}
        if not found:
            raise CodegenError(
                "%s: query %s takes :%s, but no column of that name exists on %s"
                % (query["source"], query["name"], param, "/".join(referenced)))
        distinct = set(found.values())
        if len(distinct) > 1:
            raise CodegenError(
                "%s: query %s takes :%s, which is ambiguous across %s"
                % (query["source"], query["name"], param, found))
        types[param] = distinct.pop()
    return types


def infer_result(query, tables):
    """-> (model_name, [(column, canonical)]) or (None, None) for :exec."""
    if query["kind"] == ":exec":
        return None, None
    if query["columns"]:
        columns = query["columns"]
        if query["kind"] == ":scalar":
            if len(columns) != 1:
                raise CodegenError("%s: :scalar query %s must select one column"
                                   % (query["source"], query["name"]))
            return None, columns
        return query["name"] + "Row", columns
    if not re.search(r"SELECT\s+\*", query["sql"], re.IGNORECASE):
        raise CodegenError(
            "%s: query %s selects specific columns, so it needs a "
            "'-- columns: <name> <TYPE>, ...' annotation"
            % (query["source"], query["name"]))
    referenced = referenced_tables(query["sql"], tables)
    table = referenced[0]
    return _model_name(table), tables[table]


def _model_name(table):
    """tracks -> Track, latency -> Latency (singularise a trailing 's')."""
    stem = table[:-1] if table.endswith("s") else table
    return "".join(part.capitalize() for part in stem.split("_"))


def _snake(name):
    """GetTrack -> get_track"""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

HEADER = ('"""%s\n\nGENERATED by backend/db/codegen.py from backend/db/sql/. '
          "Do not edit by hand:\nchange the .sql files and re-run "
          '`python -m backend.db.codegen`.\n"""\n')


def _wrap(items, indent, width):
    """Pack `items` onto as few `indent`-prefixed lines as fit in `width`."""
    lines, current = [], indent
    for item in items:
        candidate = current + (" " if current.strip() else "") + item
        if len(candidate) > width and current.strip():
            lines.append(current)
            current = indent + item
        else:
            current = candidate
    if current.strip():
        lines.append(current)
    return lines


def _emit_dataclass(name, columns, doc):
    lines = ["@dataclasses.dataclass(frozen=True)",
             "class %s:" % name,
             '    """%s"""' % doc, ""]
    lines += ["    %s: %s" % (c, dialect.python_type(t)) for c, t in columns]
    pairs = ['("%s", "%s"),' % ct for ct in columns]
    lines += ["", "    _FIELDS = ("]
    lines += _wrap(pairs, indent=" " * 8, width=88)
    lines += [
        "    )",
        "",
        "    @classmethod",
        "    def _from_row(cls, row):",
        "        return cls(*(decode(row[i], t)",
        "                     for i, (_, t) in enumerate(cls._FIELDS)))",
        "",
        "",
    ]
    return lines


def generate_models(tables, queries):
    out = [HEADER % "Row types for the catalog schema.",
           "from __future__ import annotations", "",
           "import dataclasses", "from typing import Any  # noqa: F401", "",
           "from .dialect import decode", "", ""]
    for table, columns in tables.items():
        out += _emit_dataclass(_model_name(table), columns,
                               "One row of the `%s` table." % table)
    for q in queries:
        model, columns = infer_result(q, tables)
        if model and q["columns"]:
            out += _emit_dataclass(model, columns,
                                   "Result row of the %s query." % q["name"])
    out.append("__all__ = [%s]" % ", ".join(
        '"%s"' % n for n in _model_names(tables, queries)))
    return "\n".join(out) + "\n"


def _model_names(tables, queries):
    names = [_model_name(t) for t in tables]
    for q in queries:
        model, _ = infer_result(q, tables)
        if model and q["columns"]:
            names.append(model)
    return names


def generate_queries(tables, queries):
    out = [HEADER % ("Typed query bindings.\n\n"
                     "Each method runs one statement from backend/db/sql/queries/, "
                     "with parameters\nencoded and result rows decoded according "
                     "to the canonical schema types."),
           "from __future__ import annotations", "",
           "from . import models", "from .dialect import decode, encode, render_query",
           "", "",
           "#: Canonical SQL, keyed by query name. Rendered per dialect on first use.",
           "SQL = {"]
    for q in queries:
        out.append('    "%s": """%s""",' % (q["name"], q["sql"]))
    out += ["}", "", "_RENDERED = {}", "", "",
            "def sql_for(dialect_name):",
            '    """Every statement rendered for `dialect_name` (cached)."""',
            "    if dialect_name not in _RENDERED:",
            "        _RENDERED[dialect_name] = {",
            "            name: render_query(text, dialect_name)",
            "            for name, text in SQL.items()}",
            "    return _RENDERED[dialect_name]", "", "",
            "class Queries:",
            '    """Statement executor bound to one connection."""', "",
            "    def __init__(self, conn, dialect_name):",
            "        self._conn = conn",
            "        self._dialect = dialect_name",
            "        self._sql = sql_for(dialect_name)", "",
            "    def _execute(self, name, params):",
            "        cur = self._conn.cursor()",
            "        cur.execute(self._sql[name], params)",
            "        return cur", ""]

    for q in queries:
        out += _emit_method(q, tables)

    out.append('__all__ = ["Queries", "SQL", "sql_for"]')
    return "\n".join(out) + "\n"


def _emit_method(query, tables):
    param_types = infer_param_types(query, tables)
    params = list(param_types)
    model, columns = infer_result(query, tables)
    name, kind = _snake(query["name"]), query["kind"]

    signature = ", ".join(["self"] + params)
    doc = {":one": "-> %s | None" % model, ":many": "-> list[%s]" % model,
           ":exec": "-> None", ":scalar": "-> %s | None" % (
               dialect.python_type(columns[0][1]) if columns else "Any")}[kind]

    lines = ["    def %s(%s):" % (name, signature),
             '        """`%s` (%s) %s"""' % (query["name"], kind, doc)]
    if params:
        lines.append("        params = {")
        lines += ['            "%s": encode(%s, "%s", self._dialect),'
                  % (p, p, param_types[p]) for p in params]
        lines.append("        }")
    else:
        lines.append("        params = {}")
    lines.append('        cur = self._execute("%s", params)' % query["name"])

    if kind == ":exec":
        lines += ["        cur.close()", ""]
    elif kind == ":one":
        lines += ["        row = cur.fetchone()",
                  "        cur.close()",
                  "        return None if row is None else models.%s._from_row(row)"
                  % model, ""]
    elif kind == ":many":
        lines += ["        rows = cur.fetchall()",
                  "        cur.close()",
                  "        return [models.%s._from_row(r) for r in rows]" % model, ""]
    else:                                                       # :scalar
        lines += ["        row = cur.fetchone()",
                  "        cur.close()",
                  '        return None if row is None else decode(row[0], "%s")'
                  % columns[0][1], ""]
    return lines


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def load():
    tables = parse_schema((SQL_DIR / "schema.sql").read_text())
    queries = []
    for path in sorted(QUERY_DIR.glob("*.sql")):
        queries += parse_queries(path.read_text(), path.name)
    names = [q["name"] for q in queries]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise CodegenError("duplicate query names: %s" % ", ".join(sorted(duplicates)))
    for q in queries:                       # validate everything before emitting
        infer_param_types(q, tables)
        infer_result(q, tables)
    return tables, queries


def render():
    tables, queries = load()
    return {MODELS_PY: generate_models(tables, queries),
            QUERIES_PY: generate_queries(tables, queries)}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    check = "--check" in argv
    stale = []
    for path, text in render().items():
        if check:
            current = path.read_text() if path.exists() else None
            if current != text:
                stale.append(path.name)
        else:
            path.write_text(text)
            print("wrote", path)
    if check and stale:
        print("stale generated files: %s\nrun: python -m backend.db.codegen"
              % ", ".join(stale), file=sys.stderr)
        return 1
    if check:
        print("generated bindings are up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
