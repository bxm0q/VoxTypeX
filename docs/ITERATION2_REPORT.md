# Итерация 2 — локальные модели по языкам

Работа ведётся в существующем VoxTypeX, ветка `codex/language-models`.
Версия для публикации — 0.1.2. Этот отчёт также перечисляет сохранённые
незакоммиченные изменения архитектурной итерации 1 относительно исходного HEAD.

## Что реализовано

- Отдельный CPU backend transcribe.cpp 0.4.0 с настоящей сборкой MSVC x64 из
  закреплённого commit. Binding использует ctypes в spawn-worker. В GUI нет
  импорта нового native runtime. Базовый x64 backend и варианты ISA входят в сборку.
- Шесть реальных моделей: GigaAM Q4/Q8 для RU, Moonshine UK / Parakeet v3 для UK,
  Canary Q4/Q8 для EN. Все файлы скачаны и проверены по полному SHA256 и размеру.
  [Каталог](../src/voxtypex/model_catalog.json) содержит закреплённые ревизии,
  HTTPS URL, лицензии, ограничения и размеры в байтах.
- Явный выбор языка и профиля, размер и лицензия перед скачиванием, асинхронная
  загрузка с прогрессом и отменой. Проверенный HTTP 206 позволяет докачку;
  неправильный Range или ответ 200 не дописываются к старым байтам.
  SHA256 проверяется до атомарного перемещения. Есть проверки диска, путей,
  redirect и межпроцессная блокировка установки модели.
- Mono PCM16 16 кГц, Silero VAD и ограниченные сегменты, повторное разбиение
  при переполнении декодера, объединение только полных результатов.
  Повторное использование worker, освобождение при смене и после 180 секунд простоя.
- Auto использует Whisper. При ошибке локального backend выполняется один
  Whisper fallback; отмена и deadline fallback не запускают. Вставкой владеет
  прежний RecordingService с проверкой цели и защитой от повторного результата.
- Settings schema v4: backend и три профиля языков. Миграция v1/v2/v3 атомарная,
  с резервной копией исходных байтов. Повреждённые и будущие форматы не затираются.
  Старый Whisper остаётся выбранным после обновления.
- Воспроизводимый benchmark на публичной речи с WER/CER, RAM, VRAM, warmup,
  скоростью и размером моделей. [Методика и результаты](ASR_BENCHMARK.md).
  Модели и аудио остаются вне Git и архивов исходников.

## Изменённые и добавленные файлы

| Область | Файлы относительно исходного HEAD |
|---|---|
| Настройки и интерфейсы | `src/voxtypex/config.py`, `interfaces.py`, `models.py`, `progress.py`, `audio_processing.py` |
| ASR и жизненный цикл | `src/voxtypex/asr.py`, `local_engine.py`, `local_worker.py`, `local_models.py`, `model_catalog.json`, `transcription.py`, `whisper_engine.py`, `whisper_worker.py`, `recording_service.py`, `main.py` |
| Qt | `src/voxtypex/ui/local_models_panel.py`, `settings_window.py`, `status_overlay.py`, `tray.py` |
| Сборка | `pyproject.toml`, `.gitignore`, `.gitattributes`, `packaging/VoxTypeX.spec`, `launcher.py`, `installer.iss`, `native-runtime.json`, `licenses/MOONSHINE-COMMUNITY.txt`, `licenses/NOTICE.txt`, `frozen_smoke.py`, `native_smoke.py` |
| Скрипты | `scripts/build.ps1`, `build-installer.ps1`, `build-native.ps1`, `stage_native.py`, `native_build_cache.py`, `release_files.py`, `smoke-local-models.ps1`, `smoke-installer.ps1`, `fetch_benchmark_corpus.py`, `benchmark_asr.py` |
| Тесты | `tests/test_config.py`, `test_daily_mvp.py`, `test_recording_service.py`, `test_ui.py`, `test_whisper_engine.py`, `test_pipeline.py`, `test_language_asr.py`, `test_local_models.py`, `test_local_models_ui.py`, `test_native_build_cache.py`, `test_benchmark.py` |
| Документация | `README.md`, `docs/ARCHITECTURE.md`, `SETTINGS.md`, `DEVELOPMENT.md`, `MANUAL_SMOKE_TEST.md`, `LOCAL_MODELS.md`, `ASR_BENCHMARK.md`, `ASR_BENCHMARK_RESULTS.json`, этот отчёт |

## Проверки

Проверенная среда: Windows 11 x64, Python 3.13.15, MSVC Build Tools 2022,
CMake 3.31.6. В этой рабочей копии используется `.local/iteration1-venv`.
Старые перенесённые окружения с несуществующими путями не изменялись.

Команды выполняются из корня проекта; `HF_HUB_OFFLINE=1` запрещает benchmark и
smoke-тестам неявно скачивать модели. Unit tests не загружают большие веса.

