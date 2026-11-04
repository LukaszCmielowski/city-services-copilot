"""Verify the starter-kit JSON Responses contract without cluster access."""
import importlib.util
import io
import json
import ssl
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from unittest.mock import patch


# Import with an absent config so tests never read a developer's credentials.
with patch.object(Path, "exists", return_value=False):
    spec = importlib.util.spec_from_file_location("city_services", Path(__file__).resolve().parents[1] / "main.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)


class AutoRAGInvocationTests(unittest.TestCase):
    def test_gateway_certificate_compatibility_preserves_peer_and_hostname_verification(self):
        context = ssl.create_default_context()
        context.verify_flags |= ssl.VERIFY_X509_STRICT
        config = {"api_token": "key", "endpoints": {"responses": "https://agent.example"},
                  "endpoint_tls_strict": {"responses": False}}
        with patch.object(app, "APP_CONFIG", config), patch.object(app.ssl, "create_default_context", return_value=context), \
                patch.object(app, "urlopen", return_value=io.BytesIO(b"{}")) as upstream:
            app.call_endpoint("responses", {})
        actual = upstream.call_args.kwargs["context"]
        self.assertTrue(actual.check_hostname)
        self.assertEqual(actual.verify_mode, ssl.CERT_REQUIRED)
        self.assertFalse(actual.verify_flags & ssl.VERIFY_X509_STRICT)

    def invoke(self, config, response, question="How do I report illegal dumping?"):
        with patch.object(app, "APP_CONFIG", config), patch.object(
            app, "urlopen", return_value=io.BytesIO(json.dumps(response).encode())
        ) as upstream:
            result = app.retrieve_guidance({"question": question})
        return result, upstream.call_args.args[0]

    def test_starter_kit_uses_api_key_and_preserves_responses_citations(self):
        config = {
            "api_token": "shared-token",
            "endpoint_tokens": {"responses": "agent-key"},
            "endpoints": {"responses": "https://agent.example/v1/responses"},
            "responses_auth_header": "X-Api-Key",
        }
        response = {"output": [{"type": "message", "content": [{
            "type": "output_text", "text": json.dumps({"answer": "Contact 311.", "source_urls": []}), "annotations": [{
                "type": "url_citation", "url": "https://www.phila.gov/311/", "title": "311"
            }]
        }]}]}
        result, request = self.invoke(config, response)
        self.assertEqual(request.get_header("X-api-key"), "agent-key")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIn("How do I report illegal dumping?", json.loads(request.data)["input"])
        self.assertIn("source_urls", json.loads(request.data)["input"])
        self.assertFalse(json.loads(request.data)["stream"])
        self.assertEqual(result["answer"], "Contact 311.")
        self.assertEqual(result["grounding"], "citations")
        self.assertEqual(result["sources"][0]["url"], "https://www.phila.gov/311/")


    def test_current_starter_kit_answer_without_citations(self):
        config = {"api_token": "key", "endpoints": {"responses": "https://agent.example/v1/responses"}}
        result, _ = self.invoke(config, {
            "output_text": json.dumps({"answer": "Contact 311.", "source_urls": []}), "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps({"answer": "Contact 311.", "source_urls": []})}
            ]}],
        })
        self.assertEqual(result, {"answer": "Contact 311.", "sources": [], "grounding": "none"})

    def test_policy_references_are_deduplicated_and_limited_to_the_corpus(self):
        url = "https://www.phila.gov/311/"
        config = {"api_token": "key", "endpoints": {"responses": "https://agent.example/v1/responses"}}
        body = {"answer": "Contact 311.", "source_urls": [url, url, "https://invented.example/policy", "javascript:alert(1)", 123]}
        result, request = self.invoke(config, {"output_text": "```json\n" + json.dumps(body) + "\n```"})
        self.assertEqual(result["answer"], "Contact 311.")
        self.assertEqual(result["grounding"], "references")
        self.assertEqual([source["url"] for source in result["sources"]], [url])
        self.assertIn("retrieved documents used for the answer", json.loads(request.data)["input"])

    def test_policy_does_not_substitute_bundled_sources_for_missing_references(self):
        config = {"api_token": "key", "endpoints": {"responses": "https://agent.example/v1/responses"}}
        for urls in ([], ["https://invented.example/policy"], "https://www.phila.gov/311/"):
            with self.subTest(urls=urls):
                result, _ = self.invoke(config, {"output_text": json.dumps({"answer": "Verify the routing.", "source_urls": urls})})
                self.assertEqual(result["sources"], [])
                self.assertEqual(result["grounding"], "none")

    def test_invalid_live_policy_answers_are_reported_without_sample_fallback(self):
        config = {"api_token": "key", "endpoints": {"responses": "https://agent.example/v1/responses"}}
        for text in ("not JSON", "[]", "{}", '{"answer":123}', '{"answer":"  "}'):
            with self.subTest(text=text), self.assertRaises(app.EndpointError):
                self.invoke(config, {"output_text": text})

    def test_autorag_auth_override_does_not_change_automl_auth(self):
        config = {
            "api_token": "scoring-token", "responses_auth_header": "X-Api-Key",
            "endpoints": {"tabular_scoring": "https://scoring.example/predict"},
        }
        with patch.object(app, "APP_CONFIG", config), patch.object(app, "urlopen", return_value=io.BytesIO(b"{}")) as upstream:
            app.call_endpoint("tabular_scoring", {})
        self.assertEqual(upstream.call_args.args[0].get_header("Authorization"), "Bearer scoring-token")

    def test_selected_request_is_included_in_the_guidance_input(self):
        config = {"api_token": "key", "endpoints": {"responses": "https://agent.example/v1/responses"}}
        with patch.object(app, "APP_CONFIG", config), patch.object(
            app, "urlopen", return_value=io.BytesIO(json.dumps({"output_text": json.dumps({"answer": "Inspect the pothole.", "source_urls": []})}).encode())
        ) as upstream:
            result = app.retrieve_guidance({
                "question": "How should this be routed?",
                "service": "Pothole",
                "neighborhood": "Center City",
            })
        self.assertIn(
            "Service request: Pothole in Center City. How should this be routed?",
            json.loads(upstream.call_args.args[0].data)["input"],
        )
        self.assertEqual(result["answer"], "Inspect the pothole.")
        self.assertEqual(result["grounding"], "none")


    def test_guidance_uses_json_and_agent_timeout(self):
        config = {"api_token": "key", "endpoints": {"responses": "https://agent.example/v1/responses"}}
        with patch.object(app, "APP_CONFIG", config), patch.object(app, "urlopen", return_value=io.BytesIO(json.dumps({"output_text": json.dumps({"answer": "Contact 311.", "source_urls": []})}).encode())) as opened:
            app.retrieve_guidance({"question": "How do I report this?"})
        self.assertEqual(opened.call_args.kwargs["timeout"], 180)
        self.assertEqual(opened.call_args.args[0].get_header("Accept"), "application/json")
        self.assertFalse(json.loads(opened.call_args.args[0].data)["stream"])

    def test_agent_failure_is_not_replaced_with_sample_guidance(self):
        config = {"api_token": "key", "endpoints": {"responses": "https://agent.example/v1/responses"}}
        error = HTTPError("https://agent.example", 502, "failed", hdrs=None, fp=io.BytesIO())
        with patch.object(app, "APP_CONFIG", config), patch.object(app, "urlopen", side_effect=error), self.assertRaisesRegex(app.EndpointError, "HTTP 502"):
            app.retrieve_guidance({"question": "How do I report this?"})

    def test_old_protocol_configuration_has_a_clear_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"api_token": "key", "endpoints": {}, "responses_protocol": "dashboard-bff"}))
            with patch.object(app, "CONFIG_PATH", path), self.assertRaisesRegex(RuntimeError, "starter-kit Responses API"):
                app.load_config()

    def test_api_ask_returns_one_json_answer_even_when_streaming_is_requested(self):
        handler = app.Handler.__new__(app.Handler)
        body = json.dumps({"question": "What information is needed?"}).encode()
        handler.rfile = io.BytesIO(body)
        handler.headers = {"Content-Length": str(len(body)), "Accept": "text/event-stream"}
        handler.path = "/api/ask"
        sent = []
        handler.send_json = lambda status, payload: sent.append((status, payload))
        answer = {"answer": "Collect the location.", "sources": [], "grounding": "none"}
        with patch.object(app, "retrieve_guidance", return_value=answer):
            handler.do_POST()
        self.assertEqual(sent, [(200, answer)])


if __name__ == "__main__":
    unittest.main()
