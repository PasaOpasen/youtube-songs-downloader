# Скачивание звука с YouTube

Скрипт читает файл со ссылками и сохраняет звук каждой ссылки в указанную папку. Если песня уже лежит в папке, она не скачивается снова. Между запросами к YouTube есть пауза.

Ролики без ограничения качаются без входа. Если YouTube пишет, что видео с возрастным ограничением и без авторизации часть форматов недоступна, скрипту нужны cookies аккаунта, которому этот ролик уже можно смотреть.

## Установка

Нужны Python 3.11+, пакеты из `requirements.txt`, `ffmpeg` и JavaScript-движок. Без движка YouTube не отдаёт медиапоток: у ролика остаются только картинки, и скрипт пишет `Requested format is not available`. `ffmpeg` нужен, чтобы вырезать звук из скачанного файла.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`yt-dlp[default]` ставит и сам yt-dlp, и пакет решателя `yt-dlp-ejs`. Движок — один из двух:

- [Deno](https://docs.deno.com/runtime/getting_started/installation/) 2.3 или новее. Его yt-dlp использует в первую очередь.
- [Node.js](https://nodejs.org) 22 или новее.

Скрипт ищет `deno`, `node` и `qjs` в `PATH`, в `~/.deno/bin`, в `~/.local/bin` и в `.tools` рядом с собой. Если движка нет, загрузка не начинается.

Дальше команды даны как `python3 download_audio.py`. Если ставили в виртуальное окружение, вызывайте `.venv/bin/python download_audio.py`.

## Запуск

```bash
python3 download_audio.py links.txt ./music
```

- `links.txt` — текстовый файл, одна ссылка на строку.
- `./music` — папка для звука. Если её нет, скрипт создаст её сам.

Подходят обычные адреса, короткие `youtu.be` и Shorts. Если в ссылке есть плейлист, берётся только само видео. Пустые строки и строки, начинающиеся с `#`, пропускаются.

Пример `links.txt`:

```text
# один ролик на строку
https://www.youtube.com/watch?v=XXXXXXXXXXX
https://youtu.be/YYYYYYYYYYY
https://www.youtube.com/shorts/ZZZZZZZZZZZ
```

Файл читается как UTF-8. Если это не получилось, скрипт пробует CP1251.

YouTube отвечает `HTTP Error 403` на отдельную звуковую дорожку, если запросить её целиком. Скрипт поэтому качает обычный mp4, где звук уже упакован вместе с картинкой, и `ffmpeg` копирует звук в `Название [id].m4a` без перекодирования. Видеофайл после этого удаляется. Трафик — это размер mp4, обычно несколько мегабайт на песню, а не одна только звуковая дорожка.

Повторный запуск пропускает ссылку, если в папке уже есть файл с тем же id в имени или файл с тем же названием (`Название.mp3`, `Название.m4a` и другие звуковые расширения). Совпадение по id не обращается к YouTube.

Пауза между запросами по умолчанию 10–14 секунд. Меньше 1 секунды поставить нельзя. Пока страница ролика разбирается, между служебными запросами ещё 2 секунды.

```bash
python3 download_audio.py links.txt ./music --pause 40
```

Ошибка одной ссылки не останавливает остальные. В конце печатается, сколько скачано, пропущено и сколько ошибок.

## Cookies для возрастного ограничения

Без cookies скрипт ходит на YouTube как гость. Гостю YouTube не отдаёт ролики 18+. Сообщение выглядит так:

```text
This video is age-restricted; some formats may be missing without authentication.
Use --cookies-from-browser or --cookies for the authentication.
```

Нужна сессия того аккаунта, который уже может открыть этот ролик в браузере: вход выполнен, возраст подтверждён, страница ролика играет. Чужие cookies использовать не нужно: это чужой вход в аккаунт.

Достаточно одного из двух аргументов.

### Взять cookies прямо из браузера

1. Откройте Firefox или Chrome и зайдите на [youtube.com](https://www.youtube.com) под нужным аккаунтом.
2. Откройте один из возрастных роликов и убедитесь, что он воспроизводится, а не просит подтвердить возраст.
3. Запустите скрипт, указав этот браузер:

```bash
python3 download_audio.py links.txt ./music --cookies-from-browser firefox
```

Для Chrome или Chromium на Fedora cookies зашифрованы связкой ключей рабочего стола. Сессия должна быть разблокирована. Для GNOME:

```bash
python3 download_audio.py links.txt ./music --cookies-from-browser chrome+gnomekeyring
```

Для KDE замените связку на `kwallet6` (или `kwallet5`, если стоит она). Имя браузера: `chrome`, `chromium`, `brave`, `edge`, `opera`, `vivaldi`, `whale`, `firefox`, `safari`.

Если профиль не тот, что открывался последним, укажите его после двоеточия. Имя профиля Chrome обычно `Default` или `Profile 1`. У Firefox это каталог вроде `xxxxxxxx.default-release` внутри `~/.mozilla/firefox/`:

```bash
python3 download_audio.py links.txt ./music --cookies-from-browser "chrome:Default"
python3 download_audio.py links.txt ./music --cookies-from-browser "firefox:xxxxxxxx.default-release"
```

Контейнер Firefox, если пользуетесь ими, пишется после `::`:

```bash
python3 download_audio.py links.txt ./music --cookies-from-browser "firefox::personal"
```

### Сохранить cookies в файл

Файл удобнее длинного прогона: браузер можно закрыть, а скрипт читает уже готовый файл. Формат — Netscape cookies.txt.

Способ через yt-dlp, он ставится вместе со скриптом. Сначала войдите в YouTube в браузере, затем выгрузите cookies в файл:

```bash
yt-dlp --cookies-from-browser firefox --cookies cookies.txt --skip-download "https://www.youtube.com/"
```

Для Chrome на GNOME та же связка ключей, что и выше: `--cookies-from-browser chrome+gnomekeyring`.

Либо поставьте в браузер расширение [Get cookies.txt LOCALLY](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies), откройте вкладку youtube.com под нужным аккаунтом и экспортируйте cookies в формате Netscape. Расширение, которое отправляет cookies на чужой сайт, не используйте.

Ограничьте доступ к файлу и передайте его скрипту:

```bash
chmod 600 cookies.txt
python3 download_audio.py links.txt ./music --cookies cookies.txt
```

Этот файл — действующий вход в аккаунт. Его не отправляют другим людям и не кладут в git. yt-dlp может дописать в него обновлённые cookies после запуска. Если возрастное видео снова перестаёт открываться, войдите в YouTube заново и выгрузите файл ещё раз: часть cookies живёт недолго.

Перед загрузкой скрипт проверяет, что в cookies есть записи для `youtube.com`. Если их нет, он предупредит и всё равно пойдёт по ссылкам: открытые ролики при этом скачиваются, возрастные — нет.

Подробности у yt-dlp: [как передать cookies](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp) и [как их экспортировать](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies).

## PO-токен

YouTube привязывает PO-токен к конкретному ролику. Без него страница может открыться, а сама загрузка звука получить `HTTP Error 403`, либо клиент `web_creator` (нужен возрастным роликам) отбросит свои форматы. Вручную передавать `--po-token` не нужно: токен живёт недолго и для каждого ролика свой.

Скрипт берёт токен у [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider). Пакет ставится из `requirements.txt`. Рядом нужен исходник генератора той же версии, что и пакет (сейчас 2.0.2), и Deno:

```bash
git clone --depth 1 --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$HOME/bgutil-ytdlp-pot-provider"
cd "$HOME/bgutil-ytdlp-pot-provider/server"
deno install --allow-scripts=npm:canvas --frozen
```

Скрипт ищет этот каталог в `~/bgutil-ytdlp-pot-provider/server`. В логе перед загрузкой должна быть строка `PO-токен: ...`. Дальше для каждого ролика yt-dlp пишет `Generating a gvs PO Token for mweb client via bgutil script`. Строка про `127.0.0.1:4416/ping` при этом нормальна: HTTP-сервер не запущен, токен выпускает скрипт.

Клиент `mweb` выбран специально. Клиенты по умолчанию отдают ссылку без токена, и загрузка заканчивается `HTTP Error 403`. Для возрастного ролика yt-dlp дополнительно запрашивает клиент `web_creator` и токен для него же, отдельно `--po-token` передавать не нужно. Cookies аккаунта, которому ролик уже доступен, по-прежнему нужны.

`--po-token` остаётся только если генератор недоступен и токен уже есть на руках. Как его достать вручную, описано в [PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide).
