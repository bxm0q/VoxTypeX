# Архитектура VoxTypeX

Сохраняется локальная PTT-диктовка. В итерации 2 добавлены отдельные модели
RU/UK/EN, загрузчик в UI и CPU runtime transcribe.cpp в spawn-worker.
Перевод и обработка через AI остаются только контрактами.

## Границы компонентов

| Компонент | Ответственность и владелец |
|---|---|
| `main.py` | Собирает зависимости; выбирает реализованный ASR по настройкам. |
| `ui/` | Только GUI thread: трей, настройки, индикатор и чтение событий таймером. |
| `RecordingService` | Рабочий поток: запись, одна сессия, цель ввода, вызов pipeline и единственная попытка вставки. Хуки ставят команды/Event. |
| `TranscriptionService` | Реализация `SpeechPipeline`: AudioProcessor → ASRBackend → очистка текста. Возвращает существующий `TranscriptionResult`; не вставляет текст. |
| `PCM16Processor` | Проверка mono PCM16/16 кГц, 200 мс и цифровой тишины. Стандартная библиотека; обычный шум обрабатывает VAD в Whisper. |
| `FasterWhisperEngine` | Управляет spawn-процессом, Pipe, timeout, отменой и повтором на CPU после сбоя CUDA. |
| `WhisperRuntime` / `WhisperModelManager` | В дочернем процессе: проверка локальной модели/кэша, загрузка весов, native inference. |
| `LanguageASRBackend` | Явный язык/профиль → локальная модель; auto → Whisper. Ошибка native даёт один fallback; отмена и timeout не запускают повтор. |
| `TranscribeCppEngine` / `LocalRuntime` | Тот же scoped IPC; CPU DLL и VAD только в дочернем процессе. Сегменты ≤20 с, неполный текст не покидает worker. |
| `LanguageModelManager` / `LocalModelsPanel` | Закреплённый GGUF-каталог, HTTPS, Range/size/SHA256, atomic replace и file lock. Download thread публикует metadata; GUI timer обновляет виджеты. |
| `TextInsertionService` / `WindowsInsertion` | HWND/PID/поле/UIA и guards отмены/ручного ввода; clipboard/Unicode без активации окна. |
| `SettingsService` | GUI thread: проверка JSON, миграции, резервная копия, явная атомарная запись. Рабочие потоки получают неизменяемые Settings. |

`interfaces.py` содержит Protocol и dataclass, не импортирует Qt или ASR SDK.
`ModelRegistry` описывает каталог; `WhisperModelRegistry` используется UI и
менеджером моделей. `ModelManager.resolve()` возвращает проверенный локальный
путь. `TranslationProvider.translate()` и `AIProvider.transform()` принимают
`TranscriptionResult`, `OperationContext` и необязательный `ProgressSink` и
возвращают результат того же типа. Встраивать их нужно между ASR и вставкой;
им запрещено самостоятельно управлять фокусом или отправлять клавиши.

## Сессия, отмена и прогресс

Один `RecordingService` допускает одну активную/зарезервированную сессию.
Повтор PTT и повторные release-команды не создают вторую вставку. Pipeline
работает синхронно в рабочем потоке; native CPU/GPU задачи остаются в spawn.
Короткий отдельный sender-поток не позволяет заполненному Pipe заблокировать
отмену владельца.

Каждый запуск pipeline получает `OperationContext`: номер сессии, общий Event
отмены и абсолютный monotonic deadline. Esc, отключение и открытие настроек
ставят Event. Владелец ASR завершает/закрывает дочерний процесс; следующая
сессия получает новый context и может повторить запуск. При выходе `cancel()`
также ставит terminal-флаг shutdown; закрытый backend повторно не используется.
После принудительной остановки Windows-процесса владелец ждёт до 5 секунд;
живой process handle не закрывается через `Process.close()`. Ошибка остановки
явно журналируется, handle сохраняется для повторной очистки.

Сохраняются лимиты этапов: старт/загрузка в память — 120 с, скачивание — 600 с,
inference — `max(60 с, duration × 5)`. Общий бюджет из настроек не продлевается
событиями этапов. Поздний успех отклоняется и pipeline, и владельцем вставки.
Некооперативную native-задачу останавливает процесс; произвольный сторонний
Python-провайдер обязан сам соблюдать context. Насильно прерывать Python-поток
приложение не пытается.

