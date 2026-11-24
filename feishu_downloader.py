import configparser
import hashlib
import json
import locale
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import requests
from tqdm import tqdm

locale.setlocale(locale.LC_CTYPE, "chinese")

config = configparser.ConfigParser(interpolation=None)
config.read("config.ini", encoding="utf-8")
minutes_cookie = config.get("Cookies", "cookie")
space_name = config.getint("下载设置", "所在空间")
auto_delete = config.get("下载设置", "是否自动删除云端妙记（是/否）", fallback="是") == "是"
vc_max_num = config.getint("下载设置", "保留云端妙记的最大数量", fallback=100) if auto_delete else 100
if vc_max_num < 1:
    raise ValueError("开启自动删除时，保留云端妙记的最大数量必须为正整数。")
check_interval = config.getint("下载设置", "检查妙记的时间间隔（单位s，太短容易报错）")
download_type = config.getint("下载设置", "文件类型")
subtitle_only = config.get("下载设置", "是否只下载字幕文件（是/否）") == "是"
save_path = config.get("下载设置", "保存路径（不填则默认为当前路径/data）") or "./data"
subtitle_params = {
    "add_speaker": config.get("下载设置", "字幕是否包含说话人（是/否）") == "是",
    "add_timestamp": config.get("下载设置", "字幕是否包含时间戳（是/否）") == "是",
    "format": 3 if config.get("下载设置", "字幕格式（srt/txt）") == "srt" else 2,
}
use_proxy = config.get("代理设置", "是否使用代理（是/否）")
proxy_address = config.get("代理设置", "代理地址")
proxies = {"http": proxy_address, "https": proxy_address} if use_proxy == "是" else None

API = "https://meetings.feishu.cn/minutes/api"
HISTORY = Path("minutes.txt")
BACKUP_RECORD = ".backup.json"
INVALID_FILENAME = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
SRT_CUE = re.compile(r"(?m)^\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}")
ITEM_ERRORS = (OSError, ValueError, TypeError, KeyError, RuntimeError, requests.RequestException)


@dataclass
class Download:
    token: str
    name: str
    subtitle: bytes
    media_url: Optional[str]
    meeting_start: Optional[float]


def media_extension(path):
    # Inspect the container even when the download URL or Content-Type is generic.
    with path.open("rb") as file:
        header = file.read(4096)
    if header[4:8] == b"ftyp":
        return {b"M4A ": ".m4a", b"M4B ": ".m4b", b"qt  ": ".mov"}.get(header[8:12], ".mp4")
    if header.startswith(b"ID3"):
        return ".mp3"
    if len(header) >= 2 and header[0] == 0xff and header[1] & 0xe0 == 0xe0:
        return ".mp3" if header[1] & 0x06 else ".aac"
    if header.startswith(b"RIFF"):
        riff_types = {b"WAVE": ".wav", b"AVI ": ".avi"}
        if header[8:12] in riff_types:
            return riff_types[header[8:12]]
    for signature, suffix in (
        (b"fLaC", ".flac"), (b"OggS", ".ogg"), (b"FLV", ".flv"),
        (b"\x30\x26\xb2\x75\x8e\x66\xcf\x11", ".asf"), (b"\x00\x00\x01\xba", ".mpg"),
    ):
        if header.startswith(signature):
            return suffix
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm" if b"webm" in header else ".mkv"
    raise ValueError("无法识别下载文件的音视频格式")


def is_json_document(text):
    if not text.lstrip().startswith(("{", "[")):
        return False
    try:
        return isinstance(json.loads(text), (dict, list))
    except ValueError:
        return False


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create_file.restype = wintypes.HANDLE
    set_file_time = kernel32.SetFileTime
    set_file_time.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 3
    set_file_time.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL


