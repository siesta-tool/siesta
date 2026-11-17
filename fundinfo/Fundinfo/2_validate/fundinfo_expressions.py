"""Small, allowlisted interpreter for the expressions in the Fundinfo export.

No eval/exec, Python attribute access, or arbitrary function calls are allowed.
Unknown syntax and ambiguous top-level truth conventions remain unevaluated.
"""

from __future__ import annotations

import ast
import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import re


class Unsupported(ValueError):
    pass


@dataclass(frozen=True)
class Unknown:
    reason: str
    # A comparison with an optional, empty cell is not applicable, rather than
    # an implementation/coverage gap. Missing columns are always coverage gaps.
    not_applicable: bool = False


IGNORED = object()
FUNCTIONS = {
    "isempty": 1, "isnotempty": 1, "isset": 1, "isnotset": 1,
    "isequal": 2, "isnotequal": 2, "contains": 2, "isignored": 1,
}
WRAPPERS = {"requiredif", "requiredifnot", "onlyif", "onlyifnot", "onlysetif", "requiredsetif"}
METHODS = {"adddays", "addmonths", "addyears"}
TOKEN = re.compile(
    r'\s*(?:(?P<ref>\[[^\]\r\n]+\])|'
    r'(?P<string>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')|'
    r'(?P<number>\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?)|'
    r'(?P<id>[A-Za-z_]\w*)|(?P<op><=|>=|!=|==|<>|[<>=+*/.,(){}-]))'
)


class Parser:
    PRECEDENCE = {"or": 1, "and": 2, "in": 3, "=": 3, "==": 3, "!=": 3,
                  "<>": 3, "<": 3, "<=": 3, ">": 3, ">=": 3, "+": 4,
                  "-": 4, "*": 5, "/": 5}

    def __init__(self, text):
        self.tokens = []
        position = 0
        text = text.strip()
        while position < len(text):
            match = TOKEN.match(text, position)
            if not match:
                raise Unsupported(f"Unrecognised expression syntax at {text[position:position+40]!r}")
            self.tokens.append((match.lastgroup, match.group(match.lastgroup)))
            position = match.end()
        self.index = 0

    def peek(self):
        return self.tokens[self.index][1].lower() if self.index < len(self.tokens) else ""

    def take(self, expected=None):
        if self.index == len(self.tokens):
            raise Unsupported("Unexpected end of expression")
        token = self.tokens[self.index]
        if expected is not None and token[1].lower() != expected:
            raise Unsupported(f"Expected {expected!r}, found {token[1]!r}")
        self.index += 1
        return token

    def parse(self):
        node = self.expression()
        if self.index != len(self.tokens):
            raise Unsupported(f"Unexpected token {self.tokens[self.index][1]!r}")
        return node

    def expression(self, minimum=0):
        kind, value = self.take()
        if kind == "ref":
            left = ("ref", value[1:-1])
        elif kind == "string":
            left = ("literal", ast.literal_eval(value))
        elif kind == "number":
            left = ("literal", Decimal(value))
        elif value.lower() in ("not", "-", "+"):
            left = ("unary", value.lower(), self.expression(6))
        elif value == "(":
            left = self.expression()
            if self.peek() == ",":
                items = [left]
                while self.peek() == ",":
                    self.take(",")
                    items.append(self.expression())
                left = ("list", tuple(items))
            self.take(")")
        elif kind == "id" and self.peek() == "{":
            name = value.lower()
            if name not in WRAPPERS:
                raise Unsupported(f"Undocumented wrapper semantics: {value}")
            self.take("{")
            left = ("wrapper", name, self.expression())
            self.take("}")
        elif kind == "id" and self.peek() == "(":
            name = value.lower()
            if name not in FUNCTIONS:
                raise Unsupported(f"Unsupported function: {value}")
            self.take("(")
            args = [self.expression()]
            while self.peek() == ",":
                self.take(",")
                args.append(self.expression())
            self.take(")")
            if len(args) != FUNCTIONS[name]:
                raise Unsupported(f"Wrong argument count for {value}")
            left = ("call", name, tuple(args))
        elif value.lower() in ("true", "false"):
            left = ("literal", value.lower() == "true")
        else:
            raise Unsupported(f"Unsupported identifier: {value}")

        while True:
            if self.peek() == ".":
                self.take(".")
                name = self.take()[1].lower()
                if name == "length":
                    left = ("length", left)
                elif name in METHODS:
                    self.take("(")
                    argument = self.expression()
                    self.take(")")
                    left = ("method", name, left, argument)
                else:
                    raise Unsupported(f"Unsupported property/method: {name}")
                continue
            operator = self.peek()
            precedence = self.PRECEDENCE.get(operator, -1)
            if precedence < minimum:
                break
            self.take()
            right = self.expression(precedence + 1)
            left = ("binary", operator, left, right)
        return left


