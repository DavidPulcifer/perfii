from pathlib import Path
from unittest import TestCase


class ImportUndoStaticTests(TestCase):
    def test_layout_renders_import_undo_as_post_text_link(self) -> None:
        template = Path("app/templates/layout.html").read_text()

        self.assertIn("msg is mapping", template)
        self.assertIn("url_for('imports.undo_last_import')", template)
        self.assertIn('name="import_session_id"', template)
        self.assertIn("btn btn-link alert-link", template)
