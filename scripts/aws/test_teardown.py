import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("teardown", Path(__file__).with_name("teardown.py"))
teardown = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(teardown)


class TeardownContracts(unittest.TestCase):
    def test_exact_secret_names(self):
        self.assertEqual(
            teardown.allowed("soloscale-resume-1"),
            {"/soloscale-resume-1/api-bearer-token", "/soloscale-resume-1/postgres-password"},
        )

    def test_absent_parameter_is_idempotent(self):
        completed = subprocess.CompletedProcess(["aws"], 254, "", "ParameterNotFound")
        with patch("subprocess.run", return_value=completed):
            self.assertIsNone(
                teardown.call("p", "us-east-1", "ssm", "get-parameter", allow_absent=True)
            )

    def test_unrelated_stack_is_not_owned(self):
        journal = {
            "task_prefix": "soloscale-resume-1",
            "operation_id": "op",
            "client_request_token": "op",
        }
        self.assertFalse(
            teardown.owned_stack(
                {
                    "Tags": [
                        {"Key": "soloscale:task", "Value": "soloscale-resume-1"},
                        {"Key": "soloscale:operation", "Value": "other"},
                    ],
                    "ClientRequestToken": "op",
                },
                journal,
            )
        )


if __name__ == "__main__":
    unittest.main()
