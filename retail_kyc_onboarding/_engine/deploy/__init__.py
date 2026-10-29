"""Deploy targets: plans that turn an export into what one platform runs (see ``base``)."""

from .base import DeployPlan, DeployProject, DeployTarget, PlaceholderTarget, register_target, target, target_names

__all__ = ["DeployPlan", "DeployProject", "DeployTarget", "PlaceholderTarget", "register_target", "target", "target_names"]
