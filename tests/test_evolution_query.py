import unittest
from unittest.mock import patch


class EvolutionQueryApiTest(unittest.TestCase):
    def test_publication_basis_and_sync_endpoint(self):
        from fastapi.testclient import TestClient
        from webui.server import app
        client = TestClient(app)
        with patch('webui.evolution_api.evolution.contract', return_value={}) as contract:
            self.assertEqual(client.get('/api/evolution?version=5.3.0&attribution_basis=publication').status_code, 200)
            self.assertEqual(contract.call_args.kwargs['attribution_basis'], 'publication')
        with patch('webui.evolution_api.release_windows.sync_releases', return_value={'ok': False, 'message': 'offline'}):
            result = client.post('/api/evolution/releases/refresh')
        self.assertEqual(result.json(), {'ok': False, 'message': 'offline'})

    def test_api_forwards_as_of_and_version(self):
        from fastapi.testclient import TestClient
        from webui.server import app

        with patch("webui.evolution_api.evolution.contract", return_value={"ok": True}) as contract:
            response = TestClient(app).get(
                "/api/evolution?as_of=2024-01-02&version=v5.3.0"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(contract.call_args.kwargs, {"as_of": "2024-01-02", "version": "v5.3.0"})

    def test_invalid_as_of_is_400(self):
        from fastapi.testclient import TestClient
        from webui.server import app

        with patch("webui.evolution_api.evolution.contract",
                   side_effect=ValueError("无效的查询截止日期")):
            response = TestClient(app).get("/api/evolution?as_of=invalid")
        self.assertEqual(response.status_code, 400)
        self.assertIn("无效的查询截止日期", response.text)


class EvolutionQueryCliTest(unittest.TestCase):
    def test_cli_publication_basis(self):
        from tools import evolution_feedback
        with patch.object(evolution_feedback.evolution, 'contract', return_value={}) as contract, patch('builtins.print'):
            self.assertEqual(evolution_feedback.main(['--version', '5.3.0', '--attribution-basis', 'publication']), 0)
        self.assertEqual(contract.call_args.kwargs['attribution_basis'], 'publication')

    def test_cli_forwards_version_and_cutoff(self):
        from tools import evolution_feedback

        with patch.object(evolution_feedback.evolution, "contract", return_value={"ok": True}) as contract, \
             patch("builtins.print"):
            self.assertEqual(evolution_feedback.main(["--as-of", "2024-01-02", "--version", "5.3.0"]), 0)
        self.assertEqual(contract.call_args.kwargs, {"as_of": "2024-01-02", "version": "5.3.0"})


if __name__ == "__main__":
    unittest.main()
