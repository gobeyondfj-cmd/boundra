from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import hosted_mcp


class HostedToolSchemaTests(unittest.TestCase):
    def test_every_argument_has_a_description_and_every_tool_has_hints(self):
        tools = hosted_mcp.server._tool_manager.list_tools()
        self.assertEqual(len(tools), 14)
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertIsNotNone(tool.annotations)
                self.assertIsNotNone(tool.annotations.read_only_hint)
                self.assertIsNotNone(tool.annotations.destructive_hint)
                self.assertIsNotNone(tool.annotations.idempotent_hint)
                self.assertIsNotNone(tool.annotations.open_world_hint)
                for name, schema in tool.parameters.get("properties", {}).items():
                    self.assertTrue(schema.get("description"), f"{tool.name}.{name} lacks a description")

    def test_read_only_tools_are_marked_accurately(self):
        expected = {
            "get_evaluation", "get_policy", "get_spend_report", "get_audit_trail",
            "get_policy_versions", "list_policy_requests", "list_templates", "render_template",
        }
        tools = hosted_mcp.server._tool_manager.list_tools()
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertEqual(tool.annotations.read_only_hint, tool.name in expected)
                self.assertEqual(tool.annotations.destructive_hint, tool.name == "revoke_policy")


if __name__ == "__main__":
    unittest.main()
