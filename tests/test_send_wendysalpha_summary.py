import unittest

from scripts.send_wendysalpha_summary import (
    contains_chinese_summary,
    fallback_classification,
    fallback_person_intro,
    fallback_project_intro,
)


def account(name: str, bio: str) -> dict[str, str]:
    return {
        "handle": "@example",
        "name": name,
        "bio": bio,
        "x_url": "https://x.com/example",
    }


class ChineseSummaryFallbackTest(unittest.TestCase):
    def test_defi_project_is_explained_in_chinese(self) -> None:
        intro = fallback_project_intro(
            account("Example Protocol", "The liquidity layer for DeFi lending and swaps")
        )
        self.assertTrue(contains_chinese_summary(intro))
        self.assertIn("去中心化金融", intro)
        self.assertNotIn("liquidity", intro.lower())

    def test_ai_project_is_explained_in_chinese(self) -> None:
        intro = fallback_project_intro(
            account("Agent Labs", "AI agents and developer tools for onchain users")
        )
        self.assertTrue(contains_chinese_summary(intro))
        self.assertIn("人工智能", intro)

    def test_person_gets_chinese_professional_direction(self) -> None:
        intro = fallback_person_intro(
            account("Jane", "Software engineer, open source developer and builder")
        )
        self.assertTrue(contains_chinese_summary(intro))
        self.assertNotIn("Software", intro)

    def test_full_fallback_never_pastes_english_bio(self) -> None:
        source = account("Example Wallet", "The easiest smart account wallet for everyone")
        source.update({"handle_key": "@example"})
        result = fallback_classification(source)
        self.assertEqual(result["type"], "项目")
        self.assertTrue(contains_chinese_summary(result["intro"]))
        self.assertNotIn(source["bio"], result["intro"])

    def test_english_model_output_fails_publish_gate(self) -> None:
        self.assertFalse(
            contains_chinese_summary("A decentralized protocol for lending and swaps")
        )


if __name__ == "__main__":
    unittest.main()
