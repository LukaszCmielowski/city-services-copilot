"""Live scoring uses model output, trained SLAs, and the fixture horizon."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


with patch.object(Path, "exists", return_value=False):
    spec = importlib.util.spec_from_file_location("city_services_live", Path(__file__).resolve().parents[1] / "main.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)


class LiveResultTests(unittest.TestCase):
    def predict(self, payload, request):
        config = {
            "api_token": "token", "endpoint_protocol": "kserve-v1",
            "endpoints": {"tabular_scoring": "https://model.example/predict"},
        }
        with patch.object(app, "APP_CONFIG", config), patch.object(
            app, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())
        ) as upstream:
            result = app.predict_request(request)
        return result, json.loads(upstream.call_args.args[0].data)

    def test_class_label_is_shown_without_a_fabricated_probability(self):
        result, body = self.predict({"predictions": [[1]]}, {
            "service": "Streetlight outage", "neighborhood": "Kensington",
            "channel": "Phone", "priority": "High",
        })
        self.assertEqual(body["instances"][0]["sla_days"], [6])
        self.assertEqual(result["mode"], "model")
        self.assertEqual(result["outcome"], "sla_miss")
        self.assertNotIn("risk", result)
        self.assertNotIn("days", result)
        missed, _ = self.predict({"predictions": [[0]]}, {"service": "Missed trash collection"})
        self.assertEqual(missed["outcome"], "within_sla")
        self.assertEqual(
            self.predict({"predictions": [[0]]}, {"service": "Missed trash collection"})[1]["instances"][0]["sla_days"],
            [3],
        )

    def test_fractional_prediction_becomes_a_probability(self):
        result, _ = self.predict({"predictions": [[0.82]]}, {"service": "Graffiti"})
        self.assertEqual(result["risk"], 82)
        self.assertEqual(result["outcome"], "sla_miss")
        labeled, _ = self.predict({"predictions": [{"label": 0, "probability": 0.27}]}, {"service": "Pothole"})
        self.assertEqual(labeled["risk"], 27)
        self.assertEqual(labeled["outcome"], "within_sla")

    def test_autogluon_class_probabilities_select_sla_miss_by_label(self):
        result, _ = self.predict({"predictions": [{"1": 0.82, "0": 0.18}]}, {"service": "Streetlight outage"})
        self.assertEqual(result["risk"], 82)
        self.assertEqual(result["outcome"], "sla_miss")
        self.assertEqual(result["sla_days"], 6)
        self.assertNotIn("days", result)
        result, _ = self.predict({"predictions": [{"0": 0.91, "1": 0.09}]}, {})
        self.assertEqual(result["risk"], 9)
        self.assertEqual(result["outcome"], "within_sla")

    def test_explicit_probabilities_include_zero_and_one_but_labels_do_not(self):
        for probability in (0, 1):
            result, _ = self.predict({"predictions": [{"0": 1-probability, "1": probability}]}, {})
            self.assertEqual(result["risk"], 100*probability)
            labeled, _ = self.predict({"predictions": [probability]}, {})
            self.assertNotIn("risk", labeled)

    def test_invalid_class_probabilities_fail_instead_of_displaying_risk(self):
        for row in ({"0": .8, "1": .8}, {"0": -.1, "1": 1.1},
                    {"0": 0, "1": float("nan")}, {"0": False, "1": True}):
            with self.subTest(row=row), self.assertRaises(app.EndpointError):
                self.predict({"predictions": [row]}, {})

    def test_forecast_uses_history_for_scoring_but_returns_only_relative_values(self):
        config = {"api_token": "token", "endpoints": {"timeseries_scoring": "https://model.example/predict"}}
        with patch.object(app, "APP_CONFIG", config), patch.object(
            app, "urlopen", return_value=io.BytesIO(json.dumps({"predictions": list(range(10, 17))}).encode())
        ) as upstream:
            result = app.forecast_volume({"service": "Illegal dumping", "neighborhood": "Kensington"})
        body = json.loads(upstream.call_args.args[0].data)
        self.assertIn("timestamp", body["instances"][0])
        self.assertEqual(result, {"points": [{"value": i} for i in range(10, 17)], "total": 91, "source": "model"})

    def test_sample_guidance_keeps_links_and_names_the_selected_request(self):
        result = app.retrieve_guidance({
            "question": "How should an operator route an illegal-dumping report?",
            "service": "Illegal dumping", "neighborhood": "Kensington",
        })
        self.assertEqual(result["grounding"], "sample")
        self.assertIn("Illegal dumping request in Kensington", result["answer"])
        self.assertTrue(result["sources"][0]["url"].startswith("https://"))
        other = app.retrieve_guidance({
            "question": "How should an operator route an illegal-dumping report?",
            "service": "Streetlight outage", "neighborhood": "Kensington",
        })
        self.assertIn("Streetlight outage request in Kensington", other["answer"])
        self.assertNotIn("safe-to-share photos", other["answer"])

    def test_sample_mode_label_and_forecast_name_the_illustration(self):
        self.assertEqual(app.demo_mode_label(), "Sample results")
        with patch.object(app, "APP_CONFIG", {"endpoints": {"tabular_scoring": "https://m", "responses": "https://r"}}):
            self.assertEqual(app.demo_mode_label(), "Live models")
        with patch.object(app, "APP_CONFIG", {"endpoints": {"responses": "https://r"}}):
            self.assertEqual(app.demo_mode_label(), "Live guidance")
        result = app.forecast_volume({"service": "Pothole", "neighborhood": "Center City"})
        self.assertEqual(result["source"], "sample")
        self.assertEqual(len(result["points"]), 7)
        self.assertNotIn("origin", result)
        self.assertNotIn("baseline", result)

    def test_page_embeds_the_single_request_catalog_without_an_editor(self):
        page = app.demo_page_html()
        start = page.index('<script id="sample-requests" type="application/json">')
        embedded = page[start:].split('>', 1)[1].split('</script>', 1)[0]
        self.assertEqual(json.loads(embedded), app.SAMPLE_REQUESTS)
        self.assertEqual(len(app.SAMPLE_REQUESTS), 5)
        self.assertNotIn('request-editor', page)
        self.assertNotIn('__SAMPLE_REQUESTS__', page)
        with patch.object(app, "SAMPLE_REQUESTS", [{"id": "</script><script>"}]):
            page = app.demo_page_html()
        self.assertNotIn('</script><script>', page)
        self.assertIn('\\u003c/script>', page)

    def test_sample_scoring_still_returns_an_illustrative_risk(self):
        result = app.predict_request({
            "service": "Pothole", "neighborhood": "Center City", "channel": "Web", "priority": "Low",
        })
        self.assertEqual(result["mode"], "sample")
        self.assertIn("risk", result)
        self.assertNotIn("days", result)
        self.assertEqual(result["sla_days"], 4)


if __name__ == "__main__":
    unittest.main()