Pipe передаёт `(request_id, audio, language)` и `(request_id, kind, value)`.
Номер IPC-вызова независим от номера сессии и увеличивается при каждом вызове;
сообщения другого вызова отбрасываются до обработки результата или прогресса.
Повтор после CUDA-сбоя работает на новом Pipe и возвращает один итоговый результат.
Вставка после частичного SendInput не повторяется другим методом.

`ProgressEvent` содержит только номер сессии и `ProgressStage`. `ProgressBuffer`
ограничен 64 событиями, отбрасывает старые сессии и читается GUI-таймером.
Из публикации события никогда не вызывается Qt-виджет. `completed` в pipeline
означает готовность результата ASR, а не подтверждение вставки Windows;
готовность приложения определяет `AppState`. Старые `transcribe(audio)`,
`progress` и `engine.phase` оставлены для совместимости.

## Настройки и данные

Схема v6 сохраняет все прежние поля и добавляет только реализованные:

- `asr_backend`: `faster-whisper` или `transcribe-cpp`.
- `ru_quality`, `uk_quality`, `en_quality`: `compact` или `accurate`.
- `transcription_timeout_seconds`: общий бюджет обработки после записи,
  1–3600 с, по умолчанию 900 с. Настраивается в JSON при закрытом приложении.

Файлы v1 (включая отсутствие schema_version), v2–v5 проверяются до миграции.
Исходные байты сохраняются в `settings.json.vN.bak` соответствующей версии; другая
существующая копия не перезаписывается. После fsync временного файла выполняется
`os.replace`. Изменённый за время записи файл вызывает ошибку вместо замены.
Повреждённый JSON, повторные/неизвестные поля и будущая версия защищены и при
load, и при явном save. Межпроцессной блокировки настроек пока нет: следует
запускать одну копию приложения; compare-before-replace не устраняет все гонки
двух независимых процессов.

Настройки, кэш и логи находятся в `%LOCALAPPDATA%\VoxTypeX`; overrides
`VOXTYPEX_DATA_DIR` / `VOXTYPEX_MODEL_DIR` нужны для изолированных проверок.
Прогресс и логи не содержат аудио, транскрипт или выделенный текст. Ошибки
backend журналируются по типу/категории, без содержимого исключения. Истории нет.
Прежний opt-in `debug_wav` сохраняет диагностический WAV вне логов; по умолчанию
выключен и не включается миграцией.

## Совместимость и следующие шаги

`PreviewWorker` читает обработанные VAD-окна через `MicrophoneRecorder.preview_audio`.
У него отдельный operation-context и одна задача inference. Перед финальной
обработкой RecordingService отменяет context и дожидается выхода preview-потока;
inserter вызывается только с финальным `TranscriptionResult`. В приватном
`TranscriptBuffer` хранится одно событие partial/final/reset с session/generation/
sequence; Qt читает его таймером. Прежний metadata-only ProgressBuffer сохранён.
Все текущие ASR имеют `batch_preview=true`, `streaming=false`,
`partial_results=false`; повторы batch не выдают за streaming. Настройки v6,
порог контекста, дедупликация и измерения описаны в [TRANSCRIPT_PREVIEW.md](TRANSCRIPT_PREVIEW.md).

- Windows 10/11 x64, tray, текущие клавиши, Esc и безопасная вставка сохранены.
- Python-запуск и frozen launcher используют тот же pipeline. `freeze_support`
  остаётся до Qt, чтобы дочерний процесс не создавал второй трей.
- Native provider и ctypes binding transcribe.cpp собраны из закреплённого
  исходника. Они, NumPy, faster-whisper, CTranslate2 и ONNX Runtime используются
  для ASR только внутри worker; модель не загружается импортом GUI.
- ZIP и Inno Setup продолжают распространять onedir EXE. Пользовательские
  данные и модели не включаются в архив исходников или установщик.
- Следующий ASR должен реализовать ASRBackend и изолировать native-задачи.
  Новые поля Settings добавляются только вместе с рабочей реализацией/миграцией.
  Перевод/AI должны проверять context до/после внешнего вызова; только
  RecordingService может передать итоговый текст существующему inserter.
