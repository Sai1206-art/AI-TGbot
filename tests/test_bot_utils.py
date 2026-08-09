import unittest

from bot_utils import format_balance_summary, mode_instruction, remaining_generations


class BotUtilsTests(unittest.TestCase):
    def test_free_generation_counts_toward_remaining_total(self) -> None:
        balance = {"balance": 10, "free_credit_used": 0}
        self.assertEqual(remaining_generations(balance), 11)
        self.assertIn("11 generation(s) remaining", format_balance_summary(balance))
        self.assertIn("free generation available", format_balance_summary(balance))

    def test_used_free_generation_leaves_paid_tokens_only(self) -> None:
        balance = {"balance": 9, "free_credit_used": 1}
        self.assertEqual(remaining_generations(balance), 9)
        self.assertIn("9 generation(s) remaining", format_balance_summary(balance))
        self.assertIn("free generation used", format_balance_summary(balance))

    def test_mode_instructions_are_specific(self) -> None:
        self.assertIn("photo caption", mode_instruction("image_edit"))
        self.assertIn("without a caption", mode_instruction("video"))


if __name__ == "__main__":
    unittest.main()
