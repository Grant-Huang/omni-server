import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omni import layers as L
from omni.instructions import DynamicBlock, InstructionPatcher, build
from omni.memory import MemoryStore, Provenance

BASE = "你是一个语音助手。"


def prov():
    return Provenance(origin_scope="user:grant")


def store_with(entries):
    s = MemoryStore()
    for layer, text, who in entries:
        s.add(text, layer=layer, scope="user:grant", written_by=who, source=prov())
    return s


class TestOrdering(unittest.TestCase):
    def test_layers_render_most_stable_first(self):
        s = store_with([
            ("task", "周二下午三点开会", L.EXTRACTION),
            ("persona", "说话简短", L.HUMAN),
            ("policy", "别在群里提体检报告", L.HUMAN),
            ("profile", "在 Y 公司做后端", L.EXTRACTION),
        ])
        out = build(BASE, s.retrieve("周二开会", output_scope="user:grant")).text
        positions = [out.index(f"【{L.LAYERS[n].heading}】") for n in ("persona", "policy", "profile", "task")]
        self.assertEqual(positions, sorted(positions))

    def test_the_stable_head_is_byte_identical_across_turns(self):
        """The property that keeps the assistant's voice from wobbling turn to turn,
        and gives any upstream prefix caching something to hold."""
        s = store_with([
            ("persona", "说话简短", L.HUMAN),
            ("policy", "别在群里提体检报告", L.HUMAN),
            ("task", "周二下午三点开会", L.EXTRACTION),
            ("task", "周四要交预算表", L.EXTRACTION),
        ])
        a = build(BASE, s.retrieve("周二", output_scope="user:grant")).text
        b = build(BASE, s.retrieve("周四", output_scope="user:grant")).text
        self.assertNotEqual(a, b)
        head = a.index(f"【{L.LAYERS['task'].heading}】")
        self.assertEqual(a[:head], b[:head])

    def test_dynamic_blocks_render_after_every_layer(self):
        s = store_with([("persona", "说话简短", L.HUMAN), ("task", "周二开会", L.EXTRACTION)])
        out = build(
            BASE,
            s.retrieve("周二", output_scope="user:grant"),
            [DynamicBlock(heading="刚查到的信息", body="今天下午三点有会")],
        ).text
        self.assertTrue(out.rstrip().endswith("今天下午三点有会"))
        self.assertLess(out.index(f"【{L.LAYERS['task'].heading}】"), out.index("【刚查到的信息】"))


class TestBudget(unittest.TestCase):
    def test_a_busy_layer_cannot_evict_another_layer(self):
        """The failure a global top-K would produce: a heavy task day silently drops the
        persona and the assistant's voice changes mid-conversation."""
        # Distinct subjects on purpose: near-identical entries would supersede each
        # other in the store and never reach the budget at all.
        subjects = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥金木水火土风雷山泽天地日月星辰"
        entries = [("persona", "说话要简短、直接、不要书面语", L.HUMAN)]
        for ch in subjects:
            entries.append(("task", f"周二要处理{ch}项目的评审，{ch}那边还欠一份材料", L.EXTRACTION))
        s = store_with(entries)
        built = build(BASE, s.retrieve("周二 项目 评审", output_scope="user:grant"))
        self.assertGreater(len(s.retrieve("周二 项目 评审", output_scope="user:grant")["task"]), 10)
        self.assertEqual(built.layer_counts.get("persona"), 1)
        self.assertIn("说话要简短", built.text)
        self.assertGreater(built.dropped.get("task", 0), 0)

    def test_each_layer_stays_within_its_own_budget(self):
        entries = [("task", f"周二事项{i}：" + "详" * 60, L.EXTRACTION) for i in range(20)]
        s = store_with(entries)
        built = build(BASE, s.retrieve("周二 事项", output_scope="user:grant"))
        section = built.text.split(f"【{L.LAYERS['task'].heading}】", 1)[1]
        self.assertLessEqual(len(section), L.LAYERS["task"].budget_chars + 10)

    def test_entries_are_kept_whole_never_truncated_mid_fact(self):
        s = store_with([("task", "周二" + "长" * 500, L.EXTRACTION)])
        built = build(BASE, s.retrieve("周二", output_scope="user:grant"))
        self.assertNotIn("长", built.text)          # dropped entirely...
        self.assertEqual(built.dropped.get("task"), 1)  # ...and reported as dropped


class TestPatcher(unittest.TestCase):
    def test_an_unchanged_rebuild_needs_no_patch(self):
        p = InstructionPatcher()
        self.assertTrue(p.needs_patch("A"))
        p.mark_sent("A")
        self.assertFalse(p.needs_patch("A"))
        self.assertTrue(p.needs_patch("B"))

    def test_an_unacked_patch_is_re_sent_next_turn(self):
        """mark_sent is only called on ack. Recording at send time would compound one
        dropped update into a session that silently never updates again."""
        p = InstructionPatcher()
        p.needs_patch("A")          # sent, but no ack arrives
        p.invalidate()
        self.assertTrue(p.needs_patch("A"))


if __name__ == "__main__":
    unittest.main()
