import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent


def module_assignments(tree, names):
    return [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id in names
            for target in node.targets
        )
    ]


def load_width_helper():
    source = (ROOT / "modules" / "registros.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = module_assignments(
        tree,
        {
            "BAUL_COLUMN_WIDTH_CM",
            "PX_PER_CM",
            "EXCEL_CHAR_PX",
            "EXCEL_COLUMN_PADDING_PX",
        },
    )
    nodes += [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "cm_to_excel_width"
    ]
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "registros", "exec"), namespace)
    return namespace


class BaulCommandTests(unittest.TestCase):
    def test_five_centimeter_column_width(self):
        namespace = load_width_helper()
        width = namespace["cm_to_excel_width"](5)
        self.assertGreaterEqual(width, 25)
        self.assertLessEqual(width, 28)

    def test_commands_are_registered(self):
        tree = ast.parse(
            (ROOT / "modules" / "registros.py").read_text(encoding="utf-8")
        )
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
        }
        self.assertIn("exportar_baul", names)
        self.assertIn("purgar_baul", names)

    def test_purge_drops_registros_table(self):
        source = (ROOT / "core" / "database.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "purge_baul"
        )
        segment = ast.get_source_segment(source, function)
        self.assertIn("DROP TABLE IF EXISTS registros", segment)
