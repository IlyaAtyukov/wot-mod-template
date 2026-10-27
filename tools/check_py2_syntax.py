# -*- coding: utf-8 -*-
# SPDX-License-Identifier: 0BSD
"""Проверка исходников мода на соответствие синтаксису Python 2.7.

ЗАПУСКАТЬ ПИТОНОМ 3.10+:

    python tools/check_py2_syntax.py                       # src/ рядом с tools/
    python tools/check_py2_syntax.py src
    python tools/check_py2_syntax.py src examples          # можно несколько целей

Зачем этот файл вообще нужен
----------------------------
Клиент «Мира танков» исполняет Python 2.7. Тело мода
(res/scripts/client/gui/mods/<пакет>/*.py) едет в .mtmod ОБЫЧНЫМ .py и
компилируется уже внутри игры, поэтому на машине сборки его не компилирует
никто: ни tools/py27c.py (он трогает только mod_*.py), ни python2.7 (его
обычно нет). Один py3-изм в теле мода -> SyntaxError в клиенте -> мод молча
мёртв, одна строка ERROR в python.log. Этот скрипт — единственный барьер.

Как он проверяет (три независимых прохода)
------------------------------------------
1. AST-проход (основной).  Файл разбирается парсером Python 3 (ast.parse),
   и дерево проверяется на узлы, которых в Python 2.7 НЕ существует:
   f-строки, аннотации (переменных, аргументов, возврата), keyword-only и
   positional-only аргументы, `raise X from Y`, `metaclass=` в скобках класса,
   `{**d}` и `[*a]`, распаковка в целях присваивания, walrus, async/await,
   `yield from`, `nonlocal`, матричное умножение `@`, `1_000`, `x = ...`,
   а также py3-семантика `print(a, b)` и `print(..., sep=)`.

   ВАЖНО: lib2to3 сам по себе НИЧЕГО из этого не ловит — его
   `pygram.python_grammar` это ОБЪЕДИНЁННАЯ грамматика 2/3 (иначе 2to3 не смог
   бы разбирать py3-код), и `def f(a, *, b=1)` она принимает молча. Поэтому
   гейтом служит AST-проход, а lib2to3 — только дополнение к нему.

2. lib2to3-проход (дополнительный).  Ловит совсем поломанную разметку и
   работает даже там, где ast.parse отказал. В Python 3.13 модуль lib2to3
   удалён — тогда проход пропускается (печатается WARN:lib2to3-unavailable),
   а AST-проход продолжает работать.

3. Текстовые эвристики.  pathlib, typing, super() без аргументов —
   синтаксически это валидный Python 2, но в клиенте 2.7 работать не будет.

Если ast.parse падает с SyntaxError — это, скорее всего, синтаксис,
существующий ТОЛЬКО в Python 2 (`print x`, `except E, e:`, `123L`, backtick,
`0777`, `<>`, `ur''`). Для .py-тела мода это законно, поэтому такой файл
не считается ошибкой: он просто проверяется одним lib2to3. Но помните, что
tools/py27c.py такой файл не соберёт (он тоже парсит через ast Python 3).
На Python 3.13+ (без lib2to3) проверить такой файл нечем, и он даёт код 1:
пишите тело в общем синтаксисе py2/py3, собирайте на Python 3.12 или
отключайте проверку (build.py --no-syntax-check).

Файлы читаются как байты и декодируются по объявлению кодировки
(`# -*- coding: ... -*-`, иначе UTF-8; BOM понимается). Файл, который не
декодируется, — это проблема в списке, а не падение проверки.

Коды возврата
-------------
    0 — проблем не найдено
    1 — найдены проблемы (список напечатан)
    2 — неверные аргументы / нечего проверять
    3 — сбой самой проверки (traceback в stderr); это не вердикт о вашем коде
"""

import ast
import io
import os
import re
import sys
import tokenize
import traceback
import warnings

warnings.filterwarnings('ignore')

SKIP_MARKER = 'WARN:lib2to3-unavailable'
EXIT_OK, EXIT_PROBLEMS, EXIT_NOTHING, EXIT_CRASH = 0, 1, 2, 3
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TARGET = os.path.join(REPO, 'src')

try:
    from lib2to3 import pygram, pytree
    from lib2to3.pgen2 import driver
    HAVE_LIB2TO3 = True
except Exception:                                    # pragma: no cover
    pygram = pytree = driver = None
    HAVE_LIB2TO3 = False


# --------------------------------------------------------------------------- #
#  1. AST-проход: узлы, которых нет в Python 2.7
# --------------------------------------------------------------------------- #

def _node_types(*names):
    """Типы узлов ast по именам, пропуская отсутствующие в этой версии Python."""
    out = []
    for name in names:
        node = getattr(ast, name, None)
        if node is not None:
            out.append(node)
    return tuple(out)


