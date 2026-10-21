"""Check page-bound quotations; text matching does not establish semantic support."""

import hashlib
import math
import re
import unicodedata


def tokens(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    text = re.sub(r"(?<=\w)[‐‑‒–—-](?=\w)", "", text)
    return re.findall(r"\w+", text)


def normalized(text):
    return " " + " ".join(tokens(text)) + " "


class QuoteVerifier:
    def __init__(self, source):
        self.source = source
        self.normalized = normalized(source)
        parts = re.split(r"\[p\.(\d+)\]", source)
        self.page_text = {}
        for i in range(1, len(parts), 2):
            page = parts[i]
            previous = self.page_text.get(page, "")
            self.page_text[page] = (previous + "\n[excerpt boundary]\n" if previous else "") + parts[i + 1]
        self.pages = set(self.page_text)

    def verify(self, quote):
        words = tokens(quote)
        if not words:
            return False
        if len(words) > 40:
            raise ValueError("The quote exceeds 40 words")
        if normalized(quote) in self.normalized:
            return True
        # Approximate checks are retained for compatibility and identified in the audit report.
        if len(words) < 10:
            return False
        size = math.ceil(len(words) * 0.9)
        return any(" " + " ".join(words[i:i + size]) + " " in self.normalized
                   for i in range(len(words) - size + 1))

    def located_source(self, location, based_on):
        if based_on == "abstract_only":
            if location != "abstract":
                raise ValueError("source_loc must be abstract when only the abstract was read")
            return self.source
        groups = re.split(r"\s*[,;]\s*", location)
        if len(groups) > 20:
            return None
        numbers = set()
        for group in groups:
            page = re.fullmatch(r"pp?\.?\s*(\d+)(?:\s*[-–—]\s*(\d+))?", group, re.I)
            if not page:
                return None
            first, last = int(page[1]), int(page[2] or page[1])
            if last < first or last - first > 20:
                return None
            numbers.update(range(first, last + 1))
        if len(numbers) > 40 or not all(str(number) in self.page_text for number in numbers):
            return None
        pieces, previous = [], None
        for number in sorted(numbers):
            if previous is not None and number != previous + 1:
                pieces.append("[noncontiguous pages]")
            pieces.append(self.page_text[str(number)])
            previous = number
        return "\n".join(pieces)

    def check_with_report(self, item, based_on):
        item = item.model_copy(deep=True)
        if not item.content.strip() or not item.source_loc.strip():
            raise ValueError("Reading-item content and source location must not be empty")
        source = self.located_source(item.source_loc, based_on)
        quote = item.quote or ""
        report = {"field": item.field, "source_loc": item.source_loc,
                  "original_quote_sha256": hashlib.sha256(quote.encode()).hexdigest() if quote else None,
                  "original_quote_words": len(tokens(quote)),
                  "source_sha256": hashlib.sha256(self.source.encode()).hexdigest(),
                  "semantic_support": "not_evaluated"}
        reason = "no_quote"
        if quote:
            if source is None:
                item.quote_verified, reason = False, "invalid_or_missing_page"
            elif len(tokens(quote)) > 40:
                if normalized(quote) in normalized(source):
                    # Shorten only after the entire original quote matches its declared page range.
                    spans = list(re.finditer(r"\S+", quote))
                    candidates = [quote[:span.end()] for span in spans[:20]
                                  if 0 < len(tokens(quote[:span.end()])) <= 20]
                    if candidates:
                        item.quote = candidates[-1]
                        item.quote_verified, reason = True, "exact_long_quote_shortened"
                    else:
                        item.quote = None
                        item.quote_verified, reason = False, "oversized_quote_not_safely_shortenable"
                else:
                    item.quote = None
                    item.quote_verified, reason = False, "oversized_quote_unmatched"
            else:
                checker = QuoteVerifier(source)
                item.quote_verified = checker.verify(quote)
                reason = ("exact_match" if normalized(quote) in normalized(source) else
                          "approximate_match" if item.quote_verified else "text_mismatch")
            if len(tokens(quote)) > 40 and item.quote_verified is not True:
                item.quote = None
                marker = "[TO VERIFY: oversized quote discarded]"
                if marker not in item.content:
                    item.content += " " + marker
        else:
            item.quote_verified = False if item.quote_verified is False else None
        if item.quote_verified is not True and item.kind == "author_claim":
            item.kind, item.confidence = "agent_inference", "low"
        report.update(reason=reason, quote_verified=item.quote_verified,
                      retained_quote_words=len(tokens(item.quote or "")))
        return item, report

    def check(self, item, based_on):
        return self.check_with_report(item, based_on)[0]
