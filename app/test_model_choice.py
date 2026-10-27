"""model_choice: what an admin is told when a key can't be used, and what is
refused before any call is made."""

import unittest
from unittest.mock import patch

import model_choice


class Failed(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.message, self.status_code = message, status_code


class Explain(unittest.TestCase):
    def say(self, error, provider="anthropic", model="claude-opus-5-5"):
        return model_choice._explain(error, provider, model)

    def test_each_failure_is_said_in_words_the_admin_can_act_on(self):
        self.assertIn("refused that key", self.say(Failed("invalid x-api-key", 401)))
        self.assertIn("no credit left", self.say(Failed("Your credit balance is too low", 400)))
        self.assertIn("doesn't offer 'claude-opus-5-5'", self.say(Failed("model: claude-opus-5-5 not found", 404)))
        self.assertIn("rate-limiting", self.say(Failed("slow down", 429)))
        self.assertIn("no credit left", self.say(Failed("You exceeded your current quota", 429), "openai", "gpt-4.1"))


class Save(unittest.TestCase):
    def test_an_unknown_provider_or_no_model_is_refused(self):
        with self.assertRaisesRegex(model_choice.ChoiceError, "Choose one of"):
            model_choice.save("gemini", "x", "k", None)
        with self.assertRaisesRegex(model_choice.ChoiceError, "Choose a model"):
            model_choice.save("anthropic", " ", "k", None)

    def test_a_first_choice_needs_a_key(self):
        with patch.object(model_choice, "_row", return_value=None), \
             self.assertRaisesRegex(model_choice.ChoiceError, "Paste your Claude"):
            model_choice.save("anthropic", "claude-opus-5-5", "", None)

    def test_a_key_is_not_tried_when_it_could_not_be_kept(self):
        with patch.object(model_choice, "_row", return_value=None), \
             patch.object(model_choice.secrets_box, "available", return_value=False), \
             patch.object(model_choice, "check_key") as check, \
             self.assertRaisesRegex(model_choice.ChoiceError, "INTEGRATIONS_KEY"):
            model_choice.save("anthropic", "claude-opus-5-5", "sk-ant-x", None)
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
