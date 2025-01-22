import logging
from functools import partial

import pulumi
from pulumi import export

from .pulumi_ephemeral_deploy.utils import append_resource_suffix_template
from .pulumi_ephemeral_deploy.utils import get_aws_account_id
from .pulumi_ephemeral_deploy.utils import get_config

logger = logging.getLogger(__name__)


def pulumi_program() -> None:
    """Execute creating the stack."""
    stack_name = pulumi.get_stack()
    project_name = pulumi.get_project()
    aws_account_id = get_aws_account_id()
    export("aws-account-id", aws_account_id)
    env = get_config("proj:env")
    export("env", env)
    append_resource_suffix = partial(append_resource_suffix_template, project_name, stack_name, env)

    # Create Resources Here
