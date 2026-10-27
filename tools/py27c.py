#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: 0BSD
"""
py27c.py -- compile a restricted subset of Python to REAL Python 2.7 bytecode
            (.pyc, magic 03 F3 0D 0A) while running on Python 3.10+.

Why this exists
---------------
The "Mir Tankov" (Lesta, BigWorld engine) client embeds Python 2.7.  Its mod
loader imports only files named
    scripts/client/gui/mods/mod_*.pyc
on retail builds (a plain .py entry point is read by development builds only),
so a mod entry point MUST be Python 2.7 bytecode.  If Python 2.7 is not
installed on the build machine there is no stock way to produce that file --
this module is that way.

Provenance: an original implementation (AST visitor, assembler, marshal
writer).  The bytecode and .pyc formats it targets follow CPython 2.7
(c) Python Software Foundation, PSF-2.0; no CPython source is copied.

Scope (deliberately small, see UNSUPPORTED below)
-------------------------------------------------
Enough to compile a loader stub / small module:
    imports (import a.b, from x import y, from x import *)
    assignments (name / attribute / subscript / tuple-unpack), augmented assign
    del, global, pass, return, raise
    def (positional args, defaults, *args, **kwargs, decorators), lambda
    class (old/new style, bases)
    if/elif/else, while, for, break, continue, try/except/else/finally
    calls incl. f(*a, **k), attributes, subscripts, slices
    literals: str/unicode/bytes/int/long/float/bool/None, tuple/list/dict
    operators: arithmetic, %, comparisons, is/in, and/or/not, conditional expr
    print(...)  ->  compiled to the Python 2 PRINT_ITEM/PRINT_NEWLINE statement

UNSUPPORTED (raises Py27CompileError with the source line):
    closures / nested functions using enclosing locals, comprehensions,
    generators (yield), with-statements, f-strings, keyword-only args,
    annotations, matrix-multiply, starred assignment targets, async,
    print(a, b) with more than one operand (see below).

THE OTHER HALF OF THE RESTRICTION -- the source is read with `ast.parse`, i.e.
the PYTHON 3 parser, so syntax that exists ONLY in Python 2 cannot get in:
    123L         ->  write 0xFFFFFFFF / int(...)
    print x      ->  write print(x)
    except E, e: ->  write except E as e:
    raise E, v, tb / `x` (backtick repr) / 0777 / <> / ur''
Those raise Py27CompileError with that explanation, not a bare SyntaxError.
In short: a file compiled by py27c must sit in the py2 AND py3 INTERSECTION.
The mod body does not have this restriction -- it ships as plain .py and is
compiled by the game itself; only `--compile-all` drags it through py27c.

print() DIVERGENCE, deliberately refused: a real Python 2.7 (no
`from __future__ import print_function`) reads `print(a, b)` as the print
STATEMENT with ONE tuple operand and prints "(a, b)".  Compiling it here to two
PRINT_ITEMs would print "a b", so the same source would behave differently
depending on which backend build.py picked.  py27c raises instead.
Single-argument `print(x)` is identical under both and is allowed.

String literals
---------------
'text'  and  b'text'  -> Python 2 `str` (raw UTF-8 bytes, matching what
                         Python 2.7 does for a `# -*- coding: utf-8 -*-` file)
u'text'                -> Python 2 `unicode`
The u-prefix is recovered from the token stream, so it survives the Python 3
parser (which erases it).

Verification
------------
    python tools/py27c.py --self-test
compiles the built-in fixtures and decompiles them back with uncompyle6 if
.tools/venv_uncompyle is present (see README, "Сторонние инструменты").
    python tools/py27c.py --check-opmap
compares the embedded opcode table against xdis (re-runs itself with the
.tools/venv_uncompyle interpreter when the current one has no xdis).
"""

from __future__ import annotations

import argparse
import ast
import io
import os
import struct
import sys
import time
import tokenize

__all__ = ["Py27CompileError", "compile_source", "compile_file", "PY27_MAGIC"]

PY27_MAGIC = b"\x03\xf3\x0d\x0a"

# --------------------------------------------------------------------------- #
# Python 2.7 opcode numbers, cmp_op, CO_* flags and marshal type codes are
# interface facts of CPython 2.7 (Lib/opcode.py, Include/code.h,
# Python/marshal.c; CPython is PSF-2.0).  No CPython source is copied.
# Embedded on purpose: build.py must run on a bare system Python 3 with no
# third-party packages installed.  xdis is used only as an optional external
# cross-check (--check-opmap), not as a source.
# --------------------------------------------------------------------------- #

OPMAP = {
    "STOP_CODE": 0, "POP_TOP": 1, "ROT_TWO": 2, "ROT_THREE": 3, "DUP_TOP": 4,
    "ROT_FOUR": 5, "NOP": 9, "UNARY_POSITIVE": 10, "UNARY_NEGATIVE": 11,
    "UNARY_NOT": 12, "UNARY_CONVERT": 13, "UNARY_INVERT": 15,
    "BINARY_POWER": 19, "BINARY_MULTIPLY": 20, "BINARY_DIVIDE": 21,
    "BINARY_MODULO": 22, "BINARY_ADD": 23, "BINARY_SUBTRACT": 24,
    "BINARY_SUBSCR": 25, "BINARY_FLOOR_DIVIDE": 26, "BINARY_TRUE_DIVIDE": 27,
    "INPLACE_FLOOR_DIVIDE": 28, "INPLACE_TRUE_DIVIDE": 29,
    "SLICE_0": 30, "SLICE_1": 31, "SLICE_2": 32, "SLICE_3": 33,
    "STORE_SLICE_0": 40, "STORE_SLICE_1": 41, "STORE_SLICE_2": 42,
    "STORE_SLICE_3": 43, "DELETE_SLICE_0": 50, "DELETE_SLICE_1": 51,
    "DELETE_SLICE_2": 52, "DELETE_SLICE_3": 53, "STORE_MAP": 54,
    "INPLACE_ADD": 55, "INPLACE_SUBTRACT": 56, "INPLACE_MULTIPLY": 57,
    "INPLACE_DIVIDE": 58, "INPLACE_MODULO": 59, "STORE_SUBSCR": 60,
    "DELETE_SUBSCR": 61, "BINARY_LSHIFT": 62, "BINARY_RSHIFT": 63,
    "BINARY_AND": 64, "BINARY_XOR": 65, "BINARY_OR": 66, "INPLACE_POWER": 67,
    "GET_ITER": 68, "PRINT_EXPR": 70, "PRINT_ITEM": 71, "PRINT_NEWLINE": 72,
    "PRINT_ITEM_TO": 73, "PRINT_NEWLINE_TO": 74, "INPLACE_LSHIFT": 75,
    "INPLACE_RSHIFT": 76, "INPLACE_AND": 77, "INPLACE_XOR": 78,
    "INPLACE_OR": 79, "BREAK_LOOP": 80, "WITH_CLEANUP": 81, "LOAD_LOCALS": 82,
    "RETURN_VALUE": 83, "IMPORT_STAR": 84, "EXEC_STMT": 85, "YIELD_VALUE": 86,
    "POP_BLOCK": 87, "END_FINALLY": 88, "BUILD_CLASS": 89, "STORE_NAME": 90,
    "DELETE_NAME": 91, "UNPACK_SEQUENCE": 92, "FOR_ITER": 93,
    "LIST_APPEND": 94, "STORE_ATTR": 95, "DELETE_ATTR": 96,
    "STORE_GLOBAL": 97, "DELETE_GLOBAL": 98, "DUP_TOPX": 99, "LOAD_CONST": 100,
    "LOAD_NAME": 101, "BUILD_TUPLE": 102, "BUILD_LIST": 103, "BUILD_SET": 104,
    "BUILD_MAP": 105, "LOAD_ATTR": 106, "COMPARE_OP": 107, "IMPORT_NAME": 108,
    "IMPORT_FROM": 109, "JUMP_FORWARD": 110, "JUMP_IF_FALSE_OR_POP": 111,
    "JUMP_IF_TRUE_OR_POP": 112, "JUMP_ABSOLUTE": 113, "POP_JUMP_IF_FALSE": 114,
    "POP_JUMP_IF_TRUE": 115, "LOAD_GLOBAL": 116, "CONTINUE_LOOP": 119,
    "SETUP_LOOP": 120, "SETUP_EXCEPT": 121, "SETUP_FINALLY": 122,
    "LOAD_FAST": 124, "STORE_FAST": 125, "DELETE_FAST": 126,
    "RAISE_VARARGS": 130, "CALL_FUNCTION": 131, "MAKE_FUNCTION": 132,
    "BUILD_SLICE": 133, "MAKE_CLOSURE": 134, "LOAD_CLOSURE": 135,
    "LOAD_DEREF": 136, "STORE_DEREF": 137, "CALL_FUNCTION_VAR": 140,
    "CALL_FUNCTION_KW": 141, "CALL_FUNCTION_VAR_KW": 142, "SETUP_WITH": 143,
    "EXTENDED_ARG": 145, "SET_ADD": 146, "MAP_ADD": 147,
}

