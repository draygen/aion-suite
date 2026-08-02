import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import aion_engine as engine


class TestTimeHelpers(unittest.TestCase):
    def test_humanize_gap_scales_units(self):
        self.assertEqual(engine.humanize_gap(45), "45s")
        self.assertEqual(engine.humanize_gap(600), "10m")
        self.assertEqual(engine.humanize_gap(3600), "1h")
        self.assertEqual(engine.humanize_gap(3900), "1h 5m")
        self.assertEqual(engine.humanize_gap(90000), "1d 1h")

    def test_humanize_gap_clamps_negative(self):
        self.assertEqual(engine.humanize_gap(-30), "0s")

    def test_parse_ts_normalizes_to_utc(self):
        naive = engine.parse_ts("2026-07-26T12:00:00")
        self.assertEqual(naive.tzinfo, timezone.utc)
        self.assertIsNone(engine.parse_ts("not a timestamp"))
        self.assertIsNone(engine.parse_ts(None))


class TestBuildMessages(unittest.TestCase):
    """The model rejects system-role messages, so every message built here must
    be user/assistant only, ending on the augmented user turn."""

    def setUp(self):
        patcher = patch.object(engine, "get_facts", return_value=[])
        self.addCleanup(patcher.stop)
        patcher.start()
        mem = patch.object(engine, "_MEMORY_AVAILABLE", False)
        self.addCleanup(mem.stop)
        mem.start()
        gpt = patch.object(engine, "chatgpt_store", None)
        self.addCleanup(gpt.stop)
        gpt.start()
        # These tests assert on turn structure; persona injection is covered
        # separately in TestPersona. Disable it here so a leading system message
        # doesn't shift every index.
        pers = patch.dict(engine.CONFIG, {"persona_as_system_message": False})
        self.addCleanup(pers.stop)
        pers.start()

    def test_appends_user_turn(self):
        prior = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}]
        msgs = engine.build_messages(prior, "what's up")
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(msgs[-1]["content"], "what's up")

    def test_drops_leading_assistant_turns(self):
        prior = [{"role": "assistant", "content": "orphan"}, {"role": "user", "content": "hi"}]
        msgs = engine.build_messages(prior, "next")
        self.assertEqual(msgs[0]["role"], "user")

    def test_windows_to_context_limit(self):
        prior = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)}
                 for i in range(100)]
        msgs = engine.build_messages(prior, "latest")
        self.assertLessEqual(len(msgs), engine.CONTEXT_WINDOW)
        self.assertEqual(msgs[-1]["content"], "latest")

    def test_skips_malformed_prior_turns(self):
        prior = [{"role": "user", "content": "keep"},
                 {"role": "system", "content": "drop"},
                 {"role": "assistant", "content": ""}]
        msgs = engine.build_messages(prior, "now")
        self.assertEqual([m["content"] for m in msgs], ["keep", "now"])

    def test_does_not_mutate_caller_history(self):
        prior = [{"role": "user", "content": "hi"}]
        engine.build_messages(prior, "second")
        self.assertEqual(prior, [{"role": "user", "content": "hi"}])

    def test_augmented_overrides_final_turn(self):
        msgs = engine.build_messages([], "/msg jenn", augmented="ARCHIVE CONTEXT")
        self.assertEqual(msgs[-1]["content"], "ARCHIVE CONTEXT")

    def test_continuity_is_folded_into_user_turn(self):
        now = engine.utc_now()
        msgs = engine.build_messages([], "hello", include_continuity=True,
                                     last_seen=now - timedelta(hours=2), gap_seconds=7200)
        content = msgs[-1]["content"]
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertIn("2h", content)
        self.assertIn("Brian: hello", content)

    def test_no_context_leaves_turn_untouched(self):
        msgs = engine.build_messages([], "plain question")
        self.assertEqual(msgs[-1]["content"], "plain question")


