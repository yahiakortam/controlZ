"""The abstract Integration contract."""

from typing import ClassVar

import pytest

from controlz import Operation, Reversibility
from controlz.integrations import Integration, UnsupportedOperationError


class TestAbstractness:
    def test_cannot_instantiate_the_abc(self):
        with pytest.raises(TypeError):
            Integration()

    def test_partial_implementation_is_rejected(self):
        class Partial(Integration):
            name = "partial"

            def snapshot(self, operation):
                return None

        with pytest.raises(TypeError):
            Partial()

    def test_required_methods(self):
        assert Integration.__abstractmethods__ == {
            "snapshot",
            "classify",
            "build_rollback_plan",
            "execute_rollback",
            "execute",
        }


class Minimal(Integration):
    name = "minimal"
    classification: ClassVar[dict] = {
        "safe": Reversibility.REVERSIBLE,
        "risky": Reversibility.IRREVERSIBLE,
    }

    def snapshot(self, operation):
        return {"seen": operation.api_call}

    def classify(self, operation):
        return self.classification.get(operation.api_call, Reversibility.UNKNOWN)

    def execute(self, operation):
        return "done"

    def build_rollback_plan(self, action):
        return None

    def execute_rollback(self, action):
        raise NotImplementedError


class TestConcreteHelpers:
    def test_supports_and_supported_operations(self):
        assert Minimal.supports("safe") is True
        assert Minimal.supports("nope") is False
        assert Minimal.supported_operations() == ["risky", "safe"]

    def test_require_supported_raises_with_a_useful_message(self):
        with pytest.raises(UnsupportedOperationError, match="supported: risky, safe"):
            Minimal()._require_supported("nope")

    def test_snapshot_after_defaults_to_snapshot(self):
        operation = Operation(tool="minimal", api_call="safe")
        assert Minimal().snapshot_after(operation, result=None) == {"seen": "safe"}

    def test_unlisted_operation_classifies_as_unknown(self):
        assert Minimal().classify(Operation(tool="minimal", api_call="???")) is (
            Reversibility.UNKNOWN
        )
