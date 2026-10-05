"""Aura remote-control identity package.

Intentionally minimal: only the self-contained cryptographic device-identity
registry lives here. The reverted `5d15082` autonomy task-orchestrator (runtime,
store, policy, actuator_worker, native planners) is deliberately NOT restored —
it was part of the commit that made the app unstartable. See the plan doc and
memory `jarvis-remote-revert-reason`.
"""

from .device_identity import DeviceIdentityRegistry  # noqa: F401
