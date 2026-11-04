"""``igni-cloud``: the platform's commands.

``igni-cloud db grant-producer`` / ``revoke-producer`` are what the node
agent and igni server run (:mod:`igni_cloud.db.admin_cli`): small on
purpose, since granting a producer its role is on the path of its VM's
first start. ``igni migrate``, with ``IGNI_PROFILE=cloud`` set, prepares a
PostgreSQL database for the cloud through the core's own command.
"""

from __future__ import annotations

import click

from igni_cloud.db.admin_cli import db


@click.group("igni-cloud")
def main() -> None:
    """igni's cloud layer."""


main.add_command(db)


__all__ = ["main"]
