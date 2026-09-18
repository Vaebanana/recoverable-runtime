"""Append-only Effect Ledger service."""

from __future__ import annotations

from collections.abc import Mapping

from browser_use.recovery.persistence.models import EffectRecord, EffectRecordDraft, EffectRecordStatus
from browser_use.recovery.persistence.storage import RuntimeStorage


class EffectLedger:
	"""Append and query immutable side-effect facts without mutation APIs."""

	def __init__(self, storage: RuntimeStorage) -> None:
		self._storage = storage

	def append_prepared(self, **fields: object) -> EffectRecord:
		"""Record intent before an external action executes."""
		return self._append(EffectRecordStatus.PREPARED, fields)

	def append_attempted(self, **fields: object) -> EffectRecord:
		"""Record that an external action was attempted."""
		return self._append(EffectRecordStatus.ATTEMPTED, fields)

	def append_committed(self, **fields: object) -> EffectRecord:
		"""Record verification that an external effect committed."""
		return self._append(EffectRecordStatus.COMMITTED, fields)

	def append_not_applied(self, **fields: object) -> EffectRecord:
		"""Record verification that an external effect did not apply."""
		return self._append(EffectRecordStatus.NOT_APPLIED, fields)

	def append_unknown(self, **fields: object) -> EffectRecord:
		"""Record that an external effect outcome is unknown."""
		return self._append(EffectRecordStatus.UNKNOWN, fields)

	def records_for_unit(
		self,
		workflow_id: str,
		unit_id: str,
		*,
		through_seq: int | None = None,
	) -> tuple[EffectRecord, ...]:
		"""Return ordered ledger history for one unit."""
		return tuple(
			record for record in self._storage.read_effects(workflow_id, through_seq=through_seq) if record.unit_id == unit_id
		)

	def latest_for_unit(
		self,
		workflow_id: str,
		unit_id: str,
		*,
		through_seq: int | None = None,
	) -> EffectRecord | None:
		"""Return the latest ledger fact for one unit."""
		return self._storage.latest_effect_for_unit(
			workflow_id,
			unit_id,
			through_seq=through_seq,
		)

	def _append(self, status: EffectRecordStatus, fields: Mapping[str, object]) -> EffectRecord:
		"""Validate fields as a draft and append the resulting fact."""
		draft = EffectRecordDraft.model_validate({**fields, 'status': status})
		return self._storage.append_effect(draft)
