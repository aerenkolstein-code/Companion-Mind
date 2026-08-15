import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from companion_mind.persona import PersonaLoadError, PersonaLoader
from companion_mind.runtime import Runtime
from companion_mind.state import (
    JsonStateStore,
    RawEvent,
    RuntimeState,
    StateStoreError,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PERSONAS_DIR = REPOSITORY_ROOT / "personas"


class PersonaLoaderTest(unittest.TestCase):
    def test_loads_fixed_lin_zhiyao_identity(self) -> None:
        persona = PersonaLoader(PERSONAS_DIR).load("LIN-ZHIYAO")

        self.assertEqual(persona.persona_id, "LIN-ZHIYAO")
        self.assertEqual(persona.display_name, "林知遥")
        self.assertEqual(persona.nickname, "遥遥")
        self.assertEqual(persona.universe, "Arna")
        self.assertEqual(persona.identity.continuity_owner, "runtime")

    def test_persona_document_has_no_provider_information(self) -> None:
        text = (PERSONAS_DIR / "lin_zhiyao.yaml").read_text(encoding="utf-8").lower()

        for forbidden in ("provider", "model", "deepseek", "grok", "xai"):
            self.assertNotIn(forbidden, text)

    def test_unknown_or_unsafe_persona_fails_closed(self) -> None:
        loader = PersonaLoader(PERSONAS_DIR)
        with self.assertRaisesRegex(PersonaLoadError, "unknown persona_id"):
            loader.load("NOT-THERE")
        with self.assertRaisesRegex(PersonaLoadError, "unsupported characters"):
            loader.load("../../private")

    def test_provider_coupling_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.yaml"
            source = (PERSONAS_DIR / "lin_zhiyao.yaml").read_text(encoding="utf-8")
            source = source.replace("LIN-ZHIYAO", "TEST", 1) + "\nprovider: forbidden\n"
            path.write_text(source, encoding="utf-8")

            with self.assertRaisesRegex(PersonaLoadError, "provider information"):
                PersonaLoader(temporary).load("TEST")


class PersonaRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"

    def test_start_session_without_any_model_connection(self) -> None:
        runtime = Runtime(personas_dir=PERSONAS_DIR, state_dir=self.state_dir)

        state = runtime.start_session(persona_id="LIN-ZHIYAO")

        self.assertEqual(state.persona.persona_id, "LIN-ZHIYAO")
        self.assertEqual(state.session.persona_id, "LIN-ZHIYAO")
        self.assertEqual(state.relationship.persona_id, "LIN-ZHIYAO")
        self.assertEqual(state.relationship.counterpart_id, "CURATOR")
        self.assertIsNone(state.session.active_provider)
        self.assertIsNone(state.session.last_provider)
        self.assertEqual(state.session.turn_index, 0)
        self.assertIs(runtime.current_state, state)

    def test_start_session_persists_validated_current_state(self) -> None:
        runtime = Runtime(personas_dir=PERSONAS_DIR, state_dir=self.state_dir)
        state = runtime.start_session(persona_id="LIN-ZHIYAO")

        saved_path = self.state_dir / f"{state.session.session_id}.json"
        self.assertTrue(saved_path.is_file())
        self.assertEqual(runtime.load_session(state.session.session_id), state)

    def test_sessions_keep_persona_id_but_receive_distinct_session_ids(self) -> None:
        first = Runtime(personas_dir=PERSONAS_DIR, state_dir=self.state_dir)
        second = Runtime(personas_dir=PERSONAS_DIR, state_dir=self.state_dir)

        first_state = first.start_session("LIN-ZHIYAO")
        second_state = second.start_session("LIN-ZHIYAO")

        self.assertEqual(first_state.persona.persona_id, second_state.persona.persona_id)
        self.assertNotEqual(first_state.session.session_id, second_state.session.session_id)

    def test_raw_event_contract_can_exist_without_provider(self) -> None:
        state = Runtime(
            personas_dir=PERSONAS_DIR, state_dir=self.state_dir
        ).start_session("LIN-ZHIYAO")

        event = RawEvent(
            session_id=state.session.session_id,
            turn_index=0,
            persona_id=state.persona.persona_id,
            universe=state.persona.universe,
            role="user",
            content="hello",
        )

        self.assertIsInstance(event.event_id, UUID)
        self.assertIsNone(event.provider)
        self.assertIsNone(event.model)

    def test_runtime_state_rejects_cross_persona_mismatch(self) -> None:
        state = Runtime(
            personas_dir=PERSONAS_DIR, state_dir=self.state_dir
        ).start_session("LIN-ZHIYAO")
        document = state.model_dump(mode="json")
        document["session"]["persona_id"] = "SOMEONE-ELSE"

        with self.assertRaisesRegex(
            ValidationError, "session persona_id must match canonical persona"
        ):
            RuntimeState.model_validate(document)


class JsonStateStoreTest(unittest.TestCase):
    def test_invalid_session_id_fails_closed(self) -> None:
        with self.assertRaisesRegex(StateStoreError, "session_id must be a UUID"):
            JsonStateStore("unused").path_for("../../private")


if __name__ == "__main__":
    unittest.main()