HAVE_ARGUMENT = 90
HASJREL = frozenset((93, 110, 120, 121, 122, 143))
HASJABS = frozenset((111, 112, 113, 114, 115, 119))

# co_flags
CO_OPTIMIZED = 0x0001
CO_NEWLOCALS = 0x0002
CO_VARARGS = 0x0004
CO_VARKEYWORDS = 0x0008
CO_NESTED = 0x0010
CO_GENERATOR = 0x0020
CO_NOFREE = 0x0040

# COMPARE_OP argument table (Python 2.7 opcode.cmp_op)
CMP_OP = ("<", "<=", "==", "!=", ">", ">=", "in", "not in", "is", "is not",
          "exception match", "BAD")


class Py27CompileError(Exception):
    """Raised for anything this restricted compiler cannot express."""


# --------------------------------------------------------------------------- #
# Python-2 flavoured constant wrappers
# --------------------------------------------------------------------------- #

class Py2Str(bytes):
    """A Python 2 `str` (8-bit).  Marshalled as TYPE_STRING / TYPE_INTERNED."""
    __slots__ = ()


class Py2Unicode(str):
    """A Python 2 `unicode`.  Marshalled as TYPE_UNICODE."""
    __slots__ = ()


class Py2Long(int):
    """A Python 2 `long`.  Marshalled as TYPE_LONG."""
    __slots__ = ()


class CodeObject:
    """A Python 2.7 code object, as plain data."""

    __slots__ = ("argcount", "nlocals", "stacksize", "flags", "code", "consts",
                 "names", "varnames", "freevars", "cellvars", "filename",
                 "name", "firstlineno", "lnotab")

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw[slot])


# --------------------------------------------------------------------------- #
# marshal writer -- Python 2.7 format (marshal.c, version 2 subset)
# --------------------------------------------------------------------------- #

TYPE_NULL = b"0"
TYPE_NONE = b"N"
TYPE_FALSE = b"F"
TYPE_TRUE = b"T"
TYPE_STOPITER = b"S"
TYPE_ELLIPSIS = b"."
TYPE_INT = b"i"
TYPE_INT64 = b"I"
TYPE_FLOAT = b"f"
TYPE_BINARY_FLOAT = b"g"
TYPE_LONG = b"l"
TYPE_STRING = b"s"
TYPE_INTERNED = b"t"
TYPE_STRINGREF = b"R"
TYPE_TUPLE = b"("
TYPE_LIST = b"["
TYPE_DICT = b"{"
TYPE_CODE = b"c"
TYPE_UNICODE = b"u"


class _Marshaller:
    """Serialise a CodeObject tree into Python 2.7 marshal bytes."""

    def __init__(self) -> None:
        self.buf = bytearray()

    # -- primitives ------------------------------------------------------- #
    def _i4(self, value: int) -> None:
        self.buf += struct.pack("<i", value)

    def _bytes(self, data: bytes) -> None:
        self._i4(len(data))
        self.buf += data

    # -- dispatcher ------------------------------------------------------- #
    def dump(self, obj, interned: bool = False) -> None:
        if obj is None:
            self.buf += TYPE_NONE
        elif obj is True:
            self.buf += TYPE_TRUE
        elif obj is False:
            self.buf += TYPE_FALSE
        elif isinstance(obj, CodeObject):
            self._code(obj)
        elif isinstance(obj, Py2Str):
            self.buf += TYPE_INTERNED if interned else TYPE_STRING
            self._bytes(bytes(obj))
        elif isinstance(obj, Py2Unicode):
            self.buf += TYPE_UNICODE
            self._bytes(str(obj).encode("utf-8"))
        elif isinstance(obj, Py2Long):
            self._long(int(obj))
        elif isinstance(obj, bytes):
            self.buf += TYPE_INTERNED if interned else TYPE_STRING
            self._bytes(obj)
        elif isinstance(obj, str):
            # bare python3 str reaching here == identifier -> python2 str
            self.buf += TYPE_INTERNED if interned else TYPE_STRING
            self._bytes(obj.encode("utf-8"))
        elif isinstance(obj, int):
            if -0x80000000 <= obj <= 0x7FFFFFFF:
                self.buf += TYPE_INT
                self._i4(obj)
            else:
                self._long(obj)
        elif isinstance(obj, float):
            self.buf += TYPE_BINARY_FLOAT
            self.buf += struct.pack("<d", obj)
        elif isinstance(obj, tuple):
            self.buf += TYPE_TUPLE
            self._i4(len(obj))
            for item in obj:
                self.dump(item)
        elif isinstance(obj, list):
            self.buf += TYPE_LIST
            self._i4(len(obj))
            for item in obj:
                self.dump(item)
        elif isinstance(obj, dict):
            self.buf += TYPE_DICT
            for key, value in obj.items():
                self.dump(key)
                self.dump(value)
            self.buf += TYPE_NULL
        else:
            raise Py27CompileError("cannot marshal %r" % (type(obj).__name__,))

    def _long(self, value: int) -> None:
        # Python 2 long: sign*ndigits (int32) then base-2**15 digits, LE.
        self.buf += TYPE_LONG
        if value == 0:
            self._i4(0)
            return
        sign = -1 if value < 0 else 1
        value = abs(value)
        digits = []
        while value:
            digits.append(value & 0x7FFF)
            value >>= 15
        self._i4(sign * len(digits))
        for digit in digits:
            self.buf += struct.pack("<H", digit)

    def _code(self, co: CodeObject) -> None:
        self.buf += TYPE_CODE
        self._i4(co.argcount)
        self._i4(co.nlocals)
        self._i4(co.stacksize)
        self._i4(co.flags)
        self.dump(Py2Str(co.code))
        self.dump(co.consts)
        self._name_tuple(co.names)
        self._name_tuple(co.varnames)
        self._name_tuple(co.freevars)
        self._name_tuple(co.cellvars)
        self.dump(Py2Str(co.filename.encode("utf-8")))
        self.dump(Py2Str(co.name.encode("utf-8")), interned=True)
        self._i4(co.firstlineno)
        self.dump(Py2Str(co.lnotab))

    def _name_tuple(self, names) -> None:
        """co_names / co_varnames entries are interned in a genuine .pyc."""
        self.buf += TYPE_TUPLE
        self._i4(len(names))
        for name in names:
            self.dump(Py2Str(name.encode("utf-8")), interned=True)


def _marshal_code(co: CodeObject) -> bytes:
    m = _Marshaller()
    m.dump(co)
    return bytes(m.buf)


# --------------------------------------------------------------------------- #
# assembler
# --------------------------------------------------------------------------- #

class Label:
    __slots__ = ("offset",)

    def __init__(self) -> None:
        self.offset = None


class Instr:
    __slots__ = ("op", "arg", "lineno", "offset")

    def __init__(self, op: int, arg, lineno: int) -> None:
        self.op = op
        self.arg = arg
        self.lineno = lineno
        self.offset = 0

    @property
    def size(self) -> int:
        return 3 if self.op >= HAVE_ARGUMENT else 1


