import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omni import layers as L
from omni.memory import MemoryStore, Provenance
from omni.persistence import SqliteMemoryPersistence


def prov(origin="user:grant", **kw):
    return Provenance(origin_scope=origin, **kw)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_written_entry_survives_a_reload(self):
        persistence = SqliteMemoryPersistence(self.db_path)
        store = MemoryStore(persist=persistence)
        store.add("我住在北京", layer="profile", scope="user:grant",
                  written_by=L.HUMAN, source=prov())
        persistence.close()

        reloaded = SqliteMemoryPersistence(self.db_path)
        fresh = MemoryStore()
        n = fresh.restore(reloaded.load_all())
        self.assertEqual(n, 1)
        self.assertEqual([e.text for e in fresh.live()], ["我住在北京"])

    def test_supersede_state_survives_a_reload(self):
        persistence = SqliteMemoryPersistence(self.db_path)
        store = MemoryStore(persist=persistence)
        old = store.add("我住在北京", layer="profile", scope="user:grant",
                        written_by=L.HUMAN, source=prov())
        store.add("我搬到上海了", layer="profile", scope="user:grant",
                  written_by=L.HUMAN, source=prov(), supersedes=[old.id])
        persistence.close()

        reloaded = SqliteMemoryPersistence(self.db_path)
        fresh = MemoryStore()
        fresh.restore(reloaded.load_all())
        self.assertEqual([e.text for e in fresh.live()], ["我搬到上海了"])
        # The superseded entry is still there for audit -- restore does not drop it.
        self.assertEqual(len(fresh.all_entries()), 2)

    def test_ephemeral_entries_are_never_written_to_disk(self):
        """Session-bound by definition -- no session from a previous process is still
        open to bind a reloaded one to, so persisting it would only ever produce inert
        garbage. Simplest fix: never write it in the first place."""
        persistence = SqliteMemoryPersistence(self.db_path)
        store = MemoryStore(persist=persistence)
        store.add("接下来都简短点", layer="ephemeral", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov(), session_id="s1")
        persistence.close()

        reloaded = SqliteMemoryPersistence(self.db_path)
        self.assertEqual(reloaded.load_all(), [])

    def test_provenance_round_trips_intact(self):
        persistence = SqliteMemoryPersistence(self.db_path)
        store = MemoryStore(persist=persistence)
        store.add("周二开会", layer="task", scope="user:grant", written_by=L.EXTRACTION,
                  source=prov(speaker_id="grant", session_id="s1", turn_id="t1", confidence=0.75))
        persistence.close()

        reloaded = SqliteMemoryPersistence(self.db_path)
        fresh = MemoryStore()
        fresh.restore(reloaded.load_all())
        entry = fresh.live()[0]
        self.assertEqual(entry.source.speaker_id, "grant")
        self.assertEqual(entry.source.turn_id, "t1")
        self.assertAlmostEqual(entry.source.confidence, 0.75)

    def test_restore_does_not_re_trigger_conflict_detection(self):
        """Two entries that would collide under a similarity detector must not be
        merged again on reload -- whatever supersede relationship existed at write time
        is exactly what should come back, no more."""
        persistence = SqliteMemoryPersistence(self.db_path)
        store = MemoryStore(persist=persistence)  # never_conflicts by default
        store.add("周二下午三点开会", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        store.add("周三下午三点开会", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        persistence.close()

        reloaded = SqliteMemoryPersistence(self.db_path)
        # Rehydrate into a store that WOULD auto-supersede on add() -- restore() must
        # not route through add(), so this store's aggressive detector never fires.
        fresh = MemoryStore(conflict_detector=lambda new, old: True)
        fresh.restore(reloaded.load_all())
        self.assertEqual(len(fresh.live()), 2)

    def test_reopening_the_same_path_reuses_the_schema(self):
        SqliteMemoryPersistence(self.db_path).close()
        second = SqliteMemoryPersistence(self.db_path)  # must not raise on existing schema
        second.close()


if __name__ == "__main__":
    unittest.main()