def set_meeting_time(path, timestamp):
    os.utime(path, (timestamp, timestamp))
    if os.name == "nt":
        write_attributes, share_all, open_existing, backup_semantics = 0x100, 7, 3, 0x02000000
        handle = create_file(str(path), write_attributes, share_all, None, open_existing, backup_semantics, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ticks = round(timestamp * 10_000_000) + 116444736000000000
            creation_time = wintypes.FILETIME(ticks & 0xffffffff, ticks >> 32)
            if not set_file_time(handle, ctypes.byref(creation_time), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            close_handle(handle)


class FeishuDownloader:
    def __init__(self, cookie):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36",
            "cookie": cookie,
            "bv-csrf-token": cookie[cookie.find("bv_csrf_token=") + len("bv_csrf_token="):cookie.find(";", cookie.find("bv_csrf_token="))],
            "referer": "https://meetings.feishu.cn/minutes/me",
            "content-type": "application/x-www-form-urlencoded",
        }
        if len(self.headers["bv-csrf-token"]) != 36:
            raise Exception("minutes_cookie中不包含bv_csrf_token，请从妙记列表请求中获取！")
        self.all_minutes = []
        self.minutes_num = 0
        self.list_complete = False
        self.root = Path(save_path).resolve()
        self.subtitle_type = "srt" if subtitle_params["format"] == 3 else "txt"

    def get_minutes(self):
        base_url = f"{API}/space/list?size=20&space_name={space_name}"
        items = {}
        cursor = None
        seen_cursors = set()
        self.list_complete = False
        while True:
            url = f"{base_url}&timestamp={cursor}" if cursor is not None else base_url
            response = requests.get(url=url, headers=self.headers, proxies=proxies)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code", 0) != 0:
                raise ValueError("飞书未返回有效的妙记列表")
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise ValueError("妙记列表响应缺少有效的 list")
            page = data["list"]
            for item in page:
                items.setdefault(item["object_token"], item)
            if not data.get("has_more", bool(page)):
                self.list_complete = True
                break
            next_cursor = page[-1].get("share_time") if page else None
            if next_cursor and str(next_cursor) not in seen_cursors:
                cursor = str(next_cursor)
            else:
                # Current list responses can also supply the continuation cursor directly.
                cursor = str(data["timestamp"]) if data.get("timestamp") else None
            if cursor is None or cursor in seen_cursors:
                print("妙记分页游标缺失或重复，本轮只处理已获取的记录。")
                break
            seen_cursors.add(cursor)
        self.all_minutes = list(reversed(items.values()))
        self.minutes_num = len(items)

    def matches_type(self, item):
        return download_type == 2 or item.get("object_type") == download_type

    def check_minutes(self):
        downloaded = set(HISTORY.read_text(encoding="utf-8").splitlines()) if HISTORY.exists() else set()
        self.get_minutes()
        print(f"云端已获取 {self.minutes_num} 个妙记")
        pending = [item for item in self.all_minutes
                   if item["object_token"] not in downloaded and self.matches_type(item)]
        print(f"需要下载 {len(pending)} 个妙记")
        if pending:
            completed = self.download_minutes(pending)
            if completed:
                with HISTORY.open("ab", buffering=0) as history:
                    history.write("".join(f"{token}\n" for token in completed).encode("utf-8"))
            print(f"成功下载 {len(completed)}/{len(pending)} 个妙记")

    def archive_name(self, item):
        if item["object_type"] == 0:
            start, stop = (time.strftime("%Y年%m月%d日%H时%M分", time.localtime(item[key] / 1000))
                           for key in ("start_time", "stop_time"))
            prefix = f"{start}至{stop}"
        else:
            prefix = time.strftime("%Y年%m月%d日%H时%M分", time.localtime(item["create_time"] / 1000))
        title = INVALID_FILENAME.sub("_", item["topic"]).strip(" .") or "未命名"
        suffix = "_" + hashlib.blake2s(item["object_token"].encode("utf-8"), digest_size=8).hexdigest()
        name = prefix + title
        if os.name == "nt":
            root_units = len(str(self.root).encode("utf-16-le")) // 2
            # The name is used both as a directory and as the file stem.
            budget = (250 - root_units - 8) // 2 - len(suffix)
            if budget < 1:
                raise ValueError("保存路径过长，请缩短保存目录")
            name = name.encode("utf-16-le")[:budget * 2].decode("utf-16-le", errors="ignore")
        name = name.encode("utf-8")[:240 - len(suffix)].decode("utf-8", errors="ignore")
        return name.rstrip(" .") + suffix

    def prepare_download(self, item):
        token = item["object_token"]
        name = self.archive_name(item)
        media_url = None
        if not subtitle_only:
            stamp = int(time.time() * 1000)
            url = f"{API}/status?object_token={token}&language=zh_cn&_t={stamp}"
            response = requests.get(url=url, headers=self.headers, proxies=proxies)
            response.raise_for_status()
            media_url = response.json()["data"]["video_info"]["video_download_url"]
            if (not isinstance(media_url, str) or urlsplit(media_url).scheme not in ("http", "https")
                    or not urlsplit(media_url).hostname or re.search(r"[\x00-\x20\x7f]", media_url)):
                raise ValueError("妙记没有可用的媒体下载链接")
        response = requests.post(
            url=f"{API}/export", params={**subtitle_params, "object_token": token},
            headers=self.headers, proxies=proxies,
        )
        response.raise_for_status()
        subtitle = response.content.decode("utf-8-sig")
        content_type = response.headers.get("content-type", "").lower()
        if ("json" in content_type or "html" in content_type or not subtitle.strip()
                or re.match(r"\s*(?:<!doctype html|<html\b)", subtitle, re.IGNORECASE)
                or is_json_document(subtitle)
                or (self.subtitle_type == "srt" and not SRT_CUE.search(subtitle))):
            raise ValueError("飞书未返回有效的字幕")
        start = item["start_time"] / 1000 if item["object_type"] == 0 else None
        return Download(token, name, response.content, media_url, start)

    def fetch_media(self, jobs, directory):
        bundled = Path(__file__).with_name("aria2c.exe")
        executable = str(bundled) if os.name == "nt" and bundled.is_file() else "aria2c"
        recovery = directory / "unfinished"
        command = [
            executable, "--no-conf=true", "--input-file=-", f"--dir={directory}", f"--save-session={recovery}",
            f"--max-download-result={max(1000, len(jobs))}", "--save-not-found=true", "--continue=false",
            "--auto-file-renaming=false", "--follow-metalink=false", "--follow-torrent=false",
            "--console-log-level=error", "--summary-interval=0", "-s16", "-x16", "-k1M",
            f"--user-agent={self.headers['User-Agent']}", f"--referer={self.headers['referer']}",
        ]
        if proxies:
            command.append(f"--all-proxy={proxies['http']}")
        hosts = {urlsplit(url).hostname for url in jobs.values() if urlsplit(url).scheme == "https"}
        feishu_hosts = {host for host in hosts if host == "feishu.cn" or host.endswith(".feishu.cn")}
        if feishu_hosts:
            # Domain-scoped cookies keep credentials off the command line and out of redirects to other hosts.
            cookies = directory / "cookies.txt"
            pairs = [pair.strip().partition("=") for pair in self.headers["cookie"].split(";")]
            entries = [f"{host}\tFALSE\t/\tTRUE\t0\t{key}\t{value}"
                       for host in feishu_hosts for key, separator, value in pairs if key and separator]
            cookies.write_text("# Netscape HTTP Cookie File\n" + "\n".join(entries) + "\n", encoding="utf-8")
            command.append(f"--load-cookies={cookies}")
        requests_file = "".join(f"{url}\n  out={name}\n" for name, url in jobs.items()).encode("utf-8")
        result = subprocess.run(command, input=requests_file, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        failed = set()
        if result.returncode:
            if recovery.exists():
                failed = {line.strip()[4:] for line in recovery.read_text(encoding="utf-8").splitlines()
                          if line.strip().startswith("out=")}
            print(f"aria2 返回退出码 {result.returncode}，只保留确认完整的下载。")
            if not failed or not failed.issubset(jobs):
                return set()
        return {name for name in jobs if name not in failed and (directory / name).is_file()
                and not (directory / f"{name}.aria2").exists()}

    def save_download(self, item, directory, key):
        target = self.root / item.name
        subtitle_path = target / f"{item.name}.{self.subtitle_type}"
        media_path = None
        if item.media_url:
            staged_media = directory / f"{key}.part"
            media_path = target / (item.name + media_extension(staged_media))
        staged_subtitle = directory / f"{key}.subtitle"
        staged_subtitle.write_bytes(item.subtitle)
        target.mkdir(exist_ok=True)
        if media_path:
            staged_media.replace(media_path)
        staged_subtitle.replace(subtitle_path)
        files = [subtitle_path, media_path] if media_path else [subtitle_path]
        if item.meeting_start is not None:
            for path in [*files, target]:
                set_meeting_time(path, item.meeting_start)
        if media_path:
            records = {}
            for path in files:
                metadata = path.stat()
                records[path.name] = [metadata.st_size, metadata.st_mtime_ns]
            record = directory / f"{key}.json"
            record.write_text(json.dumps({"token": item.token, "files": records}), encoding="utf-8")
            record.replace(target / BACKUP_RECORD)

    def download_minutes(self, items):
        self.root.mkdir(parents=True, exist_ok=True)
        prepared = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(self.prepare_download, item): item for item in items}
            for future in as_completed(futures):
                try:
                    prepared.append(future.result())
                except ITEM_ERRORS as error:
                    print(f"跳过妙记 {futures[future].get('topic', '')}（{type(error).__name__}）")
        if not prepared:
            return []
        completed = []
        with tempfile.TemporaryDirectory(prefix=".minutes-", dir=self.root) as work:
            directory = Path(work)
            jobs = {f"{key}.part": item.media_url for key, item in enumerate(prepared) if item.media_url}
            try:
                downloaded = self.fetch_media(jobs, directory) if jobs else set()
            except ITEM_ERRORS as error:
                print(f"无法完成媒体批量下载（{type(error).__name__}）")
                downloaded = set()
            for key, item in enumerate(prepared):
                if item.media_url and f"{key}.part" not in downloaded:
                    print(f"媒体下载失败 {item.name}")
                    continue
                try:
                    self.save_download(item, directory, key)
                except ITEM_ERRORS as error:
                    print(f"保存妙记失败 {item.name}（{type(error).__name__}）")
                    continue
                completed.append(item.token)
        return completed

    def has_full_backup(self, item):
        try:
            target = self.root / self.archive_name(item)
            record = json.loads((target / BACKUP_RECORD).read_text(encoding="utf-8"))
            files = record["files"]
            if record["token"] != item["object_token"] or not isinstance(files, dict) or len(files) != 2:
                return False
            for name, expected in files.items():
                if Path(name).name != name or not (target / name).is_file():
                    return False
                stat = (target / name).stat()
                if stat.st_size <= 0 or expected != [stat.st_size, stat.st_mtime_ns]:
                    return False
            return True
        except ITEM_ERRORS:
            return False

    def request_delete(self, token, destroy):
        response = requests.post(
            url=f"{API}/space/delete", params={
                "object_tokens": token, "is_destroyed": "true" if destroy else "false", "language": "zh_cn",
            }, headers=self.headers, proxies=proxies,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise ValueError("飞书拒绝删除妙记")

    def delete_minutes(self, num):
        if not auto_delete or not self.list_complete or self.minutes_num <= vc_max_num:
            return
        limit = min(num, self.minutes_num - vc_max_num)
        moved = destroyed = 0
        candidates = [item for item in self.all_minutes if self.matches_type(item)]
        for item in tqdm(candidates, desc="删除妙记"):
            if moved >= limit or self.minutes_num <= vc_max_num:
                break
            if not self.has_full_backup(item):
                continue
            token = item["object_token"]
            if not any(entry["object_token"] == token for entry in self.all_minutes):
                continue
            trash_accepted = False
            try:
                self.request_delete(token, destroy=False)
                trash_accepted = True
            except ITEM_ERRORS as error:
                print(f"移入回收站失败 {token}（{type(error).__name__}）")
            if trash_accepted:
                moved += 1
            try:
                self.get_minutes()
            except ITEM_ERRORS:
                print("无法核对云端列表，停止本轮删除。")
                break
            if not self.list_complete:
                print("云端列表不完整，停止本轮删除。")
                break
            target_visible = any(entry["object_token"] == token for entry in self.all_minutes)
            if not trash_accepted:
                # A failed request can still have changed the cloud list. Count that item against the limit.
                if not target_visible:
                    moved += 1
                continue
            if target_visible:
                print(f"尚未确认妙记 {token} 已移入回收站，跳过永久删除。")
                continue
            try:
                self.request_delete(token, destroy=True)
            except ITEM_ERRORS as error:
                print(f"永久删除失败 {token}（{type(error).__name__}）")
                continue
            destroyed += 1
        print(f"请求移入回收站 {moved} 条，确认永久删除 {destroyed} 条")


if __name__ == "__main__":
    if not minutes_cookie:
        raise Exception("cookie不能为空！")
    while True:
        print(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
        downloader = FeishuDownloader(minutes_cookie)
        downloader.check_minutes()
        if auto_delete and downloader.minutes_num > vc_max_num:
            downloader.delete_minutes(downloader.minutes_num - vc_max_num)
        time.sleep(check_interval)
