# -*- coding: utf-8 -*-
# =============================================================================
#  Hello Tanker - учебный шаблон мода для клиента «Мир танков» (Lesta), Python 2.7
# =============================================================================
#
#  ЧТО ДЕЛАЕТ МОД
#    A) при старте клиента пишет в python.log своё имя, версию и версию клиента;
#    B) в ангаре (после появления GUI аккаунта) показывает системное сообщение;
#    C) в бою один раз выводит в боевой чат карту, тип боя и состав команд;
#    D) читает настройки из mods/configs/hello_tanker/config.json (файл не обязателен);
#    E) любая ошибка внутри мода ловится и логируется, клиент не падает.
#
#  ПОЧЕМУ ИМЕННО ТАК (важно понимать до правок)
#    Загрузчик модов клиента (пакет gui.mods):
#      * берёт список файлов каталога scripts/client/gui/mods;
#      * приводит имена к нижнему регистру;
#      * оставляет только имена вида mod_*.pyc (на боевом клиенте ТОЛЬКО .pyc:
#        .py-точку входа читают лишь dev-сборки клиента);
#      * импортирует их как gui.mods.mod_xxx;
#      * вызывает init() при старте и fini() при выходе;
#      * рассылает ровно 6 событий:
#          onAccountShowGUI(ctx), onAccountBecomePlayer(), onAccountBecomeNonPlayer(),
#          onAvatarBecomePlayer(), onConnected(), onDisconnected().
#
#    КРИТИЧНО: загрузчик гасит только ImportError (на импорте модуля) и только
#    AttributeError (при вызове функций мода). Любое другое исключение из init()
#    доходит до инициализации клиента, а та в ответ на ошибку закрывает игру.
#    То есть НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ В МОДЕ ЗАКРЫВАЕТ КЛИЕНТ, а ещё обрывает
#    обход модов и лишает init() все моды, чья очередь ещё не наступила
#    (XVM и другие). Поэтому здесь КАЖДАЯ функция, которую может вызвать игра,
#    обёрнута в декоратор @_safe.
#
#  СИНТАКСИС: только Python 2.7. Никаких f-строк, аннотаций, keyword-only
#  аргументов, `raise X from Y`, {**d}, walrus, pathlib, print-функции и
#  super() без аргументов.
#
#  ЭТОТ ФАЙЛ НИКТО НЕ КОМПИЛИРУЕТ НА МАШИНЕ СБОРКИ: он едет в .mtmod обычным
#  .py и компилируется уже внутри игры. Значит, единственная защита от опечатки
#  до запуска клиента - tools/check_py2_syntax.py, который build.py гоняет по
#  всему src/ перед упаковкой (и повторно по каждому файлу в stage()).
#  Не отключайте его флагом --no-syntax-check «чтобы быстрее».
# =============================================================================

# --- Импорты верхнего уровня -------------------------------------------------
# Здесь допустимы ТОЛЬКО безопасные вещи: стандартная библиотека Python 2.7
# (она целиком лежит в scripts/common/Lib) и нативный модуль BigWorld.
# Всё игровое (Avatar, PlayerEvents, gui.*) импортируется ЛЕНИВО внутри
# функций: если в следующем патче игры модуль переименуют или он бросит
# что-то отличное от ImportError, мод просто отключится с записью в лог,
# а не уронит клиент на этапе импорта.
import json
import logging
import os
import sys
import traceback

import BigWorld

# --- Паспорт мода ------------------------------------------------------------
MOD_ID = 'ru.local.hello_tanker'
MOD_NAME = 'Hello Tanker'
MOD_VERSION = '1.0.0'
LOG_PREFIX = '[HelloTanker]'

# Логгер stdlib. BWLogging.init() вешает на КОРНЕВОЙ логгер BWLogRedirectionHandler
# и ставит ему уровень TRACE, поэтому наши записи гарантированно попадают
# в python.log в виде «INFO: [HelloTanker] ...» / «DEBUG: [HelloTanker] ...».
#
# ВАЖНО-1: debug_utils.LOG_DEBUG на релизном клиенте ничего не пишет (уровень
#   отладки там отключён), поэтому её не используем.
#
# ВАЖНО-2 (кодировка лога). Обработчик logging кодирует сообщение кодировкой
# sys.getdefaultencoding(), а debug_utils приводит его к unicode. Ключевой
# вопрос: чему равна sys.getdefaultencoding() в этом клиенте?
#
#   'utf-8', а НЕ 'ascii': клиент ставит её при старте (модуль bw_site, ещё до
#   загрузки модов), а в python.log это видно строкой
#   "INFO: [Config] Default encoding set to utf-8".
#
# Следствия:
#   * unicode-строка проходит в лог всегда;
#   * py2-str с UTF-8 байтами тоже проходит (неявный decode('utf-8') удаётся);
#   * ОПАСЕН только py2-str в ДРУГОЙ кодировке - например путь из os.getcwd()
#     на Windows с русской локалью (cp1251). Такой аргумент даёт
#     UnicodeDecodeError, logging глушит его через handleError, и запись
#     ТЕРЯЕТСЯ ЦЕЛИКОМ. Поэтому пути прогоняем через _fs_text() (см. ниже),
#     а не через _ascii().
#
# Практика в этом файле: строки ФОРМАТА оставлены английскими - их грепают,
# и они одинаково читаются в любой консоли. Данные (имя карты, путь к конфигу)
# пишем как есть, кириллицей: в python.log она видна нормально.
_logger = logging.getLogger('HelloTanker')