# (типы узлов, объяснение) — простое совпадение по типу
_SIMPLE_BAD = [
    (_node_types('JoinedStr', 'FormattedValue'), 'f-строка — в Python 2.7 не существует'),
    (_node_types('AnnAssign'), 'аннотация переменной (x: int = 1) — синтаксис Python 3'),
    (_node_types('Nonlocal'), 'nonlocal — в Python 2.7 не существует'),
    (_node_types('YieldFrom'), 'yield from — в Python 2.7 не существует'),
    (_node_types('AsyncFunctionDef', 'Await', 'AsyncFor', 'AsyncWith'),
     'async/await — в Python 2.7 не существует'),
    (_node_types('NamedExpr'), 'оператор := (walrus) — синтаксис Python 3.8'),
    (_node_types('MatMult'), 'матричное умножение @ — синтаксис Python 3.5'),
]

_PRINT_KWARGS = ('sep', 'end', 'file', 'flush')


def _pos(node, path):
    return '%s:%s' % (path, getattr(node, 'lineno', '?'))


def _check_arguments(node, args, path, out):
    """Аргументы функции/лямбды: аннотации, keyword-only, positional-only."""
    if getattr(args, 'kwonlyargs', None):
        out.append('%s: keyword-only аргумент (def f(a, *, b=1)) — синтаксис '
                   'Python 3, в 2.7 это SyntaxError' % _pos(node, path))
    if getattr(args, 'posonlyargs', None):
        out.append('%s: positional-only аргумент (def f(a, /)) — синтаксис '
                   'Python 3.8' % _pos(node, path))
    every = list(getattr(args, 'posonlyargs', None) or [])
    every += list(args.args or [])
    every += list(getattr(args, 'kwonlyargs', None) or [])
    for extra in (getattr(args, 'vararg', None), getattr(args, 'kwarg', None)):
        if extra is not None and not isinstance(extra, str):
            every.append(extra)
    for arg in every:
        if getattr(arg, 'annotation', None) is not None:
            out.append('%s: аннотация аргумента (def f(x: int)) — синтаксис '
                       'Python 3' % _pos(node, path))
            break


def _check_starred_targets(target, path, out, what):
    """`a, *rest = ...` / `for a, *b in ...` — PEP 3132, только Python 3."""
    starred = _node_types('Starred')
    if not starred:
        return
    stack = [target]
    while stack:
        node = stack.pop()
        if isinstance(node, starred):
            out.append('%s: звёздочная распаковка в %s (a, *rest = ...) — '
                       'синтаксис Python 3' % (_pos(node, path), what))
            return
        if isinstance(node, (ast.Tuple, ast.List)):
            stack.extend(node.elts)


