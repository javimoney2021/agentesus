import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent
MAX_FILE = 25 * 1024 * 1024
MAX_TOTAL = 50 * 1024 * 1024


class FakeAttachment:
    def __init__(self, filename, size):
        self.filename = filename
        self.size = size


def load_attachment_validator():
    tree = ast.parse((ROOT / "modules" / "posts.py").read_text(encoding="utf-8"))
    nodes = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "AttachmentLimitError"
        )
        or (
            isinstance(node, ast.FunctionDef)
            and node.name == "validate_attachments"
        )
    ]
    namespace = {
        "POST_MAX_ATTACHMENTS": 10,
        "POST_MAX_FILE_BYTES": MAX_FILE,
        "POST_MAX_TOTAL_BYTES": MAX_TOTAL,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "posts", "exec"), namespace)
    return namespace


class PostAttachmentLimitTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_attachment_validator()
        self.validate = self.namespace["validate_attachments"]
        self.error = self.namespace["AttachmentLimitError"]

    def test_accepts_attachments_within_limits(self):
        total = self.validate([
            FakeAttachment("a.png", 10 * 1024 * 1024),
            FakeAttachment("b.png", 10 * 1024 * 1024),
        ])
        self.assertEqual(total, 20 * 1024 * 1024)

    def test_rejects_single_file_over_limit(self):
        with self.assertRaises(self.error):
            self.validate([FakeAttachment("grande.png", MAX_FILE + 1)])

    def test_rejects_total_over_limit(self):
        with self.assertRaises(self.error):
            self.validate([
                FakeAttachment("a.png", 20 * 1024 * 1024),
                FakeAttachment("b.png", 20 * 1024 * 1024),
                FakeAttachment("c.png", 20 * 1024 * 1024),
            ])

    def test_rejects_too_many_attachments(self):
        with self.assertRaises(self.error):
            self.validate([
                FakeAttachment(f"{index}.png", 1) for index in range(11)
            ])


class SchemaSafetyTests(unittest.TestCase):
    def test_init_db_does_not_create_schema(self):
        tree = ast.parse((ROOT / "core" / "database.py").read_text(encoding="utf-8"))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "init_db"
        )
        source = ast.get_source_segment(
            (ROOT / "core" / "database.py").read_text(encoding="utf-8"),
            function,
        )
        self.assertNotIn("CREATE TABLE", source)
        self.assertNotIn("ALTER TABLE", source)

    def test_migration_declares_required_indexes(self):
        sql = (ROOT / "migrations" / "0001_initial.sql").read_text(encoding="utf-8")
        for fragment in (
            "event_registrations (guild_id, user_id)",
            "event_users (guild_id, updated_at DESC)",
        ):
            self.assertIn(fragment, sql)
