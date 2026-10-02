# Разработка VoxTypeX

[Главная страница](../README.md) · [Ручная проверка](MANUAL_SMOKE_TEST.md)

## Запуск из исходников

Проверенная сборочная среда — Python **3.14 x64**. Исходники требуют Python 3.12+,
но другой интерпретатор нужно проверять отдельно с нативными зависимостями.
В PowerShell из корня проекта:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests packaging
```

Запуск из исходников:

```powershell
.\.venv\Scripts\python.exe -m voxtypex
```

Отдельный тестовый профиль задаётся `VOXTYPEX_DATA_DIR`, отдельный кэш моделей —
`VOXTYPEX_MODEL_DIR`. Переменные действуют и в готовой сборке.

## Сборка приложения

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

Если Python Launcher отсутствует:

```powershell
.\scripts\build.ps1 -PythonPath "C:\Program Files\Python314\python.exe"
```

Скрипт создаёт `.venv-build`, устанавливает закреплённые версии из
`requirements-build.lock`, проверяет зависимости, Ruff и unit tests,
затем собирает PyInstaller **onedir / windowed**. Результат:

- `dist\VoxTypeX\VoxTypeX.exe` и `_internal` с Python, Qt, PortAudio и библиотеками STT.
- `dist\VoxTypeX-0.1.1-win-x64.zip` и контрольная сумма SHA256.
- `dist\VoxTypeX-0.1.1-source.zip` — чистые исходники для разработки и публикации.

`packaging\launcher.py` вызывает `freeze_support()` до Qt: дочерний процесс Whisper
использует тот же `.exe`, не создавая второе приложение. Спецификация явно собирает
DLL CTranslate2 и ONNX-ресурсы Silero VAD; штатные hooks собирают PySide6,
NumPy, PyAV, ONNX Runtime и PortAudio. Модели, аудио, IDE, `.local` и окружения
не поставляются. Версии закреплены для повторяемого процесса;
ZIP не обещает побитовой идентичности.

Проверка `.exe` с собственной тестовой фразой в WAV mono PCM16 / 16 кГц:

```powershell
.\scripts\smoke.ps1 -SpeechWav "C:\test\speech.wav"
```

Проверяются настоящий трей, настройки, callbacks микрофона, frozen-процесс Whisper
с VAD и штатный выход. Тест использует отдельный профиль в `.local`;
диагностический `result.json` содержит текст тестовой фразы.
Затем выполните [сценарий с Блокнотом](MANUAL_SMOKE_TEST.md).
Автоматический smoke test не заменяет реальную диктовку.

Перед первой публикацией прошли 202 unit tests, smoke test собранного EXE,
скачивание модели в пустой кэш и три живые диктовки в Блокнот.
Проверка выполнена на машине разработчика; это не проверка на чистой Windows.

### Установщик

```powershell
.\scripts\build-installer.ps1
```

Скрипт сначала собирает приложение, затем создаёт установщик Inno Setup **6.7.3**.
При первом запуске он скачивает официальный компилятор, проверяет цифровую подпись
и устанавливает его в `.local\tools`. Это сборочный инструмент; пользователю приложения
он не нужен. Оформление и значок создаются стандартным `System.Drawing` в Windows.

Если `dist\VoxTypeX` уже собран:

```powershell
.\scripts\build-installer.ps1 -SkipAppBuild
```

Результат: `dist\VoxTypeX-0.1.1-setup-x64.exe` и файл SHA256. Установщик включает
всю готовую папку приложения, библиотеки и уведомления о лицензиях; модели
скачиваются при первой диктовке. Исходный сценарий — `packaging\installer.iss`.

Проверка установки, опций, запуска и удаления с тестовой фразой:

```powershell
.\scripts\smoke-installer.ps1 -SpeechWav "C:\test\speech.wav"
```

Тест использует отдельную папку с пробелами и кириллицей, временно создаёт ярлыки
и автозапуск, затем удаляет тестовую установку. При уже установленном VoxTypeX
или существующих одноимённых ярлыках тест останавливается, чтобы не затронуть их.
На машине разработчика прошли установка в папку с кириллицей, проверка ярлыков
и автозапуска, запуск установленного EXE с реальным микрофоном и Whisper,
переустановка со снятыми галочками и удаление с сохранением настроек.

## Структура проекта

- `src/voxtypex/` — PTT, запись, состояния, настройки, STT и вставка; `ui/` — Qt/tray.
- `tests/` — unit tests.
- `packaging/`, `scripts/`, `requirements-build.lock` — сборка и smoke test.
- `docs/` — настройки и сценарий ручной проверки.
