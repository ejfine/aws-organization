import argparse
import json
import logging
from typing import Any

import boto3
import pulumi
import pulumi.runtime
import pulumi_aws
from pulumi.automation import ConfigValue
from pulumi.automation import LocalWorkspaceOptions
from pulumi.automation import ProjectBackend
from pulumi.automation import ProjectRuntimeInfo
from pulumi.automation import ProjectSettings
from pulumi.automation import PulumiFn
from pulumi.automation import Stack
from pulumi.automation import StackSettings
from pulumi.automation import create_or_select_stack
from pulumi.automation._stack import BaseResult
from pulumi_aws_native import TagArgs

logger = logging.getLogger(__name__)
PROTECTED_ENVS = ("stag", "staging", "prod", "production")
RESOURCE_SUFFIX_DELIMITER = "--"


def generate_backend_url(
    *,
    backend_bucket: str,
    aws_account_id: str,
    github_repo_name: str,
    pulumi_project_name: str,
    bucket_region: str = "us-east-1",
) -> str:
    """Create the backend URL to store the state."""
    return f"s3://{backend_bucket}/{aws_account_id}/{github_repo_name}/{pulumi_project_name}?region={bucket_region}"


AWS_ACCOUNT_ID_LENGTH = 12


def format_aws_account_id(account_id: str | int) -> str:
    """Ensure 12 digits, including leading zeros."""
    aws_account_id = str(account_id).zfill(AWS_ACCOUNT_ID_LENGTH)
    if len(aws_account_id) != AWS_ACCOUNT_ID_LENGTH or not aws_account_id.isdigit():
        raise ValueError(  # noqa: TRY003 # this doesn't warrant a custom exception
            f"AWS account id should be {AWS_ACCOUNT_ID_LENGTH} digits, but was {aws_account_id}"
        )

    return aws_account_id


def get_aws_account_id() -> str:
    return format_aws_account_id(pulumi_aws.get_caller_identity().account_id)


def get_aws_region() -> str:
    region = str(pulumi_aws.config.region)
    if not region:
        raise ValueError("Could not determine AWS region")  # noqa: TRY003 # this doesn't warrant a custom exception
    return region


SAFE_MAX_AWS_NAME_LENGTH = 56


def append_resource_suffix_template(
    project_name: str,
    stack_name: str,
    env_name: str,
    resource_name: str = "",
) -> str:
    """Append the suffix to the resource name.

    {resource_name}--{project_name}--{stack_name}

    - case is preserved, since that has conventions for things like IAM Roles
    - however, some AWS resources (e.g. S3 buckets) require names to be lowercase
    - maximum length allowed in the template is 56
        - most AWS names are a maximum 63 characters
        - lambdas are 140 though, so longer limits can be supplied
        - Pulumi reserves 7 random characters as a suffix, leaving 56.
        - the stack name is trimmed to 7 characters to help ensure a fit.
    """
    stack_name = stack_name[:7]
    if env_name in PROTECTED_ENVS and env_name not in stack_name:
        stack_name = RESOURCE_SUFFIX_DELIMITER.join((stack_name, env_name))
    if resource_name:
        resource_name = RESOURCE_SUFFIX_DELIMITER.join((resource_name, project_name, stack_name.lower()))
    else:
        resource_name = RESOURCE_SUFFIX_DELIMITER.join((project_name, stack_name.lower()))

    if len(resource_name) > SAFE_MAX_AWS_NAME_LENGTH:
        raise ValueError(  # noqa: TRY003 # this doesn't warrant a custom exception
            f"Error creating aws resource name from template.\n{resource_name} is too long: {len(resource_name)} characters."
        )
    return resource_name


def result_to_str(pulumi_result: BaseResult) -> str:
    """Convert to something printable."""
    return f"stdout:\n{pulumi_result.stdout}\n\nstderr:\n{pulumi_result.stderr}\n"


def get_env_from_cli_input(cli_stack_name: str) -> str:
    """Return the environment tier from the command line input stack name."""
    name = cli_stack_name.lower()
    if name.startswith("test"):
        return "test"
    if name.startswith("prod") or name == "pngp":
        return "prod"
    if name.startswith("mod") or name == "mngp":
        return "modl"

    return "dev"


def get_config(key: str) -> str | int | dict[str, Any]:
    """Get the configuration value as a string.

    For reasons unknown, the `pulumi.runtime.config` returns a JSON string with `'value':str` and `'secret':bool` as a dictionary, instead of just the
    value.
    """
    json_str = pulumi.runtime.get_config(key)
    if json_str is None:
        raise KeyError(f"The key {key} was not found in the Pulumi config.")  # noqa: TRY003 # this doesn't warrant a custom exception
    if not isinstance(json_str, str):
        raise NotImplementedError(
            f"get_config is always supposed to return a string.  But {json_str} was type {type(json_str)}"
        )
    try:
        json_dict = json.loads(json_str)
    except json.decoder.JSONDecodeError:
        # Not totally sure how this is happening, but sometimes the exact string value already is returned and not a JSON-formatted string, so assuming we should just directly return the value
        return json_str
    if not isinstance(json_dict, dict):
        raise NotImplementedError(
            f"The config key {key} JSON should always parse to a dictionary, but it was found to be {json_dict} which is {type(json_dict)}. Original retrieved JSON was {json_str}"
        )

    if (
        "value" in json_dict
    ):  # if the 'value' key is present, assume this is an actual attribute. Otherwise assume it's a nested dictionary
        value = json_dict["value"]  # type: ignore[reportUnknownVariableType] # TODO: understand this, so there can be better typing
        if not isinstance(value, int | str):
            raise NotImplementedError(
                f"The value for config key {key} should always be a string or int, but it was found to be {value} which is {type(value)}. Original retrieved JSON was {json_str}"  # type: ignore[reportUnknownArgumentType] # TODO: understand this, so there can be better typing
            )
        return value
    assert isinstance(json_dict, str | int | dict)
    return json_dict  # type: ignore[reportUnknownVariableType] # TODO: understand this, so there can be better typing


