"""igni's decisions service: Laya in its own process, so no backend carries torch.

The backend asks it over HTTP (:mod:`ember_code.core.decisions`). On a
desktop the backend starts it on first use and it idles out; in the
cloud it runs once per node and every backend on the node shares it.
Installed with the ``decisions`` extra (``ignite-ember[decisions]``),
which brings ``laya`` and torch.
"""
