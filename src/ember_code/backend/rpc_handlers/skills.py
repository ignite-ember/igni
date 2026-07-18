"""Skill listing + definition + details RPC handlers."""

from __future__ import annotations

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.backend.schemas_rpc import SkillDefinition
from ember_code.core.skills.parser import SkillInfo
from ember_code.protocol.rpc import RpcMethod


class SkillsRpcHandler(RpcHandler):
    """Names / full definitions / detailed info about the skills the
    backend has loaded for this session."""

    @rpc(RpcMethod.GET_SKILL_NAMES)
    def get_skill_names(self, args: dict) -> list[str]:
        return self._ctx.backend.skill_names

    @rpc(RpcMethod.GET_SKILL_DEFINITIONS)
    async def get_skill_definitions(self, args: dict) -> list[SkillDefinition]:
        pool = self._ctx.backend.get_skill_pool()
        return [
            SkillDefinition(
                name=s.name,
                description=s.description,
                prompt=s.body,
            )
            for s in pool.list_skills()
        ]

    @rpc(RpcMethod.GET_SKILL_DETAILS)
    def get_skill_details(self, args: dict) -> list[SkillInfo]:
        return self._ctx.backend.get_skill_details()