```powershell
$py = "$PWD/.local/iteration1-venv/Scripts/python.exe"
$env:HF_HUB_OFFLINE='1'
./scripts/build-native.ps1 -PythonPath $py
& $py -m pip check
& $py -m ruff check src tests packaging scripts
& $py -m pytest -q
& $py scripts/benchmark_asr.py --manifest .local/iteration2-benchmark/manifest.json --cache-dir .local/mvp-models --out .local/iteration2-benchmark/results-final
./scripts/build.ps1 -EnvironmentPath "$PWD/.local/iteration1-venv" -SkipNativeBuild
& $py -m PyInstaller --noconfirm packaging/VoxTypeX.spec
& $py scripts/release_files.py
./scripts/build-installer.ps1 -SkipAppBuild -BuildPython $py -CompilerPath "$PWD/.local/tools/inno-6.7.3/ISCC.exe"
$env:VOXTYPEX_MODEL_DIR="$PWD/.local/mvp-models"
./scripts/smoke-local-models.ps1 -Manifest .local/iteration2-benchmark/manifest.json
./scripts/smoke.ps1 -SpeechWav .local/release-speech.wav
./scripts/smoke-installer.ps1 -SpeechWav .local/release-speech.wav -NativeManifest .local/iteration2-benchmark/manifest.json
```

| Проверка | Фактический результат |
|---|---|
| Native runtime | Настоящая MSVC CPU-сборка; 12 DLL. Повторный `build-native.ps1` проверил wheel receipt, DLL и compiled commit, установил binding и завершился успешно. |
| `pip check` | No broken requirements found. |
| Ruff | All checks passed. |
| pytest | Перед публикацией 0.1.2: 300 passed, 6.76 s, без пропусков. В предыдущем прогоне: 299 passed, 1 skipped, 6.94 s из-за прав на создание symlink; в среде публикации этот тест также прошёл. |
| Benchmark | Exit 0: 30 прогонов, по 12 клипов, 0 ошибок / fallback; 15 warmup исключены из оценки. |
| PyInstaller / ZIP | `build.ps1` завершился с exit 0; onedir EXE, приложение и архив исходников собраны. |
| Native frozen smoke | Финальный EXE: 9/9, exit 0. RU/UK/EN без fallback, reuse, смена языка, VAD/объединение длинного аудио, отмена/повтор, Auto/Whisper ↔ EN с освобождением предыдущей модели, idle release, реальный GUI/tray и выход. Скриншот настроек проверен. |
| Whisper frozen smoke | 5/5, exit 0: настоящий tray, настройки, callbacks микрофона, frozen Whisper с VAD, завершение приложения. Текст в отчёт не записан. |
| Inno Setup | 6.7.3, exit 0; установщик пересобран после последних правок EXE и дополнения лицензий. |
| Установленная копия | 7 проверок, exit 0: путь с кириллицей/пробелами, EXE совпадает со сборкой, native payload/каталог/лицензии, ярлыки/автозапуск/uninstall entry, настоящий Whisper 5/5 и native 9/9, переустановка со снятыми опциями, удаление с сохранением пользовательских настроек. |

После последних правок жизненного цикла выполнены отдельные Ruff/pytest,
повторная упаковка PyInstaller и оба frozen smoke-теста. При ручном вызове
PyInstaller PATH ограничен Python-окружением и системными каталогами, как в build.ps1.

Финальные журналы (вне Git): `.local/iteration2-final-tests.log`,
`iteration2-final-freeze.log`, `iteration2-native-final-smoke.log`,
`iteration2-whisper-final-smoke.log`, `iteration2-release-installer-build.log`,
`iteration2-release-installer-smoke.log`. Последний installer smoke:
`.local/installer-smoke-384767b78d9244a499a41c3f6dfb747f/result.json`.

Для публикации 0.1.2 обновлён номер версии, повторно собраны EXE, ZIP и Inno Setup.
Проверены Ruff, 300 unit tests, `pip check` и полный installer smoke с реальными
Whisper 5/5 и native 9/9. Журналы: `.local/release012-tests.log`,
`release012-freeze.log`, `release012-installer-build.log`, `release012-installer-smoke.log`.
WER/CER в benchmark JSON измерены до смены номера версии; в JSON сохранена
фактическая версия окружения на момент измерений (0.1.1). Исходный текст лицензии Moonshine сохранён с его пробелами;
в `.gitattributes` отключена проверка пробелов только для этого файла.

Артефакты: `dist/VoxTypeX/VoxTypeX.exe`, `dist/VoxTypeX-0.1.2-win-x64.zip`,
`dist/VoxTypeX-0.1.2-source.zip`, `dist/VoxTypeX-0.1.2-setup-x64.exe`.
Для ZIP и установщика созданы `.sha256`.

