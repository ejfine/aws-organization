"""CLI helper to trigger Pulumi actions."""

import logging
import os
import sys
from collections.abc import Callable
from typing import TypedDict

from .config import GITHUB_REPO_NAME
from .config import PULUMI_PROJECT_NAME
from .config import generate_stack_config
from .program import pulumi_program
from .pulumi_ephemeral_deploy.utils import PROTECTED_ENVS
from .pulumi_ephemeral_deploy.utils import get_env_from_cli_input
from .pulumi_ephemeral_deploy.utils import get_stack
from .pulumi_ephemeral_deploy.utils import parser
from .pulumi_ephemeral_deploy.utils import result_to_str

logger = logging.getLogger(__name__)


class StackKwargs(TypedDict):
    diff: bool
    on_output: Callable[[str], None]


def main() -> None:
    """Process CLI inputs."""
    args = parser.parse_args()
    stack_name = args.stack.replace(
        "/", "-"
    )  # replace characters sometimes used in git branch names (for test/feature branches) that are incompatible with Pulumi and/or AWS resource naming

    env = get_env_from_cli_input(stack_name)

    if (env in PROTECTED_ENVS) and args.destroy:
        logger.error(f"Stack {stack_name} can't be destroyed, because it's not a test/dev stack.")
        sys.exit(1)

    stack_config = generate_stack_config()

    stack = get_stack(
        PULUMI_PROJECT_NAME,
        stack_name,
        GITHUB_REPO_NAME,
        pulumi_program,
        stack_config,
    )

    # if destroy then teardown and exit
    if args.destroy:
        destroy_response = stack.destroy()
        destroy_response_str = result_to_str(destroy_response)
        logger.info(destroy_response_str)
        # I see no reason not to completely remove the stack and history after destroying it. There is no returned output from the command https://github.com/pulumi/pulumi/blob/06ba63bb57e90706c1550861b785075ae860144a/sdk/python/lib/pulumi/automation/_local_workspace.py#L277
        stack.workspace.remove_stack(stack_name)
        sys.exit(0)
    up_and_preview_kwargs: StackKwargs = {
        "diff": True,
        "on_output": print,
        # 'on_event': print   # TODO: figure out how to log these? Seems too verbose to print to stdout though
    }
    if args.apply:
        up_response = stack.up(**up_and_preview_kwargs)
        up_response_str = result_to_str(up_response)
        logger.info(up_response_str)
    else:  # plan only
        # TODO: Make use of this feature to guarantee the plan is what is actually executed https://www.pulumi.com/blog/announcing-public-preview-update-plans/
        preview_response = stack.preview(**up_and_preview_kwargs)
        preview_response_str = result_to_str(preview_response)
        logger.info(preview_response_str)

    try:
        custom_exit_code = os.environ["CUSTOM_PULUMI_OPERATION_EXIT_CODE"]
        if not custom_exit_code.isnumeric():
            raise NotImplementedError(f"Exit codes should always be set to integers, but received {custom_exit_code}")
        print(  # noqa: T201 # TODO: figure out a way to log this that also shows up in stdout in CI
            f"Exiting with non-zero custom error code {custom_exit_code}.  Look back through output to find the reason."
        )
        sys.exit(int(custom_exit_code))
    except KeyError:
        pass


if __name__ == "__main__":
    main()
