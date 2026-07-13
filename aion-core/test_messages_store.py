import json
import os
import tempfile
import unittest

import build_messages_db
import messages_store


def _row(source, thread_id, thread_display, sender, recipient, ts_utc,
         date_est, time_est, body, participants=None):
    return {
        "source": source,
        "thread_id": thread_id,
        "thread_display": thread_display,
        "sender": sender,
        "recipient": recipient,
        "ts_utc": ts_utc,
        "date_est": date_est,
        "time_est": time_est,
        "body": body,
        "post_death": 0,
        "participants": json.dumps(participants or []),
    }


SAMPLE_ROWS = [
    # Brian ↔ Chris, two-sided, Sep 19 2025 (EDT)
    _row("brian_fb", "111", "Chef and Chris", "Chris Tierney", "Brian Wallace",
         1_700_000_000_000, "2025-09-19", "10:52 PM EDT",
         "can you handle the DNS records", ["Brian Wallace", "Chris Tierney"]),
    _row("brian_fb", "111", "Chef and Chris", "Brian Wallace", "Chris Tierney",
         1_700_000_060_000, "2025-09-19", "10:53 PM EDT",
         "yeah all of those types of DNS records", ["Brian Wallace", "Chris Tierney"]),
    # Jenn thread, custody topic, Jan 2016 (EST)
    _row("jenn", "222", "Jennifer Frotten ↔ Hayley Carey", "Jennifer Frotten",
         "Hayley Carey", 1_452_890_494_000, "2016-01-15", "03:41 PM EST",
         "For two weeks or like 10 days about custody",
         ["Jennifer Frotten", "Hayley Carey"]),
    _row("jenn", "222", "Jennifer Frotten ↔ Hayley Carey", "Hayley Carey",
         "Jennifer Frotten", 1_452_890_500_000, "2016-01-15", "03:42 PM EST",
         "hang in there", ["Jennifer Frotten", "Hayley Carey"]),
]


class TestMessagesStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        build_messages_db.create_db(cls.db_path, list(SAMPLE_ROWS))
        cls._orig_db = messages_store.DB_PATH
        messages_store.DB_PATH = cls.db_path
        messages_store._NAME_TOKENS_CACHE = None

    @classmethod
    def tearDownClass(cls):
        messages_store.DB_PATH = cls._orig_db
        messages_store._NAME_TOKENS_CACHE = None
        try:
            os.remove(cls.db_path)
        except OSError:
            pass

    def test_fts_body_search_finds_topic(self):
        rows = messages_store.search(query="custody")
        self.assertTrue(rows)
        self.assertTrue(any("custody" in r["body"].lower() for r in rows))

    def test_search_by_date(self):
        rows = messages_store.search(on_date="2025-09-19")
        self.assertEqual({r["thread_id"] for r in rows}, {"111"})

    def test_name_routing_whole_word(self):
        rows = messages_store.search(name="chris")
        self.assertTrue(rows)
        self.assertTrue(all(r["thread_id"] == "111" for r in rows))

    def test_get_thread_is_chronological(self):
        rows = messages_store.get_thread("111")
        self.assertEqual([r["ts_utc"] for r in rows],
                         sorted(r["ts_utc"] for r in rows))

    def test_format_thread_blocks_render(self):
        rows = messages_store.search(on_date="2025-09-19")
        blocks = messages_store.format_thread_blocks(rows)
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertIn("Thread: Chef and Chris", block)
        # Human timestamp with AM/PM + Eastern tz, and from → to.
        self.assertIn("[2025-09-19 10:53 PM EDT]", block)
        self.assertIn("Brian Wallace → Chris Tierney:", block)

    def test_recent_threads_newest_first(self):
        threads = messages_store.recent_threads()
        self.assertEqual(threads[0]["thread_id"], "111")  # 2025 > 2016

    def test_known_name_tokens_excludes_primary_people(self):
        toks = messages_store.known_name_tokens()
        self.assertIn("tierney", toks)
        self.assertNotIn("brian", toks)
        self.assertNotIn("jennifer", toks)


if __name__ == "__main__":
    unittest.main()
