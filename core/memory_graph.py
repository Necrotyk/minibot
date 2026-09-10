# SPDX-License-Identifier: AGPL-3.0-or-later
"""
memory_graph.py — In-Memory, Channel/Network-Scoped Bipartite Entity Graph for Ephemeral IRC Context.
Provides zero-persistence multi-user conversation linking with TTL eviction and stopword filtering.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("MemoryGraph")

# Common English and conversational stopwords to exclude from topical entity linking
DEFAULT_STOPWORDS: Set[str] = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and",
    "any", "are", "aren", "as", "at", "be", "because", "been", "before", "being",
    "below", "between", "both", "but", "by", "can", "cannot", "could", "did",
    "do", "does", "doing", "down", "during", "each", "few", "for", "from", "further",
    "had", "has", "have", "having", "he", "her", "here", "hers", "herself", "him",
    "himself", "his", "how", "i", "if", "in", "into", "is", "it", "its", "itself",
    "just", "ll", "m", "me", "more", "most", "my", "myself", "no", "nor", "not",
    "now", "o", "of", "off", "on", "once", "only", "or", "other", "our", "ours",
    "ourselves", "out", "over", "own", "re", "s", "same", "she", "should", "so",
    "some", "such", "t", "than", "that", "the", "their", "theirs", "them", "themselves",
    "then", "there", "these", "they", "this", "those", "through", "to", "too", "under",
    "until", "up", "very", "ve", "was", "we", "were", "what", "when", "where",
    "which", "while", "who", "whom", "why", "will", "with", "won", "would", "y",
    "you", "your", "yours", "yourself", "yourselves", "tell", "show", "give", "help",
    "list", "info", "query", "find", "search", "explain"
}


@dataclass(slots=True)
class MessageRef:
    nick: str
    message: str
    timestamp: float
    entities: Set[str] = field(default_factory=set)


class EphemeralChatMemory:
    """
    In-memory, network- and channel-isolated semantic context graph.
    Maintains a rolling sliding window of messages per user and a bipartite mapping
    from normalized entities to nicks who mentioned them.
    """

    def __init__(
        self,
        max_user_history: int = 6,
        ttl_seconds: int = 300,
        max_entities_per_scope: int = 100,
        stopwords: Optional[Set[str]] = None
    ) -> None:
        self.max_user_history: int = max_user_history
        self.ttl_seconds: int = ttl_seconds
        self.max_entities_per_scope: int = max_entities_per_scope
        self.stopwords: Set[str] = stopwords or DEFAULT_STOPWORDS

        # Scope (network:channel) -> Nick -> Deque[MessageRef]
        self.user_history: Dict[str, Dict[str, deque[MessageRef]]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=self.max_user_history))
        )

        # Scope (network:channel) -> Entity -> Set[Nick]
        self.entity_index: Dict[str, Dict[str, Set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )

        # Scope (network:channel) -> Nick -> monotonic timestamp
        self.last_seen: Dict[str, Dict[str, float]] = defaultdict(dict)

        # (Scope, Nick) -> (monotonic timestamp, {"query": str, "reply": str, "entities": List[str]})
        self.last_turns: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}

    def _normalize_entity(self, entity: str) -> Optional[str]:
        """Strips punctuation, normalizes to lowercase, checks stopwords and length."""
        if not entity:
            return None
        clean = re.sub(r"[^a-zA-Z0-9_\-\s]", "", entity).strip().lower()
        # Compress whitespace
        clean = re.sub(r"\s+", " ", clean)
        if len(clean) < 2 or len(clean) > 32:
            return None
        if clean in self.stopwords:
            return None
        return clean

    def make_scope(self, network: str, target: str, sender_nick: str) -> str:
        """
        Builds an isolated scope key.
        Channels (starting with # or &) map to '{network}:{channel}'.
        Private direct messages map to '{network}:pm:{nick}' to prevent leakage into channels.
        """
        net = (network or "default").strip().lower()
        tgt = (target or "").strip().lower()
        if tgt.startswith("#") or tgt.startswith("&"):
            return f"{net}:{tgt}"
        # Direct PM context is strictly isolated per caller
        nick = (sender_nick or "user").strip().lower()
        return f"{net}:pm:{nick}"

    def prune_stale(self, scope: str) -> None:
        """Evicts entries older than TTL to bound memory growth and isolate stale contexts."""
        now = time.monotonic()

        # Prune stale last_turns for this scope
        for key in list(self.last_turns.keys()):
            s, _ = key
            if s == scope:
                ts, _ = self.last_turns[key]
                if now - ts > self.ttl_seconds:
                    del self.last_turns[key]

        if scope not in self.last_seen:
            return

        stale_nicks = [
            nick for nick, ts in self.last_seen[scope].items()
            if now - ts > self.ttl_seconds
        ]

        for nick in stale_nicks:
            self.last_seen[scope].pop(nick, None)
            self.user_history[scope].pop(nick, None)

            # Scrub user references from the bipartite entity index
            for entity in list(self.entity_index[scope].keys()):
                self.entity_index[scope][entity].discard(nick)
                if not self.entity_index[scope][entity]:
                    del self.entity_index[scope][entity]

        # Clean up empty scopes
        if not self.last_seen[scope]:
            self.last_seen.pop(scope, None)
            self.user_history.pop(scope, None)
            self.entity_index.pop(scope, None)

    def record_interaction(
        self, scope: str, nick: str, message: str, entities: List[str]
    ) -> None:
        """Ingests a user query, updates active entity links, and refreshes the TTL clock."""
        self.prune_stale(scope)
        now = time.monotonic()
        clean_nick = nick.strip().lower()
        self.last_seen[scope][clean_nick] = now

        norm_entities: Set[str] = set()
        for e in entities:
            norm = self._normalize_entity(e)
            if norm:
                norm_entities.add(norm)

        ref = MessageRef(
            nick=nick.strip(),
            message=message.strip(),
            timestamp=now,
            entities=norm_entities
        )
        self.user_history[scope][clean_nick].append(ref)

        # Enforce max entities limit per scope to prevent memory ballooning
        for ent in norm_entities:
            if len(self.entity_index[scope]) >= self.max_entities_per_scope and ent not in self.entity_index[scope]:
                # Evict oldest entity with least nicks
                oldest_ent = min(self.entity_index[scope].keys(), key=lambda k: len(self.entity_index[scope][k]))
                del self.entity_index[scope][oldest_ent]
            self.entity_index[scope][ent].add(clean_nick)

    def record_assistant_reply(
        self, scope: str, nick: str, reply: str, is_error: bool = False
    ) -> None:
        """Stores the assistant's reply for the user's latest query to enable 1-turn memory."""
        if is_error or not reply:
            return

        reply_clean = reply.strip()
        reply_lower = reply_clean.lower()
        error_indicators = (
            "worker unreachable",
            "timed out",
            "usage:",
            "syntaxerror",
            "traceback (most recent call last)",
            "error:",
            "worker busy",
            "unable to generate response",
        )
        if any(ind in reply_lower for ind in error_indicators):
            return

        clean_nick = nick.strip().lower()
        now = time.monotonic()

        # Find latest query from user_history
        query = ""
        entities: List[str] = []
        if scope in self.user_history and clean_nick in self.user_history[scope] and self.user_history[scope][clean_nick]:
            last_msg = self.user_history[scope][clean_nick][-1]
            query = last_msg.message
            entities = list(last_msg.entities)

        if not query:
            return

        self.last_turns[(scope, clean_nick)] = (
            now,
            {
                "query": query,
                "reply": reply_clean,
                "entities": entities,
            }
        )

    def get_user_last_turn(
        self, scope: str, nick: str, max_age: float = 90.0
    ) -> Optional[Dict[str, Any]]:
        """Retrieves the user's last interaction turn (query & reply) if within max_age TTL."""
        clean_nick = nick.strip().lower()
        key = (scope, clean_nick)
        if key not in self.last_turns:
            return None
        ts, turn_data = self.last_turns[key]
        if time.monotonic() - ts > max_age:
            del self.last_turns[key]
            return None
        return turn_data

    def get_merged_context(
        self, scope: str, current_nick: str, active_entities: List[str], max_tokens: int = 80
    ) -> List[str]:
        """
        Cross-references recent user utterances linked via overlapping entities.
        Returns a list of sanitized context lines bounded by token budget (~4 chars/token).
        """
        self.prune_stale(scope)
        clean_current = current_nick.strip().lower()

        norm_entities: Set[str] = set()
        for e in active_entities:
            norm = self._normalize_entity(e)
            if norm:
                norm_entities.add(norm)

        # Collect other nicks discussing overlapping topics in this scope
        linked_nicks: Set[str] = set()
        for ent in norm_entities:
            if ent in self.entity_index[scope]:
                linked_nicks.update(self.entity_index[scope][ent])
        linked_nicks.discard(clean_current)

        context_lines: List[str] = []
        char_budget = max_tokens * 4  # ~4 chars per token approximation

        # Inject up to 2 most recent utterances from each linked peer sorted by recency
        sorted_peers = sorted(
            linked_nicks,
            key=lambda p: self.last_seen[scope].get(p, 0.0),
            reverse=True
        )
        for peer in sorted_peers:
            peer_msgs = list(self.user_history[scope][peer])[-2:]
            for msg in peer_msgs:
                clean_content = msg.message.replace("<", "[").replace(">", "]")
                # Clamp single line length to 80 chars
                if len(clean_content) > 80:
                    clean_content = clean_content[:77] + "..."
                line = f"{msg.nick}: {clean_content}"
                if len(line) + 2 <= char_budget:
                    context_lines.append(line)
                    char_budget -= len(line) + 2
                if char_budget <= 0:
                    break
            if char_budget <= 0:
                break

        return context_lines
