# -*- coding: utf-8 -*-
# =============================================================================
#  Hello Tanker -- ТОЧКА ВХОДА (тонкий загрузчик).
#
#  Этот файл build.py компилирует в mod_hello_tanker.PYC (Python 2.7, magic
#  03 F3 0D 0A) собственным компилятором tools/py27c.py -- Python 2.7 на машине
#  сборки НЕ НУЖЕН.  Загрузчик модов клиента видит ТОЛЬКО файлы вида
#  mod_*.pyc, поэтому .pyc здесь обязателен.
#
#  ВЕСЬ реальный код мода лежит рядом в обычных .py:
#      res/scripts/client/gui/mods/hello_tanker/main.py
#  Их править можно как угодно (классы, генераторы, with, list comprehensions -
#  всё, что умеет Python 2.7): пересобирать .pyc после этого НЕ надо, потому
#  что этот файл не меняется.
#
#  ДВА СПОСОБА ЗАГРУЗКИ (пробуются по очереди, что сработало - видно в python.log):
#    1) Штатный импортёр движка.  Движок, по-видимому, умеет импортировать .py
#       из виртуальной файловой системы (в т.ч. из .mtmod); если это так -
#       хватает обычного __import__('gui.mods.hello_tanker.main').
#    2) Запасной путь, если штатный отказал.  Сами читаем исходник через
#       ResMgr.openSection(path).asBinary (так клиент читает свои файлы из
#       виртуальной ФС), компилируем встроенным compile() и выполняем.
#       Ставится как PEP-302 finder в sys.meta_path, поэтому внутри пакета
#       можно спокойно делать import gui.mods.hello_tanker.X.
#
#  ПЕРВАЯ УСТАНОВКА ОБЯЗАНА НАЧИНАТЬСЯ С ПРОВЕРКИ ЛОГА: найдите в python.log
#  строки с "[HelloTanker/loader]".  Там должна быть ровно одна из строк
#  "loaded by the engine ResMgr importer" или "loaded by the built-in ResMgr
#  fallback loader".  Если нет ни одной - README, разделы «Как тело мода
#  попадает в игру» и «Мод не загрузился» (запасной вариант: --compile-all).
#
#  СИНТАКСИС ЭТОГО ФАЙЛА ОГРАНИЧЕН подмножеством, которое понимает py27c:
#  без with/yield/comprehension/f-строк/замыканий.  Смотри шапку tools/py27c.py.
# =============================================================================
"""Hello Tanker entry-point stub (compiled to Python 2.7 bytecode by py27c)."""
import sys

MOD_ID = 'ru.local.hello_tanker'
PACKAGE = 'gui.mods.hello_tanker'
ENTRY = 'gui.mods.hello_tanker.main'
RES_DIR = 'scripts/client/gui/mods/hello_tanker'

_impl = None
_loaded = False


# --------------------------------------------------------------------------- #
#  логирование
# --------------------------------------------------------------------------- #
def _note(message):
    """LOG_NOTE в python.log.

    Текст держим ASCII не из-за кодировки (клиент при старте ставит кодировку
    по умолчанию utf-8, поэтому кириллица в лог проходит), а потому что это
    диагностика загрузчика: её грепают, ей не нужен перевод.
    Если всё же пишете сюда не-ASCII - помните, что debug_utils приводит
    сообщение к unicode: py2-строка с байтами НЕ в utf-8 уронит запись целиком.
    """
    try:
        import debug_utils
        debug_utils.LOG_NOTE('[HelloTanker/loader] ' + message)
    except Exception:
        pass


def _error(message):
    try:
        import debug_utils
        debug_utils.LOG_ERROR('[HelloTanker/loader] ' + message)
        debug_utils.LOG_CURRENT_EXCEPTION()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  запасной загрузчик: читаем .py из .mtmod через ResMgr
# --------------------------------------------------------------------------- #
def _read(vfs_path):
    """Содержимое файла виртуальной ФС движка или None."""
    try:
        import ResMgr
        section = ResMgr.openSection(vfs_path)
        if section is None:
            return None
        return str(section.asBinary)
    except Exception:
        return None


def _locate(fullname):
    """(source, vfs_path, is_package) для gui.mods.hello_tanker[.X] или None.

    Обычный модуль <base>.py пробуем ПЕРВЫМ, пакет <base>/__init__.py вторым.
    Порядок важен: если ResMgr.openSection когда-нибудь вернёт непустую секцию
    на несуществующий путь внутри существующего каталога, main стал бы пустым
    ПАКЕТОМ без init()/fini(), и мод молча ничего бы не делал.
    """
    tail = fullname[len(PACKAGE):].replace('.', '/')
    base = RES_DIR + tail
    path = base + '.py'
    source = _read(path)
    if source is not None:
        return (source, path, False)
    path = base + '/__init__.py'
    source = _read(path)
    if source is not None:
        return (source, path, True)
    return None


