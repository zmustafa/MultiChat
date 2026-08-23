from __future__ import annotations

import io
import json
import zipfile

from sqlalchemy import select

from app import models
from app import seed_personas as seeds
from app.system_backup import build_export
from app.tools import registry as tool_registry


def test_seed_catalog_is_idempotent_and_tracks_medical_config(db, user) -> None:
    assert seeds.seed_starter_personas(db, user) == len(seeds.STARTER_PERSONAS)
    assert seeds.seed_starter_personas(db, user) == 0

    personas = db.scalars(
        select(models.Persona).where(models.Persona.user_id == user.id)
    ).all()
    states = db.scalars(
        select(models.StarterPersonaState).where(
            models.StarterPersonaState.user_id == user.id
        )
    ).all()
    assert len(personas) == len(seeds.STARTER_PERSONAS)
    assert len(states) == len(seeds.STARTER_PERSONAS)

    medical = next(p for p in personas if p.name == "Medical Information Assistant")
    assert medical.is_default is False
    assert medical.notice
    assert "not a licensed clinician" in medical.notice
    assert medical.tool_config_json == {
        "enabled": [
            "web_search",
            "fetch_url",
            "calculator",
            "current_date",
            "read_document",
        ],
        "include_workiq": False,
    }
    assert all(not lane["provider_id"] for lane in medical.lanes_json)


def test_catalog_upgrade_updates_untouched_but_preserves_customized(
    db,
    user,
    monkeypatch,
) -> None:
    spec = {
        "key": "test_starter",
        "version": 1,
        "name": "Test starter",
        "description": "v1",
        "system_prompt": "original",
        "tools_enabled": False,
        "is_default": False,
        "lanes": [],
    }
    monkeypatch.setattr(seeds, "STARTER_PERSONAS", [spec])
    assert seeds.seed_starter_personas(db, user) == 1
    persona = db.scalar(
        select(models.Persona).where(
            models.Persona.user_id == user.id,
            models.Persona.name == "Test starter",
        )
    )
    assert persona is not None

    spec.update(version=2, description="v2", system_prompt="catalog update")
    assert seeds.seed_starter_personas(db, user) == 0
    db.refresh(persona)
    assert persona.description == "v2"
    assert persona.system_prompt == "catalog update"

    persona.system_prompt = "my customization"
    db.commit()
    spec.update(version=3, description="v3", system_prompt="new catalog prompt")
    assert seeds.seed_starter_personas(db, user) == 0
    db.refresh(persona)
    assert persona.description == "v2"
    assert persona.system_prompt == "my customization"


def test_legacy_physican_is_adopted_without_overwrite(db, user, monkeypatch) -> None:
    legacy = models.Persona(
        user_id=user.id,
        name="Physican",
        description="My existing persona",
        system_prompt="keep this custom prompt",
        tools_enabled=True,
        lanes_json=[],
    )
    db.add(legacy)
    db.commit()
    medical_spec = next(
        spec
        for spec in seeds.STARTER_PERSONAS
        if spec["key"] == "medical_information_assistant"
    )
    monkeypatch.setattr(seeds, "STARTER_PERSONAS", [medical_spec])

    assert seeds.seed_starter_personas(db, user) == 0
    db.refresh(legacy)
    assert legacy.name == "Physican"
    assert legacy.description == "My existing persona"
    assert legacy.system_prompt == "keep this custom prompt"
    state = db.get(
        models.StarterPersonaState,
        (user.id, "medical_information_assistant"),
    )
    assert state is not None
    assert state.persona_id == legacy.id
    assert state.seeded_hash is None


def test_deleting_starter_records_dismissal(client, auth, db, user) -> None:
    seeds.seed_starter_personas(db, user)
    state = db.get(
        models.StarterPersonaState,
        (user.id, "medical_information_assistant"),
    )
    assert state is not None and state.persona_id
    persona_id = state.persona_id

    response = client.delete(f"/api/personas/{persona_id}", headers=auth)
    assert response.status_code == 204
    db.expire_all()
    state = db.get(
        models.StarterPersonaState,
        (user.id, "medical_information_assistant"),
    )
    assert state is not None
    assert state.dismissed is True
    assert state.persona_id is None
    assert seeds.seed_starter_personas(db, user) == 0
    assert db.get(models.Persona, persona_id) is None


def test_medical_tool_config_excludes_workiq(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(tool_registry, "workiq_tools", lambda: {"workiq": sentinel})
    tools = tool_registry.resolve_enabled_tools(
        {
            "enabled": ["web_search", "fetch_url", "calculator"],
            "include_workiq": False,
        }
    )
    assert sentinel not in tools
    assert [tool.definition.name for tool in tools] == [
        "web_search",
        "fetch_url",
        "calculator",
    ]


def test_backup_contains_persona_tool_config_and_starter_state(db, user) -> None:
    seeds.seed_starter_personas(db, user)
    archive = zipfile.ZipFile(io.BytesIO(build_export(db, user)))
    data = json.loads(archive.read("data.json"))

    medical = next(
        persona
        for persona in data["personas"]
        if persona["name"] == "Medical Information Assistant"
    )
    assert medical["tool_config_json"]["include_workiq"] is False
    assert "not a licensed clinician" in medical["notice"]
    state = next(
        state
        for state in data["starter_persona_states"]
        if state["seed_key"] == "medical_information_assistant"
    )
    assert state["persona_id"] == medical["id"]
    assert state["dismissed"] is False
