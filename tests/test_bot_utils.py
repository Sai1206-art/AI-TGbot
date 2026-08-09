import unittest

from bot_utils import format_balance_summary, mode_cost, mode_instruction, remaining_generations


class BotUtilsTests(unittest.TestCase):
    def test_free_generation_counts_toward_remaining_total(self) -> None:
        balance = {"balance": 10, "free_credit_used": 0}
        self.assertEqual(remaining_generations(balance), 11)
        self.assertIn("11 Image Edit generation(s) remaining", format_balance_summary(balance, "image_edit"))
        self.assertIn("free generation available", format_balance_summary(balance, "image_edit"))

    def test_used_free_generation_leaves_paid_tokens_only(self) -> None:
        balance = {"balance": 9, "free_credit_used": 1}
        self.assertEqual(remaining_generations(balance), 9)
        self.assertIn("9 Image Edit generation(s) remaining", format_balance_summary(balance, "image_edit"))
        self.assertIn("free generation used", format_balance_summary(balance, "image_edit"))

    def test_mode_instructions_are_specific(self) -> None:
        self.assertIn("photo caption", mode_instruction("image_edit"))
        self.assertIn("without a caption", mode_instruction("video"))
        self.assertEqual(mode_cost("image_edit"), 1)
        self.assertEqual(mode_cost("video"), 3)
        self.assertIn("video generation(s)", format_balance_summary({"balance": 6}, "video"))


if __name__ == "__main__":
    unittest.main()