class TestFactsBlock(unittest.TestCase):
    def test_retrieval_failure_degrades_to_empty(self):
        with patch.object(engine, "get_facts", side_effect=RuntimeError("brain down")), \
             patch.object(engine, "_MEMORY_AVAILABLE", False), \
             patch.object(engine, "chatgpt_store", None):
            self.assertEqual(engine.facts_block("anything"), "")

    def test_facts_are_labelled_for_the_model(self):
        with patch.object(engine, "get_facts", return_value=["Brian has a 5070"]), \
             patch.object(engine, "_MEMORY_AVAILABLE", False), \
             patch.object(engine, "chatgpt_store", None):
            self.assertIn("Relevant facts:", engine.facts_block("gpu"))


class TestSessionLastSeen(unittest.TestCase):
    """Continuity must be measured across threads — a new thread has no history
    of its own, which is exactly when the gap matters."""

    def test_prefers_explicit_departure_event(self):
        departed = engine.utc_now() - timedelta(hours=3)
        store = MagicMock()
        store.last_departure.return_value = departed
        last_seen, gap = engine.session_last_seen(store)
        self.assertEqual(last_seen, departed)
        self.assertAlmostEqual(gap, 10800, delta=5)
        store.recent_turns.assert_not_called()

    def test_falls_back_to_last_turn(self):
        ts = (engine.utc_now() - timedelta(minutes=30)).isoformat()
        store = MagicMock()
        store.last_departure.return_value = None
        store.recent_turns.return_value = [{"role": "user", "content": "x", "ts": ts}]
        last_seen, gap = engine.session_last_seen(store)
        self.assertIsNotNone(last_seen)
        self.assertAlmostEqual(gap, 1800, delta=5)

    def test_reuses_supplied_history_instead_of_querying(self):
        ts = (engine.utc_now() - timedelta(minutes=5)).isoformat()
        store = MagicMock()
        store.last_departure.return_value = None
        engine.session_last_seen(store, prior=[{"role": "user", "content": "x", "ts": ts}])
        store.recent_turns.assert_not_called()

    def test_no_history_reports_nothing(self):
        store = MagicMock()
        store.last_departure.return_value = None
        store.recent_turns.return_value = []
        self.assertEqual(engine.session_last_seen(store), (None, None))

    def test_first_conversation_wording(self):
        block = engine.continuity_block(engine.utc_now(), None, None)
        self.assertIn("No prior session on record", block)


class TestMessageArchiveHelpers(unittest.TestCase):
    def test_history_label_stays_short(self):
        self.assertEqual(engine.msg_history_label("jenn birthday"), "/msg jenn birthday")

    def test_context_turn_carries_threads_and_question(self):
        turn = engine.msg_context_turn("birthday", ["THREAD A", "THREAD B"])
        self.assertIn("THREAD A", turn)
        self.assertIn("THREAD B", turn)
        self.assertIn('about "birthday"', turn)

    def test_search_returns_empty_without_archive(self):
        with patch.object(engine, "messages_store", None):
            self.assertEqual(engine.search_messages("anything"), [])
            self.assertFalse(engine.messages_available())


class TestStoreSourceScoping(unittest.TestCase):
    """CLI and API share the Store; events must stay attributable to each."""

    def _store(self, source):
        store = engine.Store.__new__(engine.Store)
        store.ok = True
        store.user_id = 1
        store.username = "brian"
        store.source = source
        store._log_event = MagicMock()
        return store

    def test_log_tags_its_own_source(self):
        store = self._store("api")
        store.log("session_start", "app:abc", "arrived")
        self.assertEqual(store._log_event.call_args.kwargs["source"], "api")

    def test_default_source_is_cli(self):
        store = engine.Store.__new__(engine.Store)
        self.assertEqual(engine.Store.__init__.__defaults__, ("cli",))


