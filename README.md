<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/UI-Flet-purple?logo=flutter&logoColor=white"/>
  <img src="https://img.shields.io/badge/Automation-Playwright-green?logo=playwright&logoColor=white"/>
  <img src="https://img.shields.io/badge/Async-aiohttp-yellow"/>
  <img src="https://img.shields.io/badge/Video-FFmpeg-red?logo=ffmpeg&logoColor=white"/>
  <img src="https://img.shields.io/badge/license-MIT-purple"/>
</p>

<h1 align="center">GetCourseVideoDownloader</h1>

<p align="center">
  <b>Десктопное приложение для скачивания видеоуроков с платформы GetCourse</b>
  <br>
  <img src="https://img.shields.io/badge/Windows-10%2F11-0078D6?logo=windows&logoColor=white"/>
  <img src="https://img.shields.io/badge/build-passing-brightgreen"/>
</p>

<img width="1386" height="843" alt="21ч46м28с" src="https://github.com/user-attachments/assets/22250e33-d7f8-4738-ad6a-f4578eb185cd" />

## ✨ Возможности

- **🖥️ GUI** — приложение на Flet с тёмной темой
- **📋 Просмотр курсов** — удобный список с раскрывающимися карточками и поиском
- **🎯 Выборочное скачивание** — отмечай только нужные уроки через чекбоксы
- **🎞️ Выбор качества** — 360p / 480p / 720p / 1080p / Auto (автоматический выбор максимального качества)
- **🔐 Авторизация в браузере** — проверка сессии, при необходимости — вход через Firefox
- **🍪 Persistent-сессия** — данные входа сохраняются в `session_data/` для повторного использования
- **⚡ Асинхронная загрузка** — сегменты видео скачиваются конкурентно (до 10 одновременно)
- **📁 Конвертация FFmpeg** — склейка `.ts` сегментов в `.mp4`
- **📂 Сохранение структуры** — курсы → папки, видео в соответствующих директориях
- **🔍 Поиск по урокам** — фильтрация списка в реальном времени
- **🚫 Удаление курсов** — кнопка для сброса и загрузки нового списка


## 🧩 Скриншоты

> *Стартовый экран* — ввод ссылки на плейлист
>
> *Экран курсов* — карточки с чекбоксами, поиск, настройки качества и пути сохранения
>
> *Оверлей загрузки* — реаль-тайм лог скачивания сегментов
>
> *Окно авторизации* — браузер Firefox для входа в аккаунт


## 📋 Требования

| Компонент | Версия |
|-----------|--------|
| **ОС** | Windows 10 / 11 |
| **Python** | 3.12 или выше |
| **FFmpeg** | Любая (должен быть в `PATH`) |
| **Браузер** | Firefox (устанавливается автоматически через Playwright) |



## 🚀 Установка

### 1. Клонируй репозиторий

```bash
git clone https://github.com/byMarken/GetCourseVideoDownloader.git
cd GetCourseVideoDownloader
```

### 2. Создай и активируй виртуальное окружение

```bash
python -m venv .venv
.venv\Scripts\activate
```

### 3. Установи зависимости

```bash
pip install -r req.txt
```

### 4. Установи браузер Playwright

```bash
playwright install firefox
```

### 5. Проверь FFmpeg

FFmpeg нужен для конвертации `.ts` сегментов в `.mp4`. Проверь установку:

```bash
ffmpeg -version
```

Если не установлен — проще всего через winget:

```powershell
winget install --id Gyan.FFmpeg -e
```