def references(node):
    if node[0] == "ref":
        return {node[1]}
    result = set()
    for child in node[1:]:
        if isinstance(child, tuple):
            if child and isinstance(child[0], str):
                result.update(references(child))
            else:
                for element in child:
                    result.update(references(element))
    return result


def mode_for(node, expression, overrides):
    """Use explicit policy; never guess polarity from free-text error messages."""
    if expression in overrides:
        mode = overrides[expression]
        if mode not in ("valid_when_true", "invalid_when_true"):
            raise Unsupported(f"Invalid expression mode: {mode}")
        return mode

    def validity_tree(tree):
        if tree[0] == "wrapper":
            return True
        if tree[0] == "binary":
            if tree[1] in ("and", "or"):
                return validity_tree(tree[2]) and validity_tree(tree[3])
            if tree[1] in ("<", "<=", ">", ">=", "=", "==", "!=", "<>"):
                return tree[2][0] != "length" and tree[3][0] != "length"
        return False

    if validity_tree(node):
        return "valid_when_true"
    # These conflict patterns are explicit conventions used by the catalogue.
    shape = re.sub(r"\[[^\]]+\]", "[F]", expression)
    shape = re.sub(r"\s+", "", shape).lower()
    conflicts = {
        "isnotset([f])andisnotempty([f])",
        "isset([f])andisempty([f])",
        "isset([f])andisset([f])",
        "isempty([f])andisempty([f])and(isnotempty([f])orisnotempty([f]))",
        "[f].length<10",
    }
    if shape in conflicts:
        return "invalid_when_true"
    if shape == "isempty([f])orisnotequal([f],[f])":
        return "valid_when_true"
    raise Unsupported("Ambiguous expression polarity; requires an explicit expression-mode override")


def add_months(value, count):
    index = value.year * 12 + value.month - 1 + count
    year, month = divmod(index, 12)
    month += 1
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


def bool_value(value):
    if isinstance(value, (bool, Unknown)):
        return value
    return Unknown("Expression did not produce a Boolean")


def combine(left, right, operator):
    left, right = bool_value(left), bool_value(right)
    if operator == "and":
        if left is False or right is False:
            return False
    else:
        if left is True or right is True:
            return True
    unknowns = [value for value in (left, right) if isinstance(value, Unknown)]
    if unknowns:
        return next((u for u in unknowns if not u.not_applicable), unknowns[0])
    return left and right if operator == "and" else left or right


