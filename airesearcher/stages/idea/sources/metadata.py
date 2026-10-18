"""检索元数据标准化和 BibTeX 生成，不由模型补写论文信息。"""

import re
import unicodedata


def arxiv_id(value: str) -> str:
    value = value.strip().removeprefix("arxiv:")
    value = value.split("/abs/")[-1].split("/pdf/")[-1].removesuffix(".pdf")
    return re.sub(r"v\d+$", "", value)


def doi_id(value: str) -> str:
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", value.strip().lower())


def citation_key(authors: list[str], year: int | None, title: str) -> str:
    surname = authors[0].split()[-1] if authors else "unknown"
    words = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower())
    word = next((w for w in words if w not in {"a", "an", "the", "on", "of", "in", "for", "with"}), "paper")
    return re.sub(r"[^a-z0-9]", "", f"{surname.lower()}{year or 'nd'}{word}") or "unknownpaper"


def _escape(text: str) -> str:
    table = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "&": r"\&", "%": r"\%",
             "#": r"\#", "_": r"\_", "$": r"\$"}
    return "".join(table.get(c, c) for c in " ".join(str(text).split()))


def bibtex(paper) -> str:
    kind = {"journal": "article", "conference": "inproceedings", "workshop": "inproceedings"}.get(
        paper.venue_type, "misc")
    fields = {"title": paper.title, "author": " and ".join(paper.authors), "url": paper.url}
    if paper.year:
        fields["year"] = str(paper.year)
    if paper.venue:
        fields["journal" if kind == "article" else "booktitle" if kind == "inproceedings" else "note"] = paper.venue
    if "doi" in paper.ids:
        fields["doi"] = paper.ids["doi"]
    if "arxiv" in paper.ids:
        fields.update(eprint=paper.ids["arxiv"], archivePrefix="arXiv")
    return f"@{kind}{{{paper.citation_key},\n" + ",\n".join(
        f"  {key} = {{{_escape(value)}}}" for key, value in fields.items()) + "\n}"


def unique_citation_keys(papers, existing):
    used = {p.citation_key: p.paper_id for p in existing.values()}
    for p in papers:
        base = existing[p.paper_id].citation_key if p.paper_id in existing else p.citation_key
        key, index = base, 1
        while key in used and used[key] != p.paper_id:
            n, suffix = index, ""
            while n:
                suffix = chr(97 + n % 26) + suffix
                n //= 26
            key = base + suffix
            index += 1
        if key != p.citation_key:
            p.citation_key = key
            p.bibtex = bibtex(p)
        used[key] = p.paper_id
    return papers