# --- Настройки по умолчанию --------------------------------------------------
# Мод обязан работать без файла конфигурации. Эти значения используются как есть,
# а найденный JSON накладывается поверх них (рекурсивное слияние словарей).
DEFAULT_CONFIG = {
    'enabled': True,                       # общий выключатель мода
    'debug': False,                        # подробный лог (уровень DEBUG)
    'hangar_greeting': {
        'enabled': True,
        'delay_sec': 3.0,                  # пауза после появления GUI ангара
        'once_per_session': True,          # показать один раз за запуск клиента
        'text': u'{mod} v{version}: мод загружен. Хорошего боя, танкист!'
    },
    'battle_report': {
        'enabled': True,
        'delay_sec': 2.0,                  # пауза после onAvatarReady
        'show_in_replay': False,           # в реплеях по умолчанию молчим
        'text': u'{mod}: карта «{map}», режим «{battle_type}», союзников {allies}, противников {enemies}'
    },
    'count_own_hits': True,                # демонстрация безопасного монкипатча
    'log_pump': {
        # Принудительный сброс python.log на диск. Клиент делает это сам ТОЛЬКО
        # в dev-сборке, а на боевом клиенте - нет, и свежие строки лога висят в буфере,
        # пока движок не соберётся их записать. Для внешнего наблюдателя
        # (tail -f, WoT Mod Cockpit) лог из-за этого выглядит замершим.
        'enabled': True,
        'interval_sec': 0.2                # период сброса; клиент в dev-режиме берёт 5.0
    }
}

# Путь к пользовательскому конфигу ОТНОСИТЕЛЬНО корня игры.
# Это распространённое соглашение модов (mods/configs/<имя>/...), а не
# требование движка.
CONFIG_RELATIVE_PATH = os.path.join('mods', 'configs', 'hello_tanker', 'config.json')

# --- Состояние мода ----------------------------------------------------------
# Держим всё в модульных переменных: мод - синглтон по своей природе,
# загрузчик импортирует модуль ровно один раз.
_config = None                 # dict с итоговыми настройками
_greeting_shown = False        # флаг «приветствие уже показано»
_battle_reported = False       # флаг «отчёт по бою уже выведен»
_own_piercing_hits = 0         # счётчик собственных пробитий (демо монкипатча)
_subscribed = False            # подписаны ли мы на g_playerEvents

# Отложенные вызовы разделены ПО НАЗНАЧЕНИЮ. Одна общая корзина - грабли:
# выход из боя гасил бы и ангарное приветствие, поставленное с задержкой 3 с,
# а разрыв соединения - боевой отчёт. Ключи: 'hangar', 'battle', 'logpump'.
CB_HANGAR = 'hangar'
CB_BATTLE = 'battle'
# Пампинг python.log живёт в СВОЕЙ корзине и намеренно переживает разрыв
# соединения: именно в этот момент лог нужнее всего. Гасится только в fini().
CB_LOGPUMP = 'logpump'
_callbacks = {CB_HANGAR: set(), CB_BATTLE: set(), CB_LOGPUMP: set()}


# =============================================================================
#  СЛУЖЕБНОЕ: строки
# =============================================================================

def _to_unicode(value):
    """Приводит что угодно к unicode, не падая на кривой кодировке.

    В Python 2.7 смешивать str и unicode опасно: конкатенация или .format()
    с не-ASCII байтами роняет UnicodeDecodeError. Все тексты, которые уходят
    в GUI (SystemMessages, боевой чат), должны быть unicode.
    """
    if isinstance(value, unicode):
        return value
    if isinstance(value, str):
        try:
            return value.decode('utf-8')
        except UnicodeDecodeError:
            return value.decode('utf-8', 'replace')
    try:
        return unicode(value)
    except Exception:
        return u'<?>'


def _fs_text(value):
    """Путь ОС -> unicode через кодировку файловой системы.

    В Python 2.7 на Windows os.getcwd() и ResMgr.appDirectory() отдают
    БАЙТОВУЮ строку в системной ANSI-кодировке (cp1251 для русской локали),
    а вовсе не UTF-8. Слепой decode('utf-8') на пути вида C:\\Игры\\Танки дал бы
    строку из U+FFFD; в логе это мусор, а если такой «путь» ещё и вернуть
    в os.path.isfile(), файл никогда не найдётся.

    ВАЖНО: результат этой функции годится ТОЛЬКО для показа (лог, GUI).
    Для файловых операций держите путь БАЙТОВЫМ - см. _game_root().
    """
    if isinstance(value, unicode):
        return value
    if isinstance(value, str):
        encodings = []
        try:
            fs_enc = sys.getfilesystemencoding()
            if fs_enc:
                encodings.append(fs_enc)
        except Exception:
            pass
        encodings.append('utf-8')
        for enc in encodings:
            try:
                return value.decode(enc)
            except Exception:
                continue
        return value.decode('utf-8', 'replace')
    return _to_unicode(value)


def _ascii(value):
    """Приводит что угодно к ЧИСТО ASCII байтовой строке.

    В ЭТОМ клиенте sys.getdefaultencoding() == 'utf-8' (см. шапку файла),
    поэтому кириллица в логе работает и оборачивать в _ascii() имена карт
    и пути НЕ НУЖНО - это только испортит лог \\uXXXX-последовательностями.

    Функция осталась ровно для одного случая: traceback.format_exc(). Там
    в repr() чужих объектов вполне может оказаться py2-str в неизвестной
    кодировке, и тогда неявный decode('utf-8') уронил бы всю запись лога.
    """
    try:
        return _to_unicode(value).encode('ascii', 'backslashreplace')
    except Exception:
        try:
            return repr(value)[:512]
        except Exception:
            return '<unprintable>'


