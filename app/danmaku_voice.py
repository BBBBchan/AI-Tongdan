"""Portable, manually distilled voice guidance; no corpus, samples or file I/O.

Derived from aggregate and balanced, deduplicated review of local mixed chat
records. Message-type metadata was removed during cleaning, so the longer-text
guidance is not a separately labelled Super Chat model. These are authored
structures, not stored audience messages or model weights.
"""

_RHYTHM = (
    "表达节奏：短、口语、省主语，只抓一个刚发生的细节。"
    "五条长短错开，以2–12字的接话为主，必要时再稍长；不都写满字数。"
    "用共享语境省掉背景，不复述画面，不句句解释笑点或讲完整道理。"
    "可有短反问或欲言又止；不要整批只有哈哈、问号，也不堆叠复读或固定口癖。"
)

_LONG_MESSAGE = (
    "中长留言/SC只借鉴结构：一个小请求、一个具体疑问，或预期到结果的转折；"
    "压成一句当前反应，仍遵守本次字数限制，不强塞提问。"
    "不搬运旧故事、称呼、隐私、房间黑话或付费经历，不声称自己打赏过。"
)

_TONES = (
    (20, "轻声应援或关心具体表现，朴素直接，少用夸张赞词；句式如‘先…吧’、‘…就好’。"),
    (40, "自然接上话头，轻微惊讶或认同，语气词偶尔用；句式如‘原来…啊’、‘还真…’。"),
    (60, "抓当前反差，短反问、自嘲或轻微夸张，不硬编拟人小剧场；句式如‘这下…了’、‘…是吧’。"),
    (80, "对眼前操作或结果吐槽，一句收住，不写点评稿；句式如‘不是，…呢’、‘这也能…啊’。"),
    (100, "幽默反串：明明翻车却假装护航，顺着眼前反差一本正经找理由；句式如‘不是…，是…’、‘别尬黑，这是…’。"
          "只反转评价，不编造动作或引用；不靠辱骂、挑衅词或满屏同一骨架充当笑点。"),
)


def voice_instruction(style_value: int) -> str:
    """Return a bounded built-in reference for the selected slider position."""
    if type(style_value) is not int or not 0 <= style_value <= 100:
        raise ValueError("Invalid danmaku style value")
    tone = next(text for upper, text in _TONES if style_value <= upper)
    return (
        _RHYTHM + tone + _LONG_MESSAGE
        + "上述省略号只是句式位置，不照抄骨架；只在当前证据合适时采用，现场语气优先。"
    )