def _stack_effect(op: int, arg, jump: bool = False) -> int:
    """Stack delta for the opcodes this compiler emits (the Python 2.7 semantics)."""
    name = _OPNAME[op]
    if name in ("POP_TOP", "STORE_NAME", "STORE_FAST", "STORE_GLOBAL",
                "IMPORT_STAR", "PRINT_ITEM", "RETURN_VALUE", "LIST_APPEND",
                "POP_JUMP_IF_FALSE", "POP_JUMP_IF_TRUE", "END_FINALLY",
                "STORE_DEREF"):
        return -1
    if name in ("ROT_TWO", "ROT_THREE", "ROT_FOUR", "NOP", "POP_BLOCK",
                "DELETE_NAME", "DELETE_FAST", "DELETE_GLOBAL", "GET_ITER",
                "LOAD_ATTR", "JUMP_FORWARD", "JUMP_ABSOLUTE", "PRINT_NEWLINE",
                "BREAK_LOOP", "SETUP_LOOP", "SETUP_EXCEPT", "SETUP_FINALLY",
                "CONTINUE_LOOP", "STOP_CODE") or name.startswith("UNARY_"):
        return 0
    if name in ("DUP_TOP", "LOAD_CONST", "LOAD_NAME", "LOAD_GLOBAL",
                "LOAD_FAST", "LOAD_CLOSURE", "LOAD_DEREF", "IMPORT_FROM",
                "LOAD_LOCALS"):
        return 1
    if name.startswith("BINARY_") or name.startswith("INPLACE_"):
        return -1
    if name in ("COMPARE_OP", "IMPORT_NAME"):
        return -1
    if name == "DELETE_SUBSCR":
        # Pops BOTH the subscript and the container (the same effect CPython
        # 2.7 assigns to this opcode), so it is not part of the -1 group above.
        return -2
    if name == "STORE_SUBSCR":
        return -3
    if name == "STORE_ATTR":
        return -2
    if name == "DELETE_ATTR":
        return -1
    if name == "STORE_MAP":
        return -2
    if name == "BUILD_CLASS":
        return -2
    if name == "EXEC_STMT":
        return -3
    if name == "DUP_TOPX":
        return arg
    if name == "UNPACK_SEQUENCE":
        return arg - 1
    if name in ("BUILD_TUPLE", "BUILD_LIST", "BUILD_SET"):
        return 1 - arg
    if name == "BUILD_MAP":
        return 1
    if name == "BUILD_SLICE":
        return 1 - arg
    if name == "RAISE_VARARGS":
        return -arg
    if name == "MAKE_FUNCTION":
        return -arg
    if name == "MAKE_CLOSURE":
        return -arg - 1
    if name in ("JUMP_IF_TRUE_OR_POP", "JUMP_IF_FALSE_OR_POP"):
        return 0 if jump else -1
    if name == "FOR_ITER":
        return -1 if jump else 1
    if name.startswith("CALL_FUNCTION"):
        npos = arg & 0xFF
        nkw = (arg >> 8) & 0xFF
        extra = {"CALL_FUNCTION": 0, "CALL_FUNCTION_VAR": 1,
                 "CALL_FUNCTION_KW": 1, "CALL_FUNCTION_VAR_KW": 2}[name]
        return -(npos + 2 * nkw + extra)
    raise Py27CompileError("no stack effect for opcode %s" % name)


_OPNAME = {v: k for k, v in OPMAP.items()}


def _assemble(instrs, firstlineno: int):
    """Resolve labels, emit bytecode + lnotab, compute stacksize."""
    # Labels are recorded as sentinel entries in the stream.
    flat = [i for i in instrs if isinstance(i, Instr)]
    # 1st pass: assign offsets (no EXTENDED_ARG -- all our args are < 65536)
    offset = 0
    for item in instrs:
        if isinstance(item, Label):
            item.offset = offset
        else:
            item.offset = offset
            offset += item.size

    # 2nd pass: emit
    code = bytearray()
    lnotab = bytearray()
    prev_line = firstlineno
    prev_off = 0
    for ins in flat:
        if ins.lineno is not None and ins.lineno != prev_line:
            dline = ins.lineno - prev_line
            doff = ins.offset - prev_off
            if dline > 0:
                while doff > 255:
                    lnotab += bytes((255, 0))
                    doff -= 255
                while dline > 255:
                    lnotab += bytes((doff, 255))
                    doff = 0
                    dline -= 255
                lnotab += bytes((doff, dline))
                prev_line = ins.lineno
                prev_off = ins.offset
        code.append(ins.op)
        if ins.op >= HAVE_ARGUMENT:
            arg = ins.arg
            if isinstance(arg, Label):
                if ins.op in HASJABS:
                    arg = arg.offset
                else:
                    arg = arg.offset - (ins.offset + 3)
            if not 0 <= arg <= 0xFFFF:
                raise Py27CompileError(
                    "opcode argument out of range for %s: %r "
                    "(module too large for py27c)" % (_OPNAME[ins.op], arg))
            code += struct.pack("<H", arg)

    stacksize = _stack_depth(flat)
    return bytes(code), bytes(lnotab), stacksize


def _stack_depth(flat) -> int:
    """Depth-first walk of the CFG, same idea as CPython 2.7 stackdepth()."""
    if not flat:
        return 0
    by_offset = {ins.offset: idx for idx, ins in enumerate(flat)}
    seen = {}
    maxdepth = 0
    stack = [(0, 0)]
    while stack:
        idx, depth = stack.pop()
        if idx >= len(flat):
            continue
        if seen.get(idx, -1) >= depth:
            continue
        seen[idx] = depth
        ins = flat[idx]
        name = _OPNAME[ins.op]
        arg = ins.arg
        argval = arg.offset if isinstance(arg, Label) else arg
        # branch target
        if ins.op in HASJABS or ins.op in HASJREL:
            target_off = argval if ins.op in HASJABS else ins.offset + 3 + argval
            if name in ("SETUP_EXCEPT", "SETUP_FINALLY"):
                tdepth = depth + 3
            else:
                tdepth = depth + _stack_effect(ins.op, argval, jump=True)
            maxdepth = max(maxdepth, tdepth)
            tidx = by_offset.get(target_off)
            if tidx is not None:
                stack.append((tidx, tdepth))
        if name in ("SETUP_EXCEPT", "SETUP_FINALLY", "SETUP_LOOP"):
            newdepth = depth
        else:
            newdepth = depth + _stack_effect(ins.op, argval, jump=False)
        maxdepth = max(maxdepth, newdepth, depth)
        if name in ("JUMP_FORWARD", "JUMP_ABSOLUTE", "RETURN_VALUE",
                    "RAISE_VARARGS", "BREAK_LOOP", "CONTINUE_LOOP"):
            continue  # no fallthrough
        stack.append((idx + 1, newdepth))
    return maxdepth + 6  # safety margin; oversized frames are harmless


# --------------------------------------------------------------------------- #
# scope analysis
# --------------------------------------------------------------------------- #

# By NAME: node types come and go between Python versions (ast.Match exists
# since 3.10), and a missing one must not make this module unimportable.
_UNSUPPORTED_NODE_NAMES = (
    ("AsyncFunctionDef", "async def"),
    ("Await", "await"),
    ("AsyncFor", "async for"),
    ("AsyncWith", "async with"),
    ("With", "with-statement"),
    ("Yield", "yield / generators"),
    ("YieldFrom", "yield from"),
    ("ListComp", "list comprehension"),
    ("SetComp", "set comprehension"),
    ("DictComp", "dict comprehension"),
    ("GeneratorExp", "generator expression"),
    ("JoinedStr", "f-string"),
    ("FormattedValue", "f-string"),
    ("Set", "set literal"),
    ("NamedExpr", "walrus operator"),
    ("Match", "match statement"),
)
_UNSUPPORTED_NODES = {
    getattr(ast, name): label
    for name, label in _UNSUPPORTED_NODE_NAMES
    if getattr(ast, name, None) is not None
}

# ast.Index wrapped subscripts up to Python 3.8; it is deprecated and will go.
_AST_INDEX = getattr(ast, "Index", ())


def _bad(node, what: str):
    line = getattr(node, "lineno", "?")
    return Py27CompileError(
        "line %s: %s is not supported by py27c "
        "(keep the .pyc stub simple; put real logic in a .py module)"
        % (line, what))


class _Scope:
    """Local-name bookkeeping for one function."""

    def __init__(self, args, varargs, kwarg, body, node):
        self.locals = []
        self.globals = set()
        self.index = {}
        for name in args:
            self._add(name)
        if varargs:
            self._add(varargs)
        if kwarg:
            self._add(kwarg)
        for stmt in body:
            self._scan(stmt)

    def _add(self, name):
        if name not in self.index:
            self.index[name] = len(self.locals)
            self.locals.append(name)

    def _scan(self, node):
        for child in ast.walk(node):
            if isinstance(child, ast.Global):
                self.globals.update(child.names)
        stack = [node]
        while stack:
            cur = stack.pop()
            if isinstance(cur, (ast.FunctionDef, ast.ClassDef)):
                self._add(cur.name)
                continue  # do not descend: separate scope
            if isinstance(cur, ast.Lambda):
                continue
            if isinstance(cur, ast.Name) and isinstance(cur.ctx, (ast.Store, ast.Del)):
                self._add(cur.id)
            elif isinstance(cur, ast.arg):
                self._add(cur.arg)
            elif isinstance(cur, ast.ExceptHandler) and cur.name:
                self._add(cur.name)
            elif isinstance(cur, (ast.Import, ast.ImportFrom)):
                for alias in cur.names:
                    if alias.name == "*":
                        continue
                    self._add(alias.asname or alias.name.split(".")[0])
            for child in ast.iter_child_nodes(cur):
                stack.append(child)
        for name in self.globals:
            if name in self.index:
                idx = self.index.pop(name)
                self.locals[idx] = None
        self.locals = [n for n in self.locals if n is not None]
        self.index = {n: i for i, n in enumerate(self.locals)}


