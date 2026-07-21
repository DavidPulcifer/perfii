import json
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

from app.db import get_meta_db
from tests.helpers import FinanceAppTestCase


NODE_BINARY = shutil.which("node")


@unittest.skipIf(NODE_BINARY is None, "node is required for checkbox_range.js tests")
class CheckboxRangeStaticTests(unittest.TestCase):
    def test_shift_click_range_sets_inclusive_range_and_dispatches_events(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "app" / "static" / "checkbox_range.js"
        node_script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            class Event {{
              constructor(type, options) {{ this.type = type; this.bubbles = !!(options && options.bubbles); }}
            }}
            function makeScope() {{
              return {{
                boxes: [],
                querySelectorAll(selector) {{ return selector === 'input[type="checkbox"][data-range-checkbox]' ? this.boxes : []; }},
              }};
            }}
            function makeBox(scope, checked=false, disabled=false) {{
              return {{
                checked,
                disabled,
                events: [],
                closest(selector) {{ return selector === '[data-checkbox-range-scope]' ? scope : null; }},
                dispatchEvent(event) {{ this.events.push(event.type); return true; }},
              }};
            }}
            const window = {{}};
            vm.runInNewContext(fs.readFileSync({json.dumps(str(script_path))}, 'utf8'), {{ window, Event }});
            const api = window.FinanceCheckboxRanges._test;

            const scope = makeScope();
            const boxes = [makeBox(scope), makeBox(scope), makeBox(scope), makeBox(scope)];
            scope.boxes = boxes;
            boxes[3].checked = true;
            const applied = api.applyCheckboxRange(scope, boxes[0], boxes[3]);
            if (!applied) throw new Error('range was not applied');
            if (!boxes.every((box) => box.checked)) throw new Error('inclusive check range failed');
            if (boxes[1].events.join(',') !== 'input,change') throw new Error('middle box did not dispatch input/change');
            if (boxes[3].events.length !== 0) throw new Error('target natural click should not get synthetic events');

            boxes[0].checked = false;
            boxes[0].events = [];
            boxes[1].events = [];
            boxes[2].events = [];
            api.applyCheckboxRange(scope, boxes[3], boxes[0]);
            if (boxes.some((box) => box.checked)) throw new Error('reverse uncheck range failed');
            if (boxes[1].events.join(',') !== 'input,change') throw new Error('reverse range did not notify changed box');
            """
        )

        result = subprocess.run([NODE_BINARY, "-e", node_script], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_scoped_checkboxes_ignore_disabled_and_nested_panel_boxes(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "app" / "static" / "checkbox_range.js"
        node_script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            class Event {{ constructor(type) {{ this.type = type; }} }}
            const outer = {{ querySelectorAll() {{ return boxes; }} }};
            const inner = {{}};
            const boxes = [
              {{ disabled: false, closest() {{ return outer; }} }},
              {{ disabled: true, closest() {{ return outer; }} }},
              {{ disabled: false, closest() {{ return inner; }} }},
            ];
            const window = {{}};
            vm.runInNewContext(fs.readFileSync({json.dumps(str(script_path))}, 'utf8'), {{ window, Event }});
            const scoped = window.FinanceCheckboxRanges._test.scopedCheckboxes(outer);
            if (scoped.length !== 1 || scoped[0] !== boxes[0]) throw new Error('scope filtering failed');
            """
        )

        result = subprocess.run([NODE_BINARY, "-e", node_script], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


class CheckboxRangeTemplateTests(FinanceAppTestCase):
    def _select_user_in_client(self) -> None:
        row = get_meta_db().execute(
            "SELECT id FROM users WHERE LOWER(name)=LOWER(?) ORDER BY id LIMIT 1",
            ("test user",),
        ).fetchone()
        if row is None:
            row = get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(row["id"])

    def test_layout_loads_checkbox_range_script(self) -> None:
        self._select_user_in_client()
        response = self.client.get("/tx/")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("/static/checkbox_range.js?v=20260605-fin065", html)

    def test_transaction_bulk_table_opts_into_scoped_range_selection(self) -> None:
        self._select_user_in_client()
        response = self.client.get("/tx/")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="table table-sm transactions-table" data-checkbox-range-scope', html)
        self.assertIn('form="bulkForm" data-range-checkbox', html)
        self.assertNotIn('id="checkAll" title="Select all" data-range-checkbox', html)

    def test_import_review_and_reconciliation_templates_have_scoped_range_markers(self) -> None:
        root = Path(__file__).resolve().parents[1]
        import_review = (root / "app" / "templates" / "import_review.html").read_text()
        reconciliation = (root / "app" / "templates" / "reconciliation_session.html").read_text()
        import_js = (root / "app" / "static" / "import_review.js").read_text()

        self.assertIn('data-import-sort-section="exp" data-checkbox-range-scope', import_review)
        self.assertIn('data-import-sort-section="inc" data-checkbox-range-scope', import_review)
        self.assertGreaterEqual(import_review.count("data-range-checkbox"), 2)
        self.assertIn('class="table table-sm" data-checkbox-range-scope', reconciliation)
        self.assertIn('data-reconciliation-select-all', reconciliation)
        self.assertIn('name="transaction_id" value="{{ t.id }}" data-range-checkbox', reconciliation)
        self.assertIn('class="card mb-3" data-checkbox-range-scope', import_js)
        self.assertIn('name="ignore_tx[]" value="${esc(m.id)}" data-range-checkbox', import_js)
