#!/usr/bin/env python3
"""Скачать звук из YouTube по файлу со ссылками.

Пример:
    python3 download_audio.py links.txt ./music
    python3 download_audio.py links.txt ./music --cookies-from-browser firefox
    python3 download_audio.py links.txt ./music --cookies cookies.txt

Повторно звук не качается, если в папке выхода уже есть эта песня:
файл с тем же id видео или с тем же названием. Между запросами к YouTube
выдерживается пауза. Для роликов с возрастным ограничением нужны
cookies аккаунта, которому YouTube этот ролик уже показывает.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.cookies import SUPPORTED_BROWSERS, SUPPORTED_KEYRINGS, CookieLoadError
from yt_dlp.utils import DownloadError, YoutubeDLError, sanitize_filename

AUDIO_SUFFIXES = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".weba",
    ".webm",
}
MIN_PAUSE_SECONDS = 1.0
DEFAULT_PAUSE_SECONDS = 10.0
# Пауза yt-dlp между его собственными HTTP-запросами при разборе страницы.
REQUEST_SLEEP_SECONDS = 2.0
EXTRA_PAUSE_SECONDS = 4.0
# deno >= 2.3, node >= 22. Bun новее 1.3.14 yt-dlp уже не поддерживает.
_JS_RUNTIME_SPECS = (
    ("deno", ("deno",), (2, 3, 0), None),
    ("node", ("node", "nodejs"), (22, 0, 0), None),
    ("quickjs", ("qjs",), (), None),
    ("bun", ("bun",), (1, 2, 11), (1, 3, 14)),
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
ID_IN_NAME_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
YOUTUBE_HOSTS = {
    "youtube.com",
    "youtu.be",
    "music.youtube.com",
    "m.youtube.com",
    "youtube-nocookie.com",
}
# Тот же формат, что у yt-dlp: BROWSER[+KEYRING][:PROFILE][::CONTAINER]
_BROWSER_SPEC_RE = re.compile(
    r"""(?x)
    (?P<name>[^+:]+)
    (?:\s*\+\s*(?P<keyring>[^:]+))?
    (?:\s*:\s*(?!:)(?P<profile>.+?))?
    (?:\s*::\s*(?P<container>.+))?
    """
)
BrowserCookies = tuple[str, str | None, str | None, str | None]


def say(message: str) -> None:
    print(message, flush=True)


class _CliLogger:
    """Отдаёт ход загрузки в консоль, а текст ошибки оставляет нашему обработчику."""

    def debug(self, message: str) -> None:
        text = str(message).strip()
        if text and not text.startswith("[debug]"):
            say(text)

    def info(self, message: str) -> None:
        text = str(message).strip()
        if text:
            say(text)

    def warning(self, message: str) -> None:
        text = str(message).strip()
        if text:
            print(text, file=sys.stderr, flush=True)

    def error(self, message: str) -> None:
        text = str(message).strip()
        if text.startswith(("WARNING:", "Deprecated")):
            print(text, file=sys.stderr, flush=True)


def _plain_error(exc: BaseException) -> str:
    text = _ANSI_RE.sub("", " ".join(str(exc).split()))
    while text.startswith("ERROR:"):
        text = text.removeprefix("ERROR:").strip()
    if isinstance(exc, CookieLoadError):
        cause = exc.__cause__ or exc.__context__
        if cause is not None:
            cause_text = " ".join(str(cause).split())
            if cause_text and cause_text not in text:
                text = f"{text}: {cause_text}" if text else cause_text
    return text or exc.__class__.__name__


def parse_browser_spec(spec: str) -> BrowserCookies:
    """Разобрать --cookies-from-browser так же, как это делает yt-dlp."""
    match = _BROWSER_SPEC_RE.fullmatch(spec.strip())
    if match is None:
        raise ValueError(
            f"непонятная спецификация браузера: {spec}. "
            "Ожидается BROWSER[+KEYRING][:PROFILE][::CONTAINER], например firefox или chrome+gnomekeyring"
        )
    browser_name, keyring, profile, container = match.group("name", "keyring", "profile", "container")
    browser_name = browser_name.lower()
    if browser_name not in SUPPORTED_BROWSERS:
        names = ", ".join(sorted(SUPPORTED_BROWSERS))
        raise ValueError(f"браузер «{browser_name}» не поддерживается. Доступны: {names}")
    if keyring is not None:
        keyring = keyring.upper()
        if keyring not in SUPPORTED_KEYRINGS:
            names = ", ".join(sorted(name.lower() for name in SUPPORTED_KEYRINGS))
            raise ValueError(f"связка ключей «{keyring}» не поддерживается. Доступны: {names}")
    return browser_name, profile, keyring, container


def _parse_version(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if match is None:
        return ()
    return tuple(int(part) for part in match.groups())


def _version_ok(version: tuple[int, ...], minimum: tuple[int, ...], maximum: tuple[int, ...] | None) -> bool:
    if not version or not minimum:
        return True
    if version < minimum:
        return False
    return maximum is None or version <= maximum


def _runtime_version(path: str) -> tuple[int, ...]:
    try:
        completed = subprocess.run(
            [path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    return _parse_version(f"{completed.stdout}\n{completed.stderr}")


def _runtime_search_dirs() -> list[Path]:
    return [
        Path.home() / ".deno" / "bin",
        Path.home() / ".local" / "bin",
        Path(__file__).resolve().parent / ".tools",
    ]


def find_js_runtimes() -> dict[str, dict[str, str]]:
    """Найти движки, которыми yt-dlp решает JS-задачу YouTube."""
    found: dict[str, dict[str, str]] = {}
    extra_dirs = _runtime_search_dirs()
    for name, binaries, minimum, maximum in _JS_RUNTIME_SPECS:
        path = None
        for binary in binaries:
            located = shutil.which(binary)
            if located:
                path = located
                break
            for directory in extra_dirs:
                candidate = directory / binary
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    path = str(candidate)
                    break
            if path:
                break
        if path is None:
            continue
        if not _version_ok(_runtime_version(path), minimum, maximum):
            continue
        found[name] = {"path": path}
    return found


def ejs_package_available() -> bool:
    try:
        import yt_dlp_ejs  # noqa: F401
    except ImportError:
        return False
    return True


def _browser_spec_arg(value: str) -> BrowserCookies:
    try:
        return parse_browser_spec(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


class Pacer:
    """Держит большую паузу между обращениями к YouTube."""

    def __init__(self, pause: float) -> None:
        self.pause = pause
        self._requests_made = 0

    def before_request(self, reason: str) -> None:
        if self._requests_made > 0:
            delay = self.pause + random.uniform(0, EXTRA_PAUSE_SECONDS)
            say(f"Пауза {delay:.0f} с — {reason}")
            time.sleep(delay)
        self._requests_made += 1


def read_links(path: Path) -> list[str]:
    data = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = data.decode("utf-8", errors="replace")

    links: list[str] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not (line.startswith("https://") or line.startswith("http://")):
            print(f"Строка {lineno}: пропуск, это не ссылка: {line}", file=sys.stderr)
            continue
        if line in seen:
            continue
        seen.add(line)
        links.append(line)
    return links


def video_id_from_url(url: str) -> str | None:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    host = parsed.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host not in YOUTUBE_HOSTS and not host.endswith(".youtube.com"):
        return None

    candidates: list[str] = []
    if host == "youtu.be":
        head = parsed.path.strip("/").split("/")[0]
        if head:
            candidates.append(head)
    candidates.extend(parse_qs(parsed.query).get("v", []))
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v", "e"}:
        candidates.append(parts[1])

    for candidate in candidates:
        if VIDEO_ID_RE.fullmatch(candidate):
            return candidate
    return None


def _audio_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
    )


def find_existing(folder: Path, video_id: str | None, title: str | None) -> Path | None:
    """Найти уже сохранённый звук этого видео или песни с тем же названием."""
    safe_title = ""
    if title:
        safe_title = sanitize_filename(title, restricted=False).strip()

    for path in _audio_files(folder):
        stem = path.stem
        found_ids = ID_IN_NAME_RE.findall(stem)
        if video_id and (video_id in found_ids or stem == video_id):
            return path
        if not safe_title:
            continue
        bare = ID_IN_NAME_RE.sub("", stem).strip()
        same_title = bare.casefold() == safe_title.casefold() or stem.casefold() == safe_title.casefold()
        if not same_title:
            continue
        # Файл с чужим id — другая запись, даже если заголовок совпал.
        if found_ids and video_id not in found_ids:
            continue
        return path
    return None


def pot_server_home() -> Path | None:
    """Каталог server из bgutil, который выпускает PO-токен на каждый ролик."""
    candidates = (
        Path.home() / "bgutil-ytdlp-pot-provider" / "server",
        Path.home() / ".local" / "share" / "bgutil-ytdlp-pot-provider" / "server",
        Path(__file__).resolve().parent / "bgutil-ytdlp-pot-provider" / "server",
    )
    for path in candidates:
        if (path / "src" / "generate_once.ts").is_file():
            return path
    return None


def _po_token_arg(value: str) -> str:
    token = value.strip()
    if re.fullmatch(r"[^+\s]+\+.+", token) is None:
        raise argparse.ArgumentTypeError(
            "ожидается CLIENT.CONTEXT+ТОКЕН, например web_creator.gvs+...."
        )
    return token


def _ydl_options(
    output: Path,
    *,
    cookiefile: str | None = None,
    cookiesfrombrowser: BrowserCookies | None = None,
    js_runtimes: dict[str, dict[str, str]] | None = None,
    po_tokens: list[str] | None = None,
    remote_components: list[str] | None = None,
) -> dict:
    options: dict = {
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "sleep_interval_requests": REQUEST_SLEEP_SECONDS,
        "sleep_interval": 0,
        "max_sleep_interval": 0,
        "quiet": False,
        "no_warnings": False,
        "noprogress": False,
        "windowsfilenames": True,
        "logger": _CliLogger(),
        # Отдельная звуковая дорожка (DASH) обрывается 403 примерно после 1 МБ,
        # даже с PO-токеном. Прогрессивный mp4 с картинкой и звуком качается целиком,
        # затем ffmpeg копирует из него звук в m4a без перекодирования.
        "format": "best[acodec!=none][vcodec!=none][protocol^=http]/18/best",
        "http_chunk_size": 1024 * 1024,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "best"}],
        "outtmpl": "%(title)s [%(id)s].%(ext)s",
        "paths": {"home": str(output)},
        "overwrites": False,
    }
    if cookiefile:
        options["cookiefile"] = cookiefile
    if cookiesfrombrowser:
        options["cookiesfrombrowser"] = cookiesfrombrowser
    if js_runtimes:
        options["js_runtimes"] = js_runtimes
    if remote_components:
        options["remote_components"] = remote_components
    extractor_args: dict = {}
    youtube_args: dict[str, list[str]] = {}
    if server := pot_server_home():
        extractor_args["youtubepot-bgutilscript"] = {"server_home": [str(server)]}
        # У клиентов по умолчанию ссылка на звук приходит без PO-токена и YouTube отвечает 403.
        # mweb этот токен запрашивает, а для возрастных роликов yt-dlp сам добавит web_creator.
        youtube_args["player_client"] = ["mweb"]
    if po_tokens:
        youtube_args["po_token"] = list(po_tokens)
    if youtube_args:
        extractor_args["youtube"] = youtube_args
    if extractor_args:
        options["extractor_args"] = extractor_args
    return options


def _youtube_cookie_count(output: Path, cookiefile: str | None, cookiesfrombrowser: BrowserCookies | None) -> int:
    with yt_dlp.YoutubeDL(
        _ydl_options(output, cookiefile=cookiefile, cookiesfrombrowser=cookiesfrombrowser)
    ) as ydl:
        jar = ydl.cookiejar
        return sum(1 for cookie in jar if "youtube.com" in cookie.domain)


def _single_video(info: dict) -> dict:
    if info.get("_type") == "playlist":
        entries = [entry for entry in info.get("entries") or [] if entry]
        if not entries:
            raise DownloadError("По ссылке нет видео")
        info = entries[0]
    if "id" not in info:
        raise DownloadError("У видео нет id")
    return info


def process_url(
    url: str,
    output: Path,
    pacer: Pacer,
    *,
    cookiefile: str | None = None,
    cookiesfrombrowser: BrowserCookies | None = None,
    js_runtimes: dict[str, dict[str, str]] | None = None,
    po_tokens: list[str] | None = None,
    remote_components: list[str] | None = None,
) -> str:
    video_id = video_id_from_url(url)
    existing = find_existing(output, video_id, None)
    if existing is not None:
        say(f"Уже есть, пропуск: {existing.name}")
        return "skipped"

    # Одно чтение страницы, затем загрузка уже полученных сведений —
    # без второго прохода по YouTube.
    pacer.before_request("перед запросом сведений о видео")
    with yt_dlp.YoutubeDL(
        _ydl_options(
            output,
            cookiefile=cookiefile,
            cookiesfrombrowser=cookiesfrombrowser,
            js_runtimes=js_runtimes,
            po_tokens=po_tokens,
            remote_components=remote_components,
        )
    ) as ydl:
        info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            raise DownloadError("YouTube не вернул сведения о видео")
        info = _single_video(info)
        video_id = str(info["id"])
        title = str(info.get("title") or video_id)
        say(f"Песня: {title}")

        existing = find_existing(output, video_id, title)
        if existing is not None:
            say(f"Уже есть, пропуск: {existing.name}")
            return "skipped"

        pacer.before_request("перед загрузкой звука")
        ydl.process_info(info)

    saved = find_existing(output, video_id, title)
    if saved is None:
        raise DownloadError("Загрузка закончилась, но файл песни в папке не появился")
    say(f"Сохранено: {saved.name}")
    return "downloaded"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Скачать звук из YouTube-видео по списку ссылок. "
            "Песня, которая уже лежит в папке выхода, пропускается."
        )
    )
    parser.add_argument("links", type=Path, help="текстовый файл со ссылками, по одной на строку")
    parser.add_argument("output", type=Path, help="папка, куда сохранять звук")
    parser.add_argument(
        "--pause",
        type=float,
        default=DEFAULT_PAUSE_SECONDS,
        help=(
            "пауза между запросами к YouTube в секундах "
            f"(по умолчанию {DEFAULT_PAUSE_SECONDS:.0f}, не меньше {MIN_PAUSE_SECONDS:.0f}; "
            f"сверху добавляется до {EXTRA_PAUSE_SECONDS:.0f} с). "
            f"Пока yt-dlp читает страницу, между его запросами ещё {REQUEST_SLEEP_SECONDS:.0f} с"
        ),
    )
    browsers = ", ".join(sorted(SUPPORTED_BROWSERS))
    parser.add_argument(
        "--cookies",
        type=Path,
        help=(
            "файл cookies в формате Netscape. Нужен для роликов с возрастным ограничением: "
            "в файле должна быть сессия YouTube, которой этот ролик можно смотреть"
        ),
    )
    parser.add_argument(
        "--po-token",
        action="append",
        default=[],
        type=_po_token_arg,
        metavar="CLIENT.CONTEXT+TOKEN",
        help=(
            "PO-токен YouTube, если форматы клиента отброшены без него. "
            "Пример: web_creator.gvs+XXX. Можно указать несколько раз"
        ),
    )
    parser.add_argument(
        "--cookies-from-browser",
        type=_browser_spec_arg,
        metavar="BROWSER[+KEYRING][:PROFILE][::CONTAINER]",
        help=(
            "взять cookies из браузера, где вы уже вошли в YouTube. "
            f"Браузеры: {browsers}. Пример: firefox или chrome+gnomekeyring. "
            "Можно указать вместе с --cookies: yt-dlp объединит оба источника"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    links_path: Path = args.links
    output: Path = args.output

    if not links_path.is_file():
        print(f"Файл со ссылками не найден: {links_path}", file=sys.stderr)
        return 1
    if output.exists() and not output.is_dir():
        print(f"Это не папка: {output}", file=sys.stderr)
        return 1

    pause = args.pause
    if pause < MIN_PAUSE_SECONDS:
        print(
            f"Пауза {pause:g} с слишком мала, использую {MIN_PAUSE_SECONDS:.0f} с.",
            file=sys.stderr,
        )
        pause = MIN_PAUSE_SECONDS

    try:
        links = read_links(links_path)
    except OSError as exc:
        print(f"Не удалось прочитать файл: {exc}", file=sys.stderr)
        return 1
    if not links:
        print("В файле нет ссылок.", file=sys.stderr)
        return 1

    output.mkdir(parents=True, exist_ok=True)

    js_runtimes = find_js_runtimes()
    if not js_runtimes:
        print(
            "Не найден JavaScript-движок. Без него YouTube не отдаёт звук, только картинки.\n"
            "Поставьте Deno 2.3+ (предпочтительно) или Node.js 22+ и повторите запуск.\n"
            "Deno: https://docs.deno.com/runtime/getting_started/installation/\n"
            "Node.js: https://nodejs.org",
            file=sys.stderr,
        )
        return 1
    remote_components = None if ejs_package_available() else ["ejs:github"]

    cookiefile: str | None = None
    if args.cookies is not None:
        cookie_path = args.cookies.expanduser()
        if not cookie_path.is_file():
            print(f"Файл cookies не найден: {cookie_path}", file=sys.stderr)
            return 1
        cookiefile = str(cookie_path)
    cookiesfrombrowser: BrowserCookies | None = args.cookies_from_browser

    say(f"Ссылок: {len(links)}")
    say(f"Папка: {output}")
    say("JavaScript: " + ", ".join(f"{name} ({cfg['path']})" for name, cfg in js_runtimes.items()))
    if server := pot_server_home():
        say(f"PO-токен: {server}")
    else:
        print(
            "Генератор PO-токенов не найден. YouTube часто отвечает HTTP 403 без токена на каждый ролик.\n"
            "Нужны пакет bgutil-ytdlp-pot-provider и каталог ~/bgutil-ytdlp-pot-provider "
            "(ветка той же версии, что у пакета), затем в его server/: "
            "deno install --allow-scripts=npm:canvas --frozen",
            file=sys.stderr,
        )
    if remote_components:
        print(
            "Пакет yt-dlp-ejs не найден. Скрипты решателя будут скачаны с GitHub. "
            'Надёжнее поставить зависимости заново: pip install -U -r requirements.txt',
            file=sys.stderr,
        )
    if cookiefile or cookiesfrombrowser:
        if cookiesfrombrowser:
            browser_name, profile, keyring, container = cookiesfrombrowser
            details = browser_name
            if profile:
                details += f", профиль {profile}"
            if keyring:
                details += f", связка ключей {keyring.lower()}"
            if container:
                details += f", контейнер {container}"
            say(f"Cookies: браузер {details}")
        if cookiefile:
            say(f"Cookies: файл {cookiefile}")
        try:
            youtube_cookies = _youtube_cookie_count(output, cookiefile, cookiesfrombrowser)
        except YoutubeDLError as exc:
            print(f"Не удалось прочитать cookies: {_plain_error(exc)}", file=sys.stderr)
            return 1
        if youtube_cookies == 0:
            print(
                "В переданных cookies нет записей YouTube. "
                "Ролики с возрастным ограничением, скорее всего, не откроются.",
                file=sys.stderr,
            )
        else:
            say(f"Записей cookies для YouTube: {youtube_cookies}")
    say(
        f"Пауза между запросами: {pause:.0f}–{pause + EXTRA_PAUSE_SECONDS:.0f} с, "
        f"внутри разбора страницы ещё по {REQUEST_SLEEP_SECONDS:.0f} с"
    )

    pacer = Pacer(pause)
    downloaded = 0
    skipped = 0
    failed = 0

    for index, url in enumerate(links, 1):
        say(f"\n[{index}/{len(links)}] {url}")
        try:
            result = process_url(
                url,
                output,
                pacer,
                cookiefile=cookiefile,
                cookiesfrombrowser=cookiesfrombrowser,
                js_runtimes=js_runtimes,
                po_tokens=args.po_token,
                remote_components=remote_components,
            )
        except YoutubeDLError as exc:
            failed += 1
            print(f"Ошибка: {_plain_error(exc)}", file=sys.stderr)
            continue
        except Exception as exc:  # noqa: BLE001 — одна битая ссылка не должна останавливать остальные
            failed += 1
            print(f"Ошибка: {_plain_error(exc)}", file=sys.stderr)
            continue
        if result == "downloaded":
            downloaded += 1
        else:
            skipped += 1

    say(f"\nГотово. Скачано: {downloaded}. Пропущено: {skipped}. Ошибок: {failed}.")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nОстановлено.", file=sys.stderr)
        raise SystemExit(130)
