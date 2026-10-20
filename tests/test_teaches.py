"""What n8n teaches Marvin (SDK 0.9.0): its guide, and the hand-off recipe that feeds the report-back blueprints."""

import pytest
from marvin_integration_sdk import RESULT_STATUSES, HandsOff, hands_off_problems, load_guide
from marvin_integration_sdk.testing import IntegrationContract

from marvin_integration_n8n.content import WEBHOOK_SECRET_REF
from marvin_integration_n8n.provider import N8nProvider
from marvin_integration_n8n.recipes import HAND_OFF


class TestN8nContract(IntegrationContract):
    @pytest.fixture
    def provider(self):
        return N8nProvider()


def test_the_guide_ships_with_the_package():
    guide = load_guide(N8nProvider)
    assert guide.startswith("# n8n") and "N8N_WEBHOOK_SECRET" in guide and "N8N_CALLBACK_TOKEN" in guide


def test_the_hand_off_signs_with_the_workspace_secret_and_records_nothing_itself():
    (trigger,) = HAND_OFF.definition["actions"]  # the core records the hand-off; no set_metadata step to remember
    assert trigger["action"] == "trigger_workflow" and trigger["args"]["secret"] == WEBHOOK_SECRET_REF  # a reference, resolved at run time
    assert trigger["args"]["entry_id"] == "${entry.id}"  # so n8n can report back on the same entry
    assert "set_metadata" not in repr(HAND_OFF.definition) and "n8n_status" not in repr(HAND_OFF.definition)
    assert [e["kind"] for e in HAND_OFF.side_effects] == ["external_post"]  # the entry itself is not changed


def test_the_hand_off_recipe_reads_the_trigger_actions_inputs():
    (trigger,) = HAND_OFF.definition["actions"]
    action = N8nProvider().get_action("trigger_workflow")
    assert set(trigger["args"]) <= set(action.input_schema["properties"])


def _hands_off():
    return N8nProvider().get_action("trigger_workflow").hands_off


def test_trigger_workflow_declares_the_hand_off():
    assert _hands_off() == HandsOff(
        remote_id="execution_id",
        remote_url="execution_url",
        target="path",
        statuses={"success": "succeeded", "error": "failed", "waiting": "in_flight"},
    )


def test_the_hand_off_keys_are_in_the_actions_schemas():
    action = N8nProvider().get_action("trigger_workflow")
    hands_off = action.hands_off
    assert hands_off.remote_id in action.output_schema["properties"]
    assert hands_off.remote_url in action.output_schema["properties"]
    assert hands_off.target in action.input_schema["properties"]
    assert hands_off_problems(action) == []


def test_only_the_trigger_hands_work_off():
    # The read actions return at once; nothing to follow up, so no result record.
    assert [a.key for a in N8nProvider().actions if a.hands_off] == ["trigger_workflow"]


def test_every_status_word_maps_to_one_of_marvins_three():
    assert set(_hands_off().statuses.values()) <= set(RESULT_STATUSES)
    provider = N8nProvider()
    for word, status in {"success": "succeeded", "error": "failed", "waiting": "in_flight"}.items():
        assert provider.result_status(word, "trigger_workflow") == status
        assert provider.result_status(word.upper()) == status  # n8n's words, however a workflow spells them
    assert provider.result_status("failed") == "failed"  # Marvin's own words are accepted too
    assert provider.result_status("crashed") is None  # unknown: the core keeps the result in flight with the word in its message