class _ResMgrFinder(object):
    """PEP-302 finder/loader: отдаёт пакет мода прямо из .mtmod через ResMgr."""

    def find_module(self, fullname, path=None):
        if fullname != PACKAGE and not fullname.startswith(PACKAGE + '.'):
            return None
        if _locate(fullname) is None:
            return None
        return self

    def load_module(self, fullname):
        if fullname in sys.modules:
            return sys.modules[fullname]
        found = _locate(fullname)
        if found is None:
            raise ImportError('hello_tanker fallback loader: no source for ' + fullname)
        import types
        module = types.ModuleType(fullname)
        module.__file__ = found[1]
        module.__loader__ = self
        if found[2]:
            # __path__ ВЛОЖЕННОГО пакета - его собственный каталог, а не RES_DIR:
            # иначе gui.mods.hello_tanker.sub искал бы модули в корне пакета.
            module.__path__ = [found[1].rsplit('/', 1)[0]]
        sys.modules[fullname] = module
        try:
            # compile() в Python 2.7 требует ДВУХ вещей: переводы строк только
            # '\n' И завершающий перевод строки. Без второго последняя строка
            # внутри отступа даёт "SyntaxError: unexpected EOF while parsing"
            # (в Python 3.2 это требование сняли, в 2.7 оно действует).
            source = found[0].replace('\r\n', '\n').replace('\r', '\n')
            if not source.endswith('\n'):
                source = source + '\n'
            code = compile(source, found[1], 'exec')
            eval(code, module.__dict__)
        except Exception:
            del sys.modules[fullname]
            raise
        return module


def _install_fallback():
    i = 0
    while i < len(sys.meta_path):
        if getattr(sys.meta_path[i], '__class__', None) is _ResMgrFinder:
            return
        i = i + 1
    sys.meta_path.insert(0, _ResMgrFinder())


def _remove_fallback():
    """Снять свой finder с sys.meta_path (симметрично _install_fallback)."""
    i = len(sys.meta_path) - 1
    while i >= 0:
        if getattr(sys.meta_path[i], '__class__', None) is _ResMgrFinder:
            del sys.meta_path[i]
        i = i - 1


# --------------------------------------------------------------------------- #
#  загрузка тела мода
# --------------------------------------------------------------------------- #
def _load():
    global _impl
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        __import__(ENTRY)
        _impl = sys.modules[ENTRY]
        _note('loaded by the engine ResMgr importer: ' + ENTRY)
        return
    except Exception:
        # НЕ глушим молча. Это единственная запись, по которой потом можно
        # отличить "движок не умеет .py из VFS" от "в теле мода SyntaxError"
        # (во втором случае упадут ОБА пути, причём по одной и той же причине).
        _note('engine importer refused: ' + repr(sys.exc_info()[1]))
    try:
        _install_fallback()
        __import__(ENTRY)
        _impl = sys.modules[ENTRY]
        _note('loaded by the built-in ResMgr fallback loader: ' + ENTRY)
    except Exception:
        _impl = None
        _error('cannot load ' + ENTRY + ' from ' + RES_DIR)


def _call(name, args):
    """Позвать обработчик тела мода. Исключения дальше НЕ пускаем.

    Загрузчик клиента ловит только AttributeError; любое другое исключение
    доходит до game.init(), где стоит BigWorld.quit() -- клиент закроется.
    """
    if _impl is None:
        return
    handler = getattr(_impl, name, None)
    if handler is None:
        return
    try:
        handler(*args)
    except Exception:
        _error('unhandled exception in ' + name + '()')


# --------------------------------------------------------------------------- #
#  API, которое дёргает gui.mods
# --------------------------------------------------------------------------- #
def init():
    try:
        _load()
    except Exception:
        _error('loader itself crashed')
    _call('init', ())


def fini():
    _call('fini', ())
    # Симметрично _install_fallback(): свой finder с sys.meta_path снимаем.
    try:
        _remove_fallback()
    except Exception:
        pass


def onAccountShowGUI(ctx):
    _call('onAccountShowGUI', (ctx,))


def onAccountBecomePlayer():
    _call('onAccountBecomePlayer', ())


def onAccountBecomeNonPlayer():
    _call('onAccountBecomeNonPlayer', ())


def onAvatarBecomePlayer():
    _call('onAvatarBecomePlayer', ())


def onConnected():
    _call('onConnected', ())


def onDisconnected():
    _call('onDisconnected', ())