def py3_only_problems(source, path):
    """Список py3-конструкций в исходнике. [] — чисто.

    Второе значение — «разобран ли файл парсером Python 3». Возвращает ([], False),
    если ast.parse не смог разобрать файл: тогда это,
    скорее всего, py2-only синтаксис, и судить о нём должен lib2to3.
    """
    out = []
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return out, False
    except Exception:
        return out, False

    starred = _node_types('Starred')

    for node in ast.walk(tree):
        for types_, message in _SIMPLE_BAD:
            if types_ and isinstance(node, types_):
                out.append('%s: %s' % (_pos(node, path), message))

        # --- функции и лямбды ------------------------------------------- #
        if isinstance(node, ast.FunctionDef) or \
                (_node_types('AsyncFunctionDef') and
                 isinstance(node, _node_types('AsyncFunctionDef'))):
            if getattr(node, 'returns', None) is not None:
                out.append('%s: аннотация возвращаемого типа (-> T) — '
                           'синтаксис Python 3' % _pos(node, path))
            _check_arguments(node, node.args, path, out)
        elif isinstance(node, ast.Lambda):
            _check_arguments(node, node.args, path, out)

        # --- классы: metaclass= / **kwargs в списке баз ------------------ #
        elif isinstance(node, ast.ClassDef):
            if getattr(node, 'keywords', None):
                out.append('%s: class A(..., metaclass=M) — синтаксис Python 3; '
                           'в 2.7 пишут __metaclass__ = M внутри класса'
                           % _pos(node, path))
            for base in node.bases:
                if starred and isinstance(base, starred):
                    out.append('%s: class A(*bases) — синтаксис Python 3'
                               % _pos(node, path))

        # --- raise X from Y ---------------------------------------------- #
        elif isinstance(node, ast.Raise):
            if getattr(node, 'cause', None) is not None:
                out.append('%s: raise X from Y — синтаксис Python 3'
                           % _pos(node, path))

        # --- цели присваивания ------------------------------------------- #
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                _check_starred_targets(target, path, out, 'цели присваивания')
        elif isinstance(node, ast.For):
            _check_starred_targets(node.target, path, out, 'цикле for')
        elif _node_types('comprehension') and isinstance(node, ast.comprehension):
            _check_starred_targets(node.target, path, out, 'генераторе')

        # --- {**d} -------------------------------------------------------- #
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if key is None:
                    out.append('%s: {**d} в словарном литерале — синтаксис '
                               'Python 3.5' % _pos(node, path))
                    break

        # --- [*a] / (*a,) / {*a} ------------------------------------------ #
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            if isinstance(getattr(node, 'ctx', None), ast.Load) and starred:
                for elt in node.elts:
                    if isinstance(elt, starred):
                        out.append('%s: звёздочная распаковка в литерале '
                                   '([*a], {*a}) — синтаксис Python 3.5'
                                   % _pos(node, path))
                        break

        # --- вызовы: f(*a, *b), f(**a, **b), print(a, b) ------------------ #
        elif isinstance(node, ast.Call):
            if starred:
                stars = [a for a in node.args if isinstance(a, starred)]
                if len(stars) > 1:
                    out.append('%s: f(*a, *b) — несколько * в одном вызове, '
                               'синтаксис Python 3.5' % _pos(node, path))
                if stars and node.args and not isinstance(node.args[-1], starred):
                    out.append('%s: позиционный аргумент после *args в вызове — '
                               'синтаксис Python 3.5' % _pos(node, path))
            kwargs_stars = [k for k in (node.keywords or []) if k.arg is None]
            if len(kwargs_stars) > 1:
                out.append('%s: f(**a, **b) — несколько ** в одном вызове, '
                           'синтаксис Python 3.5' % _pos(node, path))
            if isinstance(node.func, ast.Name) and node.func.id == 'print':
                positional = [a for a in node.args]
                if starred and any(isinstance(a, starred) for a in positional):
                    out.append('%s: print(*args) — в Python 2.7 print это '
                               'инструкция, распаковка после неё SyntaxError'
                               % _pos(node, path))
                if len(positional) > 1:
                    out.append(
                        '%s: print(a, b) — в Python 2.7 без '
                        '`from __future__ import print_function` это '
                        'print-ИНСТРУКЦИЯ с одним операндом-кортежем и напечатает '
                        "(a, b). Пишите print('%%s %%s' %% (a, b)) или логируйте "
                        'через logging' % _pos(node, path))
                for kw in (node.keywords or []):
                    if kw.arg in _PRINT_KWARGS:
                        out.append('%s: print(..., %s=) — синтаксис Python 3'
                                   % (_pos(node, path), kw.arg))

        # --- x = ...  (Ellipsis как литерал) ------------------------------ #
        elif hasattr(ast, 'Constant') and isinstance(node, ast.Constant):
            if node.value is Ellipsis:
                out.append('%s: литерал ... вне среза — синтаксис Python 3'
                           % _pos(node, path))

    return out, True


# --------------------------------------------------------------------------- #
#  3. Текстовые эвристики (валидный py2-синтаксис, но в 2.7 не работает)
# --------------------------------------------------------------------------- #

PY3_PATTERNS = [
    # Отрицательный lookbehind на кавычку обязателен: без него 'rb' внутри
    # open(path, 'rb') читалось бы как py3-префикс rb''.
    (re.compile(r'''(?<![\w'"])(?:[fF]|[fF][rR]|[rR][fF])["']'''),
     'f-строка — в Python 2.7 не существует'),
    (re.compile(r'^\s*def\s+\w+\s*\([^)]*\)\s*->'), 'аннотация возвращаемого типа'),
    (re.compile(r'\bsuper\(\s*\)'), 'super() без аргументов — нужен super(Класс, self)'),
    (re.compile(r'^\s*import\s+pathlib|from\s+pathlib\b'), 'pathlib отсутствует в 2.7'),
    (re.compile(r'\bnonlocal\b'), 'nonlocal отсутствует в 2.7'),
    (re.compile(r'^\s*from\s+typing\b|^\s*import\s+typing\b'),
     'typing в клиенте есть, но аннотации в комментариях безопаснее'),
    (re.compile(r'\byield\s+from\b'), 'yield from отсутствует в 2.7'),
    (re.compile(r'\b\d[\d_]*_[\d_]*\b'), 'подчёркивания в числовом литерале (1_000) — Python 3.6'),
    (re.compile(r'''(?<![\w'"])[rR][bB]["']'''),
     "префикс rb'' — в Python 2.7 допустим только порядок br''"),
]


