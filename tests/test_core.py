import os
import tempfile
import unittest

from detective.core import (
    Case,
    DetectiveBoard,
    Evidence,
    GameLockedError,
    Person,
    Testimony,
    TimelineEvent,
)


def make_case(case_id="c1", culprit_id="p1", decisive_clue_id="e1"):
    return Case(case_id, title="测试案件", culprit_id=culprit_id,
                decisive_clue_id=decisive_clue_id)


class TestDuplicateMerge(unittest.TestCase):
    """Bug 1: 合并证据时重复边会增加可信度。"""

    def test_duplicate_merge_does_not_inflate_credibility(self):
        case = make_case()
        case.add_evidence(Evidence("e1", "血迹"))
        case.add_evidence(Evidence("e2", "刀"))
        self.assertTrue(case.merge("e1", "e2"))
        self.assertEqual(case.evidence["e1"].credibility, 2)
        self.assertEqual(case.evidence["e2"].credibility, 2)
        # 同向重复与反向重复都必须被忽略
        self.assertFalse(case.merge("e1", "e2"))
        self.assertFalse(case.merge("e2", "e1"))
        self.assertEqual(len(case.edges), 1)
        self.assertEqual(case.evidence["e1"].credibility, 2)
        self.assertEqual(case.evidence["e2"].credibility, 2)


class TestUndoScope(unittest.TestCase):
    """Bug 2: 撤销会删除其他案件的证据。"""

    def test_undo_is_scoped_to_own_case(self):
        board = DetectiveBoard()
        case_a = board.add_case(make_case("A"))
        case_b = board.add_case(make_case("B"))
        case_a.add_evidence(Evidence("e1", "A 的证据"))
        case_b.add_evidence(Evidence("e2", "B 的证据"))
        self.assertTrue(case_a.undo())
        self.assertNotIn("e1", case_a.evidence)
        # 案件 B 的证据必须完好
        self.assertIn("e2", case_b.evidence)
        self.assertEqual(case_b.evidence["e2"].title, "B 的证据")

    def test_undo_restores_state_repeatably(self):
        case = make_case()
        case.add_evidence(Evidence("e1", "血迹"))
        case.add_evidence(Evidence("e2", "刀"))
        case.merge("e1", "e2")
        snapshot = case.to_dict()
        case.exclude("e1")
        case.advance_round()
        case.undo()
        case.undo()
        self.assertEqual(case.to_dict(), snapshot)


class TestTimelineOrder(unittest.TestCase):
    """Bug 3: 时间线排序把未知时间放到最前。"""

    def test_unknown_time_sorts_last(self):
        case = make_case()
        case.add_event(TimelineEvent("ev1", "未知时刻的争吵", None))
        case.add_event(TimelineEvent("ev2", "十点的电话", "22:00"))
        case.add_event(TimelineEvent("ev3", "八点的晚餐", "20:00"))
        ordered = [ev.event_id for ev in case.sorted_timeline()]
        self.assertEqual(ordered, ["ev3", "ev2", "ev1"])


class TestContradictoryTestimony(unittest.TestCase):
    """Bug 4: 矛盾证词仍被标记为确认。"""

    def test_contradictory_testimony_not_confirmed(self):
        case = make_case()
        case.add_testimony(Testimony("t1", "p2", "午夜在书房", contradicts=("t2",)))
        case.add_testimony(Testimony("t2", "p3", "午夜在花园", contradicts=("t1",)))
        self.assertTrue(case.confirm_testimony("t1"))
        self.assertFalse(case.confirm_testimony("t2"))
        self.assertEqual(case.testimonies["t1"].status, "confirmed")
        self.assertEqual(case.testimonies["t2"].status, "contradicted")


class TestExpiredClue(unittest.TestCase):
    """Bug 5: 线索过期后仍可触发结局。"""

    def test_expired_clue_cannot_trigger_ending(self):
        case = make_case(culprit_id="p1", decisive_clue_id="e1")
        case.add_person(Person("p1", "嫌疑人"))
        case.add_evidence(Evidence("e1", "字条", expires_round=2))
        case.merge("e1", "p1")
        self.assertTrue(case.ending_available())
        case.advance_round(3)  # 第 3 轮，线索在第 2 轮后过期
        self.assertFalse(case.ending_available())

    def test_excluded_clue_cannot_trigger_ending(self):
        case = make_case(culprit_id="p1", decisive_clue_id="e1")
        case.add_person(Person("p1", "嫌疑人"))
        case.add_evidence(Evidence("e1", "字条"))
        case.merge("e1", "p1")
        case.exclude("e1")
        self.assertFalse(case.ending_available())


class TestSaveLoad(unittest.TestCase):
    """Bug 6/7: 保存丢失排除标记；加载后轮次重复增加。"""

    def test_save_preserves_exclusions(self):
        case = make_case()
        case.add_evidence(Evidence("e1", "血迹"))
        case.add_evidence(Evidence("e2", "伪证"))
        case.exclude("e2")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "save.json")
            case.save(path)
            loaded = Case.load(path)
        self.assertEqual(loaded.excluded, {"e2"})
        self.assertEqual(loaded.edges, case.edges)

    def test_load_does_not_increment_round(self):
        case = make_case()
        case.advance_round()
        case.advance_round()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "save.json")
            case.save(path)
            loaded = Case.load(path)
        self.assertEqual(loaded.round, 2)
        loaded.advance_round()
        self.assertEqual(loaded.round, 3)


class TestAccusationLock(unittest.TestCase):
    """Bug 8: 错误指认没有锁定失败状态。"""

    def test_wrong_accusation_locks_failure(self):
        case = make_case(culprit_id="p1")
        case.add_person(Person("p1", "真凶"))
        case.add_person(Person("p2", "无辜者"))
        self.assertEqual(case.accuse("p2"), "failed")
        self.assertEqual(case.state, "failed")
        # 锁定后任何修改命令都必须被拒绝，包括再次指认真凶
        with self.assertRaises(GameLockedError):
            case.accuse("p1")
        with self.assertRaises(GameLockedError):
            case.add_evidence(Evidence("e9", "新证据"))
        with self.assertRaises(GameLockedError):
            case.merge("p1", "p2")
        self.assertEqual(case.state, "failed")


class TestSameNamePersons(unittest.TestCase):
    """Bug 9: 同名人物被错误合并。"""

    def test_same_name_persons_not_merged(self):
        case = make_case()
        case.add_person(Person("p1", "张伟"))
        case.add_person(Person("p2", "张伟"))
        self.assertEqual(len(case.persons), 2)
        self.assertEqual({p.person_id for p in case.find_persons_by_name("张伟")},
                         {"p1", "p2"})
        case.add_evidence(Evidence("e1", "指纹"))
        case.merge("e1", "p1")
        # 边只挂在 p1 上，不能牵连同名的 p2
        self.assertEqual(case.edges, {("e1", "p1")})


class TestEmptyCaseBoundary(unittest.TestCase):
    """空案件边界：无任何节点/事件时的行为。"""

    def test_empty_case_boundaries(self):
        case = Case("empty")
        self.assertEqual(case.sorted_timeline(), [])
        self.assertFalse(case.undo())
        self.assertFalse(case.ending_available())
        self.assertEqual(case.round, 0)
        self.assertEqual(case.state, "active")
        # 空案件保存/加载往返必须保持一致
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "empty.json")
            case.save(path)
            loaded = Case.load(path)
        self.assertEqual(loaded.to_dict(), case.to_dict())
        self.assertFalse(loaded.undo())


if __name__ == "__main__":
    unittest.main()
