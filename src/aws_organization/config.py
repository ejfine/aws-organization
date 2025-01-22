import logging
from functools import partial
from typing import Any

from pulumi.automation import ConfigValue

from .pulumi_ephemeral_deploy import utils

PULUMI_PROJECT_NAME = "aws-organization"
GITHUB_REPO_NAME = "aws-organization"
GIT_REPOSITORY_URL = f"https://github.com/ejfine/{GITHUB_REPO_NAME}"

logger = logging.getLogger(__name__)

common_tags = partial(
    utils.common_tags,
    git_repository_url=GIT_REPOSITORY_URL,
)
common_tags_native = partial(
    utils.common_tags_native,
    git_repository_url=GIT_REPOSITORY_URL,
)


def generate_stack_config() -> dict[str, Any]:
    """Generate the stack configuration."""
    stack_config: dict[str, Any] = {}

    stack_config["proj:git_repository_url"] = ConfigValue(value=GIT_REPOSITORY_URL)
    stack_config["proj:org_root_id"] = ConfigValue(value="r-2rsw")
    return stack_config
