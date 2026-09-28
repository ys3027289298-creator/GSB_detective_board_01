"""侦探证据板：核心数据模型与命令处理。

一致性约定：
- 证据图用无序边集合表示，重复合并不会产生新边，也不会重复增加可信度。
- 每个案件持有独立的撤销栈，撤销只影响当前案件。
- 时间线排序时未知时间（None）永远排在最后。
- 与已确认证词矛盾的证词只能标记为 contradicted，不会被确认。
- 过期线索（expires_round < 当前轮次）不能触发结局。
- 保存/加载完整覆盖节点、边、排除集、轮次与状态，加载不额外推进轮次。
- 指认后案件锁定（solved/failed），之后所有修改命令抛出 GameLockedError。
- 人物以 person_id 为唯一键，允许同名人物共存。
"""

from __future__ import annotations

import copy
import json

MAX_CREDIBILITY = 10

STATE_ACTIVE = "active"
STATE_SOLVED = "solved"
STATE_FAILED = "failed"

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_CONTRADICTED = "contradicted"


class GameLockedError(RuntimeError):
    """案件已结束（solved/failed）后仍尝试执行修改命令。"""


class UnknownNodeError(KeyError):
    """命令引用了不存在的人物或证据节点。"""


def _edge_key(node_a, node_b):
    """证据图的边是无向的，用排序后的元组作为唯一键。"""
    return (node_a, node_b) if node_a <= node_b else (node_b, node_a)


class Person:
    def __init__(self, person_id, name, role="suspect"):
        self.person_id = person_id
        self.name = name
        self.role = role

    def to_dict(self):
        return {"person_id": self.person_id, "name": self.name, "role": self.role}

    @classmethod
    def from_dict(cls, data):
        return cls(data["person_id"], data["name"], data.get("role", "suspect"))


class Evidence:
    def __init__(self, evidence_id, title, description="", credibility=1,
                 expires_round=None):
        self.evidence_id = evidence_id
        self.title = title
        self.description = description
        self.credibility = credibility
        self.expires_round = expires_round  # None 表示永不过期

    def is_expired(self, round_no):
        return self.expires_round is not None and round_no > self.expires_round

    def to_dict(self):
        return {
            "evidence_id": self.evidence_id,
            "title": self.title,
            "description": self.description,
            "credibility": self.credibility,
            "expires_round": self.expires_round,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["evidence_id"],
            data["title"],
            data.get("description", ""),
            data.get("credibility", 1),
            data.get("expires_round"),
        )


class Testimony:
    def __init__(self, testimony_id, person_id, claim, contradicts=(),
                 status=STATUS_PENDING):
        self.testimony_id = testimony_id
        self.person_id = person_id
        self.claim = claim
        self.contradicts = tuple(contradicts)
        self.status = status

    def to_dict(self):
        return {
            "testimony_id": self.testimony_id,
            "person_id": self.person_id,
            "claim": self.claim,
            "contradicts": list(self.contradicts),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["testimony_id"],
            data["person_id"],
            data["claim"],
            data.get("contradicts", ()),
            data.get("status", STATUS_PENDING),
        )


