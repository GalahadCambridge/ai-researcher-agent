"""引文文字核对；文字匹配不能证明论断正确或研究空白成立。"""

import math
import re
import unicodedata


def tokens(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    text = re.sub(r"(?<=\w)[‐‑‒–—-](?=\w)", "", text)
    return re.findall(r"\w+", text)


class QuoteVerifier:
    def __init__(self, source):
        self.normalized = " " + " ".join(tokens(source)) + " "
        self.pages = set(re.findall(r"\[p\.(\d+)\]", source))

    def verify(self, quote):
        words = tokens(quote)
        if not words:
            return False
        if len(words) > 40:
            raise ValueError("引文超过 40 词")
        if " " + " ".join(words) + " " in self.normalized:
            return True
        # Require a >=90% contiguous span. Short snippets require exact normalized matching.
        if len(words) < 10:
            return False
        size = math.ceil(len(words) * 0.9)
        return any(" " + " ".join(words[i:i + size]) + " " in self.normalized
                   for i in range(len(words) - size + 1))

    def check(self, item, based_on):
        item = item.model_copy(deep=True)
        if not item.content.strip() or not item.source_loc.strip():
            raise ValueError("阅读条目的内容与来源位置不能为空")
        if based_on == "abstract_only" and item.source_loc != "abstract":
            raise ValueError("只读摘要时 source_loc 必须为 abstract")
        item.quote_verified = self.verify(item.quote) if item.quote else None
        page = re.fullmatch(r"p\.?\s*(\d+)", item.source_loc, re.I)
        if page and page.group(1) not in self.pages:
            item.quote_verified = False
        if item.quote_verified is not True and item.kind == "author_claim":
            item.kind, item.confidence = "agent_inference", "low"
        return item
