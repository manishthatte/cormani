# SPDX-License-Identifier: GPL-3.0-or-later
#
# Themes are data, and the default is Solarized Light.
#
# © Manish Jagdish Thatte
import unittest

from cormani.ui.theme import (DEFAULT_THEME, SOLARIZED_DARK, SOLARIZED_LIGHT,
                              THEMES, Theme, get, stylesheet)


class TestTheme(unittest.TestCase):
    def test_default_is_solarized_light(self):
        self.assertEqual(DEFAULT_THEME, "solarized-light")
        self.assertIs(get(None), SOLARIZED_LIGHT)
        self.assertEqual(SOLARIZED_LIGHT.surface, "#fdf6e3")   # base3
        self.assertFalse(SOLARIZED_LIGHT.dark)

    def test_unknown_name_falls_back_rather_than_raising(self):
        # A theme named by a newer version must not stop the client opening.
        self.assertIs(get("chartreuse"), SOLARIZED_LIGHT)
        self.assertIs(get(""), SOLARIZED_LIGHT)

    def test_names_are_matched_case_and_space_insensitively(self):
        self.assertIs(get("  Solarized-Dark "), SOLARIZED_DARK)

    def test_system_theme_imposes_nothing(self):
        # A client that insists on its own palette is one a high-contrast
        # accessibility setting cannot reach.
        self.assertEqual(stylesheet(get("system")), "")

    def test_every_theme_defines_every_role(self):
        # The point of roles being data: a new theme cannot half-exist.
        roles = [f for f in Theme.__dataclass_fields__ if f not in ("key", "name", "dark")]
        for key, theme in THEMES.items():
            if key == "system":
                continue
            for role in roles:
                value = getattr(theme, role)
                self.assertTrue(value.startswith("#"), f"{key}.{role} = {value!r}")
                self.assertIn(len(value), (4, 7), f"{key}.{role} = {value!r}")

    def test_light_and_dark_invert_the_grey_ramp(self):
        # Solarized's two variants are the same values with the greys reversed.
        # If this stops holding, a role has been given a literal colour.
        self.assertNotEqual(SOLARIZED_LIGHT.surface, SOLARIZED_DARK.surface)
        self.assertEqual(SOLARIZED_LIGHT.accent, SOLARIZED_DARK.accent)
        self.assertEqual(SOLARIZED_LIGHT.flagged, SOLARIZED_DARK.flagged)

    def test_stylesheet_names_no_colour_that_is_not_a_role(self):
        # Nothing in the interface may name a colour; widgets ask for a role.
        import re
        sheet = stylesheet(SOLARIZED_LIGHT)
        used = set(re.findall(r"#[0-9a-fA-F]{3,6}", sheet))
        defined = {getattr(SOLARIZED_LIGHT, f) for f in Theme.__dataclass_fields__
                   if isinstance(getattr(SOLARIZED_LIGHT, f), str)}
        self.assertEqual(used - defined, set())

    def test_config_default_matches_theme_default(self):
        from cormani.config.settings import Settings
        self.assertEqual(Settings().theme, DEFAULT_THEME)


if __name__ == "__main__":
    unittest.main()