Реальная сетевая проверка: RU-загрузка отменена на 1 MiB, повторный запрос
`Range: bytes=1048576-` получил 206 и точный Content-Range, итоговый SHA256 совпал.
Сетевой обрыв, EOF, неверный Range/хеш, нехватка диска и блокировки проверяются
отдельными тестами. Реальный source-worker распознал RU/UK/EN без fallback,
повторно использовал PID, сменил язык без перезапуска и освободился при простое.

Первый полный benchmark остановился после UK/small с `ValueError`; прежний CLI
сохранил только тип исключения. Его неполный отчёт не использован в итоговых таблицах.
Добавлена трассировка мест вызова без содержимого исключения. Отдельный короткий
probe и полный повторный прогон завершились с exit 0.

По сохранённым этапам первый сбой пришёлся на закрытие worker. Дополнительно
воспроизведён дефект этого пути: `Process.close()` вызывался при ещё живом процессе,
если принудительное завершение Windows занимало больше секунды. Два новых теста
сначала воспроизвели ValueError, затем прошли после исправления. После `kill()`
ожидание увеличено до 5 секунд; при неуспехе выдаётся явная `WorkerFailed`, handle
остаётся доступным для повторной очистки. Без исходного стека нельзя доказать,
что именно задержка завершения вызвала тот первый сбой. Декодирование не менялось;
закрытие worker после профиля не входит в опубликованные WER/CER/RTF.

Также тестами воспроизведено удержание неактивной модели при переходах Auto ↔
явный язык. Теперь предыдущий backend закрывается до запуска следующего;
повторные запросы в том же режиме по-прежнему переиспользуют текущую модель.

При первом review исправлены два дефекта: повторное использование wheel по одному имени
без доказательства исходного commit и задержка отмены при медленно поступающем
HTTP body. Добавлены регрессионные тесты; кэш теперь проверяет receipt с исходниками,
параметрами и SHA256, staging сверяет DLL и фактический compiled commit.

Финальное review обнаружило повторное использование terminal backend после
неуспешного `close()`. Добавлены маркеры незавершённой очистки: handle остаётся
для повторной попытки, но распознавание использует новый экземпляр только после
успешного закрытия старого. Два регрессионных сценария (local и Whisper) сначала
воспроизвели `cancelled`, затем прошли. Повторное адресное review не нашло
Critical/Important замечаний; reviewer не запускал тесты, полный прогон выполнен отдельно.

## Ручная проверка Windows

1. Запустите `dist/VoxTypeX/VoxTypeX.exe`, откройте настройки через трей.
   Выберите Whisper и проверьте обычную PTT-диктовку в пустом Блокноте.
2. Выберите отдельную модель, Русский, компактный профиль. Начните загрузку,
   отмените и повторите; после проверки SHA256 сохраните настройки.
3. Продиктуйте две фразы: каждая вставляется один раз, вторая не скачивает модель.
   Повторите для Українська и English без перезапуска приложения.
4. Во время записи и обработки нажмите Esc: отменённый текст не появляется.
   Следующая запись работает. При смене окна или поля результат не попадает в новую цель.
5. Проверьте длинную запись с паузами, auto с Whisper, выход во время обработки,
   сохранение настроек после перезапуска. Повторите на Windows 10 и 11 x64.

Полный [чек-лист](MANUAL_SMOKE_TEST.md) содержит также повторы, перезапуск и fallback.
Прохождение автоматических тестов не отмечает живую диктовку как выполненную.

## Ограничения

- На Windows 10 эта итерация не запускалась. Проверки выполнены на рабочей Windows 11,
  а не на чистой виртуальной машине. Новое GPU-ускорение не реализовано и не заявляется.
- В финальном Auto/Whisper smoke попытка CUDA дала RuntimeError при inference;
  автоматический повтор на CPU сработал. Полноценная работа Whisper на GPU этой
  проверкой не подтверждается. Новые GGUF-модели проверены на CPU.
- Корпус: по 12 прочитанных клипов на язык, плюс тот же звук с синтетическим шумом.
  Разговорных записей с эталоном нет. Это не основание обещать превосходство на любой речи.
- «Точная» — более тяжёлый профиль; Q8 не обязательно уменьшает WER.
  Moonshine UK имеет Community License; её условия и атрибуция сохранены.
- Сетевой timeout — 15 секунд: ожидание заблокированного чтения может задержать отмену.
  Повторная попытка докачивает только при подтверждённом ответе сервера.
- Пользовательский кэш не переносится в установщик. Новую модель нужно установить
  явно; отсутствующая/сломанная модель переключает диктовку на Whisper.
- Поддерживается одна запущенная копия приложения. Межпроцессной блокировки
  редактирования settings нет; блокировка загрузки модели реализована отдельно.
- Реальную вставку в приложения с повышенными правами, защищённые поля и все
  сторонние редакторы автоматические ASR-проверки не подтверждают. Сборка без подписи.
