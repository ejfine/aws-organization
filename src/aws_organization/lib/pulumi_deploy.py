# ============== WARNING ==============================================================================
# File is managed by copier template: gh:LabAutomationAndScreening/copier-aws-organization.git
# See .config/.copier-managed-files.json for details.
#
# You are welcome to make changes to this file in your repo if they are custom to your project,
# but if the change should be shared with other projects, please backport it to the template repo.
# =====================================================================================================
import logging

from ephemeral_pulumi_deploy import run_cli
from pulumi.automation import ConfigValue

from .program import pulumi_program

logger = logging.getLogger(__name__)


def generate_stack_config() -> dict[str, str | ConfigValue]:
    """Generate the stack configuration."""
    stack_config: dict[str, str | ConfigValue] = {}
    stack_config["proj:pulumi_project_name"] = "aws-organization"
    stack_config["proj:aws_org_home_region"] = ConfigValue(value="us-east-1")
    github_repo_name = "aws-organization"
    stack_config["proj:github_repo_name"] = github_repo_name

    stack_config["proj:git_repository_url"] = ConfigValue(value=f"https://github.com/ejfine/{github_repo_name}")
    return stack_config


def main() -> None:
    run_cli(stack_config=generate_stack_config(), pulumi_program=pulumi_program)


if __name__ == "__main__":
    main()
