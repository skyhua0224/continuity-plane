"""Native-probe accounting must not mistake mentions for compaction events."""

import importlib.util
import json
import unittest
from pathlib import Path


class NativeProbeTests(unittest.TestCase):
    def load(self):
        path = Path(__file__).parents[1] / "tools/run_codex_native_hook_probe.py"
        self.assertTrue(path.exists(), "native probe is missing")
        spec = importlib.util.spec_from_file_location("native_probe", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_only_host_compaction_events_count(self):
        probe = self.load()
        false_event = {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "I considered compaction"}}}
        self.assertFalse(probe.summarize([false_event])["native_compaction_observed"])
        real_event = {"method": "item/completed", "params": {"item": {"type": "contextCompaction"}}}
        self.assertTrue(probe.summarize([false_event, real_event])["native_compaction_observed"])

    def test_receipt_excludes_raw_messages_and_commands(self):
        probe = self.load()
        events = [
            {"method": "hook/completed", "params": {"run": {"eventName": "sessionStart", "status": "completed", "entries": [{"kind": "context", "text": "private-context"}]}}},
            {"method": "item/completed", "params": {"item": {"type": "commandExecution", "command": "continuity context lookup --query private-query", "aggregatedOutput": "private-output"}}},
        ]
        result = probe.summarize(events)
        self.assertEqual(result["lookup_calls"], 1)
        self.assertEqual(result["hook_counts"], {"sessionStart:completed": 1})
        self.assertNotIn("private-", json.dumps(result))

    def test_thread_overrides_do_not_quote_keys_or_change_provider(self):
        probe = self.load()
        result = probe.scoped_overrides({"plugins": {"sample@local": {}}, "mcp_servers": {"sample_db": {}}}, {})
        self.assertIn("mcp_servers.sample_db.enabled", result)
        self.assertIn("plugins.sample@local.enabled", result)
        self.assertNotIn("model", result)
        self.assertNotIn("model_provider", result)

    def test_installed_probe_keeps_only_continuity_plugins(self):
        probe = self.load()
        config = {"plugins": {"continuity-plane@continuity-plane": {}, "other@local": {}}}
        result = probe.scoped_overrides(config, {}, keep_continuity=True)
        self.assertNotIn("plugins.continuity-plane@continuity-plane.enabled", result)
        self.assertFalse(result["plugins.other@local.enabled"])

    def test_search_mcp_call_counts_as_lookup_without_saving_arguments(self):
        probe = self.load()
        result = probe.summarize([{"method": "item/completed", "params": {"item": {
            "type": "mcpToolCall", "server": "continuity-search", "tool": "continuity_context_lookup",
            "arguments": {"query": "private-query"},
        }}}])
        self.assertEqual(result["lookup_calls"], 1)
        self.assertNotIn("private-query", json.dumps(result))

    def test_compaction_event_is_not_a_ready_turn_boundary(self):
        probe = self.load()
        class FakeHost:
            def __init__(self):
                self.events = [{"method": "thread/compacted", "params": {}}]
                self.waited = False
            def until(self, predicate):
                event = {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}
                self.waited = True
                self.events.append(event)
                assert predicate(event)
                return event
        host = FakeHost()
        probe.wait_for_compaction(host, 0)
        self.assertTrue(host.waited)


if __name__ == "__main__":
    unittest.main()
