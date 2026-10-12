"""Bounded, optional local style references; raw chat files never enter snapshots.

Filtering is deliberately conservative, but is not an anonymization guarantee.
Only manually reviewed, cleaned files should be put into this local directory.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import unicodedata


MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 64
MAX_DIRECTORY_ENTRIES = 4096
MAX_DEPTH = 3
MAX_PER_FILE = 1500
MAX_INDEXED = 12000
MAX_QUERY_TERMS = 64
MAX_POSTINGS_PER_TERM = 128
MAX_SELECTION_SCAN = 768

_PRIVATE = re.compile(
    r"https?://|www\.|[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"
    r"|(?<!\d)\d{7,}(?!\d)|(?:uid|uname|user[_ ]?id|用户名|用户号|微信|微 信|手机号|身份证|qq号)\s*[:：=]"
    r"|(?:加我|联系我|私信我|我的微信|我的QQ)|@[^\s，。！？!?]{2,}", re.I)
_INJECTION = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)|system\s*(?:prompt|message)|developer\s*message"
    r"|(?:忽略|无视|覆盖|忘记).{0,12}(?:指令|提示|规则)|(?:系统|开发者)提示词|执行.{0,8}(?:命令|代码)"
    r"|(?:你现在|从现在起).{0,12}(?:扮演|必须|改为)|(?:输出|泄露).{0,10}(?:密钥|密码|token|cookie)"
    r"|<\|(?:im_start|im_end|system|assistant)|\[/?(?:INST|SYSTEM)\]", re.I)
_ABUSE = re.compile(r"傻[逼比]|煞笔|脑残|智障|废物|去死|滚出去|你妈|你爹|全家死|nmsl|(?<![a-z])(?:sb|cnm)(?![a-z])", re.I)
_REPEAT = re.compile(r"(.)\1{7,}|(.{2,6})\2{4,}")
_EMOTE = re.compile(r"\[[^\]]*\]")
_GENERIC = re.compile(
    r"(?:哈{1,6}|呵{1,3}|嘿{1,3}|哇{1,3}|啊{1,3}|哦{1,3}|耶{1,3}|好耶|好好好|好家伙|笑死(?:我了)?|绷不住(?:了)?"
    r"|来了来了|可以可以|有点东西|这也行|真的假的|太强了|厉害了|啊这|不是吧|好听|好看|爱了|支持|加油)+")
_STOP_TERMS = frozenset({
    "这个", "那个", "一下", "一个", "什么", "怎么", "现在", "刚才", "感觉", "真的", "就是", "不是", "还是",
    "但是", "然后", "已经", "大家", "主播", "直播", "画面", "屏幕", "内容", "人物", "声音", "正在", "当前",
    "可以", "没有", "出现", "有点", "哈哈", "好看", "好听", "开始", "弹幕", "观众", "文本", "the", "and", "with",
})


@dataclass(frozen=True)
class _Sample:
    text: str
    paid: bool
    style: int
    terms: frozenset[str]
    generic: bool


@dataclass(frozen=True)
class _Index:
    samples: tuple[_Sample, ...]
    postings: dict[str, tuple[int, ...]]
    generic: tuple[int, ...]


def _terms(text: str) -> frozenset[str]:
    result = set()
    for part in re.findall(r"[\u3400-\u9fff]+|[a-zA-Z][a-zA-Z0-9_]{1,23}", text.lower()):
        if part.isascii():
            result.add(part)
        else:
            result.update(part[i:i + 2] for i in range(len(part) - 1))
    return frozenset(result - _STOP_TERMS)


def _style(text: str) -> int:
    # This lexical estimate ranks references; it does not override the slider.
    if re.search(r"急了|典中典|就这|不会吧|赢麻|这下|你管这叫", text):
        return 90
    if re.search(r"离谱|难绷|绷不住|抽象|逆天|蚌埠住|还能这样", text):
        return 70
    if re.search(r"[?？]|真的假的|笑死|救命|好家伙", text):
        return 55
    if re.search(r"哈哈|嘿嘿|有点东西|来了来了", text):
        return 40
    if re.search(r"加油|支持|谢谢|感谢|好听|好看|好耶|厉害|开心", text):
        return 10
    return 30


def _clean(value: str, paid: bool) -> _Sample | None:
    # Reject controls rather than concealing a dangerous or identifying fragment.
    if any(unicodedata.category(char).startswith("C") for char in value):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    if not text or len(text) > (240 if paid else 96):
        return None
    if _PRIVATE.search(text) or _INJECTION.search(text) or _ABUSE.search(text) or _REPEAT.search(text) or _EMOTE.search(text):
        return None
    # SC can supply a longer expression, but never adds more than 48 characters.
    if len(text) > 48:
        if not paid:
            return None
        parts = re.split(r"[。！？!?；;]", text)
        text = next((part.strip() for part in parts if 8 <= len(part.strip()) <= 48), "")
        if not text:
            return None
    compact = re.sub(r"[\s，。！？!?、~～….,:：;；]", "", text)
    return _Sample(text, paid, _style(text), _terms(text), bool(_GENERIC.fullmatch(compact)))


def _records(value):
    if isinstance(value, dict):
        value = value.get("messages", value.get("danmakus", []))
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, str):
            yield item, False
        elif isinstance(item, dict):
            text = item.get("message", item.get("text"))
            if not isinstance(text, str):
                continue
            kind = str(item.get("type", "")).strip().lower().replace("_", "").replace("-", "")
            paid = kind in {"superchat", "sc", "paid"} or item.get("paid") is True
            yield text, paid


def _sample_file(value) -> tuple[_Sample, ...]:
    # Deterministic priority sampling is spread across the entire file, not its head.
    # Retain a small dedicated pool for explicitly marked paid messages.
    pools = ([], [])
    seen = set()
    for text, paid in _records(value):
        sample = _clean(text, paid)
        if sample is None or sample.text in seen:
            continue
        seen.add(sample.text)
        rank = int.from_bytes(hashlib.blake2b(sample.text.encode("utf-8"), digest_size=8).digest(), "big")
        pool = pools[int(sample.paid)]
        capacity = 50 if sample.paid else MAX_PER_FILE
        item = (-rank, sample.text, sample)
        if len(pool) < capacity:
            heapq.heappush(pool, item)
        elif item > pool[0]:
            heapq.heapreplace(pool, item)
    paid_samples = [item[2] for item in sorted(pools[1], reverse=True)]
    ordinary = [item[2] for item in sorted(pools[0], reverse=True)]
    return tuple(paid_samples + ordinary[:MAX_PER_FILE - len(paid_samples)])


class DanmakuCorpus:
    """Stat-cached index. Call refresh in a worker; select never reads the disk."""

    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._cache = {}
        self._index = _Index((), {}, ())
        self._fingerprint = None
        self._manifest = None
        self._cursor = 0
        self._recent = ()
        self._state = {"status": "not_loaded", "file_count": 0, "indexed_count": 0,
                       "skipped_files": 0, "updated_at": None, "revision": 0}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _discover(self):
        files, skipped = [], 0
        budget = MAX_DIRECTORY_ENTRIES
        stack = [(self.root, 0)]
        while stack and budget > 0:
            directory, depth = stack.pop()
            try:
                with os.scandir(directory) as items:
                    for item in items:
                        budget -= 1
                        if budget < 0:
                            break
                        try:
                            if item.is_symlink():
                                skipped += int(item.name.lower().endswith(".json"))
                            elif item.is_dir(follow_symlinks=False) and depth < MAX_DEPTH:
                                stack.append((Path(item.path), depth + 1))
                            elif item.name.lower().endswith(".json") and item.is_file(follow_symlinks=False):
                                info = item.stat(follow_symlinks=False)
                                files.append((Path(item.path).relative_to(self.root), info))
                        except OSError:
                            skipped += int(item.name.lower().endswith(".json"))
            except OSError:
                skipped += 1
        files.sort(key=lambda item: str(item[0]))
        skipped += max(0, len(files) - MAX_FILES)
        return files[:MAX_FILES], skipped

    def _load(self, relative: Path):
        # Walk by file descriptors with O_NOFOLLOW so parent-directory swaps cannot
        # redirect an import to a private backup outside the chosen corpus root.
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    raise ValueError("Corpus file exceeds its import limit")
                raw = source.read(MAX_FILE_BYTES + 1)
                if len(raw) > MAX_FILE_BYTES:
                    raise ValueError("Corpus file exceeds its import limit")
                value = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(value, (list, dict)):
                    raise ValueError("Invalid corpus container")
                if isinstance(value, dict) and not any(isinstance(value.get(key), list) for key in ("messages", "danmakus")):
                    raise ValueError("Invalid corpus messages")
                return _sample_file(value)
        finally:
            os.close(directory)

    def refresh(self) -> bool:
        with self._refresh_lock:
            return self._refresh()

    def _refresh(self) -> bool:
        skipped, files, failed = 0, [], False
        try:
            info = self.root.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                failed = True
            else:
                files, skipped = self._discover()
        except FileNotFoundError:
            pass
        except OSError:
            failed = True
        manifest = (failed, skipped,
                    tuple((str(relative), info.st_dev, info.st_ino, info.st_size,
                           info.st_mtime_ns, info.st_ctime_ns) for relative, info in files),
                    MAX_FILE_BYTES, MAX_TOTAL_BYTES, MAX_PER_FILE, MAX_INDEXED)
        if manifest == self._manifest:
            return False
        cache, total_bytes = {}, 0
        for relative, info in files:
            if info.st_size > MAX_FILE_BYTES or total_bytes + info.st_size > MAX_TOTAL_BYTES:
                skipped += 1
                continue
            total_bytes += info.st_size
            signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            existing = self._cache.get(relative)
            if existing and existing[0] == signature:
                samples = existing[1]
            else:
                try:
                    samples = self._load(relative)
                except (OSError, ValueError, UnicodeError, RecursionError):
                    samples = None
            cache[relative] = (signature, samples)
            if samples is None:
                skipped += 1
        good = [entry[1] for entry in cache.values() if entry[1] is not None]
        # Round-robin gives each file a fair chance when the shared index is full.
        selected, seen = [], set()
        for offset in range(max((len(items) for items in good), default=0)):
            for items in good:
                if offset < len(items) and items[offset].text not in seen:
                    selected.append(items[offset])
                    seen.add(items[offset].text)
                    if len(selected) >= MAX_INDEXED:
                        break
            if len(selected) >= MAX_INDEXED:
                break
        status = "ready" if selected else "error" if failed or (skipped and not good) else "empty"
        fingerprint = (status, tuple(selected), len(good), skipped)
        self._cache = cache
        self._manifest = manifest
        if fingerprint == self._fingerprint:
            return False
        postings = defaultdict(list)
        for number, sample in enumerate(selected):
            for term in sample.terms:
                postings[term].append(number)
        index = _Index(tuple(selected), {term: tuple(ids) for term, ids in postings.items()},
                       tuple(i for i, sample in enumerate(selected) if sample.generic))
        with self._lock:
            self._index = index
            self._fingerprint = fingerprint
            self._recent = ()
            self._state = {"status": status, "file_count": len(good), "indexed_count": len(selected),
                           "skipped_files": skipped, "updated_at": time.time(),
                           "revision": self._state["revision"] + 1}
        return True

    def select(self, context: dict, style_value: int, limit: int = 6) -> list[str]:
        if type(style_value) is not int or not 0 <= style_value <= 100:
            raise ValueError("Invalid danmaku style value")
        if type(limit) is not int or limit < 0:
            raise ValueError("Invalid reference limit")
        limit = min(limit, 6)
        if not limit:
            return []
        with self._lock:
            index, recent, cursor = self._index, set(self._recent), self._cursor
            self._cursor += 1
        if not index.samples:
            return []
        texts = []
        for key, fields in (("visual", ("description", "screen_text", "delta")),
                            ("audio_transcripts", ("text",)), ("audience_comments", ("text",))):
            for item in (context.get(key) or [])[-8:]:
                if isinstance(item, dict):
                    texts.extend(item[field][:240] for field in fields if isinstance(item.get(field), str))
        terms = _terms(" ".join(texts))
        query = sorted((term for term in terms if term in index.postings),
                       key=lambda term: (len(index.postings[term]), term))[:MAX_QUERY_TERMS]
        excluded = {item["text"].strip() for key in ("audience_comments", "audience_history", "recently_sent")
                    for item in (context.get(key) or [])[-64:]
                    if isinstance(item, dict) and isinstance(item.get("text"), str)}
        candidates = set()
        for term in query:
            postings = index.postings[term]
            offset = (cursor * 31) % len(postings)
            for count in range(min(MAX_POSTINGS_PER_TERM, len(postings))):
                candidates.add(postings[(offset + count) % len(postings)])
                if len(candidates) >= MAX_SELECTION_SCAN:
                    break
            if len(candidates) >= MAX_SELECTION_SCAN:
                break
        # Only a closed vocabulary can be used without a topic match. This avoids
        # pulling an arbitrary old name, claim, or catchphrase into today's stream.
        if index.generic:
            start = (cursor * 7) % len(index.generic)
            candidates.update(index.generic[(start + n) % len(index.generic)]
                              for n in range(min(32, len(index.generic))))
        scored = []
        for number in candidates:
            sample = index.samples[number]
            if sample.text in excluded:
                continue
            matches = sample.terms & terms
            topical = bool(matches)
            if not topical and not sample.generic:
                continue
            relevance = sum(math.log1p(len(index.samples) / len(index.postings[term])) for term in matches)
            # Matching syntax alone is weak; more context overlap takes precedence.
            score = min(relevance, 12) * 3 - abs(sample.style - style_value) / 12
            score += 2 if sample.paid and topical else 0
            score -= 16 if sample.text in recent else 0
            rotation = ((number * 1103515245 + cursor * (number + 1) * 12345) % 65536) / 65536
            scored.append((score + rotation, number, topical))
        scored.sort(reverse=True)
        result, total, generic_count, paid_count = [], 0, 0, 0
        for _, number, topical in scored:
            sample = index.samples[number]
            if (not topical and generic_count >= 2) or (sample.paid and paid_count >= 1):
                continue
            if total + len(sample.text) > 192:
                continue
            result.append(sample.text)
            total += len(sample.text)
            generic_count += int(not topical)
            paid_count += int(sample.paid)
            if len(result) >= limit:
                break
        with self._lock:
            if self._index is index:
                self._recent = tuple(result)
        return result