def strip_comment(line):
    """Убирает # -комментарий, НЕ трогая решётки внутри строковых литералов.

    Наивное line.split('#', 1)[0] резало строку по решётке в литерале
    (например, u'цвет #FF0000') и терял всё, что стоит правее.
    """
    out = []
    quote = None
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if quote is None:
            if ch == '#':
                break
            if ch in '"\'':
                quote = ch
            out.append(ch)
        else:
            out.append(ch)
            if ch == '\\':
                if i + 1 < n:
                    out.append(line[i + 1])
                    i += 1
            elif ch == quote:
                quote = None
        i += 1
    return ''.join(out)


# --------------------------------------------------------------------------- #
#  проверка одного файла
# --------------------------------------------------------------------------- #

def read_source(path):
    """Текст файла по его объявлению кодировки (иначе UTF-8), без BOM.

    Бросает UnicodeDecodeError / SyntaxError (неизвестная кодировка в
    объявлении) — check_file превращает их в строку отчёта.
    """
    with io.open(path, 'rb') as fh:
        raw = fh.read()
    encoding, _lines = tokenize.detect_encoding(io.BytesIO(raw).readline)
    src = raw.decode(encoding)
    if src.startswith(u'﻿'):
        src = src[1:]
    return src


def check_file(path, drv=None):
    problems = []
    try:
        src = read_source(path)
    except (UnicodeDecodeError, SyntaxError, LookupError) as exc:
        return ['%s: файл не читается в объявленной кодировке (или UTF-8, если '
                'объявления нет): %s' % (path, exc)]
    if not src.endswith('\n'):
        src += '\n'
        problems.append('%s: файл не заканчивается переводом строки — '
                        'compile() в Python 2.7 бросит "unexpected EOF while parsing", '
                        'если последняя конструкция внутри отступа' % path)

    # --- 1. AST ---------------------------------------------------------- #
    ast_problems, parsed_by_py3 = py3_only_problems(src, path)
    problems.extend(ast_problems)

    # --- 3. эвристики ---------------------------------------------------- #
    for lineno, line in enumerate(src.splitlines(), 1):
        stripped = strip_comment(line)
        for rx, msg in PY3_PATTERNS:
            if rx.search(stripped):
                problems.append('%s:%d: %s' % (path, lineno, msg))

    # --- 2. lib2to3 ------------------------------------------------------ #
    if drv is not None:
        try:
            drv.parse_string(src)
        except Exception as exc:
            problems.append('%s: НЕ ПАРСИТСЯ грамматикой lib2to3: %s: %s'
                            % (path, type(exc).__name__, exc))
    elif not parsed_by_py3 and not HAVE_LIB2TO3:
        problems.append('%s: файл не разобрался парсером Python 3, а lib2to3 '
                        'недоступен — синтаксис НЕ ПРОВЕРЕН' % path)

    return problems


def collect(targets):
    files = []
    for target in targets:
        if os.path.isfile(target):
            files.append(target)
            continue
        for root, dirs, names in os.walk(target):
            dirs[:] = sorted(d for d in dirs if d not in ('__pycache__', '.git'))
            for name in sorted(names):
                if name.endswith('.py'):
                    files.append(os.path.join(root, name))
    return files


def main(targets):
    drv = None
    if HAVE_LIB2TO3:
        drv = driver.Driver(pygram.python_grammar, convert=pytree.convert)
    else:
        sys.stderr.write(
            SKIP_MARKER + ': модуль lib2to3 удалён в Python 3.13, '
            'дополнительный грамматический проход пропущен '
            '(основная AST-проверка выполнена)\n')

    files = collect(targets)
    if not files:
        sys.stderr.write('не найдено ни одного .py в %s\n' % ', '.join(targets))
        return EXIT_NOTHING

    all_problems = []
    for path in files:
        problems = check_file(path, drv)
        status = 'OK' if not problems else 'ПРОБЛЕМЫ'
        print('[%s] %s' % (status, path))
        all_problems.extend(problems)

    if all_problems:
        print('\n--- найдено: %d ---' % len(all_problems))
        for p in all_problems:
            print('  ' + p)
        return EXIT_PROBLEMS
    print('\nвсе %d файл(ов) соответствуют синтаксису Python 2.7' % len(files))
    return EXIT_OK


def _entry(argv):
    # Русский вывод не должен ронять проверку на консоли с другой кодовой
    # страницей (cp1252, перенаправление в файл или канал).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    try:
        return main(argv or [DEFAULT_TARGET])
    except KeyboardInterrupt:
        return 130
    except Exception:
        sys.stdout.flush()
        sys.stderr.write('СБОЙ САМОЙ ПРОВЕРКИ (это не вердикт о вашем коде):\n')
        traceback.print_exc()
        return EXIT_CRASH


if __name__ == '__main__':
    raise SystemExit(_entry(sys.argv[1:]))
