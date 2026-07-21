from pathlib import Path
import unittest

from app.blueprints.invest import _investment_graph_range


class InvestmentGraphRangeTests(unittest.TestCase):
    def test_defaults_to_full_graph_when_history_is_less_than_one_year(self) -> None:
        graph_range = _investment_graph_range(
            [
                {"x": "2026-01-15", "y": 1000},
                {"x": "2026-05-05", "y": 1200},
            ],
            [
                {"x": "2026-02-01", "y": 250},
            ],
        )

        self.assertEqual(
            graph_range,
            {
                "fullMin": "2026-01-15",
                "fullMax": "2026-05-05",
                "defaultMin": "2026-01-15",
                "defaultMax": "2026-05-05",
            },
        )

    def test_defaults_to_latest_data_year_to_date_when_history_spans_a_year(self) -> None:
        graph_range = _investment_graph_range(
            [
                {"x": "2024-12-31", "y": 1000},
                {"x": "2026-05-05", "y": 1400},
            ],
            [
                {"x": "2025-03-01", "y": 250},
            ],
        )

        self.assertEqual(graph_range["fullMin"], "2024-12-31")
        self.assertEqual(graph_range["fullMax"], "2026-05-05")
        self.assertEqual(graph_range["defaultMin"], "2026-01-01")
        self.assertEqual(graph_range["defaultMax"], "2026-05-05")

    def test_ignores_blank_or_invalid_point_dates(self) -> None:
        graph_range = _investment_graph_range(
            [
                {"x": "", "y": 1000},
                {"x": "not-a-date", "y": 1100},
            ],
            [
                {"x": "2026-04-01", "y": 250},
            ],
        )

        self.assertEqual(graph_range["fullMin"], "2026-04-01")
        self.assertEqual(graph_range["defaultMin"], "2026-04-01")


class InvestmentGraphTemplateTests(unittest.TestCase):
    def test_chart_uses_x_axis_default_range_and_x_only_zoom(self) -> None:
        template = Path("app/templates/invest.html").read_text()

        self.assertIn("const graphRange", template)
        self.assertIn("min: graphRange.defaultMin || undefined", template)
        self.assertIn("max: graphRange.defaultMax || undefined", template)
        self.assertIn("zoomMode: 'x'", template)
        self.assertIn("panMode: 'x'", template)
        self.assertNotIn("mode: 'xy'", template)

    def test_contribution_dataset_uses_step_line_without_changing_valuation_dataset(self) -> None:
        template = Path("app/templates/invest.html").read_text()

        self.assertIn("contribStepped: 'before'", template)
        self.assertIn("if (opts.stepped !== undefined) ds.stepped = opts.stepped;", template)
        self.assertIn("stepped: settings.contribStepped", template)

        valuation_block = template[
            template.index("const valuationDS = buildDataset("):
            template.index("const contribDS = buildDataset(")
        ]
        self.assertNotIn("stepped:", valuation_block)

    def test_mobile_layout_orders_graph_before_valuation_list(self) -> None:
        template = Path("app/templates/invest.html").read_text()

        self.assertIn('class="col-lg-5 order-2 order-lg-1"', template)
        self.assertIn('class="col-lg-7 order-1 order-lg-2"', template)
