"""Bounded, read-only tools for the three study workloads."""

from __future__ import annotations

import ast
import csv
import io
import math
import operator
import sqlite3

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}


def calculate(expression):
    if not isinstance(expression, str) or not 1 <= len(expression) <= 500:
        raise ValueError("calculator requires 1..500 characters")
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 100:
        raise ValueError("expression exceeds 100 AST nodes")

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            value = node.value
        elif isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            value = _BINARY[type(node.op)](visit(node.left), visit(node.right))
        elif isinstance(node, ast.UnaryOp) and type(node.op) in {ast.UAdd, ast.USub}:
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        else:
            raise ValueError("only numeric literals and + - * / // % are supported")
        if abs(value) > 1e15 or not math.isfinite(value):
            raise ValueError("calculator result exceeds its numeric bound")
        return value

    return visit(tree.body)


class RepositoryTools:
    def __init__(self, source):
        self.source = dict(source)

    def search(self, query):
        if not isinstance(query, str) or not 1 <= len(query) <= 150:
            raise ValueError("search requires 1..150 characters")
        found = []
        for path, body in sorted(self.source.items()):
            if not path.endswith(".py"):
                continue
            for line_number, line in enumerate(body.splitlines(), 1):
                if query.lower() in line.lower():
                    found.append({"path": path, "line": line_number, "text": line[:240]})
                    if len(found) == 12:
                        return found
        return found

    def read(self, request):
        if not isinstance(request, dict) or set(request) != {"path", "start", "count"}:
            raise ValueError("read requires path, start and count")
        path, start, count = request["path"], request["start"], request["count"]
        if (
            path not in self.source
            or type(start) is not int
            or type(count) is not int
            or start < 1
            or not 1 <= count <= 60
        ):
            raise ValueError("read requires an allowed path and 1..60 lines")
        lines = self.source[path].splitlines()[start - 1 : start - 1 + count]
        return [{"line": start + index, "text": line[:240]} for index, line in enumerate(lines)]


class WineDatabase:
    def __init__(self, red_csv, white_csv):
        self.connection = sqlite3.connect(":memory:")
        columns = [
            name.replace(" ", "_").lower()
            for name in next(csv.reader(io.StringIO(red_csv), delimiter=";"))
        ]
        if columns != [
            "fixed_acidity",
            "volatile_acidity",
            "citric_acid",
            "residual_sugar",
            "chlorides",
            "free_sulfur_dioxide",
            "total_sulfur_dioxide",
            "density",
            "ph",
            "sulphates",
            "alcohol",
            "quality",
        ]:
            raise ValueError("unexpected frozen wine dataset schema")
        # Names are checked against the exact schema above, never model SQL.
        self.connection.execute(
            "CREATE TABLE wines (color TEXT, " + ", ".join(name + " REAL" for name in columns) + ")"
        )
        for color, body in (("red", red_csv), ("white", white_csv)):
            rows = csv.reader(io.StringIO(body), delimiter=";")
            next(rows)
            self.connection.executemany(
                "INSERT INTO wines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ((color, *map(float, row)) for row in rows),
            )
        self.connection.commit()
        self.connection.execute("PRAGMA query_only = ON")
        self.connection.set_authorizer(self._authorize)
        self.columns = ["color", *columns]

    @staticmethod
    def _authorize(action, first, second, database, source):
        if action == sqlite3.SQLITE_FUNCTION:
            functions = {
                "avg",
                "sum",
                "count",
                "min",
                "max",
                "round",
                "abs",
                "coalesce",
                "ifnull",
                "total",
                "length",
                "lower",
                "upper",
            }
            return sqlite3.SQLITE_OK if str(second).lower() in functions else sqlite3.SQLITE_DENY
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    def query(self, sql):
        if not isinstance(sql, str) or not 1 <= len(sql) <= 1500:
            raise ValueError("SQL requires 1..1500 characters")
        ticks = 0

        def progress():
            nonlocal ticks
            ticks += 1
            return ticks > 1000

        self.connection.set_progress_handler(progress, 1000)
        try:
            cursor = self.connection.execute(sql)
            rows = cursor.fetchmany(51)
            if len(rows) > 50:
                raise ValueError("query exceeds 50 output rows")
            return {
                "columns": [item[0] for item in cursor.description],
                "rows": [list(row) for row in rows],
            }
        finally:
            self.connection.set_progress_handler(None, 0)

    def close(self):
        self.connection.close()