class TestPersona(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(engine, "get_facts", return_value=[]),
                  patch.object(engine, "_MEMORY_AVAILABLE", False),
                  patch.object(engine, "chatgpt_store", None)):
            self.addCleanup(p.stop)
            p.start()

    def test_persona_prepended_as_system_when_enabled(self):
        with patch.dict(engine.CONFIG, {"persona": "BE AION", "persona_as_system_message": True}):
            msgs = engine.build_messages([{"role": "user", "content": "hi"}], "yo")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], "BE AION")
        self.assertEqual(msgs[-1]["content"], "yo")

    def test_persona_omitted_when_flag_off(self):
        with patch.dict(engine.CONFIG, {"persona": "BE AION", "persona_as_system_message": False}):
            msgs = engine.build_messages([], "hi")
        self.assertNotIn("system", [m["role"] for m in msgs])

    def test_persona_omitted_when_empty(self):
        with patch.dict(engine.CONFIG, {"persona": "", "persona_as_system_message": True}):
            msgs = engine.build_messages([], "hi")
        self.assertNotIn("system", [m["role"] for m in msgs])

    def test_shipped_config_enables_persona(self):
        # The real config.py must actually turn this on, or the app reverts to
        # the generic helpdesk voice this whole change exists to kill.
        self.assertTrue(engine.CONFIG.get("persona"))
        self.assertTrue(engine.CONFIG.get("persona_as_system_message"))


class TestActionDelegation(unittest.TestCase):
    """chat() short-circuits a machine-action request to Hermes before the LLM,
    and stays out of the way for ordinary chat."""

    def _store(self):
        s = MagicMock()
        s.thread_history.return_value = []
        return s

    def test_action_delegates_and_skips_llm(self):
        store = self._store()
        with patch.object(engine, "maybe_delegate_action", return_value="DISK: 87% used"), \
             patch.object(engine, "ask_llm_chat", side_effect=AssertionError("LLM must not run")):
            reply = engine.chat("s1", "check my disk space", store=store)
        self.assertEqual(reply, "DISK: 87% used")
        store.save_turn.assert_called_once_with("s1", "check my disk space", "DISK: 87% used")

    def test_non_action_goes_to_llm(self):
        store = self._store()
        with patch.object(engine, "maybe_delegate_action", return_value=None), \
             patch.object(engine, "ask_llm_chat", return_value="hey"):
            reply = engine.chat("s1", "hi", store=store)
        self.assertEqual(reply, "hey")

    def test_delegation_disabled_returns_none(self):
        with patch.dict(engine.CONFIG, {"hermes_enabled": False}):
            self.assertIsNone(engine.maybe_delegate_action("check my disk space"))

    def test_hermes_down_falls_back_to_chat(self):
        with patch.dict(engine.CONFIG, {"hermes_enabled": True}), \
             patch("tools.looks_like_machine_action", return_value=True), \
             patch("tools.hermes_available", return_value=False):
            self.assertIsNone(engine.maybe_delegate_action("check my disk space"))


class TestCleanReply(unittest.TestCase):
    def test_strips_plain_and_bold_labels(self):
        self.assertEqual(engine.clean_reply("Response: hey"), "hey")
        self.assertEqual(engine.clean_reply("**Response:**\n\nHi Brian"), "Hi Brian")
        self.assertEqual(engine.clean_reply("**Reply:** test"), "test")
        self.assertEqual(engine.clean_reply("AION: yo"), "yo")
        self.assertEqual(engine.clean_reply("  Answer:\nok"), "ok")

    def test_leaves_ordinary_text_untouched(self):
        self.assertEqual(engine.clean_reply("Vim. Period."), "Vim. Period.")
        # word boundary: "response to..." is prose, not a label
        self.assertTrue(engine.clean_reply("response to your question is fine")
                        .startswith("response to"))

    def test_handles_empty(self):
        self.assertEqual(engine.clean_reply(""), "")


if __name__ == "__main__":
    unittest.main()
