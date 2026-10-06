<p align="center">
  <img src="https://raw.githubusercontent.com/bxm0q/VoxTypeX/main/assets/readme-header.svg" alt="VoxTypeX — голосовой ввод для Windows" width="900">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Windows-10%20%2F%2011%20x64-2463a0?style=flat-square" alt="Windows 10/11 x64">
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776ab?style=flat-square" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/Whisper-local-52796f?style=flat-square" alt="Локальное распознавание Whisper">
</p>

VoxTypeX работает в трее и превращает речь в текст: зажмите **правый Alt**, скажите фразу и отпустите клавишу. После распознавания текст появится в активном поле.

Распознавание выполняется на компьютере через **faster-whisper**. Аудио не отправляется на сервер. Интернет нужен для первого скачивания выбранной модели; дальше можно работать офлайн.

<p align="center">
  <strong><a href="https://github.com/bxm0q/VoxTypeX/releases/latest">Скачать для Windows</a></strong>
  &nbsp;·&nbsp; <a href="docs/SETTINGS.md">Настройки</a>
  &nbsp;·&nbsp; <a href="https://github.com/bxm0q/VoxTypeX/blob/main/docs/DEVELOPMENT.md">Разработка и сборка</a>
</p>

## Что умеет

- Диктовка по удержанию клавиши, отмена через **Esc**.
- Выбор микрофона, языка и модели распознавания.
- Русский, украинский, английский и автоматическое определение языка.
- Индикатор записи и обработки без переключения фокуса.
- Сохранение настроек и включение автозапуска через установщик.

<p align="center">
  <img src="https://raw.githubusercontent.com/bxm0q/VoxTypeX/main/assets/settings.png" alt="Окно настроек VoxTypeX: клавиша диктовки, микрофон, язык и модель" width="520">
</p>

## Быстрый старт

1. Скачайте **`-setup-x64.exe`** из [последнего релиза](https://github.com/bxm0q/VoxTypeX/releases/latest), установите и запустите программу.
2. Откройте **V в трее → Настройки…**, выберите микрофон и язык, сохраните.
3. В текстовом поле зажмите **правый Alt**, произнесите фразу и отпустите. Дождитесь вставки, не печатая и не переключая поле.

Без установки: распакуйте **`-win-x64.zip`** и запустите `VoxTypeX.exe`. Папка `_internal` должна оставаться рядом.

<details>
<summary>Ограничения</summary>

- Точность зависит от модели и качества записи. В защищённые поля и некоторые редакторы текст может не вставляться.
- Клавиша диктовки сохраняет обычное действие в других программах — выбирайте свободную.
- Истории и автообновления пока нет. Запускайте одну копию. Сборка без цифровой подписи.

</details>

---

[Сообщить об ошибке](https://github.com/bxm0q/VoxTypeX/issues)
