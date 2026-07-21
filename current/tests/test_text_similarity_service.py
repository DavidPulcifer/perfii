import unittest

from app.services.text_similarity_service import (
    partial_token_similarity,
    similarity_backend,
    token_set_similarity,
)


class TextSimilarityServiceTest(unittest.TestCase):
    def test_uses_rapidfuzz_backend_when_dependency_is_installed(self) -> None:
        self.assertEqual(similarity_backend(), "rapidfuzz")

    def test_token_set_similarity_matches_reordered_tokens(self) -> None:
        self.assertGreaterEqual(token_set_similarity("acme market", "market acme"), 0.99)

    def test_partial_token_similarity_handles_noisy_payee_text(self) -> None:
        self.assertGreaterEqual(partial_token_similarity("sq joes coffee 000123", "joes coffee"), 0.9)


if __name__ == "__main__":
    unittest.main()
