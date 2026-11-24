# J.A.R.V.I.S.

Голосовой помощник для Windows в духе Джарвиса из «Железного человека». Он открывает и закрывает программы и файлы, меняет громкость и яркость, ищет в интернете, ведёт заметки, работает с почтой и календарём Google и выключает компьютер по команде. Интерфейс показывается прямо на рабочем столе: на обоях Wallpaper Engine или как живые обои через Microsoft Edge.

## Скачать

**[JARVIS-Setup.exe](https://github.com/Akhat-spec/jarvis/releases/latest/download/JARVIS-Setup.exe)** — установщик для Windows 10/11 (64-бит), около 7 МБ. Все версии лежат на странице [Releases](https://github.com/Akhat-spec/jarvis/releases).

Пошаговая инструкция лежит в релизе (файл `JARVIS-Install-Guide-RU.txt`). Коротко:

1. Скачайте и запустите `JARVIS-Setup.exe`. Если Windows покажет «Windows защитила ваш компьютер», нажмите «Подробнее» → «Выполнить в любом случае»: установщик не подписан цифровой подписью.
2. Если Python нет, установщик предложит поставить его сам.
3. Выберите папку и где показывать интерфейс.
4. В отдельном окне вставьте ключи API:
   - **Anthropic** (обязательно) — [console.anthropic.com](https://console.anthropic.com/settings/keys), оплата по факту использования;
   - **ElevenLabs** (по желанию) — голос как в фильме, без него работает бесплатный голос Microsoft.
5. Через 2–5 минут Джарвис запустится. Скажите: «Джарвис, открой браузер».

Ключи хранятся только на вашем компьютере, в файле `.env` в папке программы.

## Что нужно

- Windows 10 или 11, 64-бит
- Микрофон и интернет
- Ключ Anthropic API
- По желанию: ключ ElevenLabs, Wallpaper Engine (Steam), свой OAuth-клиент Google для почты и календаря

## Для разработчиков

| Путь | Что это |
|---|---|
| `run.py` | ядро: голос, мозг (Claude), инструменты, WebSocket для интерфейса |
| `core/` | распознавание речи, синтез голоса, действия в Windows, Google, погода, датчики |
| `hud/` | интерфейс (HTML/CSS/JS) |
| `wallpaper/`, `wallpaper.py` | обои: проект Wallpaper Engine и встраивание Edge в рабочий стол |
| `setup_wizard.py` | мастер установки, окно ключей API, удаление |
| `installer/` | загрузчик (`bootstrap.ps1`), однофайловый `Setup.exe` (C#) и сборка `build.py` |
| `tests/run_tests.py` | проверки |

Запуск из исходников:

```powershell
python -m pip install -r requirements.txt
copy .env.example .env   # впишите ANTHROPIC_API_KEY
pythonw run.py
```

Сборка установщика (нужен только .NET Framework 4, он есть в Windows):

```powershell
python installer\build.py
```

В `tools/LibreHardwareMonitor` лежит сторонняя программа [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) (MPL-2.0) для температуры процессора.
