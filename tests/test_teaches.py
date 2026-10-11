"""What n8n teaches Marvin (SDK 0.9.0): its guide, and the hand-off recipe that feeds the report-back blueprints."""

import pytest
from marvin_integration_sdk import load_guide
from marvin_integration_sdk.testing import IntegrationContract

from marvin_integration_n8n.content import FAILED, IN_FLIGHT, STATUS_KEY, WEBHOOK_SECRET_REF
from marvin_integration_n8n.provider import N8nProvider
from marvin_integration_n8n.recipes import HAND_OFF


class TestN8nContract(IntegrationContract):
    @pytest.fixture
    def provider(self):
        return N8nProvider()


def test_the_guide_ships_with_the_package():
    guide = load_guide(N8nProvider)
    assert guide.startswith("# n8n") and "N8N_WEBHOOK_SECRET" in guide and "N8N_CALLBACK_TOKEN" in guide


def test_the_hand_off_signs_with_the_workspace_secret_and_marks_the_entry_in_flight():
    trigger, mark = HAND_OFF.definition["actions"]
    assert trigger["action"] == "trigger_workflow" and trigger["args"]["secret"] == WEBHOOK_SECRET_REF  # a reference, resolved at run time
    assert trigger["args"]["entry_id"] == "${entry.id}"  # so n8n can report back on the same entry
    assert mark["metadata"][STATUS_KEY] in IN_FLIGHT.payload["smart_rules"]["where"][0]["value"]
    assert mark["metadata"][STATUS_KEY] != FAILED.payload["smart_rules"]["where"][0]["value"]
