"""The host object model: igni, as an extension sees it.

``docs/HOST_OBJECT_MODEL.md`` is the model; ``docs/HOST_MODEL_PROTOCOL.md``
its wire. Here:

* :mod:`.spec` — the grammar the model is written in;
* :mod:`.definition` — the model itself, versioned: every object,
  property, signal and method, and the permission each needs;
* :mod:`.coverage` — where every RPC, UI action and broadcast lives in it;
* :mod:`.codegen` — both SDKs, generated from the definition;
* :mod:`.model` — the model served, over :mod:`.members` (each member
  against igni's backend) and :mod:`.broadcasts` (igni's traffic as
  signals), with :mod:`.signals`, :mod:`.windows` and :mod:`.grants`;
* :mod:`.channel` — the loopback socket an extension's process reaches it on.
"""
