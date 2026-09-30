import inspect
import unittest

from app import desk_ui


class DeskStatusCopyTest(unittest.TestCase):
    def test_clean_off_book_explains_the_actual_gate(self):
        source = desk_ui.JS
        self.assertIn("NIFTYBEES is below its completed-session 200-day trend", source)
        self.assertIn("The paper book is 100% cash. Nothing is currently held.", source)

    def test_clean_book_does_not_claim_previous_losses_or_positions(self):
        source = desk_ui.JS
        self.assertNotIn("A trading halt does not erase previous losses", source)
        self.assertIn("house.positions?'No new entries; existing positions", source)

    def test_index_panel_names_only_the_executable_paper_sleeve(self):
        self.assertIn("Only NIFTYBEES can receive automated paper entries", desk_ui.JS)
        self.assertIn("Quality stock names are research-only", desk_ui.JS)


if __name__ == "__main__":
    unittest.main()
