"""setup.py's .env handling: values land in the template's lines, comments stay, and a
commented-out setting is switched on or left alone."""
import os
import shutil
import tempfile
import unittest

import setup


class EnvFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.template = os.path.join(self.dir, "example")
        self.env = os.path.join(self.dir, ".env")
        with open(self.template, "w", encoding="utf-8") as f:
            f.write("# the address\nPOPN_ADVERTISE=192.0.2.10\n\n#POPN_SEED_NAME=TEST\n#POPN_STATE=./state\n")

    def write(self, values):
        setup.write_env(values, self.env, self.template)
        with open(self.env, encoding="utf-8") as f:
            return f.read()

    def test_first_write_starts_from_the_template(self):
        text = self.write({"POPN_ADVERTISE": "203.0.113.5", "POPN_SEED_NAME": "ROOM", "POPN_NEW": 1})
        self.assertEqual(text, "# the address\nPOPN_ADVERTISE=203.0.113.5\n\nPOPN_SEED_NAME=ROOM\n"
                               "#POPN_STATE=./state\nPOPN_NEW=1\n")
        self.assertEqual(setup.read_env(self.env),
                         {"POPN_ADVERTISE": "203.0.113.5", "POPN_SEED_NAME": "ROOM", "POPN_NEW": "1"})

    def test_later_writes_keep_the_file(self):
        self.write({"POPN_SEED_NAME": "ROOM"})
        text = self.write({"POPN_ADVERTISE": "198.51.100.7"})
        self.assertIn("POPN_ADVERTISE=198.51.100.7\n", text)
        self.assertIn("\nPOPN_SEED_NAME=ROOM\n", text)

    def test_none_comments_a_line_out(self):
        self.write({"POPN_SEED_NAME": "ROOM"})
        text = self.write({"POPN_SEED_NAME": None, "POPN_OTHER": None})
        self.assertIn("\n#POPN_SEED_NAME=ROOM\n", text)
        self.assertNotIn("POPN_OTHER", text)
        self.assertNotIn("POPN_SEED_NAME", setup.read_env(self.env))

    def test_example_is_a_valid_template(self):
        values = setup.read_env(setup.ENV_EXAMPLE)
        for key in ("POPN_ADVERTISE", "POPN_ADVERTISE_MAP", "POPN_ADMIN_TOKEN", "POPN_P2P_RELAY",
                    "POPN_AUTH_OPEN", "OPENLOBBY_PAIRING"):
            self.assertIn(key, values)
        self.assertEqual(values["POPN_ADMIN_TOKEN"], "")      # never a shipped token


if __name__ == "__main__":
    unittest.main()
