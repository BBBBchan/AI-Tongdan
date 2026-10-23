"""Consistent streamer labels; the title body remains an editorial decision."""
import re


TITLE_ALIASES = {
    "阿梓从小就很可爱": "阿梓",
    "阿梓": "阿梓",
    "雪糕cheese": "雪糕",
    "雪糕": "雪糕",
    "七海Nana7mi": "七海Nana7mi",
    "帅比笙歌超可爱OvO": "笙歌",
    "星汐Seki": "星汐",
    "阿萨Aza": "阿萨",
    "雫るる_Official": "雫lulu",
}


def streamer_title_name(session):
    if session.get("mode") != "live":
        return ""
    room = session.get("room") if isinstance(session.get("room"), dict) else {}
    profile = session.get("publish_profile") or {}
    full_name = room.get("uname") or session.get("streamer") or profile.get("creator_name")
    if not isinstance(full_name, str):
        return ""
    full_name = full_name.strip()
    return TITLE_ALIASES.get(full_name, full_name).replace("【", "").replace("】", "")[:40]


def format_clip_title(title, session, maximum=80):
    """Prefix live clips once, removing only a redundant label for this streamer."""
    body = str(title or "").strip()
    name = streamer_title_name(session)
    if not name:
        return body[:maximum]
    room = session.get("room") if isinstance(session.get("room"), dict) else {}
    full = room.get("uname") or session.get("streamer") or (session.get("publish_profile") or {}).get("creator_name")
    aliases = {name, full}
    aliases.update(alias for alias, target in TITLE_ALIASES.items() if target == name)
    aliases = sorted((alias for alias in aliases if isinstance(alias, str) and alias), key=len, reverse=True)
    alternatives = "|".join(re.escape(alias) for alias in aliases)
    # Repeated already-normalized titles must stay unchanged. A topic bracket
    # or a reference to another person is part of the body and is retained.
    for _ in range(4):
        cleaned = re.sub(rf"^(?:【(?:{alternatives})】|(?:{alternatives})\s*[:：])\s*", "", body)
        if cleaned == body:
            break
        body = cleaned
    # The source label already supplies this subject. Only remove a repeated
    # known name before a clear predicate, not names such as “阿梓妈妈” or a
    # multi-person subject such as “阿梓和雪糕”.
    body = re.sub(rf"^(?:{alternatives})\s*(?=挑战|吐槽|发现|看|玩|在|聊|被|听|刚|突然|说|打|误)", "", body)
    prefix = f"【{name}】"
    return prefix + body[:max(0, maximum - len(prefix))]


def anchor_title_fields(anchor, session):
    variants = anchor.get("title_variants")
    variants = variants if isinstance(variants, list) else [anchor.get("title", "")]
    return {
        "title": format_clip_title(anchor.get("title"), session),
        "title_variants": list(dict.fromkeys(format_clip_title(x, session) for x in variants
                                            if isinstance(x, str) and x.strip())),
    }