Или скачай вручную с [ffmpeg.org](https://ffmpeg.org/download.html), распакуй и добавь `bin` в `PATH`.


## 🎮 Использование

### Запуск приложения

```bash
python -m app.main
```

### Пошаговый сценарий

1. **Загрузи курсы** 🏁
   - Вставь ссылку на страницу плейлиста GetCourse (например, `https://school.beilbei.ru/teach/control/stream/view/id/123456789`)
   - Нажми **«Загрузить курсы»** или `Enter`

2. **Авторизация** (если требуется) 🔐
   - Автоматически откроется Firefox
   - Войди в аккаунт GetCourse
   - Нажми **«Продолжить»** в приложении
   - Сессия сохранится — в следующий раз авторизация не понадобится

3. **Выбери уроки** ☑️
   - Откроется экран со списком курсов и уроков
   - Используй поиск для фильтрации
   - Отмечай нужные уроки чекбоксами (или **«Выбрать всё»**/**«Убрать всё»**)

4. **Настрой параметры** ⚙️
   - **Качество**: Auto / 1080p / 720p / 480p / 360p
   - **Папка сохранения**: нажми на иконку папки и выбери директорию

5. **Скачай** 📥
   - Нажми **«Скачать выбранное»**
   - Наблюдай за процессом в реальном времени
   - После завершения закрой оверлей


## 🧱 Структура проекта

```
GetCourseVideoDownloader/
├── app/
│   ├── main.py                  # 🚀 Точка входа (Flet-приложение)
│   ├── theme.py                 # 🎨 Тёмная тема: цвета, градиенты, тени, UI-компоненты
│   ├── data/
│   │   ├── courses.json         # 📋 Распарсенные курсы и уроки (создаётся при парсинге)
│   │   └── settings.json        # ⚙️ Сохранённый путь для скачивания
│   ├── screens/
│   │   ├── start_screen.py      # 🏠 Стартовый экран (ввод ссылки, авторизация)
│   │   └── courses_screen.py    # 📚 Экран курсов (список, поиск, скачивание)
│   ├── scripts/
│   │   └── parse_courses.py     # 🔍 Парсинг курсов через Playwright
│   ├── services/
│   │   ├── givereq.py           # ⬇️ Скачивание видео (Playwright + aiohttp + ffmpeg)
│   │   └── parser_service.py    # 🔗 Мост между GUI и parse_courses.py
│   └── session_data/            # 🍪 Сессия Firefox (persistent context)
├── utils_console.py             # 🛠 Утилита UTF-8 для консоли
├── req.txt                      # 📦 Зависимости
└── README.md                    # 📖 Этот файл
```


## ⚙️ Детали работы

### 🔄 Как это работает под капотом

1. **Парсинг** — Playwright открывает Firefox (скрытый), заходит по ссылке плейлиста, собирает все курсы и ссылки на уроки → сохраняет в `courses.json`
2. **Авторизация** — используется Firefox **persistent context** (`app/session_data/`) — куки и localStorage сохраняются между запусками
3. **Загрузка** — для каждого урока открывается страница, перехватывается ответ с **m3u8 master-плейлистом**, парсятся доступные качества
4. **Сегменты** — из выбранного качества читаются `.ts`/`.bin` сегменты и скачиваются **асинхронно** (aiohttp + asyncio.Semaphore)
5. **Конвертация** — все сегменты склеиваются в `.ts`, затем FFmpeg конвертирует в `.mp4` (codec copy, без пережатия)

### 🌐 Как выглядит ссылка на плейлист

У каждого автора курсов на GetCourse свой адрес школы. Он может быть как на собственном домене, так и на поддомене getcourse:

```
https://school.beilbei.ru/teach/control/stream/view/id/123456789   ← пример
https://ваша-школа.getcourse.ru/teach/control/stream/view/id/123456789
https://уроки.ваш-домен.рф/teach/control/stream/view/id/123456789
```

`beilbei.ru` — это просто домен конкретного автора курсов. У тебя будет свой адрес, который тебе выдала школа/автор.


## 🧰 Зависимости

| Пакет | Назначение |
|-------|-----------|
| **flet** (0.85) | UI-фреймворк (Flutter-based) |
| **playwright** (1.61) | Управление браузером Firefox |
| **aiohttp** (3.14) | Асинхронная загрузка сегментов |
| **ffmpeg** (отдельно) | Конвертация TS → MP4 |

Полный список — в [req.txt](req.txt).




## ⚖️ Лицензия

Проект распространяется под лицензией **MIT**. Подробнее — в файле [LICENSE](LICENSE).


## ❓ Поддержка
Если возникли какие-либо проблемы или вопросы по использованию программы — пишите в Telegram: @No_Resp_404
