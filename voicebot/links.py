"""Full links of what the tools found, kept out of the model's text.

Tools give the model a short reference ('L3') instead of the URL: fewer tokens, nothing to read aloud, and no
made-up links. send_to_telegram turns the references back into the real links.
"""

from collections import OrderedDict

KEEP = 300


class LinkBook:
    def __init__(self, keep: int = KEEP):
        self.keep = keep
        self.items: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self.by_url: dict[str, str] = {}
        self.count = 0

    def add(self, url: str | None, title: str | None = "") -> str | None:
        if not url:
            return None
        if url in self.by_url and self.by_url[url] in self.items:
            return self.by_url[url]
        self.count += 1
        ref = f"L{self.count}"
        self.items[ref] = (url, (title or "").strip())
        self.by_url[url] = ref
        while len(self.items) > self.keep:
            old, (old_url, _) = self.items.popitem(last=False)
            self.by_url.pop(old_url, None)
        return ref

    def get(self, ref: str) -> tuple[str, str] | None:
        return self.items.get(ref.strip().upper())


LINKS = LinkBook()