def get_config_aws_account_id(key: str) -> str:
    value = get_config(key)
    if not isinstance(value, str | int):
        raise NotImplementedError(
            f"The value for {key} should always be a string or int, but {value} was type {type(value)}."
        )
    return format_aws_account_id(value)


def get_config_str(key: str) -> str:
    value = get_config(key)
    if not isinstance(value, str):
        raise NotImplementedError(f"The value for {key} should always be a string, but {value} was type {type(value)}.")
    return value


def get_config_bool(key: str) -> bool:
    value = get_config(key)
    if not isinstance(value, str):
        raise NotImplementedError(f"The value for {key} should always be a string, but {value} was type {type(value)}.")
    return value in ("1", "True")


def get_config_int(key: str) -> int:
    value = get_config(key)
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise NotImplementedError(
            f"The value for {key} should initially always be a string, but {value} was type {type(value)}."
        )
    return int(value)


def get_stack(*, stack_name: str, pulumi_program: PulumiFn, stack_config: dict[str, Any]) -> Stack:
    env = get_env_from_cli_input(stack_name)
    project_name = stack_config["proj:pulumi_project_name"]
    github_repo_name = stack_config["proj:github_repo_name"]
    stack_config["proj:env"] = ConfigValue(value=env)

    fully_qualified_stack_name = f"{project_name}/{stack_name}"

    session = boto3.Session()
    default_cloud_region = "us-east-1"
    sts_client = session.client("sts")
    ssm_client = session.client("ssm", region_name=default_cloud_region)

    account_id = sts_client.get_caller_identity()["Account"]
    # account_id is a `str` when returned from `boto` and ConfigValue stores a `str`. However it is somehow an `int` when fetched back by get_config.
    # This is a problem when the account_id is prefixed with zeros.
    stack_config["proj:aws_account_id"] = ConfigValue(value=account_id)
    backend_bucket_param = ssm_client.get_parameter(Name="/org-managed/infra-state-bucket-name")["Parameter"]
    assert "Value" in backend_bucket_param, f"Expected 'Value' in {backend_bucket_param}"
    backend_bucket = backend_bucket_param["Value"]
    stack_config["proj:backend_bucket_name"] = ConfigValue(value=backend_bucket)

    kms_key_id_param = ssm_client.get_parameter(Name="/org-managed/infra-state-kms-key-arn")["Parameter"]
    assert "Value" in kms_key_id_param, f"Expected 'Value' in {kms_key_id_param}"
    kms_key_id = kms_key_id_param["Value"]
    stack_config["proj:kms_key_id"] = ConfigValue(value=kms_key_id)

    secrets_provider = f"awskms:///{kms_key_id}?region={default_cloud_region}"  # TODO: add context parameters https://www.pulumi.com/docs/iac/concepts/secrets/
    logger.info("Stack is: %s", fully_qualified_stack_name)
    project_runtime_info = ProjectRuntimeInfo(  # I have no idea what this does or if it is necessary, but this was copied off of a template
        name="python", options={"virtualenv": "venv"}
    )
    backend_url = generate_backend_url(
        backend_bucket=backend_bucket,
        aws_account_id=account_id,
        github_repo_name=github_repo_name,
        pulumi_project_name=project_name,
    )

    project_backend = ProjectBackend(url=backend_url)
    project_settings = ProjectSettings(name=project_name, runtime=project_runtime_info, backend=project_backend)
    stack_settings = StackSettings(
        secrets_provider=secrets_provider,
        config=stack_config,
    )
    workspace_options = LocalWorkspaceOptions(
        secrets_provider=secrets_provider,  # Since secrets_provider is already in the ProjectSettings, unclear if it's needed in both places or if just one spot would be better
        project_settings=project_settings,
        stack_settings={stack_name: stack_settings},
    )

    return create_or_select_stack(
        stack_name,
        project_name=project_name,
        program=pulumi_program,
        opts=workspace_options,
    )


def common_tags(*, name: str = "", git_repository_url: str | None = None) -> dict[str, str]:
    """Create common tags that all resources should have."""
    if git_repository_url is None:
        git_repository_url = get_config_str("proj:git_repository_url")

    return {
        "git-repository-url": git_repository_url,
        "managed-by": "pulumi",
        "stack-name": pulumi.get_stack(),
        "pulumi-project-name": pulumi.get_project(),
        "Name": name if name != "" else pulumi.get_stack(),
    }


def common_tags_native(*, name: str = "", git_repository_url: str | None = None) -> list[TagArgs]:
    """Generate tags in the format expected in AWS Native."""
    tags = common_tags(
        name=name,
        git_repository_url=git_repository_url,
    )
    native_tags: list[TagArgs] = []
    for key, value in tags.items():
        native_tags.append(TagArgs(key=key, value=value))
    return native_tags


parser = argparse.ArgumentParser(description="pulumi-auto-deploy")
_ = parser.add_argument(
    "--stack",
    required=True,
    type=str,
    help="Pulumi stack name.",
)

_ = parser.add_argument("--apply", action="store_true")
_ = parser.add_argument("--destroy", action="store_true")
