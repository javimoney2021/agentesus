import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "modules" / "registro_eventos.py"


def function_source(tree, source, name):
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )
    return ast.get_source_segment(source, function) or ""


class ParticipantsRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.source = MODULE.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_pagination_uses_a_single_edit_request(self):
        segment = function_source(self.tree, self.source, "change_page")
        self.assertIn("interaction.response.edit_message", segment)
        self.assertNotIn("interaction.edit_original_response", segment)

    def test_show_participants_sends_a_limit_notice(self):
        segment = function_source(self.tree, self.source, "show_participants")
        self.assertIn("send_rate_limit_notice", segment)

    def test_notice_helper_handles_discord_errors(self):
        segment = function_source(self.tree, self.source, "send_rate_limit_notice")
        self.assertIn("RATE_LIMIT_NOTICE", segment)
        self.assertIn("discord.HTTPException", segment)
        self.assertIn("discord.InteractionError", segment)