class TimelineEvent:
    def __init__(self, event_id, description, time=None):
        self.event_id = event_id
        self.description = description
        self.time = time  # "HH:MM" 或 None（未知时间）

    def to_dict(self):
        return {
            "event_id": self.event_id,
            "description": self.description,
            "time": self.time,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(data["event_id"], data["description"], data.get("time"))


class Case:
    """单个案件的完整推理状态。"""

    def __init__(self, case_id, title="", culprit_id=None, decisive_clue_id=None):
        self.case_id = case_id
        self.title = title
        self.culprit_id = culprit_id
        self.decisive_clue_id = decisive_clue_id
        self.persons = {}
        self.evidence = {}
        self.edges = set()
        self.excluded = set()
        self.testimonies = {}
        self.timeline = []
        self.round = 0
        self.state = STATE_ACTIVE
        self._undo_stack = []

    # ---- 内部工具 -------------------------------------------------

    def _ensure_active(self):
        if self.state != STATE_ACTIVE:
            raise GameLockedError(
                f"案件 {self.case_id} 已结束（{self.state}），命令被拒绝")

    def _snapshot(self):
        self._undo_stack.append(copy.deepcopy(self._state_dict()))

    def _state_dict(self):
        return {
            "persons": {pid: p.to_dict() for pid, p in self.persons.items()},
            "evidence": {eid: e.to_dict() for eid, e in self.evidence.items()},
            "edges": sorted(self.edges),
            "excluded": sorted(self.excluded),
            "testimonies": {tid: t.to_dict() for tid, t in self.testimonies.items()},
            "timeline": [ev.to_dict() for ev in self.timeline],
            "round": self.round,
            "state": self.state,
        }

    def _restore_state(self, state):
        self.persons = {pid: Person.from_dict(p) for pid, p in state["persons"].items()}
        self.evidence = {eid: Evidence.from_dict(e) for eid, e in state["evidence"].items()}
        self.edges = {tuple(edge) for edge in state["edges"]}
        self.excluded = set(state["excluded"])
        self.testimonies = {tid: Testimony.from_dict(t) for tid, t in state["testimonies"].items()}
        self.timeline = [TimelineEvent.from_dict(ev) for ev in state["timeline"]]
        self.round = state["round"]
        self.state = state["state"]

    def _require_node(self, node_id):
        if node_id not in self.persons and node_id not in self.evidence:
            raise UnknownNodeError(node_id)

    # ---- 命令：人物 / 证据 ----------------------------------------

    def add_person(self, person):
        self._ensure_active()
        self._snapshot()
        self.persons[person.person_id] = person

    def add_evidence(self, evidence):
        self._ensure_active()
        self._snapshot()
        self.evidence[evidence.evidence_id] = evidence

    def find_persons_by_name(self, name):
        """同名人物全部返回，不做合并。"""
        return [p for p in self.persons.values() if p.name == name]

    def merge(self, node_a, node_b):
        """合并两个节点形成一条边；重复合并不产生效果，返回 False。"""
        self._ensure_active()
        self._require_node(node_a)
        self._require_node(node_b)
        key = _edge_key(node_a, node_b)
        if key in self.edges:
            return False
        self._snapshot()
        self.edges.add(key)
        for node_id in (node_a, node_b):
            ev = self.evidence.get(node_id)
            if ev is not None:
                ev.credibility = min(MAX_CREDIBILITY, ev.credibility + 1)
        return True

    def exclude(self, evidence_id):
        self._ensure_active()
        if evidence_id not in self.evidence:
            raise UnknownNodeError(evidence_id)
        self._snapshot()
        self.excluded.add(evidence_id)

    # ---- 命令：证词 -----------------------------------------------

    def add_testimony(self, testimony):
        self._ensure_active()
        self._snapshot()
        self.testimonies[testimony.testimony_id] = testimony

    def confirm_testimony(self, testimony_id):
        """确认证词；若与任何已确认证词矛盾，则标记为 contradicted。"""
        self._ensure_active()
        testimony = self.testimonies[testimony_id]
        for other in self.testimonies.values():
            if other.status != STATUS_CONFIRMED:
                continue
            if (other.testimony_id in testimony.contradicts
                    or testimony_id in other.contradicts):
                self._snapshot()
                testimony.status = STATUS_CONTRADICTED
                return False
        self._snapshot()
        testimony.status = STATUS_CONFIRMED
        return True

    # ---- 命令：时间线 / 轮次 --------------------------------------

    def add_event(self, event):
        self._ensure_active()
        self._snapshot()
        self.timeline.append(event)

    def sorted_timeline(self):
        """已知时间升序排列，未知时间（None）固定排在最后。"""
        return sorted(self.timeline,
                      key=lambda ev: (ev.time is None, ev.time or ""))

    def advance_round(self, steps=1):
        self._ensure_active()
        self._snapshot()
        self.round += steps

    # ---- 结局与指认 -----------------------------------------------

    def _connected(self, start, goal):
        if start is None or goal is None:
            return False
        adjacency = {}
        for node_a, node_b in self.edges:
            adjacency.setdefault(node_a, set()).add(node_b)
            adjacency.setdefault(node_b, set()).add(node_a)
        seen = {start}
        stack = [start]
        while stack:
            node = stack.pop()
            if node == goal:
                return True
            for nxt in adjacency.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return False

    def ending_available(self):
        """决定性线索未过期、未排除且与凶手连通时，结局才可触发。"""
        if self.state != STATE_ACTIVE:
            return False
        clue = self.evidence.get(self.decisive_clue_id)
        if clue is None:
            return False
        if clue.is_expired(self.round) or clue.evidence_id in self.excluded:
            return False
        return self._connected(self.decisive_clue_id, self.culprit_id)

    def accuse(self, person_id):
        """指认凶手。无论对错，指认后案件立即锁定。"""
        self._ensure_active()
        if person_id not in self.persons:
            raise UnknownNodeError(person_id)
        if person_id == self.culprit_id:
            self.state = STATE_SOLVED
        else:
            self.state = STATE_FAILED
        return self.state

    # ---- 撤销（按案件隔离） ---------------------------------------

    def undo(self):
        """撤销当前案件的最近一次修改；不影响其他案件。"""
        if not self._undo_stack:
            return False
        self._restore_state(self._undo_stack.pop())
        return True

    # ---- 保存 / 加载 ----------------------------------------------

    def to_dict(self):
        return {
            "case_id": self.case_id,
            "title": self.title,
            "culprit_id": self.culprit_id,
            "decisive_clue_id": self.decisive_clue_id,
            "persons": [p.to_dict() for p in self.persons.values()],
            "evidence": [e.to_dict() for e in self.evidence.values()],
            "edges": sorted(list(edge) for edge in self.edges),
            "excluded": sorted(self.excluded),
            "testimonies": [t.to_dict() for t in self.testimonies.values()],
            "timeline": [ev.to_dict() for ev in self.timeline],
            "round": self.round,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, data):
        case = cls(
            data["case_id"],
            data.get("title", ""),
            data.get("culprit_id"),
            data.get("decisive_clue_id"),
        )
        for p in data.get("persons", []):
            person = Person.from_dict(p)
            case.persons[person.person_id] = person
        for e in data.get("evidence", []):
            evidence = Evidence.from_dict(e)
            case.evidence[evidence.evidence_id] = evidence
        case.edges = {tuple(edge) for edge in data.get("edges", [])}
        case.excluded = set(data.get("excluded", []))
        for t in data.get("testimonies", []):
            testimony = Testimony.from_dict(t)
            case.testimonies[testimony.testimony_id] = testimony
        case.timeline = [TimelineEvent.from_dict(ev)
                         for ev in data.get("timeline", [])]
        # 轮次与状态原样恢复，加载本身不推进轮次。
        case.round = data.get("round", 0)
        case.state = data.get("state", STATE_ACTIVE)
        return case

    def save(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


class DetectiveBoard:
    """管理多个案件；每个案件的撤销栈彼此独立。"""

    def __init__(self):
        self.cases = {}

    def add_case(self, case):
        self.cases[case.case_id] = case
        return case

    def get_case(self, case_id):
        return self.cases[case_id]
