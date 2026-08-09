import unittest

from context_control_plane.sanitizer import (
    SanitizationError,
    sanitize_text,
    validate_admission,
)


class SanitizerTests(unittest.TestCase):
    def test_redacts_bare_jwt_and_provider_uuid(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwcml2YXRlLXVzZXIifQ.signature123"
        provider_uuid = "019fe216-111c-71e3-a1af-497306ba2391"

        result = sanitize_text(f"credential {jwt} source {provider_uuid}")

        self.assertNotIn(jwt, result.text)
        self.assertNotIn(provider_uuid, result.text)
        self.assertEqual(result.findings_by_category, {"secret": 1, "provider-id": 1})

    def test_known_secret_categories_are_redacted_without_echoing_values(self):
        text = (
            "Authorization: Bearer super-secret-token\n"
            "openai=sk-test-1234567890abcdef\n"
            "password=correct horse battery staple"
        )

        result = sanitize_text(text)

        self.assertEqual(result.findings_by_category["secret"], 3)
        self.assertNotIn("super-secret-token", result.text)
        self.assertNotIn("sk-test-1234567890abcdef", result.text)
        self.assertNotIn("correct horse battery staple", result.text)
        self.assertIn("[REDACTED:secret]", result.text)

    def test_personal_and_machine_specific_values_are_redacted(self):
        text = (
            "contact alice@example.com or +86 138 0013 8000\n"
            "workspace=/home/alice/Projects/private-repo"
        )

        result = sanitize_text(text)

        self.assertEqual(result.findings_by_category["pii"], 2)
        self.assertEqual(result.findings_by_category["machine"], 1)
        self.assertNotIn("alice@example.com", result.text)
        self.assertNotIn("138 0013 8000", result.text)
        self.assertNotIn("/home/alice/Projects/private-repo", result.text)

    def test_unapproved_spdx_license_is_reported_and_allowed_license_is_preserved(self):
        text = "SPDX-License-Identifier: GPL-3.0-only\nSPDX-License-Identifier: MIT"

        result = sanitize_text(text, allowed_licenses={"MIT"})

        self.assertEqual(result.findings_by_category["license"], 1)
        self.assertIn("SPDX-License-Identifier: MIT", result.text)
        self.assertNotIn("GPL-3.0-only", result.text)

    def test_sanitization_is_deterministic(self):
        text = "token=abc@example.com path=/Users/alice/secret"

        first = sanitize_text(text)
        second = sanitize_text(text)

        self.assertEqual(first, second)
        self.assertEqual(first.content_sha256, second.content_sha256)

    def test_admission_requires_clean_sanitized_result_and_provenance(self):
        clean = sanitize_text("decision: use current evidence")
        validate_admission(clean, provenance_valid=True)

        with self.assertRaises(SanitizationError):
            validate_admission(sanitize_text("token=secret-value"), provenance_valid=True)
        with self.assertRaises(SanitizationError):
            validate_admission(clean, provenance_valid=False)


if __name__ == "__main__":
    unittest.main()
