import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch, MagicMock

spec = importlib.util.spec_from_file_location("notifier", Path(__file__).with_name("backup_failure_notification.py"))
notifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notifier)


class BackupFailureNotificationTests(unittest.TestCase):
    def test_healthy_backup_does_not_send_email(self):
        result = subprocess.CompletedProcess([], 0, "Id=enclave-backup.service\nActiveState=inactive\nResult=success\n")
        with patch.object(notifier.subprocess, "run", return_value=result), patch.object(notifier.smtplib, "SMTP") as smtp:
            with self.assertRaises(ValueError):
                notifier.send_notification("enclave-backup.service", {}, "alerts@example.test")
            smtp.assert_not_called()

    def test_other_units_cannot_use_the_notifier(self):
        with patch.object(notifier.subprocess, "run") as systemctl:
            with self.assertRaises(ValueError):
                notifier.failure_summary("arbitrary.service")
            systemctl.assert_not_called()

    def test_failure_mail_uses_verified_tls_and_omits_secret_settings(self):
        result = subprocess.CompletedProcess([], 0, "Id=enclave-backup.service\nActiveState=failed\nResult=exit-code\nExecMainCode=1\nExecMainStatus=1\n")
        settings = {"SMTP_HOST": "smtp.example.test", "SMTP_PORT": "2525", "SMTP_USER": "test-user", "SMTP_PASS": "private-test-password", "SMTP_FROM": "Liberator <sender@example.test>", "SECRET_KEY": "private-test-auth-secret"}
        client = MagicMock()
        client.send_message.return_value = {}
        with patch.object(notifier.subprocess, "run", return_value=result), patch.object(notifier.smtplib, "SMTP") as smtp:
            smtp.return_value.__enter__.return_value = client
            receipt = notifier.send_notification("enclave-backup.service", settings, "alerts@example.test")
        context = client.starttls.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, notifier.ssl.CERT_REQUIRED)
        client.login.assert_called_once_with("test-user", "private-test-password")
        message = client.send_message.call_args.args[0]
        self.assertEqual(message["To"], "alerts@example.test")
        self.assertEqual(message["X-Mailgun-Track"], "no")
        for secret in [settings["SMTP_PASS"], settings["SECRET_KEY"]]:
            self.assertNotIn(secret, message.as_string())
            self.assertNotIn(secret, str(receipt))
        self.assertTrue(receipt["smtp_accepted"])

    def test_rejected_recipient_is_not_success(self):
        result = subprocess.CompletedProcess([], 0, "Id=enclave-backup.service\nActiveState=failed\nResult=exit-code\n")
        settings = {"SMTP_HOST": "smtp.example.test", "SMTP_PORT": "2525", "SMTP_USER": "user", "SMTP_PASS": "test-secret", "SMTP_FROM": "sender@example.test"}
        with patch.object(notifier.subprocess, "run", return_value=result), patch.object(notifier.smtplib, "SMTP") as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {"alerts@example.test": (550, b"rejected")}
            with self.assertRaises(RuntimeError):
                notifier.send_notification("enclave-backup.service", settings, "alerts@example.test")


if __name__ == "__main__":
    unittest.main()