class Context:
    def __init__(self, values, current_code, case_sensitive=()):
        self.values = values
        self.current = current_code
        self.case_sensitive = set(case_sensitive)

    def get(self, code):
        return self.values.get(code, Unknown(f"Referenced column {code} is absent"))

    def compare(self, left, right, operator, node):
        for value in (left, right):
            if isinstance(value, Unknown):
                return value
            if value is IGNORED:
                return Unknown("Referenced value is [IGNORE]; stored value is unavailable")
        if left is None or right is None:
            return Unknown("Optional comparison has an empty operand", not_applicable=True)
        if isinstance(left, bool) and isinstance(right, str):
            left = "yes" if left else "no"
        if isinstance(right, bool) and isinstance(left, str):
            right = "yes" if right else "no"
        sensitive = bool(references(node) & self.case_sensitive)
        if isinstance(left, str) and isinstance(right, str) and not sensitive:
            left, right = left.casefold(), right.casefold()
        try:
            if operator in ("=", "=="):
                return left == right
            if operator in ("!=", "<>"):
                return left != right
            if operator == "<": return left < right
            if operator == "<=": return left <= right
            if operator == ">": return left > right
            if operator == ">=": return left >= right
        except TypeError:
            return Unknown("Incompatible types in comparison")
        return Unknown(f"Unsupported comparison {operator}")

    def evaluate(self, node):
        kind = node[0]
        if kind == "literal": return node[1]
        if kind == "ref": return self.get(node[1])
        if kind == "list": return [self.evaluate(child) for child in node[1]]
        if kind == "wrapper":
            name, condition = node[1:]
            current = self.get(self.current)
            if isinstance(current, Unknown): return current
            if current is IGNORED: return Unknown("Target value is [IGNORE]")
            if name.startswith("required") and current is not None:
                if name != "requiredsetif" or current is True: return True
            if name.startswith("only") and (current is None or (name == "onlysetif" and current is False)):
                return True
            result = bool_value(self.evaluate(condition))
            if isinstance(result, Unknown): return result
            if name.endswith("not"): result = not result
            if name in ("requiredif", "requiredifnot", "requiredsetif"):
                return not result
            return result
        if kind == "binary":
            operator, a, b = node[1:]
            left = self.evaluate(a)
            if operator == "and" and left is False: return False
            if operator == "or" and left is True: return True
            right = self.evaluate(b)
            if operator in ("and", "or"): return combine(left, right, operator)
            if operator == "in":
                if not isinstance(right, list): right = [right]
                result = False
                for item in right:
                    result = combine(result, self.compare(left, item, "=", node), "or")
                return result
            if operator in ("+", "-", "*", "/"):
                for value in (left, right):
                    if isinstance(value, Unknown): return value
                    if value is IGNORED: return Unknown("Arithmetic operand is [IGNORE]")
                if left is None or right is None:
                    return Unknown("Optional arithmetic has an empty operand", not_applicable=True)
                if not isinstance(left, Decimal) or not isinstance(right, Decimal):
                    return Unknown("Arithmetic requires numeric operands")
                try:
                    if operator == "+": return left + right
                    if operator == "-": return left - right
                    if operator == "*": return left * right
                    return left / right
                except ArithmeticError:
                    return Unknown("Invalid arithmetic operation")
            return self.compare(left, right, operator, node)
        if kind == "unary":
            value = self.evaluate(node[2])
            if isinstance(value, Unknown): return value
            if node[1] == "not":
                value = bool_value(value)
                return value if isinstance(value, Unknown) else not value
            if not isinstance(value, Decimal): return Unknown("Numeric unary operand required")
            return -value if node[1] == "-" else value
        if kind == "length":
            value = self.evaluate(node[1])
            if isinstance(value, Unknown): return value
            if value is None: return Decimal(0)
            if not isinstance(value, str): return Unknown("Length requires a string")
            return Decimal(len(value.encode("utf-16-le", errors="surrogatepass")) // 2)
        if kind == "method":
            value, count = self.evaluate(node[2]), self.evaluate(node[3])
            if isinstance(value, Unknown): return value
            if value is None: return Unknown("Empty date operand", not_applicable=True)
            if not isinstance(value, (date, datetime)) or not isinstance(count, Decimal) or count != int(count):
                return Unknown("Date arithmetic requires a date and an integer")
            try:
                if node[1] == "adddays": return value + timedelta(days=int(count))
                return add_months(value, int(count) * (12 if node[1] == "addyears" else 1))
            except (ValueError, OverflowError): return Unknown("Date arithmetic is out of range")
        if kind == "call":
            name = node[1]
            args = [self.evaluate(child) for child in node[2]]
            for value in args:
                if isinstance(value, Unknown): return value
            if name == "isignored": return args[0] is IGNORED
            if any(value is IGNORED for value in args): return Unknown("Referenced value is [IGNORE]")
            if name == "isempty": return args[0] is None
            if name == "isnotempty": return args[0] is not None
            if name in ("isset", "isnotset"):
                value = args[0]
                if isinstance(value, str) and value.casefold() in ("yes", "no"):
                    value = value.casefold() == "yes"
                if value is not None and not isinstance(value, bool):
                    return Unknown("IsSet/IsNotSet requires a Boolean field")
                return value is True if name == "isset" else value is not True
            if name in ("isequal", "isnotequal"):
                if None in args:
                    equal = args[0] is args[1]
                    return equal if name == "isequal" else not equal
                return self.compare(*args, "=" if name == "isequal" else "!=", node)
            if name == "contains":
                if args[0] is None: return False
                if not all(isinstance(arg, str) for arg in args): return Unknown("Contains requires text")
                a, b = args
                if not (references(node) & self.case_sensitive): a, b = a.casefold(), b.casefold()
                return b in a
        return Unknown("Unsupported expression node")
