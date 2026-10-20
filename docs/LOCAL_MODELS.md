# Локальные модели RU / UK / EN

В настройках выберите «Отдельная локальная модель языка», затем язык и профиль.
«Автоматически» использует многоязычный Whisper. Раскладка клавиатуры не участвует
в определении языка. Whisper tiny/base/small остаётся отдельным выбором и fallback.

| Язык | Компактная | Байт | Точная | Байт |
|---|---|---:|---|---:|
| Русский | GigaAM v3 e2e RNNT Q4_K_M | 183948704 | GigaAM v3 e2e RNNT Q8_0 | 273724832 |
| Українська | Moonshine Base UK Q8_0 | 77476512 | Parakeet TDT 0.6B v3 Q8_0 | 739508576 |
| English | Canary 180M Flash Q4_K_M | 139223840 | Canary 180M Flash Q8_0 | 218447648 |

Профиль «Точная» использует более тяжёлую модель. Для GigaAM и Canary меняется
квантизация; меньший WER зависит от записи. Результаты сравнения —
в [ASR_BENCHMARK.md](ASR_BENCHMARK.md).

Нажмите «Скачать / проверить модель»: до начала показываются размер и лицензия.
Отмена сохраняет `.part`; повторная попытка использует Range только после
проверки ответа 206 и точного Content-Range. Ответ 200 начинает файл заново.
После скачивания проверяются размер и полный SHA256, затем выполняется
`os.replace`. Неверный файл не становится установленной моделью. Есть проверка
свободного места, HTTPS/redirect allowlist, отказ от ссылок/reparse points и
межпроцессная блокировка установки одной модели. При обрыве сети повторите
скачивание. Заблокированное сетевое чтение может задержать отмену до 15 секунд.

Кэш: `%LOCALAPPDATA%\VoxTypeX\models\<model-id>`. `VOXTYPEX_MODEL_DIR` задаёт
отдельный каталог для разработки. Изменение профиля не удаляет прежние файлы.
`installed` — файл нужного размера; `ready` — SHA256 проверен; `broken` — ошибка
файла/установки; `downloading` — активная установка. Перед загрузкой в ASR
файл снова проверяется. Скачивания при каждом запуске нет. Установка новых
GGUF выполняется явно, отсутствие модели при диктовке даёт Whisper fallback.

Настройки schema v6 хранят backend и `ru_quality`, `uk_quality`, `en_quality`.
Миграция v1–v5 сохраняет исходный файл в резервной копии. Старый
Whisper остаётся выбранным после обновления. Нажмите «Сохранить»: замена
worker выполняется между операциями, без перезапуска приложения.

Runtime — transcribe.cpp 0.4.0, commit
`f1dcd15858c4bbba87ca97862b4e448b2cf15b8f`. Runtime собирается MSVC x64
из исходников. Python binding использует ctypes.
Собственный CPU wheel содержит базовый x64 backend и варианты ISA с выбором
при запуске. Этот runtime работает на CPU; CUDA/Vulkan отключены.
Native DLL, NumPy и Silero VAD загружаются только
в spawn-worker; сбой DLL не должен завершать GUI.

Аудио — mono PCM16, 16 кГц. Silero VAD делит речь по паузам и ограничению
длительности; VoxTypeX дополнительно ограничивает каждый вход модели 20 секундами
и фактическим session limit. Общий предел записи остаётся 120 секунд.
Сегменты не перекрываются; тексты соединяются пробелами. При переполнении
декодера неполный текст отбрасывается и сегмент делится пополам. Ни один
частичный результат не передаётся вставке. Модель используется повторно;
смена модели и 180 секунд простоя освобождают worker. При переходе между Auto
и явным языком предыдущий backend закрывается сразу, до загрузки следующего.
Esc/deadline завершают
worker; отмена не запускает fallback. Вставкой по-прежнему владеет RecordingService.

## Лицензии и источники

Ревизии, HTTPS URL, размеры и SHA256 записаны в `src/voxtypex/model_catalog.json`.
Хеши получены из LFS metadata Hugging Face и проверены по скачанным файлам.

- GigaAM — MIT. [Исходная модель](https://huggingface.co/ai-sage/GigaAM-v3).
- Canary и Parakeet — CC-BY-4.0. [Canary](https://huggingface.co/nvidia/canary-180m-flash),
  [Parakeet](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3).
- Moonshine Base UK — **Moonshine AI Community License**. Несмотря на MIT
  в карточке конвертации GGUF, upstream отдельно сохраняет Community License
  для legacy Ukrainian Base/Tiny. Личное использование и оценка разрешены;
  коммерческое использование требует регистрации и соблюдения условий,
  для крупного бизнеса требуется отдельная лицензия. Перед загрузкой прочитайте
  [полный текст](https://github.com/moonshine-ai/moonshine/blob/234f60faa0eb388b01cdf7e60aca232af37aefda/LICENSE).
  Он также приложен в `packaging/licenses/MOONSHINE-COMMUNITY.txt`.
  Обязательная атрибуция — в `packaging/licenses/NOTICE.txt`; оба файла входят
  в `docs/licenses` готового приложения и установщика.
  **Powered by Moonshine AI.**
- transcribe.cpp, ggml и miniz — MIT; тексты лицензий входят в native payload.

Веса выбранных ASR и аудиокорпус не входят в EXE, ZIP исходников или Git.
Небольшой Silero VAD входит в runtime как ресурс faster-whisper.

## Сборка из исходников

Нужны Windows x64, Python 3.12+ (проверен 3.13), MSVC C++ Build Tools 2022 и CMake.
Из корня проекта, в своём Python-окружении:

```powershell
pip install -e '.[dev]' pyinstaller
./scripts/build-native.ps1 -PythonPath "$PWD/.venv/Scripts/python.exe"
python -m voxtypex
```

Для полной сборки можно передать существующее окружение:

```powershell
./scripts/build.ps1 -EnvironmentPath "$PWD/.venv"
./scripts/build-installer.ps1 -SkipAppBuild -BuildPython "$PWD/.venv/Scripts/python.exe"
```

`build-native.ps1` получает закреплённые исходники в `.local/tools`, собирает
CPU wheel, устанавливает binding/provider и сохраняет DLL/лицензии/хеши в
`.local/native`. Новая сборка не требует сети для уже установленной модели.