# --------------------------------------------------------------------------- #
# compiler
# --------------------------------------------------------------------------- #

class _CodeBuilder:
    def __init__(self, name, filename, firstlineno, scope=None, kind="module",
                 docstring=None):
        self.name = name
        self.filename = filename
        self.firstlineno = firstlineno
        self.scope = scope            # _Scope for functions, None for module/class
        self.kind = kind              # module | function | class | lambda
        self.instrs = []
        self.consts = [docstring]     # consts[0] is always the docstring or None
        self.names = []
        self.argcount = 0
        self.flags = CO_NOFREE
        self.loop_stack = []          # (continue_label, break_label)

    # -- pools ------------------------------------------------------------ #
    def const(self, value) -> int:
        for i, existing in enumerate(self.consts):
            if type(existing) is type(value) and not isinstance(value, CodeObject):
                if existing == value:
                    return i
            elif existing is value:
                return i
        self.consts.append(value)
        return len(self.consts) - 1

    def nameidx(self, name: str) -> int:
        if name not in self.names:
            self.names.append(name)
        return self.names.index(name)

    # -- emit ------------------------------------------------------------- #
    def emit(self, opname: str, arg=None, lineno=None):
        op = OPMAP[opname]
        if op >= HAVE_ARGUMENT and arg is None:
            raise Py27CompileError("%s needs an argument" % opname)
        self.instrs.append(Instr(op, arg, lineno))

    def label(self, lbl: Label):
        self.instrs.append(lbl)


