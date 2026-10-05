"""Browser-vs-desktop routing, including the two misroutes seen in a real run.

The installed-app index is stubbed so this is deterministic on any machine.
"""

import unittest

import task_router as tr
from actions import app_launcher


# What the stubbed machine "has installed".
INSTALLED = ["Notepad", "Notepad++", "Visual Studio Code", "Excel", "BlueJ",
             "Calculator", "Google Chrome", "Spotify"]


class RouterTests(unittest.TestCase):
    def setUp(self):
        self._real = app_launcher.installed_app_name

        def fake(name: str) -> str:
            want = "".join(c for c in name.lower() if c.isalnum())
            if len(want) < 3:
                return ""
            for display in INSTALLED:
                if "".join(c for c in display.lower() if c.isalnum()) == want:
                    return display
            hits = [d for d in INSTALLED
                    if "".join(c for c in d.lower() if c.isalnum()).startswith(want)]
            return min(hits, key=len) if hits else ""

        app_launcher.installed_app_name = fake

    def tearDown(self):
        app_launcher.installed_app_name = self._real

    # ── The typed payload is content, not a surface ──
    def test_web_words_inside_the_typed_text_do_not_route_to_the_browser(self):
        for goal in ('Open Notepad and type "visit example.com"',
                     "open notepad and write a note about YouTube",
                     "Open Notepad and type I am online"):
            self.assertEqual(tr.route_task("browser", goal)[0], "computer", goal)

    def test_a_trailing_in_app_clause_still_names_the_target(self):
        self.assertEqual(tr.route_task("browser", "type hello in notepad")[0], "computer")
        self.assertEqual(tr.route_task("computer", "write an email in gmail")[0], "browser")

    # ── The observed failures ──
    def test_open_notepad_and_type_goes_to_the_desktop(self):
        kind, why = tr.route_task("browser", 'Open Notepad and type "hello world"')
        self.assertEqual(kind, "computer")
        self.assertIn("Notepad", why)

    def test_bare_open_notepad_goes_to_the_desktop(self):
        kind, _ = tr.route_task("browser", "open notepad")
        self.assertEqual(kind, "computer")

    # ── Web stays web ──
    def test_an_explicit_url_is_browser_work(self):
        self.assertEqual(tr.route_task("computer", "go to https://example.com")[0], "browser")

    def test_a_bare_domain_is_browser_work(self):
        self.assertEqual(tr.route_task("computer", "open youtube.com/feed")[0], "browser")

    def test_a_known_web_service_beats_a_same_named_shortcut(self):
        self.assertEqual(tr.route_task("computer", "open youtube and play lofi")[0], "browser")

    def test_naming_the_browser_keeps_it_on_the_browser(self):
        self.assertEqual(tr.route_task("computer", "in the browser, search for cats")[0],
                         "browser")

    def test_a_browser_app_name_routes_to_the_browser(self):
        self.assertEqual(tr.route_task("computer", "open chrome and search for cats")[0],
                         "browser")

    # ── Desktop stays desktop ──
    def test_multi_word_app_names_resolve(self):
        kind, why = tr.route_task("browser", "open Visual Studio Code and make a file")
        self.assertEqual(kind, "computer")
        self.assertIn("Visual Studio Code", why)

    def test_in_app_phrasing_resolves(self):
        self.assertEqual(tr.route_task("browser", "in Excel, sum column A")[0], "computer")

    def test_a_desktop_only_surface_phrase_resolves(self):
        self.assertEqual(tr.route_task("browser", "open task manager and end Chrome")[0],
                         "computer")

    # ── Restraint: no evidence → keep the model's choice ──
    def test_an_unknown_app_leaves_the_choice_alone(self):
        self.assertEqual(tr.route_task("browser", "open Weatherly and check the forecast")[0],
                         "browser")
        self.assertEqual(tr.route_task("computer", "open Weatherly and check the forecast")[0],
                         "computer")

    def test_a_generic_goal_leaves_the_choice_alone(self):
        for kind in ("browser", "computer"):
            self.assertEqual(tr.route_task(kind, "book a table for two on friday")[0], kind)

    def test_no_reason_is_given_when_nothing_was_overridden(self):
        _, why = tr.route_task("computer", "open notepad")
        self.assertEqual(why, "", "agreeing with the model is not an override")

    def test_an_empty_goal_is_left_alone(self):
        self.assertEqual(tr.route_task("browser", "")[0], "browser")

    def test_short_words_never_match_an_app(self):
        # "go" / "up" must not prefix-match their way into an app name.
        self.assertEqual(tr.route_task("browser", "scroll up")[0], "browser")

    def test_stopwords_are_trimmed_off_candidates(self):
        # "open the file and ..." — 'file' is grammar here, not an app.
        self.assertEqual(tr.route_task("browser", "open the file and read it")[0], "browser")

    def test_a_launcher_failure_falls_back_to_the_model_choice(self):
        def boom(_name):
            raise RuntimeError("index unavailable")

        app_launcher.installed_app_name = boom
        self.assertEqual(tr.route_task("browser", "open notepad")[0], "browser")


if __name__ == "__main__":
    unittest.main()
