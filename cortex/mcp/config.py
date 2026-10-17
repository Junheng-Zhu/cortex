"""Local trusted MCP policy. Never derive grants from server annotations."""
import json
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from cortex.tools.base import SideEffectPolicy
from cortex.tools.permission import Permission


class ToolGrant(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True, frozen=True)
    permission: Permission
    side_effect_policy: SideEffectPolicy
    authorize_external_effects: bool = False

    @model_validator(mode='after')
    def safe_policy(self):
        if self.side_effect_policy is SideEffectPolicy.WORKSPACE_REVERSIBLE:
            raise ValueError('MCP cannot participate in workspace rollback')
        risky = self.permission is not Permission.READ or self.side_effect_policy is not SideEffectPolicy.NONE
        if risky and (not self.authorize_external_effects or self.side_effect_policy is SideEffectPolicy.NONE):
            raise ValueError('External effects require explicit authorization and classification')
        return self


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True, frozen=True, strict=True)
    server_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,24}$')
    command: str = Field(min_length=1, repr=False)
    args: list[str] = Field(default_factory=list, repr=False)
    env: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    cwd: str | None = Field(default=None, repr=False)
    required: bool = True
    connect_timeout: float = Field(default=10, gt=0, le=300, allow_inf_nan=False)
    call_timeout: float = Field(default=10, gt=0, le=300, allow_inf_nan=False)
    tools: dict[str, ToolGrant] = Field(default_factory=dict)


class MCPConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True, frozen=True)
    enabled: bool = False
    allowed_servers: frozenset[str] = frozenset()
    servers: tuple[ServerConfig, ...] = ()

    @model_validator(mode='after')
    def unique(self):
        ids = [s.server_id for s in self.servers]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate MCP server id')
        if set(self.allowed_servers) - set(ids):
            raise ValueError('Unknown allowed server')
        return self


def load_config(path: str | Path) -> MCPConfig:
    try:
        raw = Path(path).read_text(encoding='utf-8')
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('Duplicate configuration key')
                result[key] = value
            return result
        json.loads(raw, object_pairs_hook=unique_keys)
        return MCPConfig.model_validate_json(raw)
    except Exception:
        # Pydantic errors can embed input values (including commands and secrets).
        raise ValueError('Invalid MCP configuration') from None