class Py27Compiler:
    def __init__(self, source: str, filename: str):
        self.source = source
        self.filename = filename
        self.str_prefix = self._collect_string_prefixes(source)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _collect_string_prefixes(source: str) -> dict:
        prefixes = {}
        try:
            for tok in tokenize.generate_tokens(io.StringIO(source).readline):
                if tok.type == tokenize.STRING:
                    text = tok.string
                    i = 0
                    while i < len(text) and text[i] not in "\"'":
                        i += 1
                    prefixes[(tok.start[0], tok.start[1])] = text[:i].lower()
        except (tokenize.TokenError, IndentationError):
            pass
        return prefixes

    def _wrap_str(self, node, value):
        """Turn a python3 str/bytes constant into its python2 counterpart."""
        prefix = self.str_prefix.get(
            (getattr(node, "lineno", -1), getattr(node, "col_offset", -1)), "")
        if isinstance(value, bytes):
            return Py2Str(value)
        if "u" in prefix:
            return Py2Unicode(value)
        return Py2Str(value.encode("utf-8"))

    # ------------------------------------------------------------------ #
    def compile(self) -> CodeObject:
        try:
            tree = ast.parse(self.source, self.filename)
        except SyntaxError as exc:
            # The source is read with the Python 3 parser, so syntax that only
            # exists in Python 2 (123L, `print x`, `except E, e:`, backticks,
            # 0777, <>, ur'') lands here.  That is a legitimate mistake to make
            # -- report it as a compiler limitation, not as an internal crash.
            raise Py27CompileError(
                "line %s: %s -- py27c reads the source with the PYTHON 3 parser, "
                "so this file must sit in the py2 AND py3 intersection: no 123L "
                "(write 0xFFFFFFFF or int(...)), no `print x`, no "
                "`except E, e:` (write `except E as e:`), no `raise E, v, tb`, "
                "no backtick-repr, no 0777 (write 0o777), no <> (write !=), "
                "no ur''. Alternatively compile with a real Python 2.7 "
                "(build.py --py2 <path>)." % (getattr(exc, "lineno", "?"), exc.msg)
            ) from None
        for node in ast.walk(tree):
            for bad_type, label in _UNSUPPORTED_NODES.items():
                if isinstance(node, bad_type):
                    raise _bad(node, label)
        doc = ast.get_docstring(tree, clean=False)
        builder = _CodeBuilder("<module>", self.filename, 1, None, "module",
                               Py2Str(doc.encode("utf-8")) if doc is not None else None)
        body = tree.body
        if doc is not None:
            builder.emit("LOAD_CONST", 0, lineno=body[0].lineno)
            builder.emit("STORE_NAME", builder.nameidx("__doc__"),
                         lineno=body[0].lineno)
            body = body[1:]
        for stmt in body:
            self.stmt(builder, stmt)
        last = body[-1] if body else None
        if not self._ends_with_return(builder):
            builder.emit("LOAD_CONST", builder.const(None),
                         lineno=getattr(last, "lineno", 1))
            builder.emit("RETURN_VALUE", lineno=getattr(last, "lineno", 1))
        return self.finish(builder)


    @staticmethod
    def _ends_with_return(b) -> bool:
        """CPython 2.7 assemble() appends `LOAD_CONST None; RETURN_VALUE` only
        when the final basic block does not already end in RETURN_VALUE."""
        if not b.instrs:
            return False
        last = b.instrs[-1]
        return isinstance(last, Instr) and last.op == OPMAP["RETURN_VALUE"]

    def finish(self, b: _CodeBuilder) -> CodeObject:
        code, lnotab, stacksize = _assemble(b.instrs, b.firstlineno)
        varnames = tuple(b.scope.locals) if b.scope else ()
        return CodeObject(
            argcount=b.argcount,
            nlocals=len(varnames),
            stacksize=stacksize,
            flags=b.flags,
            code=code,
            consts=tuple(b.consts),
            names=tuple(b.names),
            varnames=varnames,
            freevars=(),
            cellvars=(),
            filename=b.filename,
            name=b.name,
            firstlineno=b.firstlineno,
            lnotab=lnotab,
        )

    # ================================================================== #
    # statements
    # ================================================================== #
    def stmt(self, b, node):
        method = getattr(self, "st_" + type(node).__name__, None)
        if method is None:
            raise _bad(node, "statement %s" % type(node).__name__)
        method(b, node)

    def st_Pass(self, b, node):
        pass

    def st_Expr(self, b, node):
        # print(...) / exec(...) as a bare statement compile to the python 2
        # statement forms, which leave nothing on the stack -- no POP_TOP.
        call = node.value
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id in ("print", "exec")):
            if call.func.id == "print":
                self._print(b, call)
            else:
                self._exec(b, call)
            return
        self.expr(b, node.value)
        b.emit("POP_TOP", lineno=node.lineno)

    def st_Assign(self, b, node):
        self.expr(b, node.value)
        for i, target in enumerate(node.targets):
            if i < len(node.targets) - 1:
                b.emit("DUP_TOP", lineno=node.lineno)
            self.store(b, target)

    def st_AugAssign(self, b, node):
        target = node.target
        if isinstance(target, ast.Name):
            self.load_name(b, target.id, node.lineno)
            self.expr(b, node.value)
            b.emit(_INPLACE_OP[type(node.op)], lineno=node.lineno)
            self.store(b, target)
        elif isinstance(target, ast.Attribute):
            self.expr(b, target.value)
            b.emit("DUP_TOP", lineno=node.lineno)
            b.emit("LOAD_ATTR", b.nameidx(target.attr), lineno=node.lineno)
            self.expr(b, node.value)
            b.emit(_INPLACE_OP[type(node.op)], lineno=node.lineno)
            b.emit("ROT_TWO", lineno=node.lineno)
            b.emit("STORE_ATTR", b.nameidx(target.attr), lineno=node.lineno)
        elif isinstance(target, ast.Subscript):
            self.expr(b, target.value)
            self.subscript_index(b, target)
            b.emit("DUP_TOPX", 2, lineno=node.lineno)
            b.emit("BINARY_SUBSCR", lineno=node.lineno)
            self.expr(b, node.value)
            b.emit(_INPLACE_OP[type(node.op)], lineno=node.lineno)
            b.emit("ROT_THREE", lineno=node.lineno)
            b.emit("STORE_SUBSCR", lineno=node.lineno)
        else:
            raise _bad(node, "augmented assignment to this target")

    def st_AnnAssign(self, b, node):
        raise _bad(node, "variable annotation")

    def st_Delete(self, b, node):
        for target in node.targets:
            if isinstance(target, ast.Name):
                if b.scope is None:
                    b.emit("DELETE_NAME", b.nameidx(target.id), lineno=node.lineno)
                elif target.id in b.scope.index:
                    b.emit("DELETE_FAST", b.scope.index[target.id], lineno=node.lineno)
                else:
                    b.emit("DELETE_GLOBAL", b.nameidx(target.id), lineno=node.lineno)
            elif isinstance(target, ast.Attribute):
                self.expr(b, target.value)
                b.emit("DELETE_ATTR", b.nameidx(target.attr), lineno=node.lineno)
            elif isinstance(target, ast.Subscript):
                self.expr(b, target.value)
                self.subscript_index(b, target)
                b.emit("DELETE_SUBSCR", lineno=node.lineno)
            else:
                raise _bad(node, "del of this target")

    def st_Global(self, b, node):
        pass  # handled by _Scope

    def st_Nonlocal(self, b, node):
        raise _bad(node, "nonlocal")

    def st_Return(self, b, node):
        if b.scope is None:
            raise _bad(node, "'return' outside a function")
        if node.value is None:
            b.emit("LOAD_CONST", b.const(None), lineno=node.lineno)
        else:
            self.expr(b, node.value)
        b.emit("RETURN_VALUE", lineno=node.lineno)

    def st_Raise(self, b, node):
        if node.exc is None:
            b.emit("RAISE_VARARGS", 0, lineno=node.lineno)
            return
        if node.cause is not None:
            raise _bad(node, "raise ... from ...")
        self.expr(b, node.exc)
        b.emit("RAISE_VARARGS", 1, lineno=node.lineno)

    def st_Assert(self, b, node):
        raise _bad(node, "assert")

    # -- imports ---------------------------------------------------------- #
    def st_Import(self, b, node):
        for alias in node.names:
            b.emit("LOAD_CONST", b.const(-1), lineno=node.lineno)
            b.emit("LOAD_CONST", b.const(None), lineno=node.lineno)
            b.emit("IMPORT_NAME", b.nameidx(alias.name), lineno=node.lineno)
            if alias.asname:
                # import a.b.c as x  ->  IMPORT_NAME a.b.c then LOAD_ATTR b, c
                for part in alias.name.split(".")[1:]:
                    b.emit("LOAD_ATTR", b.nameidx(part), lineno=node.lineno)
                self.store_name(b, alias.asname, node.lineno)
            else:
                self.store_name(b, alias.name.split(".")[0], node.lineno)

    def st_ImportFrom(self, b, node):
        level = node.level or 0
        module = node.module or ""
        star = len(node.names) == 1 and node.names[0].name == "*"
        b.emit("LOAD_CONST", b.const(-1 if level == 0 else level), lineno=node.lineno)
        if star:
            fromlist = (Py2Str(b"*"),)
        else:
            fromlist = tuple(Py2Str(a.name.encode("utf-8")) for a in node.names)
        b.emit("LOAD_CONST", b.const(fromlist), lineno=node.lineno)
        b.emit("IMPORT_NAME", b.nameidx(module), lineno=node.lineno)
        if star:
            b.emit("IMPORT_STAR", lineno=node.lineno)
            return
        for alias in node.names:
            b.emit("IMPORT_FROM", b.nameidx(alias.name), lineno=node.lineno)
            self.store_name(b, alias.asname or alias.name, node.lineno)
        b.emit("POP_TOP", lineno=node.lineno)

    # -- control flow ------------------------------------------------------ #
    def st_If(self, b, node):
        l_else = Label()
        l_end = Label()
        self.expr(b, node.test)
        b.emit("POP_JUMP_IF_FALSE", l_else, lineno=node.lineno)
        for stmt in node.body:
            self.stmt(b, stmt)
        # A real Python 2.7 emits JUMP_FORWARD here unconditionally, even when
        # there is no else branch.  Keep it: uncompyle6's 2.7 grammar expects a
        # JUMP_FORWARD after the if-body, and real .pyc files look like this.
        b.emit("JUMP_FORWARD", l_end, lineno=node.lineno)
        if node.orelse:
            b.label(l_else)
            for stmt in node.orelse:
                self.stmt(b, stmt)
            b.label(l_end)
        else:
            b.label(l_else)
            b.label(l_end)

    def st_While(self, b, node):
        l_break = Label()
        l_start = Label()
        l_else = Label()
        b.emit("SETUP_LOOP", l_break, lineno=node.lineno)
        b.label(l_start)
        self.expr(b, node.test)
        b.emit("POP_JUMP_IF_FALSE", l_else, lineno=node.lineno)
        b.loop_stack.append((l_start, l_break))
        for stmt in node.body:
            self.stmt(b, stmt)
        b.loop_stack.pop()
        b.emit("JUMP_ABSOLUTE", l_start, lineno=node.lineno)
        b.label(l_else)
        b.emit("POP_BLOCK", lineno=node.lineno)
        for stmt in node.orelse:
            self.stmt(b, stmt)
        b.label(l_break)

    def st_For(self, b, node):
        l_break = Label()
        l_start = Label()
        l_cleanup = Label()
        b.emit("SETUP_LOOP", l_break, lineno=node.lineno)
        self.expr(b, node.iter)
        b.emit("GET_ITER", lineno=node.lineno)
        b.label(l_start)
        b.emit("FOR_ITER", l_cleanup, lineno=node.lineno)
        self.store(b, node.target)
        b.loop_stack.append((l_start, l_break))
        for stmt in node.body:
            self.stmt(b, stmt)
        b.loop_stack.pop()
        b.emit("JUMP_ABSOLUTE", l_start, lineno=node.lineno)
        b.label(l_cleanup)
        b.emit("POP_BLOCK", lineno=node.lineno)
        for stmt in node.orelse:
            self.stmt(b, stmt)
        b.label(l_break)

    def st_Break(self, b, node):
        if not b.loop_stack:
            raise _bad(node, "'break' outside a loop")
        b.emit("BREAK_LOOP", lineno=node.lineno)

    def st_Continue(self, b, node):
        if not b.loop_stack:
            raise _bad(node, "'continue' outside a loop")
        b.emit("JUMP_ABSOLUTE", b.loop_stack[-1][0], lineno=node.lineno)

    def st_Try(self, b, node):
        if node.finalbody:
            l_fin = Label()
            b.emit("SETUP_FINALLY", l_fin, lineno=node.lineno)
            if node.handlers or node.orelse:
                self._try_except(b, node)
            else:
                for stmt in node.body:
                    self.stmt(b, stmt)
            b.emit("POP_BLOCK", lineno=node.lineno)
            b.emit("LOAD_CONST", b.const(None), lineno=node.lineno)
            b.label(l_fin)
            for stmt in node.finalbody:
                self.stmt(b, stmt)
            b.emit("END_FINALLY", lineno=node.lineno)
        else:
            self._try_except(b, node)

    # python <=3.10 spelling
    st_TryExcept = st_Try
    st_TryFinally = st_Try

    def _try_except(self, b, node):
        if not node.handlers:
            for stmt in node.body:
                self.stmt(b, stmt)
            return
        l_handler = Label()
        l_else = Label()
        l_end = Label()
        b.emit("SETUP_EXCEPT", l_handler, lineno=node.lineno)
        for stmt in node.body:
            self.stmt(b, stmt)
        b.emit("POP_BLOCK", lineno=node.lineno)
        b.emit("JUMP_FORWARD", l_else, lineno=node.lineno)
        b.label(l_handler)
        for handler in node.handlers:
            l_next = Label()
            if handler.type is not None:
                b.emit("DUP_TOP", lineno=handler.lineno)
                self.expr(b, handler.type)
                b.emit("COMPARE_OP", CMP_OP.index("exception match"),
                       lineno=handler.lineno)
                b.emit("POP_JUMP_IF_FALSE", l_next, lineno=handler.lineno)
            b.emit("POP_TOP", lineno=handler.lineno)
            if handler.name:
                self.store_name(b, handler.name, handler.lineno)
            else:
                b.emit("POP_TOP", lineno=handler.lineno)
            b.emit("POP_TOP", lineno=handler.lineno)
            for stmt in handler.body:
                self.stmt(b, stmt)
            b.emit("JUMP_FORWARD", l_end, lineno=handler.lineno)
            b.label(l_next)
        b.emit("END_FINALLY", lineno=node.lineno)
        b.label(l_else)
        for stmt in node.orelse:
            self.stmt(b, stmt)
        b.label(l_end)

    # -- def / class ------------------------------------------------------- #
    def st_FunctionDef(self, b, node):
        # decorators are *evaluated* top-to-bottom and *applied* bottom-up,
        # so they are pushed in source order and CALL_FUNCTIONed in reverse.
        for dec in node.decorator_list:
            self.expr(b, dec)
        self._make_function(b, node, node.name)
        for _ in node.decorator_list:
            b.emit("CALL_FUNCTION", 1, lineno=node.lineno)
        self.store_name(b, node.name, node.lineno)

    def _make_function(self, b, node, name):
        args = node.args
        if args.kwonlyargs or args.posonlyargs or getattr(args, "kw_defaults", None):
            if args.kwonlyargs:
                raise _bad(node, "keyword-only arguments")
        for a in args.args:
            if a.annotation is not None:
                raise _bad(node, "argument annotations")
        if getattr(node, "returns", None) is not None:
            raise _bad(node, "return annotations")
        for default in args.defaults:
            self.expr(b, default)
        code = self.compile_function(node, name)
        b.emit("LOAD_CONST", b.const(code), lineno=node.lineno)
        b.emit("MAKE_FUNCTION", len(args.defaults), lineno=node.lineno)

    def compile_function(self, node, name) -> CodeObject:
        args = node.args
        argnames = [a.arg for a in (list(args.posonlyargs) + list(args.args))]
        varargs = args.vararg.arg if args.vararg else None
        kwarg = args.kwarg.arg if args.kwarg else None
        is_lambda = isinstance(node, ast.Lambda)
        body = [ast.Return(value=node.body, lineno=node.lineno,
                           col_offset=node.col_offset)] if is_lambda else list(node.body)
        doc = None if is_lambda else ast.get_docstring(node, clean=False)
        scope = _Scope(argnames, varargs, kwarg, body, node)
        builder = _CodeBuilder(name, self.filename, node.lineno, scope,
                               "function",
                               Py2Str(doc.encode("utf-8")) if doc is not None else None)
        builder.argcount = len(argnames)
        builder.flags = CO_OPTIMIZED | CO_NEWLOCALS | CO_NOFREE
        if varargs:
            builder.flags |= CO_VARARGS
        if kwarg:
            builder.flags |= CO_VARKEYWORDS
        self._check_no_closure(node, scope, body)
        stmts = body[1:] if doc is not None else body
        for stmt in stmts:
            self.stmt(builder, stmt)
        lastline = getattr(stmts[-1], "lineno", node.lineno) if stmts else node.lineno
        if not self._ends_with_return(builder):
            builder.emit("LOAD_CONST", builder.const(None), lineno=lastline)
            builder.emit("RETURN_VALUE", lineno=lastline)
        return self.finish(builder)

    def _check_no_closure(self, node, scope, body):
        """Reject nested defs that would need cell/free variables."""
        for stmt in body:
            for child in ast.walk(stmt):
                if isinstance(child, (ast.FunctionDef, ast.Lambda)):
                    if child is node:
                        continue
                    inner_body = ([child.body] if isinstance(child, ast.Lambda)
                                  else child.body)
                    inner_args = {a.arg for a in child.args.args}
                    for sub in inner_body:
                        for nm in ast.walk(sub):
                            if (isinstance(nm, ast.Name)
                                    and isinstance(nm.ctx, ast.Load)
                                    and nm.id in scope.index
                                    and nm.id not in inner_args):
                                raise _bad(
                                    child,
                                    "closure over local %r (py27c has no "
                                    "cell/free variable support)" % nm.id)

    def st_ClassDef(self, b, node):
        if node.keywords or getattr(node, "starargs", None):
            raise _bad(node, "class keyword arguments (metaclass=...)")
        for dec in node.decorator_list:
            self.expr(b, dec)
        b.emit("LOAD_CONST", b.const(Py2Str(node.name.encode("utf-8"))),
               lineno=node.lineno)
        for base in node.bases:
            self.expr(b, base)
        b.emit("BUILD_TUPLE", len(node.bases), lineno=node.lineno)
        code = self.compile_class(node)
        b.emit("LOAD_CONST", b.const(code), lineno=node.lineno)
        b.emit("MAKE_FUNCTION", 0, lineno=node.lineno)
        b.emit("CALL_FUNCTION", 0, lineno=node.lineno)
        b.emit("BUILD_CLASS", lineno=node.lineno)
        for _ in node.decorator_list:
            b.emit("CALL_FUNCTION", 1, lineno=node.lineno)
        self.store_name(b, node.name, node.lineno)

    def compile_class(self, node) -> CodeObject:
        doc = ast.get_docstring(node, clean=False)
        builder = _CodeBuilder(node.name, self.filename, node.lineno, None,
                               "class",
                               Py2Str(doc.encode("utf-8")) if doc is not None else None)
        builder.flags = CO_NEWLOCALS | CO_NOFREE
        builder.emit("LOAD_NAME", builder.nameidx("__name__"), lineno=node.lineno)
        builder.emit("STORE_NAME", builder.nameidx("__module__"), lineno=node.lineno)
        body = list(node.body)
        if doc is not None:
            builder.emit("LOAD_CONST", 0, lineno=node.lineno)
            builder.emit("STORE_NAME", builder.nameidx("__doc__"), lineno=node.lineno)
            body = body[1:]
        for stmt in body:
            self.stmt(builder, stmt)
        lastline = getattr(body[-1], "lineno", node.lineno) if body else node.lineno
        builder.emit("LOAD_LOCALS", lineno=lastline)
        builder.emit("RETURN_VALUE", lineno=lastline)
        return self.finish(builder)

    # ================================================================== #
    # stores / loads
    # ================================================================== #
    def store_name(self, b, name, lineno):
        if b.scope is None:
            b.emit("STORE_NAME", b.nameidx(name), lineno=lineno)
        elif name in b.scope.index:
            b.emit("STORE_FAST", b.scope.index[name], lineno=lineno)
        else:
            b.emit("STORE_GLOBAL", b.nameidx(name), lineno=lineno)

    def load_name(self, b, name, lineno):
        if b.scope is None:
            b.emit("LOAD_NAME", b.nameidx(name), lineno=lineno)
        elif name in b.scope.index:
            b.emit("LOAD_FAST", b.scope.index[name], lineno=lineno)
        else:
            b.emit("LOAD_GLOBAL", b.nameidx(name), lineno=lineno)

    def store(self, b, target):
        if isinstance(target, ast.Name):
            self.store_name(b, target.id, target.lineno)
        elif isinstance(target, ast.Attribute):
            self.expr(b, target.value)
            b.emit("STORE_ATTR", b.nameidx(target.attr), lineno=target.lineno)
        elif isinstance(target, ast.Subscript):
            self.expr(b, target.value)
            self.subscript_index(b, target)
            b.emit("STORE_SUBSCR", lineno=target.lineno)
        elif isinstance(target, (ast.Tuple, ast.List)):
            b.emit("UNPACK_SEQUENCE", len(target.elts), lineno=target.lineno)
            for elt in target.elts:
                self.store(b, elt)
        else:
            raise _bad(target, "assignment target %s" % type(target).__name__)

    def subscript_index(self, b, node):
        sl = node.slice
        if isinstance(sl, ast.Slice):
            if sl.lower is None:
                b.emit("LOAD_CONST", b.const(None), lineno=node.lineno)
            else:
                self.expr(b, sl.lower)
            if sl.upper is None:
                b.emit("LOAD_CONST", b.const(None), lineno=node.lineno)
            else:
                self.expr(b, sl.upper)
            if sl.step is None:
                b.emit("BUILD_SLICE", 2, lineno=node.lineno)
            else:
                self.expr(b, sl.step)
                b.emit("BUILD_SLICE", 3, lineno=node.lineno)
        elif isinstance(sl, _AST_INDEX):         # python <= 3.8
            self.expr(b, sl.value)
        else:
            self.expr(b, sl)

    # ================================================================== #
    # expressions
    # ================================================================== #
    def expr(self, b, node):
        method = getattr(self, "ex_" + type(node).__name__, None)
        if method is None:
            raise _bad(node, "expression %s" % type(node).__name__)
        method(b, node)

    def ex_Constant(self, b, node):
        value = node.value
        if isinstance(value, (str, bytes)):
            value = self._wrap_str(node, value)
        elif value is Ellipsis:
            raise _bad(node, "Ellipsis literal")
        b.emit("LOAD_CONST", b.const(value), lineno=node.lineno)

    ex_Str = ex_Constant
    ex_Bytes = ex_Constant
    ex_Num = ex_Constant
    ex_NameConstant = ex_Constant

    def ex_Name(self, b, node):
        if node.id in ("True", "False", "None"):
            const = {"True": True, "False": False, "None": None}[node.id]
            b.emit("LOAD_CONST", b.const(const), lineno=node.lineno)
            return
        self.load_name(b, node.id, node.lineno)

    def ex_Attribute(self, b, node):
        self.expr(b, node.value)
        b.emit("LOAD_ATTR", b.nameidx(node.attr), lineno=node.lineno)

    def ex_Subscript(self, b, node):
        self.expr(b, node.value)
        self.subscript_index(b, node)
        b.emit("BINARY_SUBSCR", lineno=node.lineno)

    def ex_Tuple(self, b, node):
        # CPython 2.7's peephole optimiser folds `LOAD_CONST xN; BUILD_TUPLE N`
        # into a single LOAD_CONST.  Reproduce it: it is what real .pyc files
        # look like (and what uncompyle6's 2.7 grammar expects).
        folded = self._fold_tuple(node)
        if folded is not None:
            b.emit("LOAD_CONST", b.const(folded), lineno=node.lineno)
            return
        for elt in node.elts:
            self.expr(b, elt)
        b.emit("BUILD_TUPLE", len(node.elts), lineno=node.lineno)

    def _fold_tuple(self, node):
        out = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant):
                value = elt.value
                if isinstance(value, (str, bytes)):
                    value = self._wrap_str(elt, value)
                elif value is Ellipsis:
                    return None
                out.append(value)
            elif isinstance(elt, ast.Tuple):
                sub = self._fold_tuple(elt)
                if sub is None:
                    return None
                out.append(sub)
            elif (isinstance(elt, ast.UnaryOp) and isinstance(elt.op, ast.USub)
                  and isinstance(elt.operand, ast.Constant)
                  and isinstance(elt.operand.value, (int, float))
                  and not isinstance(elt.operand.value, bool)):
                out.append(-elt.operand.value)
            else:
                return None
        return tuple(out)

    def ex_List(self, b, node):
        for elt in node.elts:
            self.expr(b, elt)
        b.emit("BUILD_LIST", len(node.elts), lineno=node.lineno)

    def ex_Dict(self, b, node):
        b.emit("BUILD_MAP", len(node.keys), lineno=node.lineno)
        for key, value in zip(node.keys, node.values):
            if key is None:
                raise _bad(node, "dict unpacking {**x}")
            self.expr(b, value)
            self.expr(b, key)
            b.emit("STORE_MAP", lineno=getattr(key, "lineno", node.lineno))

    def ex_UnaryOp(self, b, node):
        self.expr(b, node.operand)
        b.emit({ast.Not: "UNARY_NOT", ast.USub: "UNARY_NEGATIVE",
                ast.UAdd: "UNARY_POSITIVE", ast.Invert: "UNARY_INVERT"}[
                   type(node.op)], lineno=node.lineno)

    def ex_BinOp(self, b, node):
        opname = _BINARY_OP.get(type(node.op))
        if opname is None:
            raise _bad(node, "operator %s" % type(node.op).__name__)
        self.expr(b, node.left)
        self.expr(b, node.right)
        b.emit(opname, lineno=node.lineno)

    def ex_BoolOp(self, b, node):
        end = Label()
        jump = "JUMP_IF_FALSE_OR_POP" if isinstance(node.op, ast.And) \
            else "JUMP_IF_TRUE_OR_POP"
        for i, value in enumerate(node.values):
            self.expr(b, value)
            if i < len(node.values) - 1:
                b.emit(jump, end, lineno=node.lineno)
        b.label(end)

    def ex_Compare(self, b, node):
        if len(node.ops) == 1:
            self.expr(b, node.left)
            self.expr(b, node.comparators[0])
            b.emit("COMPARE_OP", self._cmp(node, node.ops[0]), lineno=node.lineno)
            return
        # chained comparison a < b < c
        end = Label()
        cleanup = Label()
        self.expr(b, node.left)
        for i, (op, comp) in enumerate(zip(node.ops, node.comparators)):
            self.expr(b, comp)
            if i < len(node.ops) - 1:
                b.emit("DUP_TOP", lineno=node.lineno)
                b.emit("ROT_THREE", lineno=node.lineno)
                b.emit("COMPARE_OP", self._cmp(node, op), lineno=node.lineno)
                b.emit("JUMP_IF_FALSE_OR_POP", cleanup, lineno=node.lineno)
            else:
                b.emit("COMPARE_OP", self._cmp(node, op), lineno=node.lineno)
        b.emit("JUMP_FORWARD", end, lineno=node.lineno)
        b.label(cleanup)
        b.emit("ROT_TWO", lineno=node.lineno)
        b.emit("POP_TOP", lineno=node.lineno)
        b.label(end)

    @staticmethod
    def _cmp(node, op):
        table = {ast.Lt: "<", ast.LtE: "<=", ast.Eq: "==", ast.NotEq: "!=",
                 ast.Gt: ">", ast.GtE: ">=", ast.In: "in", ast.NotIn: "not in",
                 ast.Is: "is", ast.IsNot: "is not"}
        if type(op) not in table:
            raise _bad(node, "comparison %s" % type(op).__name__)
        return CMP_OP.index(table[type(op)])

    def ex_IfExp(self, b, node):
        l_else = Label()
        l_end = Label()
        self.expr(b, node.test)
        b.emit("POP_JUMP_IF_FALSE", l_else, lineno=node.lineno)
        self.expr(b, node.body)
        b.emit("JUMP_FORWARD", l_end, lineno=node.lineno)
        b.label(l_else)
        self.expr(b, node.orelse)
        b.label(l_end)

    def ex_Lambda(self, b, node):
        for default in node.args.defaults:
            self.expr(b, default)
        code = self.compile_function(node, "<lambda>")
        b.emit("LOAD_CONST", b.const(code), lineno=node.lineno)
        b.emit("MAKE_FUNCTION", len(node.args.defaults), lineno=node.lineno)

    def ex_Call(self, b, node):
        # print(...) and exec(...) are STATEMENTS in python 2 -- neither name
        # exists at runtime there, so they get their own opcodes.
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            self._print(b, node)
            b.emit("LOAD_CONST", b.const(None), lineno=node.lineno)
            return
        if isinstance(node.func, ast.Name) and node.func.id == "exec":
            self._exec(b, node)
            b.emit("LOAD_CONST", b.const(None), lineno=node.lineno)
            return
        self.expr(b, node.func)
        star = None
        dstar = None
        positional = []
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                star = arg.value
            else:
                positional.append(arg)
        keywords = []
        for kw in node.keywords:
            if kw.arg is None:
                dstar = kw.value
            else:
                keywords.append(kw)
        for arg in positional:
            self.expr(b, arg)
        for kw in keywords:
            b.emit("LOAD_CONST", b.const(Py2Str(kw.arg.encode("utf-8"))),
                   lineno=node.lineno)
            self.expr(b, kw.value)
        if star is not None:
            self.expr(b, star)
        if dstar is not None:
            self.expr(b, dstar)
        argc = len(positional) | (len(keywords) << 8)
        if star is not None and dstar is not None:
            b.emit("CALL_FUNCTION_VAR_KW", argc, lineno=node.lineno)
        elif star is not None:
            b.emit("CALL_FUNCTION_VAR", argc, lineno=node.lineno)
        elif dstar is not None:
            b.emit("CALL_FUNCTION_KW", argc, lineno=node.lineno)
        else:
            b.emit("CALL_FUNCTION", argc, lineno=node.lineno)

    def _print(self, b, node):
        if node.keywords:
            raise _bad(node, "print(..., sep=/end=/file=)")
        if len(node.args) > 1:
            # A REAL Python 2.7 compiler sees `print(a, b)` as the print
            # STATEMENT applied to one tuple operand and prints "(a, b)".
            # Emitting two PRINT_ITEMs here would print "a b" instead, i.e. the
            # same source would behave differently depending on which backend
            # build.py happened to pick.  Refuse instead of diverging.
            raise Py27CompileError(
                "line %s: print(a, b) with more than one operand. A real Python "
                "2.7 (no `from __future__ import print_function`) reads this as "
                "the print STATEMENT with ONE tuple operand and prints (a, b), "
                "not 'a b' -- so the same source would behave differently "
                "depending on the compiler backend. Write "
                "print('%%s %%s' %% (a, b)), or log through the logging module."
                % getattr(node, "lineno", "?")
            )
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                raise _bad(node, "print(*args)")
            self.expr(b, arg)
            b.emit("PRINT_ITEM", lineno=node.lineno)
        b.emit("PRINT_NEWLINE", lineno=node.lineno)

    def _exec(self, b, node):
        """exec(code[, globals[, locals]])  ->  python 2 EXEC_STMT."""
        if node.keywords or not 1 <= len(node.args) <= 3:
            raise _bad(node, "exec() with these arguments")
        self.expr(b, node.args[0])
        if len(node.args) == 1:
            b.emit("LOAD_CONST", b.const(None), lineno=node.lineno)
            b.emit("DUP_TOP", lineno=node.lineno)
        else:
            self.expr(b, node.args[1])
            if len(node.args) == 3:
                self.expr(b, node.args[2])
            else:
                b.emit("DUP_TOP", lineno=node.lineno)
        b.emit("EXEC_STMT", lineno=node.lineno)


