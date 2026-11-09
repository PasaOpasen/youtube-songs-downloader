#!/usr/bin/env python3
"""Скачать звук из YouTube по файлу со ссылками.

Пример:
    python3 download_audio.py links.txt ./music

Повторно звук не качается, если в папке выхода уже есть эта песня:
файл с тем же id видео или с тем же названием. Между запросами к YouTube
выдерживается длинная пауза.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.utils import DownloadError, sanitize_filename

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
DEFAULT_PAUSE_SECONDS = 5.0
# Пауза yt-dlp между его собственными HTTP-запросами при разборе страницы.
REQUEST_SLEEP_SECONDS = 2.0
EXTRA_PAUSE_SECONDS = 4.0
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
ID_IN_NAME_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
YOUTUBE_HOSTS = {
    "youtube.com",
    "youtu.be",
    "music.youtube.com",
    "m.youtube.com",
    "youtube-nocookie.com",
}


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
    text = " ".join(str(exc).split())
    return re.sub(r"^ERROR:\s*", "", text) or exc.__class__.__name__


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


def _ydl_options(output: Path) -> dict:
    return {
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
        "format": "bestaudio/best",
        "outtmpl": "%(title)s [%(id)s].%(ext)s",
        "paths": {"home": str(output)},
        "overwrites": False,
    }


def _single_video(info: dict) -> dict:
    if info.get("_type") == "playlist":
        entries = [entry for entry in info.get("entries") or [] if entry]
        if not entries:
            raise DownloadError("По ссылке нет видео")
        info = entries[0]
    if "id" not in info:
        raise DownloadError("У видео нет id")
    return info


def process_url(url: str, output: Path, pacer: Pacer) -> str:
    video_id = video_id_from_url(url)
    existing = find_existing(output, video_id, None)
    if existing is not None:
        say(f"Уже есть, пропуск: {existing.name}")
        return "skipped"

    # Одно чтение страницы, затем загрузка уже полученных сведений —
    # без второго прохода по YouTube.
    pacer.before_request("перед запросом сведений о видео")
    with yt_dlp.YoutubeDL(_ydl_options(output)) as ydl:
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
    say(f"Ссылок: {len(links)}")
    say(f"Папка: {output}")
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
            result = process_url(url, output, pacer)
        except DownloadError as exc:
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