# =============================================================================
#  СЛУЖЕБНОЕ: защита от исключений
# =============================================================================

def _safe(func):
    """Декоратор-«предохранитель».

    Оборачивает функцию в try/except Exception и пишет полный traceback в лог.
    Наружу исключение НЕ выпускается никогда - именно это и не даёт моду
    закрыть клиент через BigWorld.quit() в game.init().

    Отдельно важно, что мы ловим и логируем даже AttributeError: если он
    вылетит из init() мода, загрузчик проглотит его молча
    (except AttributeError: pass в _callModMethod), и мод «тихо не заработает»
    без единой строки в логе.
    """

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            # Само логирование тоже под защитой: если и оно упадёт
            # (например, обработчик логов уже разрушен при выходе из клиента),
            # мод всё равно не должен ронять игру.
            try:
                _logger.error('%s error in %s():\n%s',
                              LOG_PREFIX,
                              getattr(func, '__name__', '?'),
                              _ascii(traceback.format_exc()))
            except Exception:
                pass
            return None

    # Сохраняем имя и документацию, чтобы отладка по логу оставалась читаемой.
    # functools.wraps здесь не используем намеренно: в py2.7 он копирует
    # __dict__ и может неожиданно перетереть служебные атрибуты монкипатча.
    wrapper.__name__ = getattr(func, '__name__', 'wrapper')
    wrapper.__doc__ = getattr(func, '__doc__', None)
    return wrapper


# =============================================================================
#  РАБОТА С КОНФИГУРАЦИЕЙ (требование D)
# =============================================================================