_BINARY_OP = {
    ast.Add: "BINARY_ADD", ast.Sub: "BINARY_SUBTRACT",
    ast.Mult: "BINARY_MULTIPLY", ast.Div: "BINARY_DIVIDE",
    ast.Mod: "BINARY_MODULO", ast.Pow: "BINARY_POWER",
    ast.LShift: "BINARY_LSHIFT", ast.RShift: "BINARY_RSHIFT",
    ast.BitOr: "BINARY_OR", ast.BitXor: "BINARY_XOR",
    ast.BitAnd: "BINARY_AND", ast.FloorDiv: "BINARY_FLOOR_DIVIDE",
}

_INPLACE_OP = {
    ast.Add: "INPLACE_ADD", ast.Sub: "INPLACE_SUBTRACT",
    ast.Mult: "INPLACE_MULTIPLY", ast.Div: "INPLACE_DIVIDE",
    ast.Mod: "INPLACE_MODULO", ast.Pow: "INPLACE_POWER",
    ast.LShift: "INPLACE_LSHIFT", ast.RShift: "INPLACE_RSHIFT",
    ast.BitOr: "INPLACE_OR", ast.BitXor: "INPLACE_XOR",
    ast.BitAnd: "INPLACE_AND", ast.FloorDiv: "INPLACE_FLOOR_DIVIDE",
}


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def compile_source(source: str, dfile: str, mtime: int | None = None) -> bytes:
    """Compile `source` and return the full .pyc payload (header + marshal)."""
    if source.startswith("﻿"):
        source = source[1:]
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    if not source.endswith("\n"):
        source += "\n"
    co = Py27Compiler(source, dfile).compile()
    body = _marshal_code(co)
    stamp = int(time.time()) if mtime is None else int(mtime)
    return PY27_MAGIC + struct.pack("<I", stamp & 0xFFFFFFFF) + body


