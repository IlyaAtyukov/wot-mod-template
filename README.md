# wot-mod-template

Шаблон и инструменты сборки Python-модов для клиента «Мир танков» (Lesta Games).

> **Правовое.** Проект не связан с Lesta Games и Wargaming и не одобрен ими; «Мир танков»,
> Lesta, World of Tanks и Wargaming — товарные знаки их правообладателей. В репозитории нет
> кода клиента игры. Исходники клиента, если они вам нужны, вы декомпилируете локально из
> своей копии игры (`tools/extract_client_source.py`, результат ложится в `.scratch/`, которая
> исключена в `.gitignore`) и не публикуете и не передаёте. Шаблон предназначен только для
> модов, разрешённых правилами игры (см. [Правила игры](#правила-игры)). За соблюдение
> лицензионного соглашения Lesta и правил игры отвечаете вы сами.

Что даёт шаблон:

- **`build.py`** собирает из `src/` пакет `.mtmod` ровно в том виде, который принимает
  движок, и компилирует точку входа в байткод Python 2.7 собственным компилятором
  (`tools/py27c.py`) — **Python 2.7 на машине сборки не нужен**;
- **проверку синтаксиса Python 2.7** до упаковки (`tools/check_py2_syntax.py`): тело мода
  компилирует уже сама игра, и других проверок до запуска клиента у него нет;
- установку в игру, dev-раскладку в `res_mods` и удаление — **только по явным флагам**: без
  них `build.py` в папку игры ничего не пишет;
- демонстрационный мод **Hello Tanker** (`src/`): точка входа, загрузчик тела мода, конфиг,
  сообщения в ангаре и в бою, безопасный монкипатч, аккуратная работа с логом;
- извлечение исходников **вашего** клиента для чтения и поиска (`tools/extract_client_source.py`).

Шаблон работает сам по себе из командной строки и служит основой для
[WoT Mod Cockpit](https://github.com/IlyaAtyukov/wot-mod-cockpit) — настольного
приложения, которое создаёт из него проекты, собирает, ставит в игру и показывает живой лог.

Проверено на клиентах «Мира танков» 1.44–1.46 (2026 год).

## Содержание

- [Что внутри](#что-внутри)
- [Требования](#требования)
- [Быстрый старт](#быстрый-старт)
- [Свой мод из шаблона](#свой-мод-из-шаблона)
- [Сборка](#сборка)
- [Папка игры](#папка-игры)
- [Установка в игру](#установка-в-игру)
- [Как тело мода попадает в игру](#как-тело-мода-попадает-в-игру)
- [Конфиг мода](#конфиг-мода)
- [Проверка: загрузился ли мод](#проверка-загрузился-ли-мод)
- [Отладка](#отладка)
- [Мод не загрузился: чек-лист](#мод-не-загрузился-чек-лист)
- [Новая версия клиента](#новая-версия-клиента)
- [Исходники клиента](#исходники-клиента)
- [Инструменты](#инструменты)
- [Контракт с WoT Mod Cockpit](#контракт-с-wot-mod-cockpit)
- [Сторонние инструменты](#сторонние-инструменты)
- [Правила игры](#правила-игры)
- [Лицензия](#лицензия)

## Что внутри

```
wot-mod-template/
├─ build.py                        сборка .mtmod, установка, dev-раскладка, удаление
├─ build.ps1                       тонкая обёртка PowerShell над build.py
├─ config.sample.json              образец конфига -> <папка игры>\mods\configs\hello_tanker\config.json
├─ src/                            ИСХОДНИКИ МОДА (правите только их)
│  ├─ meta.xml                     <id>, <version>, <name>, <description>
│  └─ res/                         mount root: всё внутри монтируется в корень res игры
│     └─ scripts/client/gui/mods/
│        ├─ mod_hello_tanker.py    ТОНКИЙ СТАБ -> компилируется в .pyc
│        └─ hello_tanker/          ТЕЛО МОДА, остаётся обычным .py
│           ├─ __init__.py
│           └─ main.py
├─ examples/                       НЕ попадает в .mtmod, только для чтения и копипасты
│  └─ mod_minimal_example.py       минимальный работающий мод целиком
├─ tools/
│  ├─ py27c.py                     компилятор .py -> Python 2.7 .pyc (работает на Python 3)
│  ├─ check_py2_syntax.py          гейт синтаксиса Python 2.7
│  ├─ wot_game.py                  поиск папки игры и разбор paths.xml (только чтение)
│  ├─ extract_client_source.py     извлечение и декомпиляция scripts.pkg вашего клиента
│  ├─ requirements-decompile.txt   закреплённые версии необязательного декомпилятора
│  ├─ test_stub_loader.py          прогон логики стаба под Python 3 с заглушками
│  └─ test_cockpit_contract.py     проверка того, на что опирается WoT Mod Cockpit
├─ LICENSE                         0BSD
├─ THIRD-PARTY-NOTICES.md          сторонние инструменты и их лицензии
├─ dist/                           РЕЗУЛЬТАТ сборки (в git не попадает)
├─ .tools/                         необязательный venv с декомпилятором (в git не попадает)
└─ .scratch/                       исходники вашего клиента и прочие рабочие файлы (в git не попадает)
```

Имя архива берётся из `src/meta.xml`: `<id>_<version>.mtmod`, для шаблона —
`ru.local.hello_tanker_1.0.0.mtmod`. `meta.xml` разбирается XML-парсером, поэтому
самозакрывающиеся теги вроде `<description />` читаются правильно, а сломанная разметка даёт
предупреждение.

В собранном `.mtmod` **ровно одна** точка входа — `mod_hello_tanker.pyc`. Загрузчик клиента
импортирует **все** файлы `mod_*.pyc` каталога модов и зовёт `init()` у каждого, поэтому второй
`mod_*.py` в `src/` включил бы игроку два мода. По этой же причине
`mod_minimal_example.py` лежит в `examples/`: чтобы попробовать его в игре, скопируйте его в
`src/res/scripts/client/gui/mods/` и пересоберите.

## Требования

| Что | Зачем | Обязательно |
|---|---|---|
| Python 3.10+ (проверено на 3.12) | `build.py` и `tools/*.py` | да |
| Python 2.7.18 | запасной компилятор для слишком сложной точки входа | нет |
| uncompyle6 + xdis в `.tools\venv_uncompyle` | извлечение исходников своего клиента, самопроверка `py27c` | нет |

Python 3.8–3.9 не подходит: `build.py` сразу скажет, что нужен 3.10+. В Python 3.13 и новее
нет модуля `lib2to3`; проверка синтаксиса тогда пропускает дополнительный проход и
предупреждает об этом, основной проход работает. Но файл, который разбирает только Python 2
(`print x`, `123L`), на 3.13+ проверить нечем, и сборка на нём останавливается: пишите тело мода
в общем синтаксисе py2/py3, собирайте на Python 3.12 или отключите проверку флагом
`--no-syntax-check`. Если первым в `PATH` у вас стоит Python 2.7 или заглушка Microsoft Store,
запускайте `py -3 build.py` или `.\build.ps1` — обёртка сама ищет Python 3.10+ (`py -3`, затем
`python` и `python3`, заглушку Store пропускает; явно — параметр `-Python <путь>` или
переменная `WOT_PYTHON`).

Если на `.\build.ps1` Windows отвечает «running scripts is disabled on this system» (политика
выполнения по умолчанию запрещает скрипты), запускайте обёртку так:
`powershell -ExecutionPolicy Bypass -File .\build.ps1 -List`. Если политика — `RemoteSigned`, а
мешает отметка «скачано из интернета» на копии из ZIP-архива, достаточно один раз выполнить в
папке шаблона `Get-ChildItem -Recurse | Unblock-File`. Или обходитесь без обёртки: `py -3 build.py`.

Всё, кроме Python 3, необязательно. Декомпилятор ставится только для чтения исходников
клиента (см. [Исходники клиента](#исходники-клиента)).

## Быстрый старт

### С WoT Mod Cockpit

Склонируйте кокпит и шаблон в одну папку — тогда кокпит найдёт шаблон сам:

```powershell
git clone https://github.com/IlyaAtyukov/wot-mod-cockpit.git
git clone https://github.com/IlyaAtyukov/wot-mod-template.git
```

Дальше всё в кокпите: кнопка **«Новый мод…»** создаёт проект из шаблона (переименовывает
пакет и точку входа, подставляет идентификатор, имя, версию и тег лога), а кнопки сборки и
установки запускают `build.py` проекта. Сам шаблон кокпит рабочим проектом не открывает.
Подробности — в README кокпита.

### Из командной строки

Сделайте свою копию шаблона (ZIP-архив, `git clone` или кнопка «Use this template» на GitHub,
если она включена) и соберите демонстрационный мод:

```powershell
cd wot-mod-template
python build.py                 # dist\ru.local.hello_tanker_1.0.0.mtmod, игра не трогается
python build.py --install       # + копия в <папка игры>\mods\<версия>\
```

Папку игры `build.py` находит сам (см. [Папка игры](#папка-игры)); если не нашёл или нашёл
несколько, укажите её явно: `--game-dir C:\Games\Tanki` или переменная `WOT_GAME_DIR`.
Затем перезапустите клиент и проверьте лог (см. [Проверка](#проверка-загрузился-ли-мод)).
Чтобы сделать из шаблона свой мод, переименуйте его по разделу
[Свой мод из шаблона](#свой-мод-из-шаблона).

## Свой мод из шаблона

**Проще всего — мастером «Новый мод…» в WoT Mod Cockpit:** он делает всё перечисленное ниже
сам, включая уникальный `_PATCH_MARKER`, и проверяет, что ни одного старого имени не осталось.

Вручную это синхронная правка двух имён на диске и полутора десятков мест в шести файлах.
Ошибка в любом из них даёт тихую поломку: пакет принят, точка входа импортирована, а тело мода
не нашлось (см. пункт 4a [чек-листа](#мод-не-загрузился-чек-лист)). Пример: `hello_tanker` →
`my_mod`, `ru.local.hello_tanker` → `ru.example.my_mod`. Для модов, которые вы будете
публиковать, возьмите свой обратный домен, например `io.github.<ник>.my_mod`.

**Файлы на диске:**

| Было | Стало |
|---|---|
| `src/res/scripts/client/gui/mods/mod_hello_tanker.py` | `…/mod_my_mod.py` |
| `src/res/scripts/client/gui/mods/hello_tanker/` | `…/my_mod/` |

Имя точки входа обязано начинаться с `mod_` и быть **строчным**.

**`src/meta.xml`:** `<id>` (из него получается имя `.mtmod`), `<name>` (его показывают
менеджеры модов), по вкусу `<version>` и `<description>`.

**Точка входа `mod_<имя>.py`:** константы `MOD_ID`, `PACKAGE` (`gui.mods.my_mod`), `ENTRY`
(`gui.mods.my_mod.main`), `RES_DIR` (`scripts/client/gui/mods/my_mod`) и префикс лога
`[HelloTanker/loader]` в функциях `_note()` и `_error()`.

**Тело мода `<имя>/main.py`:** `MOD_ID`, `MOD_NAME`, `LOG_PREFIX`, имя логгера в
`logging.getLogger(...)`, `CONFIG_RELATIVE_PATH` (подкаталог `mods/configs/<имя>/`) и
`_PATCH_MARKER` — он должен быть уникальным среди всех модов.

**Прочее:** `tools/test_stub_loader.py` (имя точки входа и пакета), строка
`FALLBACK_ARCHIVE_NAME` в `build.py` и `config.sample.json` (имя мода и путь
`mods/configs/<имя>/` в `_readme`, тег `[HelloTanker]` в `_debug`).

Старое имя стоит ещё в комментариях и строках документации этих файлов и `<имя>/__init__.py`.
На работу мода они не влияют, но поиск ниже покажет и их — поправьте заодно.

Проверка:

```powershell
python build.py --list
Get-ChildItem src, tools, build.py, config.sample.json -Recurse -File -Include *.py, *.xml, *.json | Select-String -Pattern 'hello_tanker', 'HelloTanker'
python tools\test_cockpit_contract.py
```

В листинге архива не должно остаться `hello_tanker`, имя архива — `ru.example.my_mod_1.0.0.mtmod`,
поиск пуст, а контрактный тест проходит.

## Сборка

```powershell
python build.py                # обычная сборка в dist\
python build.py --list         # + распечатать содержимое архива
python build.py --clean        # сначала стереть dist\ (dev_manifest.json сохраняется)
.\build.ps1 -List              # то же через PowerShell
```

Что происходит по шагам:

1. проверка раскладки `src/`: есть ли `res/`, есть ли `mod_*.py`, корректно ли имя, есть ли
   `__init__.py` у вложенных пакетов;
2. **проверка синтаксиса Python 2.7** (`tools/check_py2_syntax.py`) по `src/` и `examples/`:
   f-строки, аннотации, keyword-only аргументы, `{**d}`, `raise X from Y` и остальной
   py3-синтаксис ловятся до компиляции. Отключается флагом `--no-syntax-check` — не делайте
   этого;
3. сборка во временном дереве `dist\_stage\`: точки входа `mod_*.py` компилируются в `.pyc`,
   остальное копируется как есть. **Каждый** копируемый `.py` ещё раз проходит тот же
   AST-фильтр — этот гейт отключить нельзя, потому что вложенные модули компилирует уже игра;
4. упаковка в `.mtmod` (ZIP, метод Stored);
5. повторное открытие архива и проверка всех требований движка;
6. если папка игры известна — чтение установленных модов и предупреждение о конфликтах путей
   (только чтение).

Типовой вывод:

```
[build] game dir: C:\Games\Tanki (from registry)
[build] client version: 1.46.0.0 (from paths.xml)
[build] entry points found: mod_hello_tanker.py
[build] mod packages (stay plain .py): hello_tanker
[build] OK   python 2.7 syntax check passed (tools/check_py2_syntax.py)
[build] meta.xml: id=ru.local.hello_tanker version=1.0.0 name=Hello Tanker
[build] external python 2.7: not found -- using the built-in tools/py27c.py
[build] staged 3 file(s) verbatim
[build] compiled (py27c) src/res/scripts/client/gui/mods/mod_hello_tanker.py  ->  mod_hello_tanker.pyc  [dfile=scripts/client/gui/mods/mod_hello_tanker.py]
[build] OK   entry point(s) compiled by py27c: mod_hello_tanker.pyc
[build] shipped as plain .py (loaded by the engine importer / stub fallback): res/scripts/client/gui/mods/hello_tanker/__init__.py, res/scripts/client/gui/mods/hello_tanker/main.py
[build] OK   ...\dist\ru.local.hello_tanker_1.0.0.mtmod (67393 bytes, 10 entries, all Stored)
[build] entry point bytecode produced by: py27c
[build] not installed (pass --install to copy into C:\Games\Tanki\mods\1.46.0.0)
[build] finished clean
```

`entry points found` перечисляет **все** `mod_*.py` в `src/…/mods/`: сколько их, столько модов
получит игрок. Сообщения `build.py` намеренно на английском и в ASCII, чтобы их не портила
кодовая страница консоли; русский вывод `check_py2_syntax.py` пересылается в UTF-8.

### Что build.py гарантирует в архиве

Движок отвергает или молча игнорирует пакет, если нарушен любой пункт:

| Требование | Что будет иначе (сообщение в python.log) |
|---|---|
| все записи ZIP методом **Stored (0)** | `Mod package '%s' load error: compression not supported` |
| только прямые слеши `/` в именах записей | пакет читается неправильно |
| всё лежит под `res/` (mount root) | `Mod package '%s' load error: mod is empty or dont have mount root` |
| точка входа `res/scripts/client/gui/mods/mod_*.pyc` с магией `03 F3 0D 0A` | пакет смонтируется, мод не запустится |
| имя точки входа в нижнем регистре | загрузчик приводит имена к нижнему регистру, импорт не найдёт модуль |
| `meta.xml` (если есть) — валидный XML с `<id>`, `<version>`, `<name>` | движку он не нужен, но его читают менеджеры модов — предупреждение |

Архив после сборки открывается заново и проверяется по этим пунктам; нарушение — ошибка
сборки. `meta.xml` идёт первой записью, затем записи каталогов и файлы.

> **Не собирайте `.mtmod` через `Compress-Archive` или .NET `ZipArchive`.** В Windows
> PowerShell 5.1 оба дают Deflate даже с `-CompressionLevel NoCompression`, а
> `Compress-Archive` вдобавок пишет обратные слеши в имена записей. Пакет не загрузится.

### Почему точка входа — .pyc и как он получается без Python 2.7

Боевой клиент импортирует из `scripts/client/gui/mods` только прямые дочерние файлы вида
`mod_*.pyc` — байткод Python 2.7 с магией `03 F3 0D 0A`. `.py`-точку входа читают лишь
dev-сборки клиента. Ограничение касается **только точки входа**: всё, что лежит глубже
(`hello_tanker/main.py`), — обычные модули, они могут оставаться исходниками.

`tools/py27c.py` — компилятор ограниченного подмножества Python в настоящий байткод 2.7: он
разбирает исходник модулем `ast` из Python 3, генерирует опкоды 2.7 и пишет code object в
формате `marshal` версии 2 с заголовком `03 F3 0D 0A` + mtime.

Что py27c **умеет**: импорты, присваивания (в т.ч. распаковка), `del`, `global`, `def`
(значения по умолчанию, `*args`, `**kwargs`, декораторы), `lambda`, `class`, `if/elif/else`,
`while`, `for`, `break/continue`, `try/except/else/finally`, `raise`, вызовы `f(*a, **k)`,
срезы, арифметику и сравнения, условное выражение, `print(...)` и `exec(...)` с одним
аргументом.

Чего py27c **не умеет** (падает с указанием строки):

1. **Со стороны Python 3** — замыкания над локальными переменными объемлющей функции,
   list/dict/set comprehension, генераторы (`yield`), `with`, f-строки, аннотации,
   keyword-only аргументы, `match`.
2. **Со стороны Python 2** — исходник читается парсером Python 3, поэтому синтаксиса, которого
   в Python 3 нет, py27c не понимает: `123L`, `print x`, `except E, e:`, `raise E, v, tb`,
   `` `x` ``, `0777`, `<>`, `ur''`. Файл для py27c обязан лежать в **пересечении** py2 и py3.
3. **`print(a, b)` с двумя и более операндами** отклоняется намеренно: настоящий Python 2.7
   прочтёт это как print-инструкцию с кортежем и напечатает `(a, b)`. Пишите
   `print('%s %s' % (a, b))` или логируйте через `logging`.

Поэтому шаблон и разделён: крошечный стаб `mod_hello_tanker.py` (его хватает для py27c) и тело
мода в `hello_tanker/main.py`, где можно писать любой Python 2.7. Исключение — `--compile-all`:
тогда тело тоже проходит через компилятор, и без внешнего Python 2.7 `main.py` шаблона не
соберётся (в нём есть замыкание).

Строковые литералы: `'текст'` и `b'текст'` становятся Python 2 `str` (байты UTF-8 — ровно как
у Python 2.7 с `# -*- coding: utf-8 -*-`), а `u'текст'` — `unicode`.

Если стаб всё же стал слишком сложным, поставьте Python 2.7 и передайте его `build.py`:

```powershell
python build.py --compiler py27 --py2 C:\Python27\python.exe
$env:WOT_PY27 = 'C:\Python27\python.exe'    # либо через переменную окружения
```

| `--compiler` | Поведение |
|---|---|
| `auto` (по умолчанию) | сначала `tools/py27c.py`; если он отказался — внешний Python 2.7, если найден |
| `builtin` | только `tools/py27c.py` |
| `py27` | только внешний Python 2.7 (без него сборка падает) |

`build.py` проверяет, что внешний интерпретатор действительно 2.7 и что у каждого `.pyc`
правильная магия. Если ни один компилятор не справился, сборка падает с кодом 1: положить в
пакет `.py` вместо `.pyc` она не может.

### Параметры build.py

| Параметр | Что делает |
|---|---|
| `--src DIR` | другая папка исходников (по умолчанию `src/` рядом с `build.py`) |
| `--dist DIR` | другая папка результата (по умолчанию `dist/`) |
| `--clean` | стереть папку результата перед сборкой; `dev_manifest.json` сохраняется |
| `--list` | распечатать содержимое архива |
| `--name X.mtmod` | своё имя архива вместо `<id>_<version>.mtmod` |
| `--game-dir DIR` | папка игры (та, где `paths.xml`); по умолчанию — см. [Папка игры](#папка-игры) |
| `--game-version VER` | версия клиента; по умолчанию читается из `<папка игры>\paths.xml` |
| `--install` | **пишет в игру**: копирует `.mtmod` в `<папка игры>\mods\<версия>\` |
| `--dev` | **пишет в игру**: раскладывает файлы россыпью в `<папка игры>\res_mods\<версия>\` |
| `--uninstall` | **пишет в игру**: убирает то, что положили `--install` и `--dev` |
| `--compiler auto\|builtin\|py27` | чем компилировать точку входа (см. выше); переменная `WOT_MOD_COMPILER` |
| `--py2 PATH`, `--py27 PATH` | путь к Python 2.7; переменные `WOT_PY27`, `PYTHON27`, `PYTHON2` |
| `--no-py2` | не искать внешний Python 2.7 (то же, что `--compiler builtin`) |
| `--compile-all` | компилировать в `.pyc` и вложенные модули; нужен внешний Python 2.7 |
| `--keep-py` | положить `.py` рядом с `.pyc` |
| `--no-syntax-check` | отключить `tools/check_py2_syntax.py` (AST-гейт при сборке останется) |
| `--no-dir-entries` | не писать в ZIP записи каталогов |

У `build.ps1` те же параметры в стиле PowerShell (`-Install`, `-GameDir C:\Games\Tanki`,
`-NoSyntaxCheck`, …), а любой другой флаг `build.py` обёртка передаёт как есть:
`.\build.ps1 -List --no-dir-entries`.

## Папка игры

Папка игры — та, где лежит `paths.xml`. `build.py` и `tools/extract_client_source.py` ищут её
одинаково (`tools/wot_game.py`), в таком порядке:

1. `--game-dir`;
2. переменная окружения `WOT_GAME_DIR`, затем `WOTCOCKPIT_GAME_DIR` (переменная кокпита);
   пустое значение считается незаданным;
3. список установленных программ Windows (ключи `Uninstall` в реестре): издатель Lesta или
   Wargaming либо имя «Мир танков», World of Tanks или Tanki;
4. типовые папки на всех несъёмных дисках: `Games\World_of_Tanks_RU`, `Games\Tanki`,
   `Games\Мир танков` и другие (полный список — в `tools/wot_game.py`, он совпадает со списком
   кокпита).

Явно заданная папка (пункты 1–2) берётся как есть. Найденные (пункты 3–4) учитываются, только
если в них есть `paths.xml`; клиент Lesta (пакеты `*.mtmod`) идёт первым. Посмотреть, что
найдено, можно без сборки:

```powershell
python tools\wot_game.py
```

Версию клиента и папки `mods\<версия>` и `res_mods\<версия>` `build.py` берёт из `paths.xml` —
так же, как кокпит. Каждая сборка пишет, с какой папкой она работает:
`[build] game dir: C:\Games\Tanki (from registry)`.

- **Игра не найдена или в папке нет `paths.xml`.** Обычная сборка всё равно проходит: она
  пропускает проверку конфликтов и сообщает об этом строкой лога. `--install`, `--dev` и
  `--uninstall` отказываются работать и просят `--game-dir` или `WOT_GAME_DIR`.
- **Найдено несколько установок.** Обычная сборка берёт первую и перечисляет остальные, а
  `--install`, `--dev` и `--uninstall` не угадывают: укажите папку явно.
- **Версия не читается из `paths.xml`.** Пишущие действия отказываются; подскажите версию
  через `--game-version`.
- **Клиент грузит не `.mtmod`** (в `paths.xml` маска `*.wotmod`). `--install` и `--dev`
  отказываются: шаблон собирает пакеты для клиента «Мира танков».

## Установка в игру

### Обычная установка

```powershell
python build.py --install
.\build.ps1 -Install -GameDir C:\Games\Tanki
```

Копирует `dist\*.mtmod` в `<папка игры>\mods\<версия>\`. Больше ничего не трогает.

### Dev-раскладка (быстрая итерация)

```powershell
python build.py --dev
```

Раскладывает файлы из `dist\_stage\res\` россыпью в `<папка игры>\res_mods\<версия>\`, без
архива. `res_mods` стоит в `paths.xml` первым, то есть перекрывает и `.mtmod`, и файлы игры.
Удобно для отладки, но легко забыть и потом ловить «призраков» от старой версии.

Список разложенных файлов и то, куда они легли, записывается в `dist\dev_manifest.json` —
**до** копирования, чтобы сбой на середине не оставил в игре файлов без учёта. Повторный
`--dev` сначала убирает файлы прошлой раскладки, которых в новой больше нет
(переименованный или удалённый модуль), и только потом копирует новые.

### Удаление

```powershell
python build.py --uninstall
```

Удаляет установленный `.mtmod` и все файлы из `dev_manifest.json`. Обе операции ограждены:
пакет удаляется только из `<папка игры>\mods\<версия>\`, файлы россыпи — только из той папки
`res_mods\<версия>`, которая записана в манифесте (проверяется, что это действительно
`res_mods` папки игры). Поэтому удаление работает и после обновления клиента, и с папкой
игры, записанной в другом регистре. Всё вне этих папок пропускается с предупреждением.

Манифест удаляется, только когда в игре не осталось ни одного файла из него. Если файл не
удалился (например, его держит запущенный клиент), манифест переписывается списком оставшихся
файлов, а `build.py` завершается с ошибкой — закройте клиент и повторите `--uninstall`.
`--clean` манифест не стирает: без него `--uninstall` не знает, что убирать из `res_mods`.

## Как тело мода попадает в игру

Стаб `mod_hello_tanker.pyc` загружает `gui.mods.hello_tanker.main` двумя способами, по
очереди; какой сработал, видно в `python.log`:

1. **Штатный импортёр движка.** Движок, по-видимому, умеет импортировать `.py` из виртуальной
   файловой системы, в том числе из `.mtmod`; тогда хватает обычного `__import__`.
   В логе: `loaded by the engine ResMgr importer`.
2. **Запасной загрузчик стаба.** Если штатный путь отказал, стаб пишет причину
   (`engine importer refused: ...`) и ставит в `sys.meta_path` свой загрузчик: тот читает
   исходник через `ResMgr`, нормализует переводы строк, добавляет завершающий `\n` (без него
   `compile()` в Python 2.7 падает на последней строке с отступом), компилирует встроенным
   `compile()` и выполняет. В логе: `loaded by the built-in ResMgr fallback loader`.

Логику стаба проверяет `tools/test_stub_loader.py` — под Python 3 с заглушками вместо движка.
Что именно сработало в вашей игре, показывает только лог, поэтому **первая установка начинается
с его проверки** (см. [Проверка](#проверка-загрузился-ли-мод)).

Практический вывод: правка `hello_tanker/*.py` **не требует** перекомпиляции `.pyc`, достаточно
пересобрать архив или разложить файлы через `--dev`.

Если в логе **нет ни одной** из двух строк, `.py` из `.mtmod` не читается вовсе:

1. пересоберите так, чтобы тело мода тоже уехало байткодом:
   `python build.py --compile-all --py2 C:\Python27\python.exe` (нужен Python 2.7: в `main.py`
   есть замыкание, которого py27c не умеет);
2. если и это не помогло, слейте тело мода прямо в стаб (один файл `mod_<имя>.py`) и держитесь
   подмножества py27c либо собирайте внешним Python 2.7 (`--compiler py27`).

## Конфиг мода

Мод читает **необязательный** JSON по пути `CONFIG_RELATIVE_PATH` из `main.py` — относительно
корня игры:

```
<папка игры>\mods\configs\hello_tanker\config.json
```

Подкаталог `mods/configs/<имя>/` — распространённое соглашение модов, а не требование движка:
путь целиком задаёт ваш код. Корень игры мод определяет так: сначала `os.getcwd()` (клиент
стартует из корня игры), затем `ResMgr.appDirectory()` и его родитель (`appDirectory()` — это
каталог исполняемого файла, `<папка игры>\win64\`). Берётся первый кандидат, где реально лежит
`config.json`. Пути при этом не приводятся к `unicode`: в Python 2.7 на Windows это байты в
кодировке ANSI, и установка в путь с кириллицей (`C:\Игры\Танки`) работает.

Поставить образец:

```powershell
$game = 'C:\Games\Tanki'          # ваша папка игры (там, где paths.xml)
New-Item -ItemType Directory -Force "$game\mods\configs\hello_tanker" | Out-Null
Copy-Item config.sample.json "$game\mods\configs\hello_tanker\config.json"
```

Файла может не быть — тогда мод работает на `DEFAULT_CONFIG`. Битый JSON мод не ломает: в лог
уйдёт ошибка, значения возьмутся по умолчанию. Ваш JSON накладывается на умолчания
**рекурсивно**, поэтому можно указать одну вложенную настройку:
`{"battle_report": {"enabled": false}}`.

| Ключ | Тип | По умолчанию | Что делает |
|---|---|---|---|
| `enabled` | bool | `true` | Общий выключатель: `false` — мод напишет `disabled by config` и больше ничего не сделает. Проверяется и в `init()`, и в обработчиках событий. |
| `debug` | bool | `false` | Уровень логгера `DEBUG` вместо `INFO`. |
| `count_own_hits` | bool | `true` | Демонстрационный монкипатч `Avatar.PlayerAvatar.showShotResults`: счёт своих пробитий за бой. |
| `hangar_greeting.enabled` | bool | `true` | Системное сообщение в ангаре. |
| `hangar_greeting.delay_sec` | float | `3.0` | Пауза после `onAccountShowGUI`. Если игрок за это время ушёл в бой, приветствие покажется при следующем заходе в ангар. |
| `hangar_greeting.once_per_session` | bool | `true` | Показать один раз за запуск клиента. |
| `hangar_greeting.text` | str | см. ниже | Поля: `{mod}`, `{version}`, `{id}`. |
| `battle_report.enabled` | bool | `true` | Сводка в боевом чате. |
| `battle_report.delay_sec` | float | `2.0` | Пауза после `g_playerEvents.onAvatarReady`. |
| `battle_report.show_in_replay` | bool | `false` | В реплеях по умолчанию мод молчит. |
| `battle_report.text` | str | см. ниже | Поля: `{mod}`, `{version}`, `{map}` (локализованное имя), `{geometry}` (техническое), `{battle_type}`, `{gui_type}`, `{allies}` — союзники **без самого игрока**, `{enemies}`. |
| `log_pump.enabled` | bool | `true` | Регулярно сбрасывать `python.log` на диск, чтобы живой лог не отставал. |
| `log_pump.interval_sec` | float | `0.2` | Период сброса. |

Тексты по умолчанию:

```
{mod} v{version}: мод загружен. Хорошего боя, танкист!
{mod}: карта «{map}», режим «{battle_type}», союзников {allies}, противников {enemies}
```

Опечатка в имени поля не лишает игрока сообщения: мод поймает ошибку форматирования, напишет
предупреждение и возьмёт текст по умолчанию.

## Проверка: загрузился ли мод

Полностью перезапустите клиент — горячей перезагрузки Python в боевом клиенте нет. Затем
смотрите `<папка игры>\python.log`:

```powershell
$game = 'C:\Games\Tanki'          # ваша папка игры
Select-String -Path "$game\python.log" -Pattern 'hello_tanker' -SimpleMatch
```

**Шаг 1. Пакет принят движком:**

```
INFO: [PY_DEBUG] Mod package 'c:/games/tanki/mods/<версия>/ru.local.hello_tanker_1.0.0.mtmod' loaded
```

Пути в логе приводятся к нижнему регистру. Возможные ответы движка:

```
Mod package '%s' loaded                                            — всё хорошо
Mod package '%s' not loaded                                        — пакет отвергнут без уточнения
Mod package '%s' load error: compression not supported             — в архиве Deflate
Mod package '%s' load error: mod is empty or dont have mount root  — нет папки res/
Mod package '%s' load error: mod is not valid zip archive          — битый zip
Mod package '%s' load error: unexpected error                      — прочее
Mod package '%s' conflicting with '%s'. Conflicted file: '%s'      — называет пересёкшийся файл
Package '%s' ignored due to conflict with package '%s'             — конфликт путей с другим модом
```

**Шаг 2. Модуль импортирован, `init()` отработал** — видно по строкам вашего мода. Ошибка
импорта выглядит так:

```powershell
Select-String -Path "$game\python.log" -Pattern 'There is problem while import gui mod'
```

**Шаг 3. Каким путём загрузилось тело мода** (см.
[Как тело мода попадает в игру](#как-тело-мода-попадает-в-игру)). Обязательный шаг первой
установки:

```powershell
Select-String -Path "$game\python.log" -Pattern 'HelloTanker/loader' -SimpleMatch
```

```
NOTICE: [NOTE] (scripts/client/gui/mods/mod_hello_tanker.py, <строка>) [HelloTanker/loader] loaded by the engine ResMgr importer: gui.mods.hello_tanker.main
NOTICE: [NOTE] (...) [HelloTanker/loader] engine importer refused: ImportError('No module named hello_tanker.main',)
NOTICE: [NOTE] (...) [HelloTanker/loader] loaded by the built-in ResMgr fallback loader: gui.mods.hello_tanker.main
ERROR: [ERROR] (...) [HelloTanker/loader] cannot load gui.mods.hello_tanker.main from scripts/client/gui/mods/hello_tanker
```

Обратите внимание на формат: `debug_utils.LOG_NOTE` и `LOG_ERROR` пишут в лог **категорию**
(`[NOTE]`, `[ERROR]`) и место вызова `(файл, строка)`, а ваш префикс стоит уже в теле
сообщения. Строк вида `NOTE:` в `python.log` не бывает — **ищите по своему префиксу**. Место
вызова — всегда обёртка `_note()`/`_error()` в стабе, ориентируйтесь на текст. `logging`
(см. [Отладка](#отладка)) пишет в категорию имя логгера:
`INFO: [HelloTanker] Hello Tanker v1.0.0 loaded ...`.

**Шаг 4. Падения:**

```powershell
Select-String -Path "$game\python.log" -Pattern 'EXCEPTION', 'Traceback' | Select-Object -First 20
```

Формат: `ERROR: [EXCEPTION] (scripts/client/gui/mods/mod_hello_tanker.py, 42)` — путь берётся
из `dfile`, который `build.py` прописывает в `.pyc` при компиляции, поэтому там путь внутри
игры, а не путь на вашем диске.

Лог дописывается и время от времени обрезается — при долгой отладке копируйте его сразу после
падения.

## Отладка

### Логирование из мода

```python
import logging
_logger = logging.getLogger('hello_tanker')
_logger.info('init done')      # -> INFO: [hello_tanker] init done
```

Клиент подключает к корневому логгеру свой обработчик, поэтому обычный `logging` попадает в
`python.log`. **`debug_utils.LOG_DEBUG` на боевом клиенте ничего не пишет**; работают
`LOG_NOTE`, `LOG_WARNING`, `LOG_ERROR`, `LOG_CURRENT_EXCEPTION`.

### Кодировка сообщений

Оба пути логирования приводят сообщение к строке через кодировку по умолчанию, а в этом
клиенте `sys.getdefaultencoding()` — **`utf-8`**: клиент ставит её при старте, до загрузки
модов (в `python.log` есть строка `Default encoding set to utf-8`). Отсюда:

- **`unicode` проходит всегда;**
- **`str` с байтами UTF-8 тоже проходит** — `_logger.info('мод загружен')` в файле с
  `# -*- coding: utf-8 -*-` окажется в логе кириллицей;
- **опасен `str` в другой кодировке**, например сырой путь из `os.getcwd()` на русской Windows
  (байты cp1251): неявный `decode('utf-8')` бросит `UnicodeDecodeError`, `logging` его проглотит,
  и **запись пропадёт целиком**. Такие значения приводите к `unicode` через
  `sys.getfilesystemencoding()` — в шаблоне это делает `_fs_text()` в `main.py`;
- `traceback.format_exc()` тоже `str` неизвестного происхождения, поэтому в шаблоне только он
  проходит через `_ascii()` — экранирование `\uXXXX`.

Строки формата в шаблоне английские по другой причине: их удобно искать, и они одинаково
читаются в любой консоли.

### Главное правило устойчивости

Загрузчик модов гасит только `ImportError` при импорте и `AttributeError` при вызове функций
мода. Любое другое исключение из `init()` доходит до инициализации клиента, и **клиент
закрывается на старте**; вдобавок моды, чья очередь ещё не наступила (XVM и другие), не получат
`init()`. Поэтому `init()`, `fini()` и каждый обработчик событий целиком лежат в
`try/except Exception` (в шаблоне — декоратор `@_safe`).

Обратная сторона: `AttributeError` внутри вашего `init()` будет съеден молча — мод не работает,
а в логе пусто. Логируйте сами.

## Мод не загрузился: чек-лист

Идите строго сверху вниз.

1. **Строки `Mod package ... loaded` нет в `python.log`.** Пакет не найден или отвергнут.
   Проверьте: файл лежит в `<папка игры>\mods\<версия>\` (версия — из `paths.xml`), расширение
   именно `.mtmod`, рядом в логе нет `load error: ...` с причиной.
2. **`load error: compression not supported`.** В архиве Deflate. Собирайте через `build.py`, не
   через `Compress-Archive`.
3. **`load error: mod is empty or dont have mount root`.** В архиве нет папки `res/`: все файлы
   должны лежать под `src/res/`.
4. **Пакет `loaded`, но мод не работает.** Загрузчик не нашёл точку входа. Проверьте:
   - файл называется `mod_<имя>.pyc`, **строчными буквами**;
   - префикс ровно `mod_`;
   - расширение `.pyc`, а не `.py` (боевой клиент `.py`-точку входа не читает);
   - подстроки `.pyc` нет в середине имени — загрузчик вырезает её и испортит имя модуля;
   - путь внутри архива ровно `res/scripts/client/gui/mods/`.

   4a. **Точка входа есть, но в логе `[HelloTanker/loader] cannot load ...`.** Не нашлось тело
   мода:
   1. `python build.py --list` — в архиве есть `res/scripts/client/gui/mods/hello_tanker/__init__.py`
      и `.../main.py`;
   2. константы `PACKAGE`, `RES_DIR`, `ENTRY` стаба совпадают с именами каталога и файла (см.
      [Свой мод из шаблона](#свой-мод-из-шаблона));
   3. строка `engine importer refused: ...` рядом называет причину отказа первого пути. Если там
      `SyntaxError`, дело в `main.py`: `python tools\check_py2_syntax.py`;
   4. рядом в логе — traceback второго пути.

   4b. **Нет ни `loaded by the engine ResMgr importer`, ни `loaded by the built-in ResMgr
   fallback loader`.** См. конец раздела [Как тело мода попадает в игру](#как-тело-мода-попадает-в-игру).
5. **`There is problem while import gui mod`.** `ImportError` в вашем модуле; traceback рядом.
   Обычно это опечатка в импорте, переименованный в новой версии модуль клиента или файл,
   скомпилированный не тем Python.
6. **Клиент закрывается сразу после запуска.** Необработанное исключение в `init()` какого-то
   мода. Ищите `ERROR: [EXCEPTION]` в логе. Если исключение не ваше, временно уберите чужие моды.
7. **Мод «тихо не работает», в логе пусто.** Скорее всего `AttributeError` внутри `init()` —
   его гасит загрузчик. Поставьте `logging` первой строкой `init()` и в `except`.
8. **Работает через раз или конфликтует с другим модом.** Совпали пути внутри `res/`
   (`Package ... ignored due to conflict`); строка `conflicting with ... Conflicted file: ...`
   называет файл. `build.py` при сборке сам предупреждает о пересечениях с установленными
   `.mtmod`. Проверьте и старую россыпь в `res_mods\<версия>\` — она перекрывает архив:
   `python build.py --uninstall`.
9. **Порядок инициализации.** Порядок `init()` у разных модов не определён: он стабилен между
   запусками, но меняется при добавлении или удалении любого мода. Не рассчитывайте, что XVM
   или другой мод уже инициализирован, — используйте отложенную регистрацию:
   `BigWorld.callback(0, ...)` или событие `onAccountShowGUI`.
10. **`init()` вызывается до аккаунта и до боя.** В этот момент ещё нет ни `BigWorld.player()`,
    ни `sessionProvider.shared`. Всё боевое подписывайте на `g_playerEvents.onAvatarReady` /
    `onAvatarBecomePlayer`.

Клиент вызывает у модуля мода только `init()`, `fini()` и шесть событий: `onConnected()`,
`onDisconnected()`, `onAccountBecomePlayer()`, `onAccountBecomeNonPlayer()`,
`onAvatarBecomePlayer()`, `onAccountShowGUI(ctx)`. Отсутствующие функции игнорируются. Событий
«начало боя / конец боя» среди них нет — их берут из `PlayerEvents.g_playerEvents` и
`BigWorld.player().arena`. В шаблоне все они объявлены в стабе, который пересылает вызов
одноимённой функции `hello_tanker/main.py` под `try/except` — обработчики пишите в `main.py`.

## Новая версия клиента

Папки модов привязаны к версии: `paths.xml` указывает на `./res_mods/<версия>` и
`./mods/<версия>`, после обновления клиента старые папки перестают читаться.

1. Обновите игру.
2. `python build.py --install` — версия прочитается из `paths.xml` сама. Если вы пользовались
   `--dev`, сначала `python build.py --uninstall`: манифест помнит старую папку `res_mods`.
3. Перепроверьте монкипатчи: приватные методы (`__foo` → `_Class__foo`) и внутренние классы
   интерфейса ломаются при обновлении чаще всего.
4. Извлеките дерево исходников новой версии (`python tools\extract_client_source.py --tree`) и
   сверьте с прошлым — или поручите это WoT Mod Cockpit (экран «Моды», переход на новую версию).

Устойчивый монкипатч: сохраняйте оригинал, всегда вызывайте его, ставьте флаг-маркер против
повторной установки, а при отсутствии атрибута тихо отключайтесь, а не падайте.

## Исходники клиента

**Правило:** дерево исходников — это декомпилированный код клиента Lesta. Извлекайте его только
из своей копии игры, держите на своей машине и не коммитьте ни в шаблон, ни в репозиторий своего
мода, не публикуйте и не передавайте. Рабочая папка `.scratch/` исключена в `.gitignore`.
Сверяйтесь только со своим клиентом: дерево извлекается из вашей копии игры и в репозиторий не
коммитится.

### Всё дерево

```powershell
python tools\extract_client_source.py --tree
```

Скрипт находит игру (см. [Папка игры](#папка-игры); явно — `--game-dir` или `--pkg`), читает
`res\packages\scripts.pkg` и декомпилирует его в несколько потоков. Игра только читается.
Результат:

```
.scratch\wot-src-<версия>\            например wot-src-1.46
├─ client\  client_common\  common\   исходники, без префикса scripts\
├─ .cockpit-failed-modules.txt        модули, которые не декомпилировались (в игре они есть)
└─ .cockpit-extra-modules.txt         модули дополнительных .pkg клиента (режимы, события)
.scratch\wot-src-<версия>.extra\      листинги и строки тех модулей, что не декомпилировались
```

Это ровно та раскладка, которую ждёт WoT Mod Cockpit: укажите папку кнопкой выбора дерева в
браузере API. Файлы, которые декомпилятор осилил лишь частично, остаются в дереве с пометкой
в первой строке. Дерево собирается во временной папке и встаёт на место одним переименованием
в конце, поэтому прерванное извлечение не выглядит готовым. Если папка уже есть, скрипт
откажется её перезаписывать: удалите её или укажите `--out`. Если первые 20 файлов не дали ни
строчки исходника, скрипт останавливается — значит, декомпилятор установлен неправильно.

Поиск по дереву:

```powershell
Get-ChildItem .scratch\wot-src-<версия>\client -Recurse -Filter *.py | Select-String 'g_playerEvents' | Select-Object -First 10
Select-String -Path .scratch\wot-src-<версия>\client\gui\SystemMessages.py -Pattern 'def pushMessage'
```

### Отдельные модули

```powershell
python tools\extract_client_source.py "*avatar_getter*" --list     # что есть
python tools\extract_client_source.py gui/mods/__init__.pyc        # декомпилировать
python tools\extract_client_source.py Avatar.pyc --mode disasm     # только дизассемблер
python tools\extract_client_source.py "scripts/client/gui/battle_control/controllers/*.pyc" --limit 100
```

Результат — в `.scratch\client-src\`: `pyc\<путь>` (сырой `.pyc`), `src\<путь>.py`
(исходник), `disasm\<путь>.txt` (листинг, если декомпиляция не прошла), `strings\<путь>.txt`
(печатные литералы, последний рубеж).

| Параметр | Что делает |
|---|---|
| `--tree` | всё дерево в `.scratch\wot-src-<версия>` (шаблон тогда работает как фильтр) |
| `--pkg PATH` | другой `scripts.pkg` |
| `--game-dir DIR` / `--game-version VER` | папка игры / версия для имени дерева |
| `--out DIR` | другая папка результата (вне `.scratch` — с предупреждением) |
| `--mode auto\|decompile\|disasm\|strings\|raw` | что делать с каждым `.pyc` (без `--tree`) |
| `--list` | только перечислить совпадения |
| `--limit N` | сколько файлов обработать, `0` — без ограничения (по умолчанию 50, у `--tree` ограничения нет) |
| `--venv PATH` | Python с декомпилятором: папка venv, её `Scripts` или сам `python.exe` |
| `--jobs N` | сколько файлов декомпилировать параллельно |

### Декомпилятор

Без декомпилятора скрипт всё равно работает (выдаёт строки), а `--tree` требует его. Поставьте
закреплённые версии в отдельное окружение в папке шаблона:

```powershell
python -m venv .tools\venv_uncompyle
.tools\venv_uncompyle\Scripts\python.exe -m pip install -r tools\requirements-decompile.txt
```

Проверено с Python 3.12, uncompyle6 3.9.3 и xdis 6.1.8. Версии закреплены, потому что разные
выпуски uncompyle6 по-разному оформляют код, и два дерева, извлечённые разными версиями,
различались бы там, где клиент не менялся. Скрипт ищет Python с декомпилятором так: `--venv`,
затем `.tools\venv_uncompyle`, затем тот Python, которым он запущен. Запускается интерпретатор
(`python -m uncompyle6.bin.uncompile`), а не `uncompyle6.exe` из venv: такие запускатели
перестают работать, если папку шаблона перенести или переименовать. Успех определяется по
строке `# okay decompiling` и наличию кода, а не по коду возврата: uncompyle6 завершается с 0
и тогда, когда разобрать модуль не смог.

## Инструменты

### tools/py27c.py

Компилятор `.py` → `.pyc` Python 2.7 под Python 3 (подробности — в разделе
[Сборка](#почему-точка-входа--pyc-и-как-он-получается-без-python-27) и в шапке файла).

```powershell
python tools\py27c.py --self-test        # фикстура + обратная декомпиляция (если есть .tools\venv_uncompyle)
python tools\py27c.py --check-opmap      # сверка таблицы опкодов с xdis
python tools\py27c.py in.py -o out.pyc --dfile scripts/client/gui/mods/mod_x.py --mtime 0
```

### tools/check_py2_syntax.py

Единственная проверка синтаксиса, которую тело мода получает до запуска игры: эти файлы едут в
`.mtmod` обычными `.py` и компилируются уже внутри клиента.

```powershell
python tools\check_py2_syntax.py                 # src\ рядом с tools\
python tools\check_py2_syntax.py src examples    # можно несколько целей
```

Три прохода:

1. **AST (основной).** `ast.parse` + чёрный список узлов, которых в 2.7 нет: f-строки,
   аннотации, keyword-only и positional-only аргументы, `raise X from Y`,
   `class A(metaclass=M)`, `{**d}`, `[*a]`, `a, *rest = ...`, `:=`, `async`/`await`,
   `yield from`, `nonlocal`, `@`, `1_000`, `x = ...`, `print(a, b)`, `print(..., sep=)`,
   `f(*a, *b)`, `f(**a, **b)`.
2. **lib2to3 (дополнительный).** Ловит совсем поломанную разметку. Его грамматика —
   объединённая для Python 2 и 3, поэтому один он py3-синтаксис не ловит; гейтом служит проход 1.
3. **Текстовые эвристики.** `pathlib`, `typing`, `super()` без аргументов, префикс `rb''` —
   синтаксически допустимо, но в клиенте 2.7 не работает.

Файлы читаются по объявлению кодировки (`# -*- coding: ... -*-`, иначе UTF-8). Файл, который
`ast.parse` не разобрал, скорее всего содержит синтаксис только Python 2 (`print x`, `123L`) —
для тела мода это законно, но `py27c` такой файл не соберёт. Такой файл проверяет один
`lib2to3`, поэтому на Python 3.13+ проверить его нечем: проверка возвращает `1`, и сборка
останавливается. Пишите тело в общем синтаксисе py2/py3, собирайте на Python 3.12 или
отключите проверку флагом `--no-syntax-check` (AST-гейт при сборке такой файл пропускает).

Коды возврата: `0` — чисто, `1` — есть проблемы, `2` — нечего проверять, `3` — сбой самой
проверки (это не вердикт о вашем коде). Если `lib2to3` нет, первая строка `stderr` —
`WARN:lib2to3-unavailable`, а результат по-прежнему `0`/`1`. `build.py` вызывает проверку сам.

### tools/test_stub_loader.py

Прогоняет логику загрузчика из `mod_hello_tanker.py` под Python 3 с подставными `ResMgr`,
`debug_utils` и `gui.mods`: чтение исходника, PEP-302 загрузчик, выбор между штатным импортёром
и запасным, запись причины отказа, нормализацию переводов строк, `__path__` вложенного пакета,
снятие загрузчика в `fini()`, рассылку событий и — главное — что исключение из обработчика не
выходит наружу. Чего он **не доказывает**: движок здесь поддельный, тест проверяет логику
стаба, а не то, что игра отдаёт `.py` из `.mtmod`.

```powershell
python tools\test_stub_loader.py
```

### tools/test_cockpit_contract.py

Проверяет то, на что опирается WoT Mod Cockpit (см. следующий раздел): токены шаблона, имена
файлов, флаги `build.py`, строку артефакта, `dev_manifest.json`, коды возврата
`check_py2_syntax.py`, пути декомпилятора и дерева исходников, а в самом шаблоне — ещё и типы
файлов с токенами, отсутствие путей чьей-то машины и `LICENSE`. Работает и в проекте, созданном
из шаблона. Запускайте перед каждым коммитом в шаблон:

```powershell
python tools\test_cockpit_contract.py
```

### tools/wot_game.py

Поиск папки игры и разбор `paths.xml` — общий для `build.py` и `extract_client_source.py`
(см. [Папка игры](#папка-игры)). `python tools\wot_game.py` показывает, что найдено.

## Контракт с WoT Mod Cockpit

Кокпит копирует шаблон в новые проекты и запускает его скрипты, опираясь на конкретный текст.
**Всё перечисленное меняйте только вместе с кокпитом**; `tools/test_cockpit_contract.py`
проверяет большую часть автоматически.

- **Как кокпит узнаёт шаблон:** в каталоге есть и `build.py`, и `src/meta.xml`.
- **Токены, которые заменяет мастер «Новый мод…»:** `ru.local.hello_tanker`, `Hello Tanker`,
  `HelloTanker`, `hello_tanker`, а также `<version>1.0.0</version>` и `<description>` в
  `src/meta.xml`.
- **Имена на диске:** `src/res/scripts/client/gui/mods/mod_hello_tanker.py`, каталог
  `hello_tanker/` и `hello_tanker/main.py` (мастер проверяет их после создания). Мастер
  переименовывает только сегменты пути, равные `hello_tanker` или `mod_hello_tanker.py`.
- **Строка `FALLBACK_ARCHIVE_NAME = "ru.local.hello_tanker_1.0.0.mtmod"` в `build.py`** —
  мастер меняет её точной текстовой заменой: одинарные кавычки или вычисленное значение молча
  выключат обновление версии.
- **Где делается замена:** только в файлах `.py .pyi .json .xml .md .ps1 .txt .cfg .ini .yml
  .yaml .gitignore`. Токен в файле другого типа (`.toml`, `.bat`, `.cmd`) скопируется как есть.
- **Что не копируется:** каталоги `.git .scratch .tools .vs .idea dist __pycache__ bin obj
  node_modules` и файлы `.pyc`. Всё остальное, включая `README.md`, `LICENSE` и будущий
  `.github/`, попадает в каждый новый проект.
- **Флаги `build.py`:** `--src --dist --game-dir --install --dev --uninstall --list --clean
  --no-syntax-check --compile-all --keep-py` и `--py27` (синоним `--py2`).
- **Строка артефакта:** путь к `.mtmod` кокпит берёт из последней строки, подходящей под
  `^\[build\]\s+OK\s+(?<path>.+?\.mtmod)\s*\(` — это строка
  `[build] OK   <путь>.mtmod (N bytes, M entries, all Stored)`. Другие строки `OK` не должны
  иметь `(` после `.mtmod`.
- **`dist\dev_manifest.json`:** `{"files": [абсолютные пути], ...}` — кокпит читает `files`.
- **`tools/check_py2_syntax.py`:** коды возврата `0`/`1`/`2` (`3` — сбой проверки);
  `tools/test_stub_loader.py` печатает `check group(s) passed`.
- **Декомпилятор и дерево:** `.tools\venv_uncompyle\Scripts\python.exe`, деревья
  `.scratch\wot-src` и `.scratch\wot-src-<версия>`, файлы `.cockpit-failed-modules.txt` и
  `.cockpit-extra-modules.txt`, первая строка частичного файла `# ВНИМАНИЕ: декомпиляция частичная…`.
- **Поиск игры:** списки издателей, имён и типовых папок в `tools/wot_game.py` совпадают с
  `GameDirectoryFinder` кокпита, разбор `paths.xml` — с `PathsXmlGameInstallLocator`.

## Сторонние инструменты

| Инструмент | Лицензия | Зачем | Обязателен |
|---|---|---|---|
| Python 3.10+ | PSF-2.0 | `build.py` и `tools/*.py` | да |
| Python 2.7 | PSF-2.0 | запасной компилятор точки входа | нет |
| uncompyle6 | GPL-3.0 | декомпиляция `scripts.pkg` вашего клиента, `py27c.py --self-test` | нет |
| xdis | GPL-2.0 | дизассемблер, зависимость uncompyle6, `py27c.py --check-opmap` | нет |
| spark-parser | MIT | зависимость uncompyle6 | нет |

Все они ставятся пользователем: uncompyle6 и xdis — в `.tools\venv_uncompyle`, который
исключён в `.gitignore`. Скрипты шаблона запускают их отдельными процессами (`py27c` импортирует
xdis только во время необязательной проверки `--check-opmap`), и ничего из этого не поставляется
вместе с шаблоном. **Не коммитьте `.tools\` и не кладите uncompyle6 или xdis в один архив или
установщик с шаблоном или своим модом**: такая поставка стала бы производным GPL-комплектом.
Подробнее — в [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

`tools/py27c.py` — собственная реализация. Из CPython 2.7 он берёт только интерфейсные факты,
нужные для совместимости (номера опкодов, `cmp_op`, флаги `CO_*`, коды `marshal`, магия `.pyc`);
код CPython (PSF-2.0) в шаблон не скопирован.

## Правила игры

Шаблон предназначен только для модов, которые разрешены правилами игры и политикой Lesta в
отношении модификаций; прежде чем публиковать мод, сверьтесь с актуальными правилами.
Безопасные направления: переформатирование уже показанных клиентом данных, статистика,
счётчики, таймеры своей перезарядки, интерфейс ангара, кастомизация в рамках штатных
возможностей.

Запрещено и грозит блокировкой аккаунта: позиции и состояние незасвеченной техники, чужие
таймеры перезарядки, автонаведение и упреждение, изменение прозрачности объектов и кустов,
метки засвета, направление орудий противника, автоматизация действий игрока — и вообще всё, что
даёт преимущество за счёт данных, которых игрок не видит.

Клиент сообщает серверу, что моды установлены, и отмечает это в реплеях: факт установки модов
известен всегда, даже если они полностью разрешены.

## Лицензия

Шаблон распространяется по лицензии **0BSD** (BSD Zero Clause, файл [LICENSE](LICENSE)). Его
код можно копировать в любой мод — открытый или закрытый — без каких-либо условий и без
указания авторства.

Проект, созданный из шаблона (в том числе мастером «Новый мод…» WoT Mod Cockpit, который
копирует `LICENSE` как есть), — ваш: удалите `LICENSE` или замените его своей лицензией.
Сторонние инструменты и их лицензии — в [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
