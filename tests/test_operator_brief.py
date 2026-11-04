"""Verify policy briefs, evidence labels, and failure behavior without cluster access."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

with patch.object(Path, 'exists', return_value=False):
    spec = importlib.util.spec_from_file_location('city_services_brief', Path(__file__).resolve().parents[1] / 'main.py')
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)


class OperatorBriefTests(unittest.TestCase):
    request = {'service': 'Illegal dumping', 'neighborhood': 'Kensington', 'priority': 'High', 'channel': 'Phone'}

    def invoke(self, brief, annotations=None):
        response = {'output': [{'content': [{'type': 'output_text', 'text': brief, 'annotations': annotations or []}]}]}
        with patch.object(app, 'open_endpoint', return_value=io.BytesIO(json.dumps(response).encode())) as upstream:
            result = app.operator_brief(self.request)
        return result, upstream.call_args

    def test_live_brief_preserves_steps_reply_and_agent_references(self):
        url = next(doc['url'] for doc in app.GUIDANCE if doc['title'] == 'Report illegal dumping')
        brief = {'handling_steps': ['Collect a precise location.', 'Confirm the material reported.'],
                 'resident_reply': 'Please share the closest address and a description.',
                 'source_urls': [url, url, 'https://invented.example/policy']}
        result, call = self.invoke('```json\n' + json.dumps(brief) + '\n```')
        self.assertEqual(result['handling_steps'], brief['handling_steps'])
        self.assertEqual(result['resident_reply'], brief['resident_reply'])
        self.assertEqual([source['url'] for source in result['sources']], [url])
        self.assertEqual(result['grounding'], 'references')
        self.assertEqual(call.kwargs['timeout'], 180)
        prompt = call.args[1]['input']
        self.assertIn('Illegal dumping in Kensington', prompt)
        self.assertIn('Operator-assigned priority: High', prompt)
        self.assertIn('Do not invent numerical risk', prompt)
        self.assertFalse(call.args[1]['stream'])

    def test_no_sources_are_reported_honestly(self):
        result, _ = self.invoke(json.dumps({'handling_steps': ['Verify the routing.'], 'resident_reply': 'Please provide a location.'}))
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['grounding'], 'none')

    def test_structured_citations_remain_distinct_from_generated_references(self):
        url = 'https://www.phila.gov/311/'
        result, _ = self.invoke(json.dumps({'handling_steps': ['Collect details.'], 'resident_reply': 'Please describe the issue.'}),
                                [{'type': 'url_citation', 'url': url, 'title': '311'}])
        self.assertEqual(result['grounding'], 'citations')
        self.assertEqual(result['sources'][0]['url'], url)

    def test_malformed_or_incomplete_live_brief_does_not_use_sample_guidance(self):
        for brief in ['not JSON', '[]', '{}', '{"handling_steps":[1],"resident_reply":"Hi"}',
                      '{"handling_steps":[],"resident_reply":"Hi"}']:
            with self.subTest(brief=brief), self.assertRaises(app.EndpointError):
                self.invoke(brief)

    def test_endpoint_failure_is_not_replaced_with_a_sample(self):
        with patch.object(app, 'open_endpoint', side_effect=app.EndpointError('unreachable')), self.assertRaisesRegex(app.EndpointError, 'unreachable'):
            app.operator_brief(self.request)

    def test_sample_brief_covers_every_supported_service_without_network(self):
        with patch.object(app, 'APP_CONFIG', {}):
            for service in app.SERVICES:
                with self.subTest(service=service):
                    result = app.operator_brief({**self.request, 'service': service})
                    self.assertEqual(result['grounding'], 'sample')
                    self.assertTrue(result['handling_steps'])
                    self.assertTrue(result['sources'][0]['url'].startswith('https://www.phila.gov/'))
                    self.assertIn(service.lower(), result['resident_reply'])

    def test_unknown_service_is_rejected_before_inference(self):
        with patch.object(app, 'open_endpoint') as upstream, self.assertRaises(ValueError):
            app.operator_brief({'service': 'Unknown'})
        upstream.assert_not_called()


if __name__ == '__main__':
    unittest.main()
