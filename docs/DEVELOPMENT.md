# Разработка VoxTypeX

[Главная страница](../README.md) · [Ручная проверка](MANUAL_SMOKE_TEST.md)

## Запуск из исходников

Сборочная среда — Python **3.13 x64**, Windows 11.
Исходники требуют Python 3.12+; другой интерпретатор нужно проверять отдельно
с нативными зависимостями.
В PowerShell из корня проекта:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests packaging scripts
```

Запуск из исходников:

```powershell
.\.venv\Scripts\python.exe -m voxtypex
```

Отдельный тестовый профиль задаётся `VOXTYPEX_DATA_DIR`, отдельный кэш моделей —
`VOXTYPEX_MODEL_DIR`. Переменные действуют и в готовой сборке.
Whisper работает с обычными Python-зависимостями. Для отдельных моделей сначала
соберите [native runtime](LOCAL_MODELS.md#сборка-из-исходников); нужны MSVC C++
Build Tools 2022 и CMake. Его библиотеки загружаются только в дочернем процессе.

## Сборка приложения

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

Если Python Launcher отсутствует:

```powershell
.\scripts\build.ps1 -PythonPath "C:\Program Files\Python313\python.exe"
```

Скрипт создаёт `.local\build-venv`, устанавливает закреплённые версии из
`requirements-build.lock`, собирает CPU runtime transcribe.cpp из закреплённого
commit, проверяет зависимости, Ruff и unit tests,
затем собирает PyInstaller **onedir / windowed**. Результат:

- `dist\VoxTypeX\VoxTypeX.exe` и `_internal` с Python, Qt, PortAudio и библиотеками STT.
- `dist\VoxTypeX-0.1.3-win-x64.zip` и контрольная сумма SHA256.
- `dist\VoxTypeX-0.1.3-source.zip` — чистые исходники для разработки и публикации.

`packaging\launcher.py` вызывает `freeze_support()` до Qt: дочерний процесс ASR
использует тот же `.exe`, не создавая второе приложение. Спецификация явно собирает
DLL CTranslate2 и ONNX-ресурсы Silero VAD; штатные hooks собирают PySide6,
NumPy, PyAV, ONNX Runtime и PortAudio. Модели, аудио, IDE, `.local` и окружения
не поставляются.

Звуковой тракт включает WebRTC NS/VAD. Локальный hook `packaging/hooks/hook-webrtcvad.py`
сохраняет метаданные именно `webrtcvad-wheels`; для `pywebrtc-audio` сохраняются
также `pywebrtc_audio.libs` и полные лицензии. [Настройки звука](AUDIO_PIPELINE.md).

Проверка `.exe` с собственной тестовой фразой в WAV mono PCM16 / 16 кГц:

```powershell
.\scripts\smoke.ps1 -SpeechWav "C:\test\speech.wav"
```

Проверяются трей, настройки, callbacks микрофона, frozen-процесс Whisper
с VAD и штатный выход. Тест использует отдельный профиль в `.local`;
диагностический `result.json` содержит число символов, без текста тестовой фразы.
Затем выполните [сценарий с Блокнотом](MANUAL_SMOKE_TEST.md).

Проверка нативного шумоподавления, VAD, тихой речи, точного bypass и повторного
открытия микрофона без ASR:

```powershell
.\scripts\smoke-audio.ps1 -SpeechWav "C:\test\speech.wav"
```

Скрипт не сохраняет запись микрофона; результат содержит только метрики и названия
проверок. Для ZIP распакуйте архив и передайте `-Exe` с путём к распакованному EXE.

Для проверки трёх локальных моделей установите их в тестовый кэш, подготовьте
[публичный корпус](ASR_BENCHMARK.md) и выполните:

```powershell
$env:VOXTYPEX_MODEL_DIR="$PWD/.local/models"
.\scripts\smoke-local-models.ps1 -Manifest .local/asr-benchmark/manifest.json
```

Проверяются frozen-worker, RU/UK/EN без fallback, повторное использование,
смена языка, длинная запись, отмена, повторный запуск, освобождение при простое,
переход Auto/Whisper ↔ явный язык с закрытием предыдущей модели и выход.

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

Результат: `dist\VoxTypeX-0.1.3-setup-x64.exe` и файл SHA256. Установщик включает
всю готовую папку приложения, библиотеки и уведомления о лицензиях. Whisper
скачивается при первой диктовке, отдельные модели — кнопкой в настройках.
Исходный сценарий — `packaging\installer.iss`.

Проверка установки, опций, запуска и удаления с тестовой фразой:

```powershell
.\scripts\smoke-installer.ps1 -SpeechWav "C:\test\speech.wav"
```

Добавьте `-NativeManifest .local/asr-benchmark/manifest.json`, чтобы также
проверить RU/UK/EN и native DLL из установленной папки. Модели должны быть уже
установлены в кэш, заданный `VOXTYPEX_MODEL_DIR`.

Тест использует отдельную папку с пробелами и кириллицей, временно создаёт ярлыки
и автозапуск, затем удаляет тестовую установку. При уже установленном VoxTypeX
или существующих одноимённых ярлыках тест останавливается, чтобы не затронуть их.

## Структура проекта

- `src/voxtypex/` — PTT, запись, состояния, настройки, STT и вставка; `ui/` — Qt/tray.
- `tests/` — unit tests.
- `packaging/`, `scripts/`, `requirements-build.lock` — сборка и smoke test.
- `docs/` — настройки и сценарий ручной проверки.