def compile_file(src_path: str, out_path: str, dfile: str | None = None,
                 mtime: int | None = None) -> bytes:
    with open(src_path, "r", encoding="utf-8") as fh:
        source = fh.read()
    if dfile is None:
        dfile = os.path.basename(src_path)
    blob = compile_source(source, dfile, mtime)
    folder = os.path.dirname(os.path.abspath(out_path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with open(out_path, "wb") as fh:
        fh.write(blob)
    return blob


# --------------------------------------------------------------------------- #
# self test
# --------------------------------------------------------------------------- #

_FIXTURE = '''\
# -*- coding: utf-8 -*-
"""fixture module."""
import sys
import os.path as _p
from math import pi, e

CONST = 42
TEXT = 'hello'
UNI = u'\\u043f\\u0440\\u0438\\u0432\\u0435\\u0442'
SEQ = (1, 2, 3)
MAP = {'a': 1, 'b': 2}


class Thing(object):
    """doc."""
    kind = 'thing'

    def __init__(self, value=0):
        self.value = value

    def bump(self, by=1):
        self.value = self.value + by
        return self.value


def main(a, b=2, *args, **kwargs):
    total = a + b
    for item in args:
        total = total + item
    while total > 100:
        total = total - 10
    if total < 0:
        total = 0
    elif total == 0:
        total = 1
    else:
        total = total * 2
    try:
        raise ValueError('boom')
    except (ValueError, TypeError) as exc:
        sys.stderr.write('%s\\n' % (exc,))
    except Exception:
        pass
    else:
        total = -1
    finally:
        total = total + 0
    data = [x for x in ()] if False else []
    data.append(total)
    return data[0], _p.sep, pi > e, kwargs.get('k', None)


def call_through(fn, args, kw):
    return fn(*args, **kw)
'''


def _self_test(verbose: bool = True) -> int:
    import shutil
    import subprocess
    import tempfile

    fixtures = {"fixture_full.py": _FIXTURE.replace("[x for x in ()] if False else ", "")}
    tmp = tempfile.mkdtemp(prefix="py27c_")
    failures = 0
    produced = []
    for name, src in fixtures.items():
        src_path = os.path.join(tmp, name)
        with open(src_path, "w", encoding="utf-8") as fh:
            fh.write(src)
        out_path = src_path + "c"
        try:
            blob = compile_file(src_path, out_path, dfile=name, mtime=0)
        except Py27CompileError as exc:
            print("FAIL compile %s: %s" % (name, exc))
            failures += 1
            continue
        assert blob[:4] == PY27_MAGIC, "bad magic"
        produced.append(out_path)
        if verbose:
            print("OK   compiled %s -> %d bytes, magic %s"
                  % (name, len(blob), blob[:4].hex()))

    venv = _venv_python()
    if venv is None:
        print("NOTE %s not found -- skipping the decompile round-trip "
              "(README, section about the optional tools)" % _VENV_HINT)
        shutil.rmtree(tmp, ignore_errors=True)
        return 1 if failures else 0
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for path in produced:
        # The uncompyle6 command exits 0 even when it fails to parse a module,
        # so the verdict is its "# okay decompiling" line, not the exit code.
        proc = subprocess.run(
            [venv, "-m", "uncompyle6.bin.uncompile", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        if proc.returncode != 0 or "# okay decompiling" not in proc.stdout:
            print("FAIL uncompyle6 %s\n%s\n%s" % (path, proc.stdout, proc.stderr))
            failures += 1
        elif verbose:
            print("---- uncompyle6 round-trip of %s ----" % os.path.basename(path))
            print(proc.stdout)
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failures else 0


_VENV_HINT = ".tools/venv_uncompyle"


def _venv_python():
    """<template>/.tools/venv_uncompyle's interpreter, or None."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in (("Scripts", "python.exe"), ("bin", "python")):
        path = os.path.join(root, ".tools", "venv_uncompyle", *rel)
        if os.path.isfile(path):
            return path
    return None


def _check_opmap() -> int:
    try:
        from xdis.opcodes import opcode_27 as ref
    except ImportError:
        venv = _venv_python()
        if venv and os.path.normcase(os.path.abspath(venv)) != \
                os.path.normcase(os.path.abspath(sys.executable)):
            import subprocess
            print("xdis is not importable here -- re-running with %s" % venv, flush=True)
            return subprocess.call([venv, os.path.abspath(__file__), "--check-opmap"])
        print("xdis is not importable from this interpreter; create %s first "
              "(README, section about the optional tools)" % _VENV_HINT)
        return 2
    bad = 0
    for name, value in OPMAP.items():
        if ref.opmap.get(name) != value:
            print("MISMATCH %s: ours=%s xdis=%s" % (name, value, ref.opmap.get(name)))
            bad += 1
    for name, value in ref.opmap.items():
        if name not in OPMAP:
            print("MISSING %s=%s" % (name, value))
            bad += 1
    if set(HASJREL) != set(ref.hasjrel):
        print("hasjrel mismatch", sorted(HASJREL), sorted(ref.hasjrel))
        bad += 1
    if set(HASJABS) != set(ref.hasjabs):
        print("hasjabs mismatch", sorted(HASJABS), sorted(ref.hasjabs))
        bad += 1
    print("opmap check: %d problem(s)" % bad)
    return 1 if bad else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile a restricted Python subset to Python 2.7 .pyc")
    parser.add_argument("source", nargs="?", help="input .py")
    parser.add_argument("-o", "--output", help="output .pyc (default: <source>c)")
    parser.add_argument("--dfile", help="path baked into co_filename "
                                        "(shows up in python.log tracebacks)")
    parser.add_argument("--mtime", type=int, default=None,
                        help="timestamp for the .pyc header (default: now)")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--check-opmap", action="store_true",
                        help="compare the embedded opcode table against xdis")
    args = parser.parse_args(argv)

    if args.check_opmap:
        return _check_opmap()
    if args.self_test:
        return _self_test()
    if not args.source:
        parser.error("source is required")
    out = args.output or (args.source + "c")
    try:
        blob = compile_file(args.source, out, args.dfile, args.mtime)
    except Py27CompileError as exc:
        sys.stderr.write("py27c: %s\n" % exc)
        return 2
    print("%s -> %s (%d bytes, magic %s)"
          % (args.source, out, len(blob), blob[:4].hex()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
