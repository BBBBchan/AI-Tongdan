"""Bounded, optional local style references; raw chat files never enter snapshots.

Filtering is deliberately conservative, but is not an anonymization guarantee.
Only manually reviewed, cleaned files should be put into this local directory.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
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
_REACTION_WORDS = frozenset({
    "笑死", "笑死我了", "乐了", "懂了", "坏了", "完了", "急了", "好好好", "可以", "确实", "合理",
    "害怕", "好急", "无敌", "寄了", "难绷", "绷", "蚌", "逆天", "抽象", "正常", "无语", "还好",
    "还行", "厉害", "好玩", "幽默", "我去", "好惨", "有道理", "绷不住", "蚌埠住", "没事", "没毛病",
    "可爱", "辛苦", "晚上好", "下午好", "早上好", "晚安", "早安", "收到", "再见", "拜拜", "爱看",
    "爱听", "好汀", "好听", "听不懂", "看不懂", "就这", "牛", "牛哇", "牛的", "行", "行的",
    "行啊", "典", "典中典", "乐", "懂", "懂得都懂", "难说", "好家伙", "我懂了", "那咋了", "这咋了",
    "怎么说", "怎么讲", "咋回事", "啥情况", "什么意思", "啥意思", "不知道", "不清楚", "没听清",
    "没看清", "等下", "等等", "等会", "求求了", "拜托了", "好的", "来了", "来咯", "走了", "润了",
    "溜了", "开了", "可以的", "差不多", "还真是", "确实是", "不会吧", "不至于", "真的假的",
    "真的吗", "那确实", "是这样的", "也没错", "那没事了", "那没事", "我不信", "有一说一", "什么情况",
    "不明白", "原来如此", "又来了", "还来", "救命", "绝了", "有点意思", "有意思", "好搞笑",
    "好尴尬", "好好笑", "没绷住", "好耶", "好看", "好诶", "好滴", "谢谢", "感谢", "支持", "加油",
})
_REACTION_PARTICLES = re.compile(r"[啊哦噢喔哎唉诶欸嗯恩呃额哈嘿嘻呵哼呜咦耶哇嚯嗷哟呀啦呢呗嘞呐咯唷]{1,8}")
_REACTION_PHRASE = re.compile(r"(?:[真假是好对]{1,3}[的吗吧呀嘞啊诶呢了嘛]?|[不没][懂会行对错好][了啦呀啊嘛吧吗呢]?)")
_REACTION_PUNCTUATION = re.compile(r"[?？!！~～…]+")
_COMPACT_PUNCTUATION = re.compile(r"[\s，。！？!?、~～….,:：;；]")
_IRONY_STRUCTURE = re.compile(
    r"(?:别尬黑|不许尬黑)[^。！？!?]{0,16}(?:这是|这叫|人家是在|人家是|主打一个)[^。！？!?]{2,16}"
    r"|(?:不是|不叫)(?:失误|翻车|跑调|掉水|送分)[，, ]*(?:而?是|这叫)[^。！？!?]{0,6}"
    r"(?:给[^。！？!?]{1,8}(?:机会|留面子|信心)|让[^。！？!?]{1,8}体验|测试水温|考察水温|检查复活点|控制胜率)")


def is_common_reaction(text: str) -> bool:
    """Closed, short reaction grammar: never a test for arbitrary safe chat.

    These portable expressions can recur naturally, unlike a distinctive quote
    or a name/story. The input must be entirely accounted for by this grammar.
    """
    if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 8
            or any(unicodedata.category(char).startswith("C") for char in text)):
        return False
    text = text.strip()
    if _REACTION_PUNCTUATION.fullmatch(text):
        return True
    compact = _COMPACT_PUNCTUATION.sub("", text)
    return bool(compact and (compact in _REACTION_WORDS
                or _REACTION_PARTICLES.fullmatch(compact)
                or _REACTION_PHRASE.fullmatch(compact)
                or re.fullmatch(r"0{1,6}|1{1,6}|6{1,6}|8{1,6}", compact)
                or _GENERIC.fullmatch(compact)))


def _reaction_family(text: str) -> str:
    """Avoid using all voice slots on punctuation/laughter length variants."""
    compact = _COMPACT_PUNCTUATION.sub("", text)
    if not compact:
        return "punctuation"
    if re.fullmatch(r"[哈嘿呵嘻]+|笑死(?:我了)?|好好笑|好搞笑|乐(?:了)?", compact):
        return "laughter"
    collapsed = re.sub(r"(.)\1+", r"\1", compact)
    return re.sub(r"^(.{2,4})(?:\1)+$", r"\1", collapsed)
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
    frequency: int = 1


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
    # Only a retrieval hint, never a semantic judgment that a line is funny or
    # ironic. Paired mock-defence/reframing cues outrank plain provocative words;
    # ambiguous "故意/战术/这下/不会吧" alone carry no high-style preference.
    if _IRONY_STRUCTURE.search(text):
        return 90
    if re.search(r"急了|典中典|就这|赢麻|你管这叫|离谱|难绷|绷不住|抽象|逆天|蚌埠住|还能这样", text):
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
    return _Sample(text, paid, _style(text), _terms(text), is_common_reaction(text))


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
    # Count before deduplicating: repeated short reactions are part of the voice.
    # A frequency stratum preserves them; deterministic hashing still gives the
    # whole file and less frequent expressions a share of the bounded index.
    samples = {}
    for (text, paid), frequency in Counter(_records(value)).items():
        sample = _clean(text, paid)
        if sample is None:
            continue
        previous = samples.get(sample.text)
        samples[sample.text] = replace(sample, frequency=frequency + (previous.frequency if previous else 0),
                                       paid=sample.paid or bool(previous and previous.paid))
    def rank(sample):
        return int.from_bytes(hashlib.blake2b(sample.text.encode("utf-8"), digest_size=8).digest(), "big")
    chosen = {}
    def reserve(pool, capacity, *, frequent=False):
        key = (lambda s: (-s.frequency, rank(s))) if frequent else rank
        for sample in heapq.nsmallest(capacity, pool, key=key):
            chosen[sample.text] = sample
    reserve((s for s in samples.values() if s.paid), min(50, MAX_PER_FILE))
    reserve((s for s in samples.values() if s.generic and not s.paid),
            max(1, MAX_PER_FILE // 10), frequent=True)
    reserve((s for s in samples.values() if len(s.text) <= 8 and not s.paid),
            max(1, MAX_PER_FILE // 4), frequent=True)
    capacity = max(0, MAX_PER_FILE - len(chosen))
    reserve((s for s in samples.values() if s.text not in chosen), capacity)
    return tuple(list(chosen.values())[:MAX_PER_FILE])


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
        frequencies = Counter()
        for items in good:
            for sample in items:
                frequencies[sample.text] += sample.frequency
        # Round-robin gives each file a fair chance when the shared index is full.
        selected, seen = [], set()
        for offset in range(max((len(items) for items in good), default=0)):
            for items in good:
                if offset < len(items) and items[offset].text not in seen:
                    selected.append(replace(items[offset], frequency=frequencies[items[offset].text]))
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
                       tuple(sorted((i for i, sample in enumerate(selected) if sample.generic),
                                    key=lambda i: (-selected[i].frequency, i))))
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
        # Separate expression slots from topic search. Otherwise long sentences
        # sharing several topic terms crowd out the corpus's frequent reactions.
        # Off-topic references must still match a closed portable grammar.
        reaction_ids = set()
        if index.generic:
            start = (cursor * 7) % len(index.generic)
            reaction_ids.update(index.generic[:32])
            reaction_ids.update(index.generic[(start + n) % len(index.generic)]
                                for n in range(min(64, len(index.generic))))
            candidates.update(reaction_ids)
        topics, reactions = [], []
        for number in candidates:
            sample = index.samples[number]
            if sample.text in excluded:
                continue
            matches = sample.terms & terms
            topical = bool(matches)
            if not topical and not sample.generic:
                continue
            # 30 is "unclassified", not a reliable semantic tone annotation.
            # Clearly sharp lexical markers must not bypass a gentle slider just
            # because several nouns happen to match the current scene.
            if (style_value <= 20 and sample.style >= 70) or (style_value <= 60 and sample.style >= 90):
                continue
            relevance = sum(math.log1p(len(index.samples) / len(index.postings[term])) for term in matches)
            style_score = 0 if sample.style == 30 else 2.5 - abs(sample.style - style_value) / 12
            frequency_score = min(math.log1p(sample.frequency), 6)
            score = style_score + frequency_score
            score -= 5 if sample.text in recent else 0
            rotation = ((number * 1103515245 + cursor * (number + 1) * 12345) % 65536) / 65536
            if sample.generic and not sample.paid:
                reactions.append((score + rotation, number))
            if topical and not sample.generic:
                # Topic slots teach specific replies, while the separate voice
                # slots carry high-frequency fragments. Raw frequency must not
                # promote a common short noun over a richer contextual phrase.
                topic_score = style_score + min(relevance, 12) / 2 + frequency_score / 3
                topic_score -= 5 if sample.text in recent else 0
                topics.append((topic_score + (1 if sample.paid else 0) + rotation, number))
        topics.sort(reverse=True)
        reactions.sort(reverse=True)
        result, chosen, total, paid_count = [], set(), 0, 0
        reaction_families = set()
        def take(pool, count, *, distinct_reactions=False):
            nonlocal total, paid_count
            added = 0
            for _, number in pool:
                sample = index.samples[number]
                if number in chosen or (sample.paid and paid_count >= 1) or total + len(sample.text) > 192:
                    continue
                family = _reaction_family(sample.text) if distinct_reactions else None
                if distinct_reactions and family in reaction_families:
                    continue
                result.append(sample.text)
                chosen.add(number)
                total += len(sample.text)
                paid_count += int(sample.paid)
                if distinct_reactions:
                    reaction_families.add(family)
                added += 1
                if added >= count or len(result) >= limit:
                    break
        # Small requested samples remain topic-first. The regular six-reference
        # request reserves half its budget for corpus voice even during a dense
        # visual description. Empty slots can be filled from the other pool.
        topic_quota = (limit + 1) // 2 if reactions else limit
        take(topics, topic_quota)
        if len(result) < limit:
            take(reactions, min(3, limit - len(result)), distinct_reactions=True)
        if len(result) < limit:
            take(topics, limit - len(result))
        with self._lock:
            if self._index is index:
                self._recent = tuple(result)
        return result
