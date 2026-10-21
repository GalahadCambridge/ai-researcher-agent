"""Switch sources after three retries; return all waiting to the engine through SourceWait."""

import hashlib
import json
import time

from .http import SourceFailure, SourceWait


class FallbackSource:
    name = "semantic_scholar+arxiv"

    def __init__(self, sources, state: dict, events, clock=time.time):
        self.sources, self.state, self.events, self.clock = sources, state, events, clock

    def search(self, query, *, year_from=None, limit=20):
        key = hashlib.sha256(json.dumps([query, year_from, limit]).encode()).hexdigest()
        pending = self.state.setdefault(key, {"source": 0, "failures": 0, "next_at": 0})
        if pending["next_at"] > self.clock():
            raise SourceWait(pending["next_at"] - self.clock(), "Waiting to retry retrieval")
        source = self.sources[pending["source"]]
        try:
            result = source.search(query, year_from=year_from, limit=limit)
        except SourceWait:
            raise
        except SourceFailure as exc:
            pending["failures"] += 1
            if pending["failures"] < 4:  # initial attempt + three retries
                delay = max(2 ** (pending["failures"] - 1), exc.retry_after)
                pending["next_at"] = self.clock() + delay
                raise SourceWait(delay, f"{source.name} failed; preparing retry {pending['failures']}") from exc
            if pending["source"] + 1 < len(self.sources):
                pending.update(source=pending["source"] + 1, failures=0, next_at=0)
                self.events.append("literature.source_switched", (
                    "Primary literature source failed after retries; switching to the backup source"
                ), actor="agent:idea",
                                   data={"query": query, "from": source.name,
                                         "to": self.sources[pending["source"]].name})
                raise SourceWait(0.05, "Literature source switched") from exc
            self.state.pop(key)
            raise SourceFailure(
                "All literature sources failed; failure snapshots were retained. Please retry "
                "later"
            ) from exc
        self.state.pop(key)
        return result

    def get(self, paper_id):
        # Query-level retry/fallback is handled by search; get is a single-source lookup.
        return self.sources[1 if paper_id.startswith("arxiv:") else 0].get(paper_id)