def _deep_merge(base, override):
    """Рекурсивно накладывает override на base и возвращает НОВЫЙ словарь.

    Зачем не base.update(override): нужно, чтобы пользователь мог указать
    в своём JSON только одну вложенную настройку (например,
    {"battle_report": {"enabled": false}}), не переписывая весь блок.

    В py2.7 dict.items() возвращает СПИСОК пар - итерироваться по нему
    безопасно даже если словарь меняют.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _game_root_candidates():
    """Возможные корни установленной игры, в порядке правдоподобия.

    Почему не один способ:
      * os.getcwd() - клиент стартует из корня игры (относительные пути вроде
        './replays' клиент и моды считают именно от него).
      * ResMgr.appDirectory() - это каталог ИСПОЛНЯЕМОГО файла. В python.log
        он виден в строке "[WinMain] Command line: <папка игры>/win64/Tanki.exe",
        то есть appDirectory() почти наверняка вернёт <папка игры>/win64/,
        а НЕ корень игры. Поэтому берём и его, и его родителя.

    Пути НЕ приводим к unicode: в py2 на Windows это байтовые строки в ANSI,
    и os.path.isfile()/open() отработают их через ANSI-API корректно.
    Слепой decode('utf-8') сломал бы установку в путь с кириллицей.
    """
    candidates = []

    def _add(value):
        if not value:
            return
        try:
            value = value.rstrip('/\\')
        except Exception:
            return
        if value and value not in candidates:
            candidates.append(value)

    try:
        _add(os.getcwd())
    except Exception:
        pass
    try:
        import ResMgr
        app_dir = ResMgr.appDirectory()
        _add(app_dir)
        if app_dir:
            _add(os.path.dirname(app_dir.rstrip('/\\')))
    except Exception:
        _logger.debug('%s ResMgr.appDirectory() is unavailable', LOG_PREFIX)
    if not candidates:
        candidates.append('.')
    return candidates


def _game_root():
    """Корень игры: первый кандидат, в котором есть подкаталог mods/."""
    candidates = _game_root_candidates()
    for root in candidates:
        try:
            if os.path.isdir(os.path.join(root, 'mods')):
                return root
        except Exception:
            continue
    return candidates[0]


def _config_path():
    """Полный путь к пользовательскому config.json.

    Возвращает первый кандидат, по которому файл РЕАЛЬНО существует; если ни по
    одному нет - путь от _game_root(), чтобы в логе было что показать.
    """
    for root in _game_root_candidates():
        try:
            path = os.path.join(root, CONFIG_RELATIVE_PATH)
            if os.path.isfile(path):
                return path
        except Exception:
            continue
    return os.path.join(_game_root(), CONFIG_RELATIVE_PATH)


@_safe
def _load_config():
    """Читает config.json поверх DEFAULT_CONFIG.

    Требование D: файла может не быть - тогда молча работаем на умолчаниях.
    Битый JSON тоже не должен ломать мод: логируем и берём умолчания.
    """
    global _config
    _config = dict(DEFAULT_CONFIG)

    path = _config_path()
    if not os.path.isfile(path):
        _logger.info('%s config not found (%s), using defaults',
                     LOG_PREFIX, _fs_text(path))
        return

    try:
        # Читаем как БАЙТЫ ('rb') и отдаём json.loads: он сам распознает UTF-8
        # и вернёт unicode-строки. Так мы не зависим от системной локали.
        f = open(path, 'rb')
        try:
            raw = f.read()
        finally:
            f.close()
        # BOM у файлов, сохранённых блокнотом Windows, json.loads не переваривает.
        # Литерал пишем с префиксом b: в Python 2.7 b'..' это тот же str, а файл
        # при этом остаётся проверяемым питоном 3 (см. tools/check_py2_syntax.py).
        if raw.startswith(b'\xef\xbb\xbf'):
            raw = raw[3:]
        user_config = json.loads(raw)
        if not isinstance(user_config, dict):
            raise ValueError('config.json root element must be an object')
        _config = _deep_merge(DEFAULT_CONFIG, user_config)
        _logger.info('%s config loaded: %s', LOG_PREFIX, _fs_text(path))
    except Exception:
        # Не пробрасываем: мод обязан продолжить работу на умолчаниях.
        _logger.error('%s cannot read %s, using defaults:\n%s',
                      LOG_PREFIX, _fs_text(path), _ascii(traceback.format_exc()))
        _config = dict(DEFAULT_CONFIG)


def _cfg(section, key, default=None):
    """Безопасное чтение вложенной настройки: _cfg('battle_report', 'enabled', True).

    Никогда не бросает KeyError/TypeError - даже если пользователь записал
    в JSON строку там, где ожидался словарь.
    """
    try:
        source = _config if _config is not None else DEFAULT_CONFIG
        block = source.get(section)
        if not isinstance(block, dict):
            block = DEFAULT_CONFIG.get(section, {})
        if key in block:
            return block[key]
        return DEFAULT_CONFIG.get(section, {}).get(key, default)
    except Exception:
        return default


def _cfg_root(key, default=None):
    """Чтение настройки верхнего уровня (enabled, debug, count_own_hits)."""
    try:
        source = _config if _config is not None else DEFAULT_CONFIG
        return source.get(key, DEFAULT_CONFIG.get(key, default))
    except Exception:
        return default


# =============================================================================
#  СЛУЖЕБНОЕ: отложенные вызовы BigWorld.callback
# =============================================================================
#
#  BigWorld.callback(delay, fn) возвращает id. Если его не погасить через
#  BigWorld.cancelCallback(id), колбэк переживёт бой и выстрелит уже в мёртвые
#  объекты. Поэтому все id складываем в _callbacks и чистим в fini() и при
#  выходе из боя.

@_safe
def _delayed(group, delay, func):
    """Ставит отложенный вызов и запоминает его id в корзине `group`."""
    bucket = _callbacks.setdefault(group, set())
    holder = {}

    def _runner():
        # Колбэк уже сработал - его id больше не нужно гасить.
        bucket.discard(holder.get('id'))
        try:
            func()
        except Exception:
            try:
                _logger.error('%s error in delayed call:\n%s',
                              LOG_PREFIX, _ascii(traceback.format_exc()))
            except Exception:
                pass

    try:
        delay = float(delay)
    except Exception:
        delay = 0.0
    if delay < 0.0:
        delay = 0.0

    cb_id = BigWorld.callback(delay, _runner)
    holder['id'] = cb_id
    bucket.add(cb_id)
    return cb_id


@_safe
def _cancel_callbacks(group=None):
    """Гасит отложенные вызовы одной корзины (или все, если group is None).

    Выход из боя гасит только CB_BATTLE, выход из клиента (fini) - всё.
    """
    if group is None:
        groups = list(_callbacks.keys())
    else:
        groups = [group]
    for name in groups:
        bucket = _callbacks.get(name)
        if not bucket:
            continue
        for cb_id in list(bucket):
            try:
                BigWorld.cancelCallback(cb_id)
            except Exception:
                # Колбэк мог уже сработать - движок тогда бросает исключение.
                pass
        bucket.clear()


# =============================================================================
#  СЛУЖЕБНОЕ: пампинг python.log
# =============================================================================
#
#  ЗАЧЕМ. Движок держит python.log открытым в режиме дозаписи и сбрасывает его
#  на диск не сразу. В dev-сборке клиент сам каждые 5 с вызывает
#  BigWorld.flushPythonLog(); на боевом клиенте эта петля не запускается.
#  Мы её воспроизводим, но с шагом 0.2 с вместо 5 с: живой лог в кокпите
#  (WoT Mod Cockpit) должен отставать незаметно.
#
#  ЦЕНА. flushPythonLog() - это fflush уже открытого дескриптора, без переоткрытия
#  файла. Пять вызовов в секунду на фоне обычного потока строк клиента не заметны.

@_safe
def _flush_python_log():
    """Один сброс python.log на диск + постановка следующего.

    Петля самоподдерживающаяся: каждый вызов ставит следующий через _delayed(),
    поэтому id всегда лежит в корзине CB_LOGPUMP и гасится в fini().
    """
    try:
        BigWorld.flushPythonLog()
    except Exception:
        # Метода нет (другая сборка движка) или он бросил - петлю не продолжаем,
        # иначе будем засорять лог одной и той же ошибкой пять раз в секунду.
        _logger.error('%s BigWorld.flushPythonLog failed, log pump stopped:\n%s',
                      LOG_PREFIX, _ascii(traceback.format_exc()))
        return

    _delayed(CB_LOGPUMP,
             _cfg('log_pump', 'interval_sec', 0.2),
             _flush_python_log)


@_safe
def _start_log_pump():
    """Запускает петлю сброса лога. Повторный вызов не создаёт вторую петлю."""
    if not _cfg('log_pump', 'enabled', True):
        _logger.info('%s log pump disabled by config', LOG_PREFIX)
        return

    if _callbacks.get(CB_LOGPUMP):
        # Петля уже крутится: второй _flush_python_log() удвоил бы частоту.
        return

    interval = _cfg('log_pump', 'interval_sec', 0.2)
    _logger.info('%s log pump started (flushPythonLog every %s s)', LOG_PREFIX, interval)
    _flush_python_log()


# =============================================================================
#  ВЫВОД ТЕКСТА ИГРОКУ
# =============================================================================

@_safe
def _push_system_message(text):
    """Системное сообщение в ангаре (центр уведомлений). Требование B.

    gui.SystemMessages.pushMessage(text, type=SM_TYPE.Information, priority=None,
                                   messageData=None, savedData=None)
    проксирует в dependency.instance(ISystemMessages).pushMessage, а тот -
    в proto.serviceChannel.pushClientSysMessage.
    В БОЮ это НЕ даёт видимого текста: там нужен боевой чат (см. ниже).
    """
    from gui import SystemMessages
    SystemMessages.pushMessage(_to_unicode(text), type=SystemMessages.SM_TYPE.Information)
    _logger.debug('%s system message pushed', LOG_PREFIX)


@_safe
def _push_battle_message(text):
    """Произвольный текст в боевой чат. Требование C.

    MessengerEntry.g_instance.gui.addClientMessage(message, isCurrentPlayer=False)
    -> BattleEntry.addClientMessage -> view.addMessage(message, fillColor=BLACK).
    Это единственный штатный способ показать в бою ПРОИЗВОЛЬНУЮ строку:
    sessionProvider.shared.messages.showVehicleMessage и FadingMessages
    принимают только ключи локализации (#ingame_gui:...), а не свободный текст.
    """
    from messenger import MessengerEntry
    MessengerEntry.g_instance.gui.addClientMessage(_to_unicode(text))
    _logger.debug('%s battle chat message pushed', LOG_PREFIX)


def _format_template(template, defaults_key_path, values):
    """Подставляет значения в шаблон из конфига.

    str.format с ИМЕНОВАННЫМИ полями: если пользователь напишет в шаблоне
    неизвестное поле, .format бросит KeyError. Ловим его здесь и откатываемся
    на шаблон по умолчанию, чтобы опечатка в конфиге не лишала игрока сообщения.
    """
    try:
        return _to_unicode(template).format(**values)
    except Exception:
        _logger.warning('%s bad template in config (%s), using the default one',
                        LOG_PREFIX, defaults_key_path)
        fallback = DEFAULT_CONFIG[defaults_key_path]['text']
        try:
            return fallback.format(**values)
        except Exception:
            # Совсем крайний случай: отдаём хотя бы имя мода.
            return u'%s v%s' % (MOD_NAME, MOD_VERSION)


# =============================================================================
#  B) ПРИВЕТСТВИЕ В АНГАРЕ
# =============================================================================

@_safe
def _show_hangar_greeting():
    """Формирует и показывает приветствие. Вызывается отложенно."""
    global _greeting_shown

    if _cfg('hangar_greeting', 'once_per_session', True) and _greeting_shown:
        return

    # За 3 секунды задержки игрок мог уже уехать в бой. SystemMessages в бою
    # не видны, поэтому проверяем, что арены нет - иначе тихо пропускаем
    # (флаг _greeting_shown не ставим: покажем в следующем заходе в ангар).
    try:
        from gui.battle_control import avatar_getter
        if avatar_getter.getArena() is not None:
            _logger.debug('%s player is already in battle, greeting skipped',
                          LOG_PREFIX)
            return
    except Exception:
        _logger.debug('%s avatar_getter is unavailable, showing the greeting anyway',
                      LOG_PREFIX)

    values = {'mod': MOD_NAME, 'version': MOD_VERSION, 'id': MOD_ID}
    text = _format_template(_cfg('hangar_greeting', 'text', u''),
                            'hangar_greeting', values)
    _push_system_message(text)
    _greeting_shown = True
    _logger.info('%s hangar greeting shown', LOG_PREFIX)


# =============================================================================
#  C) ОТЧЁТ ПО БОЮ
# =============================================================================

@_safe
def _collect_battle_info():
    """Собирает ПУБЛИЧНУЮ информацию о бое.

    Всё, что здесь читается, игрок и так видит на экране:
      * arena.arenaType.geometryName - название карты (оно же на экране загрузки);
      * arena.guiType                 - тип боя (значок режима в интерфейсе);
      * arena.vehicles                - ростер команд (панель игроков и таблица Tab).

    ЗАПРЕЩЁННОГО здесь нет: мы не трогаем позиции, матрицы, здоровье и
    перезарядку чужих машин - только состав команд.

    'allies' считается БЕЗ самого игрока (его машина тоже лежит в
    arena.vehicles), поэтому в рандоме 15x15 будет 14 союзников и 15 противников.

    Возвращает dict или None, если данных ещё нет.
    """
    from gui.battle_control import avatar_getter
    from constants import ARENA_GUI_TYPE_LABEL

    arena = avatar_getter.getArena()
    if arena is None:
        _logger.debug('%s arena is not created yet', LOG_PREFIX)
        return None

    # --- карта -------------------------------------------------------------
    geometry_name = u''
    arena_type = getattr(arena, 'arenaType', None)
    if arena_type is not None:
        geometry_name = _to_unicode(getattr(arena_type, 'geometryName', u''))

    map_name = geometry_name
    if geometry_name:
        # Локализованное имя карты. Ровно этот шаблон использует сам клиент
        # в gui/Scaleform/daapi/view/lobby/clans/profile/__init__.py.
        try:
            from helpers import i18n
            localized = _to_unicode(i18n.makeString('#arenas:%s/name' % geometry_name))
            if localized:
                map_name = localized
        except Exception:
            _logger.debug('%s map localization is unavailable, keeping geometryName',
                          LOG_PREFIX)

    # --- тип боя -----------------------------------------------------------
    gui_type = getattr(arena, 'guiType', None)
    battle_type = _to_unicode(ARENA_GUI_TYPE_LABEL.LABELS.get(gui_type, u'unknown'))

    # --- состав команд -----------------------------------------------------
    # arena.vehicles - dict: vehicleID -> {'team': int, 'name': unicode,
    #                                      'isAlive': bool, 'vehicleType': ..., ...}
    my_team = avatar_getter.getPlayerTeam()
    # Собственная машина ТОЖЕ лежит в arena.vehicles. Без этой строки в обычном
    # рандоме 15x15 вышло бы «союзников 15», хотя у игрока их 14.
    # avatar_getter.getPlayerVehicleID() возвращает playerVehicleID игрока,
    # а если атрибута ещё нет - 0. Настоящий vehicleID нулём не бывает,
    # поэтому 0 трактуем как «неизвестно».
    my_vehicle_id = None
    try:
        candidate = avatar_getter.getPlayerVehicleID()
        if candidate:
            my_vehicle_id = candidate
        else:
            _logger.debug('%s playerVehicleID is not set yet, counting myself as an ally',
                          LOG_PREFIX)
    except Exception:
        _logger.debug('%s getPlayerVehicleID is unavailable, counting myself as an ally',
                      LOG_PREFIX)
    allies = 0
    enemies = 0
    vehicles = getattr(arena, 'vehicles', None)
    if vehicles:
        # В py2.7 у dict есть iteritems(), но .items() работает везде,
        # а ростер всегда маленький (до ~30 записей) - экономить нечего.
        for vehicle_id, info in vehicles.items():
            try:
                if my_vehicle_id is not None and vehicle_id == my_vehicle_id:
                    continue
                if info.get('team') == my_team:
                    allies += 1
                else:
                    enemies += 1
            except Exception:
                continue

    return {
        'map': map_name,
        'geometry': geometry_name,
        'battle_type': battle_type,
        'gui_type': gui_type,
        'allies': allies,
        'enemies': enemies
    }


@_safe
def _report_battle():
    """Выводит однократный отчёт в боевой чат. Вызывается отложенно."""
    global _battle_reported

    if _battle_reported:
        return

    # Реплей: клиент сам гасит часть сообщений при перемотке, мод должен так же.
    if not _cfg('battle_report', 'show_in_replay', False):
        try:
            import BattleReplay
            if BattleReplay.isPlaying():
                _logger.debug('%s replay detected, battle report skipped', LOG_PREFIX)
                return
        except Exception:
            _logger.debug('%s BattleReplay is unavailable, continuing', LOG_PREFIX)

    info = _collect_battle_info()
    if not info:
        return

    values = dict(info)
    values['mod'] = MOD_NAME
    values['version'] = MOD_VERSION

    text = _format_template(_cfg('battle_report', 'text', u''),
                            'battle_report', values)
    _push_battle_message(text)

    # geometryName и метка режима - латиница; локализованное имя карты не пишем,
    # чтобы строку было удобно грепать. Кириллица в лог всё равно проходит
    # (sys.getdefaultencoding() == 'utf-8', см. шапку файла).
    _logger.info('%s battle: map=%s type=%s allies=%s enemies=%s',
                 LOG_PREFIX, info['geometry'], info['battle_type'],
                 info['allies'], info['enemies'])
    _battle_reported = True


# =============================================================================
#  ДЕМОНСТРАЦИЯ БЕЗОПАСНОГО МОНКИПАТЧА
# =============================================================================
#
#  Патчим Avatar.PlayerAvatar.showShotResults - метод, которым сервер сообщает
#  клиенту результаты СОБСТВЕННЫХ выстрелов игрока (их и так рисует индикатор
#  попаданий). Считаем свои пробития и в конце боя пишем итог в python.log.
#
#  ПРАВИЛА БЕЗОПАСНОГО ПАТЧА (иначе сломаете XVM и другие моды, которые
#  патчат то же самое):
#    1. сохранить оригинал и ВСЕГДА его вызывать - даже если наш код упал;
#    2. свою логику целиком обернуть в отдельный try/except;
#    3. поставить флаг-маркер, чтобы не навесить обёртку дважды
#       (помните: в модуле Avatar имя Avatar - псевдоним того же класса
#        PlayerAvatar, патч через любое из двух имён попадает в один объект);
#    4. если атрибута нет (игра обновилась) - тихо отключиться, а не падать.
#
#  Приватные методы клиента подвергнуты name mangling: __foo внутри класса Bar
#  лежит в атрибутах как _Bar__foo - это надо учитывать в getattr/setattr.

_PATCH_MARKER = '_hello_tanker_patched'


@_safe
def _install_shot_results_patch():
    """Навешивает обёртку на Avatar.PlayerAvatar.showShotResults."""
    if not _cfg_root('count_own_hits', True):
        _logger.debug('%s count_own_hits=false, monkey patch skipped', LOG_PREFIX)
        return

    import Avatar
    from constants import VEHICLE_HIT_FLAGS

    cls = Avatar.PlayerAvatar
    original = getattr(cls, 'showShotResults', None)
    if original is None:
        # Игра изменилась - просто выключаемся, клиент при этом жив.
        _logger.warning('%s PlayerAvatar.showShotResults not found, patch skipped',
                        LOG_PREFIX)
        return
    if getattr(original, _PATCH_MARKER, False):
        # Уже пропатчено (например, модуль импортировали повторно).
        _logger.debug('%s showShotResults is already patched', LOG_PREFIX)
        return

    def wrapper(self, results, *args, **kwargs):
        global _own_piercing_hits
        # --- НАША ЛОГИКА: отдельный, полностью изолированный try -----------
        try:
            for r in results:
                # Элемент results - целое число: младшие 32 бита - vehicleID
                # цели, старшие 32 - флаги попадания (VEHICLE_HIT_FLAGS).
                # У ">>" приоритет выше, чем у "&", скобки не нужны.
                # Пишем 0xFFFFFFFF, а НЕ 4294967295L: py2-суффикс L сделал бы
                # файл непарсибельным для любого инструмента на Python 3
                # (ast.parse -> SyntaxError), в том числе для tools/py27c.py
                # и build.py --compile-all. В Python 2.7 значение то же самое.
                flags = r >> 32 & 0xFFFFFFFF
                if flags & VEHICLE_HIT_FLAGS.IS_ANY_PIERCING_MASK:
                    _own_piercing_hits += 1
        except Exception:
            try:
                _logger.error('%s error in showShotResults wrapper:\n%s',
                              LOG_PREFIX, _ascii(traceback.format_exc()))
            except Exception:
                pass

        # --- ОРИГИНАЛ ВЫЗЫВАЕТСЯ ВСЕГДА ------------------------------------
        # Даже если наш код упал: иначе игрок лишится штатных сообщений
        # о попаданиях, а следующий в цепочке мод (XVM) - своих данных.
        return original(self, results, *args, **kwargs)

    setattr(wrapper, _PATCH_MARKER, True)
    wrapper.__name__ = 'showShotResults'
    setattr(cls, 'showShotResults', wrapper)
    _logger.info('%s showShotResults monkey patch installed', LOG_PREFIX)


# =============================================================================
#  ОБРАБОТЧИКИ ИГРОВЫХ СОБЫТИЙ (g_playerEvents)
# =============================================================================

@_safe
def _on_avatar_ready():
    """g_playerEvents.onAvatarReady - игрок появился в бою и клиент готов.

    Событие вызывается БЕЗ аргументов из Avatar.setClientReady(),
    сразу после g_playerEvents.onArenaStateChange.
    На этот момент arena уже заполнена, но боевой GUI (battle.swf, чат)
    может ещё догружаться - поэтому вывод сообщения откладываем
    на battle_report.delay_sec секунд.
    """
    global _battle_reported, _own_piercing_hits
    _battle_reported = False
    _own_piercing_hits = 0

    _logger.info('%s entering battle', LOG_PREFIX)

    if not _cfg('battle_report', 'enabled', True):
        return
    _delayed(CB_BATTLE, _cfg('battle_report', 'delay_sec', 2.0), _report_battle)


@_safe
def _on_avatar_become_non_player():
    """g_playerEvents.onAvatarBecomeNonPlayer - игрок покидает бой.

    Здесь обязательно: погасить БОЕВЫЕ отложенные вызовы и подвести итоги.
    Если этого не делать, колбэк выстрелит уже в разрушенном бою.
    Ангарную корзину не трогаем: приветствие, поставленное с задержкой,
    не должно исчезать из-за того, что игрок ушёл в бой.
    """
    _cancel_callbacks(CB_BATTLE)
    if _cfg_root('count_own_hits', True):
        _logger.info('%s battle finished, own piercing hits: %s',
                     LOG_PREFIX, _own_piercing_hits)


# =============================================================================
#  СОБЫТИЯ, КОТОРЫЕ ЗАГРУЗЧИК ШЛЁТ МОДУ НАПРЯМУЮ (gui.mods.sendEvent)
# =============================================================================
#  Объявлять их не обязательно - отсутствие метода загрузчик глушит
#  через except AttributeError. Но полезно видеть полный список: других
#  точек входа у мода НЕТ, событий «начало/конец боя» среди них тоже нет,
#  их берут из g_playerEvents (см. выше).

@_safe
def onAccountShowGUI(ctx):
    """Показан GUI аккаунта - игрок в ангаре. Требование B.

    Событие приходит из gui/shared/personality.py (после g_prbLoader
    и g_clanCache). Сообщение всё равно откладываем: центр уведомлений
    в этот момент ещё может достраиваться.
    """
    _logger.debug('%s onAccountShowGUI', LOG_PREFIX)
    # Общий выключатель проверяем ЗДЕСЬ тоже: init() при enabled=false просто
    # выходит, но события из gui.mods.sendEvent приходят моду всё равно -
    # без этой строки приветствие показывалось бы у выключенного мода.
    if not _cfg_root('enabled', True):
        return
    if not _cfg('hangar_greeting', 'enabled', True):
        return
    _delayed(CB_HANGAR, _cfg('hangar_greeting', 'delay_sec', 3.0), _show_hangar_greeting)


@_safe
def onAccountBecomePlayer():
    """Аккаунт стал активным игроком (вход в ангар на уровне сущностей)."""
    _logger.debug('%s onAccountBecomePlayer', LOG_PREFIX)


@_safe
def onAccountBecomeNonPlayer():
    """Аккаунт перестал быть игроком (уход в бой или разлогин)."""
    _logger.debug('%s onAccountBecomeNonPlayer', LOG_PREFIX)


@_safe
def onAvatarBecomePlayer():
    """Аватар стал игроком - началась загрузка боя.

    Данных об арене здесь ещё может не быть, поэтому отчёт строим
    не тут, а на g_playerEvents.onAvatarReady.
    """
    _logger.debug('%s onAvatarBecomePlayer', LOG_PREFIX)


@_safe
def onConnected():
    """Установлено соединение с игровым сервером."""
    _logger.debug('%s onConnected', LOG_PREFIX)


@_safe
def onDisconnected():
    """Соединение с игровым сервером разорвано."""
    _logger.debug('%s onDisconnected', LOG_PREFIX)
    # Соединения нет - ни боевой отчёт, ни ангарное приветствие уже не нужны.
    # Гасим ИМЕННО эти две корзины, а не всё подряд: пампинг лога (CB_LOGPUMP)
    # обязан пережить разрыв соединения, потому что разбор причины разрыва
    # начинается как раз со свежего python.log.
    _cancel_callbacks(CB_HANGAR)
    _cancel_callbacks(CB_BATTLE)


# =============================================================================
#  ЖИЗНЕННЫЙ ЦИКЛ МОДА
# =============================================================================

@_safe
def init():
    """Вызывается загрузчиком один раз при старте клиента. Требование A.

    Момент вызова: САМАЯ ПОСЛЕДНЯЯ строка gui.shared.personality.init().
    Аккаунта, боя, sessionProvider и Scaleform-приложений здесь ЕЩЁ НЕТ -
    поэтому в init() мы только читаем конфиг, ставим монкипатч и
    подписываемся на события.

    Порядок init() у разных модов ПРОИЗВОЛЬНЫЙ: загрузчик обходит
    set() имён файлов, то есть порядок задан хешами строк. Он стабилен
    между запусками (в CPython 2.7 рандомизация хешей по умолчанию
    выключена), но меняется при добавлении или удалении любого мода.
    Полагаться на него нельзя: нельзя рассчитывать, что XVM или
    другие моды уже проинициализированы.
    """
    global _subscribed

    _load_config()

    if _cfg_root('debug', False):
        _logger.setLevel(logging.DEBUG)
    else:
        _logger.setLevel(logging.INFO)

    if not _cfg_root('enabled', True):
        _logger.info('%s disabled by config (enabled=false)', LOG_PREFIX)
        return

    # Пампинг лога поднимаем ПЕРВЫМ делом: всё, что мод напишет ниже (и всё,
    # что напишет клиент дальше по загрузке), должно сразу оказаться на диске,
    # иначе внешний наблюдатель увидит эти строки с непредсказуемой задержкой.
    _start_log_pump()

    # Версия клиента - удобно видеть в логе, к какой сборке относится сессия.
    client_version = '?'
    try:
        import helpers
        client_version = _to_unicode(helpers.getClientVersion(True))
    except Exception:
        _logger.debug('%s helpers.getClientVersion is unavailable', LOG_PREFIX)

    # Требование A: узнаваемый префикс, имя, версия мода и версия клиента.
    _logger.info('%s %s v%s loaded (client %s, id=%s)',
                 LOG_PREFIX, MOD_NAME, MOD_VERSION, client_version, MOD_ID)

    # Подписка на игровые события. g_playerEvents - синглтон _PlayerEvents
    # из scripts/client/PlayerEvents.py; подписка оператором += (Event.__iadd__).
    try:
        from PlayerEvents import g_playerEvents
        # Флаг ставим ДО подписок и по факту импорта, а не после обоих «+=».
        # Иначе так: первое «+=» прошло, второе бросило (после патча игры
        # события могло не стать) -> _subscribed остался False -> fini()
        # не снял уже поставленный обработчик.
        _subscribed = True
        g_playerEvents.onAvatarReady += _on_avatar_ready
        g_playerEvents.onAvatarBecomeNonPlayer += _on_avatar_become_non_player
        _logger.debug('%s subscribed to g_playerEvents', LOG_PREFIX)
    except Exception:
        # Не пробрасываем: без боевой части мод всё равно полезен в ангаре.
        _logger.error('%s cannot subscribe to g_playerEvents:\n%s',
                      LOG_PREFIX, _ascii(traceback.format_exc()))

    _install_shot_results_patch()


@_safe
def fini():
    """Вызывается загрузчиком при выходе из клиента.

    Момент вызова: САМАЯ ПЕРВАЯ строка gui.shared.personality.fini().
    Обязанности: снять все подписки (симметрично +=) и погасить колбэки,
    иначе после нескольких боёв обработчик будет вызываться N раз
    и держать ссылки на мёртвые объекты.

    Монкипатч НЕ снимаем сознательно: другой мод мог навесить свою обёртку
    поверх нашей, и откат через setattr сломал бы его цепочку.
    Клиент всё равно завершается.
    """
    global _subscribed

    _cancel_callbacks()

    if _subscribed:
        # Отписываемся КАЖДЫМ «-=» отдельно: если один атрибут исчез после
        # патча игры, второй обработчик всё равно должен быть снят.
        # Event.__isub__ на отсутствующем делегате просто ничего не делает,
        # лишняя отписка безопасна.
        try:
            from PlayerEvents import g_playerEvents
        except Exception:
            g_playerEvents = None
            _logger.error('%s cannot import PlayerEvents in fini():\n%s',
                          LOG_PREFIX, _ascii(traceback.format_exc()))
        if g_playerEvents is not None:
            try:
                g_playerEvents.onAvatarReady -= _on_avatar_ready
            except Exception:
                _logger.error('%s cannot unsubscribe onAvatarReady:\n%s',
                              LOG_PREFIX, _ascii(traceback.format_exc()))
            try:
                g_playerEvents.onAvatarBecomeNonPlayer -= _on_avatar_become_non_player
            except Exception:
                _logger.error('%s cannot unsubscribe onAvatarBecomeNonPlayer:\n%s',
                              LOG_PREFIX, _ascii(traceback.format_exc()))
        _subscribed = False

    _logger.info('%s unloaded', LOG_PREFIX)

    # Петля сброса лога уже погашена вызовом _cancel_callbacks() выше, поэтому
    # строку «unloaded» и всё, что клиент напишет при выходе, дожать на диск
    # больше некому. Делаем это руками - иначе последние секунды жизни клиента
    # (а там и происходит самое интересное при падении) в python.log не попадут.
    try:
        BigWorld.flushPythonLog()
    except Exception:
        pass
